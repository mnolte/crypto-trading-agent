"""Supported markets: the eligibility check, the anti-flapping buffer and
the warming-up backfill.

Any Bitvavo EUR market can be traded if it passes the check; Marc chooses
which eligible markets are active. The check runs at startup and daily at
01:00 UTC (``daily_market_loop`` in ``app.main``), using Bitvavo's
``/markets`` and ``/ticker/24h`` plus stored candles.

Anti-flapping: an eligible market fails only after ``eligibility_fail_days``
consecutive daily checks below a threshold, or at once when a numeric rule
is more than ``eligibility_beyond_pct`` beyond its threshold (under €0.8M
volume, for example). Categorical rules (status, stablecoin type) have no
numeric distance, so they fail at once. A failed market needs
``eligibility_recover_days`` consecutive passing checks to pass again.

A check with bad data keeps the previous result (item 026): when
``/markets`` or ``/ticker/24h`` fails, or volume/spread is missing for
``check_data_error_pct`` percent or more of the previously eligible
markets, nothing moves and the run is stored as failed.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.market.base import (
    Candle,
    MarketClient,
    MarketDataError,
    MarketInfo,
    Ticker24h,
    drop_forming,
)
from app.agent.market.sync import (
    SyncRuns,
    backfill_retry_due,
    next_cycle_start,
)
from app.db import MarketCheckRow, MarketRow, utcnow
from app.errors import plain_error
from app.models import MarketSettings, Tf
from app.repo import candle_counts, load_candles, upsert_candles, upsert_market_row

log = logging.getLogger(__name__)

RuleName = Literal["volume", "spread", "history", "type", "status"]
RULE_ORDER: tuple[RuleName, ...] = ("volume", "spread", "history", "type", "status")
BACKFILL_TFS: tuple[Tf, ...] = ("1h", "4h", "1d")
CATEGORICAL_RULES = {"type", "status"}  # no numeric distance: fail at once
VOLUME_LOOKBACK_DAYS = 30
# One extra closed daily candle gives the 31 closes the vs-BTC return and
# the BTC correlation need (30 daily returns).
SCREENING_DAYS = VOLUME_LOOKBACK_DAYS + 1
BTC_MARKET = "BTC-EUR"

FUNNEL_RULES: tuple[tuple[str, str], ...] = (
    ("listed", "Bitvavo EUR markets"),
    ("type", "not a stablecoin or fiat token"),
    ("status", "trading, not halted or delisting"),
    ("volume", "30-day average daily volume"),
    ("spread", "average spread over 24h"),
    ("history", "daily candles"),
    ("all", "pass every rule today"),
)


@dataclass
class MarketMetrics:
    market: str
    base: str
    quote: str
    status: str
    is_stablecoin: bool
    volume_30d_eur: Decimal | None
    spread_pct: Decimal | None
    daily_candles: int | None
    daily_closes: list[Decimal] = field(default_factory=list)


def failed_rules(metrics: MarketMetrics, cfg: MarketSettings) -> list[RuleName]:
    """Every eligibility rule the market currently fails, in spec order."""
    fails: list[RuleName] = []
    if metrics.volume_30d_eur is None or metrics.volume_30d_eur < cfg.min_avg_daily_volume_eur:
        fails.append("volume")
    if metrics.spread_pct is None or metrics.spread_pct > cfg.max_avg_spread_pct:
        fails.append("spread")
    if metrics.daily_candles is not None and metrics.daily_candles < cfg.min_daily_candles:
        fails.append("history")
    if metrics.is_stablecoin:
        fails.append("type")
    if metrics.status != "trading":
        fails.append("status")
    return fails


def beyond_threshold(metrics: MarketMetrics, cfg: MarketSettings) -> bool:
    """True when a numeric rule is more than ``eligibility_beyond_pct`` past
    its limit, or a categorical rule fails (no distance, so instant)."""
    margin = cfg.eligibility_beyond_pct / Decimal(100)
    if metrics.volume_30d_eur is not None and metrics.volume_30d_eur < (
        cfg.min_avg_daily_volume_eur * (1 - margin)
    ):
        return True
    if metrics.spread_pct is not None and metrics.spread_pct > (
        cfg.max_avg_spread_pct * (1 + margin)
    ):
        return True
    if metrics.daily_candles is not None and metrics.daily_candles < (
        cfg.min_daily_candles * (1 - margin)
    ):
        return True
    return metrics.is_stablecoin or metrics.status != "trading"


def apply_check(
    row: MarketRow,
    metrics: MarketMetrics,
    cfg: MarketSettings,
    now: datetime,
    *,
    is_first_check: bool,
) -> Literal["failed", "recovered", None]:
    """Advance the streak state machine one daily check. Returns the
    transition, if the eligibility flag flipped (one alert per change)."""
    fails = failed_rules(metrics, cfg)
    _fill_row(row, metrics, fails, now)
    if is_first_check:
        row.eligible = not fails
        if fails:
            row.failed_rule = fails[0]
        else:
            row.eligible_since = now
        return None
    if row.eligible:
        if not fails:
            row.below_streak = 0
            row.failed_rule = None
            return None
        row.below_streak = (row.below_streak or 0) + 1
        row.failed_rule = fails[0]
        if beyond_threshold(metrics, cfg) or row.below_streak >= cfg.eligibility_fail_days:
            row.eligible = False
            row.eligible_since = None
            row.failing_since = now
            return "failed"
        return None
    if fails:
        row.within_streak = 0
        row.failed_rule = fails[0]
        return None
    row.within_streak = (row.within_streak or 0) + 1
    if row.within_streak >= cfg.eligibility_recover_days:
        row.eligible = True
        row.below_streak = 0
        row.failed_rule = None
        row.eligible_since = now
        row.failing_since = None
        return "recovered"
    return None


def _fill_row(row: MarketRow, metrics: MarketMetrics, fails: list[RuleName], now: datetime) -> None:
    row.base = metrics.base
    row.quote = metrics.quote
    row.status = metrics.status
    row.volume_30d_eur = metrics.volume_30d_eur
    row.spread_pct = metrics.spread_pct
    row.daily_candles = metrics.daily_candles
    row.checked_at = now
    if fails:
        row.failed_rule = fails[0]


# ---------------------------------------------------------------- the check --


def _eur_volume(candles: list[Candle]) -> Decimal | None:
    """Average daily EUR volume over the last ``VOLUME_LOOKBACK_DAYS``
    candles we have (volume x close)."""
    window = candles[-VOLUME_LOOKBACK_DAYS:]
    if not window:
        return None
    total = sum((c.volume * c.close for c in window), Decimal(0))
    return total / Decimal(len(window))


async def _daily_candles(
    client: MarketClient, session: Session, market: str, limit: int, now: datetime
) -> list[Candle]:
    """Closed daily candles, freshest from the exchange, stored for reuse."""
    # Ask for one extra bar: the exchange's `limit` includes the still
    # forming candle, which we drop — this lands exactly `limit` closed.
    fetched = drop_forming(await client.candles(market, "1d", limit + 1, end=now), "1d", now)
    if fetched:
        upsert_candles(session, market, "1d", fetched)
        return fetched
    return drop_forming(load_candles(session, market, "1d", limit), "1d", now)


async def check_market(
    client: MarketClient,
    session: Session,
    info: MarketInfo,
    ticker: Ticker24h | None,
    cfg: MarketSettings,
    now: datetime,
) -> MarketMetrics:
    """Evaluate one market. Numeric metrics come from /ticker/24h and stored
    or freshly fetched daily candles; the 300-bar history is only fetched
    for markets that pass the cheaper rules."""
    is_stable = info.base.upper() in {b.upper() for b in cfg.stablecoin_bases}
    spread = ticker.spread_pct if ticker and ticker.spread_pct is not None else None

    screening = await _daily_candles(client, session, info.market, SCREENING_DAYS, now)
    volume = _eur_volume(screening)
    daily_closes = [c.close for c in screening[-SCREENING_DAYS:]]

    cheap = MarketMetrics(
        market=info.market,
        base=info.base,
        quote=info.quote,
        status=info.status,
        is_stablecoin=is_stable,
        volume_30d_eur=volume,
        spread_pct=spread,
        daily_candles=None,
        daily_closes=daily_closes,
    )
    cheap_fails = failed_rules(cheap, cfg)
    history_known = len(screening) < SCREENING_DAYS
    daily_count: int | None = len(screening) if history_known else None
    if (
        "volume" not in cheap_fails
        and "spread" not in cheap_fails
        and not is_stable
        and info.status == "trading"
    ):
        # The screening read may have left a transaction open — end it
        # before the full-history fetch so no connection spans the await.
        session.rollback()
        full = await _daily_candles(client, session, info.market, cfg.min_daily_candles, now)
        daily_count = len(full)
    return MarketMetrics(
        market=info.market,
        base=info.base,
        quote=info.quote,
        status=info.status,
        is_stablecoin=is_stable,
        volume_30d_eur=volume,
        spread_pct=spread,
        daily_candles=daily_count,
        daily_closes=daily_closes,
    )


def _funnel_steps(
    eur_infos: list[MarketInfo],
    metrics: dict[str, MarketMetrics],
    cfg: MarketSettings,
) -> list[dict[str, Any]]:
    """One bar per rule in check order: the markets left and the ones
    dropped (docs/SPEC.md, Universe screen → Today's check)."""
    survivors = {i.market for i in eur_infos}
    steps: list[dict[str, Any]] = []

    def drop(rule: str, keep: Callable[[MarketMetrics], bool]) -> None:
        nonlocal survivors
        dropped = sorted(m for m in survivors if not keep(metrics[m]))
        survivors = {m for m in survivors if keep(metrics[m])}
        steps.append({"rule": rule, "left": len(survivors), "dropped": dropped})

    steps.append({"rule": "listed", "left": len(survivors), "dropped": []})
    drop("type", lambda m: not m.is_stablecoin)
    drop("status", lambda m: m.status == "trading")
    drop(
        "volume",
        lambda m: m.volume_30d_eur is not None and m.volume_30d_eur >= cfg.min_avg_daily_volume_eur,
    )
    drop("spread", lambda m: m.spread_pct is not None and m.spread_pct <= cfg.max_avg_spread_pct)
    drop(
        "history",
        lambda m: m.daily_candles is None or m.daily_candles >= cfg.min_daily_candles,
    )
    drop("all", lambda m: not failed_rules(m, cfg))
    return steps


def _btc_metrics(
    closes: list[Decimal], btc_closes: list[Decimal]
) -> tuple[Decimal | None, Decimal | None]:
    """(vs BTC 30d %, BTC correlation) from the last 31 closes → 30 daily
    returns. None when 31 closes aren't there or the series is flat."""
    if len(closes) < SCREENING_DAYS or len(btc_closes) < SCREENING_DAYS:
        return None, None
    window = closes[-SCREENING_DAYS:]
    btc_window = btc_closes[-SCREENING_DAYS:]
    if any(c <= 0 for c in window) or any(c <= 0 for c in btc_window):
        return None, None

    def returns(series: list[Decimal]) -> list[float]:
        return [float(series[i] / series[i - 1] - 1) for i in range(1, len(series))]

    m_ret = float(window[-1] / window[0] - 1)
    b_ret = float(btc_window[-1] / btc_window[0] - 1)
    vs_btc = (Decimal(str(m_ret - b_ret)) * 100).quantize(Decimal("0.1"))

    mr, br = returns(window), returns(btc_window)
    n = len(mr)
    m_mean = sum(mr) / n
    b_mean = sum(br) / n
    cov = sum((a - m_mean) * (b - b_mean) for a, b in zip(mr, br, strict=True))
    m_var = sum((a - m_mean) ** 2 for a in mr)
    b_var = sum((b - b_mean) ** 2 for b in br)
    if m_var <= 0 or b_var <= 0:
        return vs_btc, None
    corr = cov / math.sqrt(m_var * b_var)
    return vs_btc, Decimal(str(corr)).quantize(Decimal("0.01"))


def _thresholds_json(cfg: MarketSettings) -> str:
    return json.dumps(
        {
            "min_avg_daily_volume_eur": str(cfg.min_avg_daily_volume_eur),
            "max_avg_spread_pct": str(cfg.max_avg_spread_pct),
            "min_daily_candles": cfg.min_daily_candles,
            "eligibility_fail_days": cfg.eligibility_fail_days,
            "eligibility_beyond_pct": str(cfg.eligibility_beyond_pct),
            "eligibility_recover_days": cfg.eligibility_recover_days,
            "stablecoin_bases": list(cfg.stablecoin_bases),
            "check_data_error_pct": str(cfg.check_data_error_pct),
        }
    )


def _data_error(
    session: Session,
    metrics: dict[str, MarketMetrics],
    cfg: MarketSettings,
) -> str | None:
    """Plain-words reason when the check must keep the previous result:
    volume or spread missing for ``check_data_error_pct`` percent or more
    of the markets that were eligible at the previous check."""
    previously = [
        m.market for m in session.scalars(select(MarketRow).where(MarketRow.eligible)).all()
    ]
    if not previously:
        return None
    missing = 0
    for market in previously:
        m = metrics.get(market)
        if m is None or m.volume_30d_eur is None or m.spread_pct is None:
            missing += 1
    share = Decimal(missing) / Decimal(len(previously)) * 100
    if share >= cfg.check_data_error_pct:
        return (
            f"Bitvavo data looks wrong: volume or spread missing for "
            f"{missing} of {len(previously)} previously eligible markets"
        )
    return None


async def run_eligibility_check(
    client: MarketClient,
    session: Session,
    cfg: MarketSettings,
    now: datetime | None = None,
    *,
    trigger: str = "daily",
    on_progress: Callable[[int, int | None], None] | None = None,
    on_market_failure: Callable[[str, str], None] | None = None,
) -> tuple[list[MarketRow], dict[str, str]]:
    """Run the daily check over every EUR market the exchange lists.

    Returns (rows, transitions) where transitions maps market -> "failed" or
    "recovered" so the caller can send one alert per change. Every run is
    stored as a ``market_checks`` row; a data error stores a failed row and
    moves nothing (docs/SPEC.md, Universe screen).
    """
    now = now or utcnow()
    check_row = MarketCheckRow(
        started_at=now.replace(tzinfo=None),
        trigger=trigger,
        status="ok",
        thresholds=_thresholds_json(cfg),
    )
    session.add(check_row)
    session.commit()

    def fail_run(error: str) -> tuple[list[MarketRow], dict[str, str]]:
        check_row.status = "failed"
        check_row.error = error[:500]
        check_row.finished_at = now.replace(tzinfo=None)
        session.commit()
        log.warning(
            "eligibility check failed: %s",
            error,
            extra={"event": "eligibility.check_failed", "data": {"error": error}},
        )
        return [], {}

    try:
        infos = await client.markets()
        tickers = {t.market: t for t in await client.ticker_24h()}
    except Exception as exc:  # noqa: BLE001 - a data error keeps the previous result
        return fail_run(plain_error(exc))

    eur_infos = [i for i in infos if i.quote == "EUR"]
    metrics: dict[str, MarketMetrics] = {}
    for index, info in enumerate(eur_infos):
        try:
            metrics[info.market] = await check_market(
                client, session, info, tickers.get(info.market), cfg, now
            )
        except MarketDataError as exc:
            # One broken market (for example an auction market whose candles
            # endpoint 400s) must not abort the other markets' check. Its
            # categorical status/type still apply; numeric metrics are missing.
            ticker = tickers.get(info.market)
            metrics[info.market] = MarketMetrics(
                market=info.market,
                base=info.base,
                quote=info.quote,
                status=info.status,
                is_stablecoin=info.base.upper() in {b.upper() for b in cfg.stablecoin_bases},
                volume_30d_eur=None,
                spread_pct=ticker.spread_pct if ticker and ticker.spread_pct is not None else None,
                daily_candles=None,
            )
            if on_market_failure is not None:
                on_market_failure(info.market, plain_error(exc))
        # The next market's candle fetches are network awaits — end the
        # transaction so the pooled connection goes back between markets.
        session.rollback()
        if on_progress is not None:
            on_progress(index + 1, len(eur_infos))
    session.commit()

    error = _data_error(session, metrics, cfg)
    if error is not None:
        return fail_run(error)

    btc_closes = metrics.get(
        BTC_MARKET,
        MarketMetrics(
            market=BTC_MARKET,
            base="BTC",
            quote="EUR",
            status="",
            is_stablecoin=False,
            volume_30d_eur=None,
            spread_pct=None,
            daily_candles=None,
        ),
    ).daily_closes

    rows: list[MarketRow] = []
    transitions: dict[str, str] = {}
    fails_by_market: dict[str, list[RuleName]] = {}
    for info in eur_infos:
        m = metrics[info.market]
        fails_by_market[info.market] = failed_rules(m, cfg)
        row = session.get(MarketRow, info.market)
        first = row is None
        if row is None:
            row = MarketRow(market=info.market, eligible=False)
        if info.market != BTC_MARKET:
            vs_btc, corr = _btc_metrics(m.daily_closes, btc_closes)
            row.vs_btc_30d_pct = vs_btc
            row.corr_btc_30d = corr
        change = apply_check(row, m, cfg, now, is_first_check=first)
        upsert_market_row(session, row)
        if change:
            transitions[info.market] = change
        rows.append(row)

    # The funnel as stored: one step per rule plus the kept/held-back
    # markets the 3-day streak rule changed (docs/SPEC.md).
    funnel = _funnel_steps(eur_infos, metrics, cfg)
    kept = sorted(r.market for r in rows if r.eligible and r.failed_rule)
    held_back = sorted(
        r.market
        for r in rows
        if not r.eligible and (r.within_streak or 0) > 0 and not r.failed_rule
    )
    check_row.funnel = json.dumps(funnel)
    check_row.kept = json.dumps(kept)
    check_row.held_back = json.dumps(held_back)
    check_row.finished_at = now.replace(tzinfo=None)
    session.commit()
    return rows, transitions


# ------------------------------------------------------- activation/backfill


class IneligibleMarketError(Exception):
    def __init__(self, market: str, failed_rule: str | None) -> None:
        super().__init__(f"{market} is not eligible: {failed_rule or 'not yet checked'}")
        self.market = market
        self.failed_rule = failed_rule


class UnknownMarketError(Exception):
    def __init__(self, market: str) -> None:
        super().__init__(f"{market} is not a Bitvavo EUR market")
        self.market = market


def warming_up(session: Session, market: str, cfg: MarketSettings) -> bool:
    """An active market stays Warming up until its history is fully loaded."""
    row = session.get(MarketRow, market)
    if row is None or not row.active:
        return False
    counts = candle_counts(session, market)
    return (
        counts.get("1h", 0) < cfg.history_1h
        or counts.get("4h", 0) < cfg.history_4h
        or counts.get("1d", 0) < cfg.history_1d
    )


def entries_allowed(session: Session, market: str) -> bool:
    """A market takes new entries only while it is active and passing the
    daily eligibility check — a market that is failing stays active for
    display but takes no new entries, and a deactivated one takes none.
    A missing row means the check never ran — an unchecked market takes
    no entries either."""
    row = session.get(MarketRow, market)
    return row is not None and row.active and row.eligible


async def backfill_market(
    client: MarketClient,
    session: Session,
    market: str,
    cfg: MarketSettings,
    now: datetime | None = None,
) -> dict[str, int]:
    """Load the activation history: 800 1h, 300 4h and 300 1d candles."""
    now = now or utcnow()
    limits: dict[Tf, int] = {"1h": cfg.history_1h, "4h": cfg.history_4h, "1d": cfg.history_1d}
    counts: dict[str, int] = {}
    for tf, limit in limits.items():
        # Ask for one extra bar: the exchange's `limit` includes the still
        # forming candle, which we drop — this lands exactly `limit` closed.
        candles = drop_forming(await client.candles(market, tf, limit + 1, end=now), tf, now)
        upsert_candles(session, market, tf, candles)
        counts[tf] = len(candles)
    return counts


def _backfill_retry_at(client: MarketClient, now: datetime, offset_s: int) -> datetime:
    """When a failed backfill retries: the next cycle's start, or later
    when Bitvavo named a ban time (a 429's reset)."""
    retry = next_cycle_start(now, offset_s=offset_s)
    reset = getattr(client, "rate_limit_reset_at", None)
    if reset is not None:
        retry = max(retry, reset)
    return retry


async def backfill_job(
    client: MarketClient,
    session_factory: sessionmaker[Session],
    market: str,
    cfg: MarketSettings,
    now: datetime | None = None,
    *,
    tfs: set[str] | None = None,
    syncs: SyncRuns | None = None,
    cycle_offset_s: int = 0,
) -> None:
    """One backfill run for ``market`` — its own ``sync_runs`` row
    (``backfill:<market>``), progress = candles stored, failures per
    timeframe with the next cycle's start as ``next_retry``. Failed
    timeframes retry at the next cycle via ``backfill_retry_due``."""
    now = now or utcnow()
    limits: dict[Tf, int] = {"1h": cfg.history_1h, "4h": cfg.history_4h, "1d": cfg.history_1d}
    wanted = [tf for tf in BACKFILL_TFS if tfs is None or tf in tfs]
    total = sum(limits[tf] for tf in wanted)
    run = syncs.begin(f"backfill:{market}", total=total) if syncs is not None else None
    stored = 0
    try:
        for tf in wanted:
            limit = limits[tf]
            try:
                candles = drop_forming(
                    await client.candles(market, tf, limit + 1, end=now), tf, now
                )
            except Exception as exc:  # noqa: BLE001 - per-timeframe failure
                if run is not None:
                    run.fail(
                        market,
                        plain_error(exc),
                        next_retry=_backfill_retry_at(client, now, cycle_offset_s),
                        tf=tf,
                    )
                continue
            with session_factory() as session:
                upsert_candles(session, market, tf, candles)
                session.commit()
            stored += len(candles)
            if run is not None:
                run.progress(stored, total)
        if run is not None:
            run.finish()
    except Exception as exc:
        if run is not None:
            run.abort(plain_error(exc))
        else:
            raise


async def retry_due_backfills(
    client: MarketClient,
    session_factory: sessionmaker[Session],
    cfg: MarketSettings,
    now: datetime | None = None,
    *,
    syncs: SyncRuns,
    cycle_offset_s: int = 0,
) -> None:
    """Before each cycle: backfills whose recorded ``next_retry`` has
    passed run again for their failed timeframes only."""
    now = now or utcnow()
    with session_factory() as session:
        due = backfill_retry_due(session, now)
    for market, tfs in due.items():
        await backfill_job(
            client,
            session_factory,
            market,
            cfg,
            now,
            tfs=tfs,
            syncs=syncs,
            cycle_offset_s=cycle_offset_s,
        )


async def activate_market(
    client: MarketClient,
    session: Session,
    market: str,
    cfg: MarketSettings,
    now: datetime | None = None,
    *,
    on_active: Callable[[str], None] | None = None,
) -> MarketRow:
    """Mark a market active and start its history backfill.

    Refused with ``IneligibleMarketError`` (carrying the failed rule) when the
    latest check says the market is below the bar. ``on_active`` is called
    after the row is saved so the caller can spawn the backfill — the
    request returns before the history finishes loading. Without it the
    backfill is awaited (tests, one-off callers).
    """
    now = now or utcnow()
    row = session.get(MarketRow, market)
    if row is None:
        # The on-demand check below is a network call — release the
        # transaction first (the lookup missed, so nothing is lost).
        session.rollback()
        # Never checked: run the check for this market on demand so the
        # refusal still names the failed rule.
        infos = {i.market: i for i in await client.markets()}
        info = infos.get(market)
        tickers = {t.market: t for t in await client.ticker_24h()}
        if info is None or info.quote != "EUR":
            raise UnknownMarketError(market)
        metrics = await check_market(client, session, info, tickers.get(market), cfg, now)
        row = MarketRow(market=market, eligible=False)
        apply_check(row, metrics, cfg, now, is_first_check=True)
    if not row.eligible:
        raise IneligibleMarketError(market, row.failed_rule)
    row.active = True
    row.activated_at = now
    upsert_market_row(session, row)
    if on_active is not None:
        on_active(market)
    else:
        await backfill_market(client, session, market, cfg, now)
    return row


def deactivate_market(session: Session, market: str) -> MarketRow | None:
    """Stop new entries on a market. Any open position keeps being managed
    by its stop, target, time stop and the model until it closes."""
    row = session.get(MarketRow, market)
    if row is None:
        return None
    row.active = False
    upsert_market_row(session, row)
    return row
