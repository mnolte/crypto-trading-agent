"""Supported markets: the eligibility check, the anti-flapping buffer and
the warming-up backfill.

Any Bitvavo EUR market can be traded if it passes the check; Marc chooses
which eligible markets are active. The check runs at startup and daily at
01:00 UTC (the daily job lands with the scheduler), using Bitvavo's
``/markets`` and ``/ticker/24h`` plus stored candles.

Anti-flapping: an eligible market fails only after ``eligibility_fail_days``
consecutive daily checks below a threshold, or at once when a numeric rule
is more than ``eligibility_beyond_pct`` beyond its threshold (under €0.8M
volume, for example). Categorical rules (status, stablecoin type) have no
numeric distance, so they fail at once. A failed market needs
``eligibility_recover_days`` consecutive passing checks to pass again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.agent.market.base import (
    Candle,
    MarketClient,
    MarketInfo,
    Ticker24h,
    drop_forming,
)
from app.db import MarketRow, utcnow
from app.models import MarketSettings, Tf
from app.repo import candle_counts, load_candles, upsert_candles, upsert_market_row

RuleName = Literal["volume", "spread", "history", "type", "status"]
RULE_ORDER: tuple[RuleName, ...] = ("volume", "spread", "history", "type", "status")
CATEGORICAL_RULES = {"type", "status"}  # no numeric distance: fail at once
VOLUME_LOOKBACK_DAYS = 30


@dataclass
class MarketMetrics:
    market: str
    base: str
    quote: str
    status: str
    is_stablecoin: bool
    volume_30d_eur: float | None
    spread_pct: float | None
    daily_candles: int | None


def failed_rules(metrics: MarketMetrics, cfg: MarketSettings) -> list[RuleName]:
    """Every eligibility rule the market currently fails, in spec order."""
    fails: list[RuleName] = []
    if metrics.volume_30d_eur is None or metrics.volume_30d_eur < float(
        cfg.min_avg_daily_volume_eur
    ):
        fails.append("volume")
    if metrics.spread_pct is None or metrics.spread_pct > float(cfg.max_avg_spread_pct):
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
    margin = float(cfg.eligibility_beyond_pct) / 100.0
    if metrics.volume_30d_eur is not None and metrics.volume_30d_eur < float(
        cfg.min_avg_daily_volume_eur
    ) * (1 - margin):
        return True
    if metrics.spread_pct is not None and metrics.spread_pct > float(cfg.max_avg_spread_pct) * (
        1 + margin
    ):
        return True
    if metrics.daily_candles is not None and metrics.daily_candles < cfg.min_daily_candles * (
        1 - margin
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


def _eur_volume(candles: list[Candle]) -> float | None:
    """Average daily EUR volume over the candles we have (volume x close)."""
    if not candles:
        return None
    return float(sum(float(c.volume * c.close) for c in candles) / len(candles))


async def _daily_candles(
    client: MarketClient, session: Session, market: str, limit: int, now: datetime
) -> list[Candle]:
    """Closed daily candles, freshest from the exchange, stored for reuse."""
    fetched = drop_forming(await client.candles(market, "1d", limit, end=now), "1d", now)
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
    spread = float(ticker.spread_pct) if ticker and ticker.spread_pct is not None else None

    screening = await _daily_candles(client, session, info.market, VOLUME_LOOKBACK_DAYS, now)
    volume = _eur_volume(screening[-VOLUME_LOOKBACK_DAYS:])

    cheap = MarketMetrics(
        market=info.market,
        base=info.base,
        quote=info.quote,
        status=info.status,
        is_stablecoin=is_stable,
        volume_30d_eur=volume,
        spread_pct=spread,
        daily_candles=None,
    )
    cheap_fails = failed_rules(cheap, cfg)
    history_known = len(screening) < VOLUME_LOOKBACK_DAYS
    daily_count: int | None = len(screening) if history_known else None
    if (
        "volume" not in cheap_fails
        and "spread" not in cheap_fails
        and not is_stable
        and info.status == "trading"
    ):
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
    )


async def run_eligibility_check(
    client: MarketClient,
    session: Session,
    cfg: MarketSettings,
    now: datetime | None = None,
) -> tuple[list[MarketRow], dict[str, str]]:
    """Run the daily check over every EUR market the exchange lists.

    Returns (rows, transitions) where transitions maps market -> "failed" or
    "recovered" so the caller can send one alert per change.
    """
    now = now or utcnow()
    infos = await client.markets()
    tickers = {t.market: t for t in await client.ticker_24h()}
    rows: list[MarketRow] = []
    transitions: dict[str, str] = {}
    for info in infos:
        if info.quote != "EUR":
            continue
        metrics = await check_market(client, session, info, tickers.get(info.market), cfg, now)
        row = session.get(MarketRow, info.market)
        first = row is None
        if row is None:
            row = MarketRow(market=info.market, eligible=False)
        change = apply_check(row, metrics, cfg, now, is_first_check=first)
        upsert_market_row(session, row)
        if change:
            transitions[info.market] = change
        rows.append(row)
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


async def activate_market(
    client: MarketClient,
    session: Session,
    market: str,
    cfg: MarketSettings,
    now: datetime | None = None,
) -> MarketRow:
    """Mark a market active and start its history backfill.

    Refused with ``IneligibleMarketError`` (carrying the failed rule) when the
    latest check says the market is below the bar.
    """
    now = now or utcnow()
    row = session.get(MarketRow, market)
    if row is None:
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
