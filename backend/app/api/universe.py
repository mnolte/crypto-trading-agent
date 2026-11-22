"""Universe screen endpoints (item 026).

``GET /api/universe`` is the whole screen in one payload: the check line,
the tiles, the calls-a-day numbers and every EUR market with one status,
its chip facts and the line under it. ``GET /api/universe/check`` is the
latest check run (and the one in use when the latest failed);
``GET /api/universe/sync`` the latest run per sync job plus the exit
watcher's own state; ``POST /api/universe/sync/{job}/retry`` reruns a
failed job.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app import tasks as tasks_mod
from app.agent.engine import CYCLE_S, TF_LIMITS
from app.agent.market.base import drop_forming
from app.agent.market.sync import plain_error
from app.agent.market.universe import (
    backfill_job,
    run_eligibility_check,
    warming_up,
)
from app.api import deps
from app.api.deps import AppState, get_state, require_token
from app.db import AMSTERDAM, MarketCheckRow, MarketRow, SyncRunRow, utcnow
from app.models import MarketSettings, MoneyDecimal, Regime

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

PRICE_CACHE_S = 60.0

StatusKind = Literal[
    "warming_up",
    "failing",
    "fails",
    "paused",
    "held",
    "active",
    "eligible",
    "recovering",
    "excluded",
    "held_not_active",
]


# ------------------------------------------------------------------ schemas --


class CheckLineOut(BaseModel):
    last_check_at: datetime | None
    trigger: str | None
    status: Literal["ok", "failed", "running"] | None
    error: str | None
    result_in_use_at: datetime | None


class FunnelStepOut(BaseModel):
    rule: str
    label: str | None = None
    left: int
    dropped: list[str]


class CheckRunOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    trigger: str
    status: str
    error: str | None
    thresholds: dict[str, Any]
    funnel: list[FunnelStepOut]
    kept: list[str]
    held_back: list[str]


class CheckDetailOut(BaseModel):
    latest: CheckRunOut | None
    in_use: CheckRunOut | None


class MarketStatusOut(BaseModel):
    kind: StatusKind
    line: str
    held: bool = False
    facts: dict[str, Any] = {}


class UniverseMarketOut(BaseModel):
    market: str
    name: str | None
    status: MarketStatusOut
    eligible: bool
    active: bool
    failed_rule: str | None
    below_streak: int
    within_streak: int
    checked_at: datetime | None
    activated_at: datetime | None
    last_price: MoneyDecimal | None
    price_at: datetime | None
    volume_30d_eur: MoneyDecimal | None
    spread_pct: MoneyDecimal | None
    vs_btc_30d_pct: MoneyDecimal | None
    corr_btc_30d: MoneyDecimal | None
    regime: Regime | None = None


class UniverseCallsOut(BaseModel):
    per_market_per_day: int
    active_per_day: int
    daily_call_budget: int


class UniverseTilesOut(BaseModel):
    eur_markets: int
    eur_excluded: int
    eligible: int
    active: int
    active_calls_per_day: int
    held: list[str]
    failing: dict[str, Any]
    warming: dict[str, Any]


class UniverseOut(BaseModel):
    check: CheckLineOut
    tiles: UniverseTilesOut
    calls: UniverseCallsOut
    markets: list[UniverseMarketOut]


class SyncFailureOut(BaseModel):
    what: str
    tf: str | None = None
    error: str
    next_retry: str | None = None


class SyncJobOut(BaseModel):
    job: str
    market: str | None = None
    status: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    done: int | None
    total: int | None
    failures: list[SyncFailureOut]
    progress: dict[str, Any] | None = None


class WatcherStateOut(BaseModel):
    checked_at: datetime | None
    errors: int


class SyncOut(BaseModel):
    jobs: list[SyncJobOut]
    watcher: WatcherStateOut | None


# ------------------------------------------------------------- market status --


def _eur_volume_m(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value / Decimal(1_000_000):.2f}"


def _pct(value: Decimal | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _rule_line(row: MarketRow, cfg: MarketSettings) -> str:
    """The excluded/failing line's rule with its numbers (spec wording)."""
    rule = row.failed_rule
    if rule == "volume":
        return (
            f"volume €{_eur_volume_m(row.volume_30d_eur)}M < "
            f"€{_eur_volume_m(cfg.min_avg_daily_volume_eur)}M a day"
        )
    if rule == "spread":
        return f"spread {_pct(row.spread_pct)}% > {_pct(cfg.max_avg_spread_pct)}%"
    if rule == "history":
        return f"daily history {row.daily_candles or 0} of {cfg.min_daily_candles} candles"
    if rule == "type":
        return "stablecoin"
    if rule == "status":
        return f"not trading on Bitvavo ({row.status or 'unknown'})"
    return rule or "not checked"


def _rule_facts(row: MarketRow, cfg: MarketSettings) -> dict[str, Any]:
    return {
        "rule": row.failed_rule,
        "volume_30d_eur": str(row.volume_30d_eur) if row.volume_30d_eur is not None else None,
        "spread_pct": str(row.spread_pct) if row.spread_pct is not None else None,
        "daily_candles": row.daily_candles,
        "min_avg_daily_volume_eur": str(cfg.min_avg_daily_volume_eur),
        "max_avg_spread_pct": str(cfg.max_avg_spread_pct),
        "min_daily_candles": cfg.min_daily_candles,
    }


def _fmt_when(value: datetime | None) -> str:
    if value is None:
        return "—"
    return repo.aware_utc(value).astimezone(AMSTERDAM).strftime("%a %H:%M")


def _position(session: Session, state: AppState, market: str):
    for row in repo.list_position_rows(session, state.account_id):
        if row.pair == market:
            return row
    return None


def _last_decision(session: Session, state: AppState, market: str):
    return repo.latest_decisions(session, state.account_id, [market]).get(market)


def _decision_text(drow) -> str | None:
    if drow is None:
        return None
    action = getattr(drow, "action", None)
    if action is None:
        return None
    return str(action)


def _deactivated_at(session: Session, market: str) -> datetime | None:
    rows = repo.list_activity(session, pair=market, kind="alert", limit=50)
    for row in rows:
        if "Deactivated" in (row.text or ""):
            return repo.aware_utc(row.at)
    return None


def market_status(
    session: Session,
    state: AppState,
    row: MarketRow,
    cfg: MarketSettings,
    *,
    counts: dict[str, int] | None = None,
    backfill_failures: list[dict[str, Any]] | None = None,
    backfill_next_retry: str | None = None,
) -> MarketStatusOut:
    """The one status a market gets, first match wins (docs/SPEC.md,
    Universe screen → The market table)."""
    market = row.market
    pos = _position(session, state, market)
    held = pos is not None
    drow = _last_decision(session, state, market)
    decision = _decision_text(drow)

    if row.active:
        if warming_up(session, market, cfg):
            counts = counts or repo.candle_counts(session, market)
            tfs: dict[str, dict[str, int]] = {}
            for tf, need in (
                ("1h", cfg.history_1h),
                ("4h", cfg.history_4h),
                ("1d", cfg.history_1d),
            ):
                tfs[tf] = {"have": counts.get(tf, 0), "need": need}
            line = " · ".join(f"{tf} {v['have']} of {v['need']}" for tf, v in tfs.items())
            if backfill_failures:
                first = backfill_failures[0]
                line += f" · {first.get('error', 'failed')}"
                if backfill_next_retry:
                    retry_at = datetime.fromisoformat(backfill_next_retry.replace("Z", "+00:00"))
                    line += f" · retry {_fmt_when(retry_at)}"
            line += " · trades once loaded"
            return MarketStatusOut(
                kind="warming_up",
                line=line,
                held=held,
                facts={"tfs": tfs, "failures": backfill_failures or []},
            )
        if not row.eligible:
            since = _fmt_when(row.failing_since or row.checked_at)
            days = (
                max(1, (utcnow() - repo.aware_utc(row.failing_since)).days + 1)
                if row.failing_since is not None
                else (row.below_streak or cfg.eligibility_fail_days)
            )
            plural = "s" if cfg.eligibility_recover_days != 1 else ""
            line = (
                f"{_rule_line(row, cfg)} · failing since {since} · "
                f"no new entries · deactivate or wait for "
                f"{cfg.eligibility_recover_days} passing day{plural}"
            )
            return MarketStatusOut(
                kind="failing",
                line=line,
                held=held,
                facts={
                    "days": days,
                    "since": row.failing_since.isoformat() if row.failing_since else None,
                    **_rule_facts(row, cfg),
                },
            )
        if row.below_streak and row.below_streak < cfg.eligibility_fail_days:
            remaining = cfg.eligibility_fail_days - row.below_streak
            day_word = "day" if remaining == 1 else "days"
            line = (
                f"{row.below_streak} of {cfg.eligibility_fail_days} fails · "
                f"{_rule_line(row, cfg)} · "
                f"loses its place after {remaining} more failing {day_word} · "
                "entries still allowed"
            )
            return MarketStatusOut(
                kind="fails",
                line=line,
                held=held,
                facts={
                    "fails": row.below_streak,
                    "of": cfg.eligibility_fail_days,
                    **_rule_facts(row, cfg),
                },
            )
        pause = repo.active_entry_pause(session, market, utcnow())
        if pause is not None:
            until = repo.aware_utc(pause.until) if pause.until is not None else None
            when = "until resumed" if until is None else f"until {_fmt_when(until)}"
            line = f"entries paused {when} · exits keep working"
            return MarketStatusOut(
                kind="paused",
                line=line,
                held=held,
                facts={"until": until.isoformat() if until else None},
            )
        if held:
            qty = getattr(pos, "qty", None)
            opened = getattr(pos, "opened_at", None)
            line = f"held {qty or ''} since {_fmt_when(opened)}"
            if decision:
                line += f" · last {decision}"
            return MarketStatusOut(kind="held", line=line, held=True, facts={"qty": qty})
        line = f"last {decision}" if decision else "no decisions yet"
        return MarketStatusOut(
            kind="active", line=line, held=False, facts={"last_decision": decision}
        )

    # Not active.
    if held:
        deact = _deactivated_at(session, market)
        qty = getattr(pos, "qty", None)
        opened = getattr(pos, "opened_at", None)
        when = f"deactivated {_fmt_when(deact)} · " if deact is not None else ""
        line = f"{when}the position is still managed"
        if qty is not None:
            line = f"held {qty} since {_fmt_when(opened)} · {line}"
        return MarketStatusOut(
            kind="held_not_active",
            line=line,
            held=True,
            facts={"qty": qty, "deactivated_at": deact.isoformat() if deact else None},
        )
    if row.eligible:
        line = f"passing since {_fmt_when(row.eligible_since or row.checked_at)}"
        return MarketStatusOut(
            kind="eligible",
            line=line,
            facts={"since": row.eligible_since.isoformat() if row.eligible_since else None},
        )
    if not row.failed_rule and (row.within_streak or 0) > 0:
        remaining = cfg.eligibility_recover_days - (row.within_streak or 0)
        nxt = _fmt_when(_next_check_at())
        line = (
            f"eligible from {nxt} if it passes {remaining} more day{'s' if remaining != 1 else ''}"
        )
        return MarketStatusOut(
            kind="recovering",
            line=line,
            facts={"passing": row.within_streak, "need": cfg.eligibility_recover_days},
        )
    line = _rule_line(row, cfg)
    return MarketStatusOut(
        kind="excluded",
        line=line,
        facts=_rule_facts(row, cfg),
    )


def _next_check_at() -> datetime:
    """The next daily check: tomorrow 01:00 UTC at the latest."""
    now = utcnow()
    due = now.replace(hour=1, minute=0, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    return due


# ------------------------------------------------------------------ helpers --


def _check_run_out(row: MarketCheckRow) -> CheckRunOut:
    def loads(value: str | None, default: Any) -> Any:
        try:
            return json.loads(value) if value else default
        except (TypeError, ValueError):
            return default

    funnel_raw = loads(row.funnel, [])
    labels = {
        "listed": "Bitvavo EUR markets",
        "type": "not a stablecoin or fiat token",
        "status": "trading, not halted or delisting",
        "volume": "30-day average daily volume",
        "spread": "average spread over 24h",
        "history": "daily candles",
        "all": "pass every rule today",
    }
    funnel = [
        FunnelStepOut(
            rule=str(step.get("rule", "")),
            label=labels.get(str(step.get("rule", ""))),
            left=int(step.get("left", 0)),
            dropped=[str(d) for d in step.get("dropped", [])],
        )
        for step in funnel_raw
        if isinstance(step, dict)
    ]
    return CheckRunOut(
        id=row.id,
        started_at=repo.aware_utc(row.started_at),
        finished_at=repo.aware_utc(row.finished_at) if row.finished_at else None,
        trigger=row.trigger,
        status=row.status,
        error=row.error,
        thresholds=loads(row.thresholds, {}),
        funnel=funnel,
        kept=[str(m) for m in loads(row.kept, [])],
        held_back=[str(m) for m in loads(row.held_back, [])],
    )


def _latest_check(session: Session, status: str | None = None) -> MarketCheckRow | None:
    conds = []
    if status is not None:
        conds.append(MarketCheckRow.status == status)
    stmt = select(MarketCheckRow).order_by(MarketCheckRow.id.desc()).limit(1)
    if conds:
        stmt = stmt.where(*conds)
    return session.scalar(stmt)


async def _prices(state: AppState) -> dict[str, Decimal]:
    """Last prices for every market, fetched at most once a minute; a
    failed fetch keeps the cached prices and their time."""
    now = utcnow()
    fresh = (
        state.universe_prices_at is not None
        and (now - state.universe_prices_at).total_seconds() < PRICE_CACHE_S
    )
    if fresh:
        return state.universe_prices
    try:
        prices = await state.market_client.ticker_prices()
    except Exception as exc:  # noqa: BLE001 - keep the cached prices
        log.warning("universe price refresh failed: %s", plain_error(exc))
        return state.universe_prices
    state.universe_prices = prices
    state.universe_prices_at = now
    session = state.session_factory()
    try:
        for market, price in prices.items():
            row = session.get(MarketRow, market)
            if row is not None:
                row.last_price = price
                row.price_at = now.replace(tzinfo=None)
        session.commit()
    finally:
        session.close()
    return prices


def _backfill_run(session: Session, market: str) -> SyncRunRow | None:
    return session.scalar(
        select(SyncRunRow)
        .where(SyncRunRow.job == f"backfill:{market}")
        .order_by(SyncRunRow.id.desc())
        .limit(1)
    )


def _backfill_failures(run: SyncRunRow | None) -> tuple[list[dict[str, Any]], str | None]:
    if run is None or not run.failures:
        return [], None
    try:
        failures = json.loads(run.failures)
    except (TypeError, ValueError):
        return [], None
    nxt = next(
        (f.get("next_retry") for f in failures if isinstance(f, dict) and f.get("next_retry")),
        None,
    )
    return [f for f in failures if isinstance(f, dict)], nxt


def _sort_key(item: tuple[int, MarketStatusOut, MarketRow]) -> tuple[Any, ...]:
    rank, status, row = item
    attention = 0 if status.kind in {"warming_up", "failing", "fails", "paused", "held"} else 1
    volume = row.volume_30d_eur if row.volume_30d_eur is not None else Decimal(0)
    return (rank, attention, -volume)


# -------------------------------------------------------------------- routes --


@router.get("/universe")
async def get_universe(state: Annotated[AppState, Depends(get_state)]) -> UniverseOut:
    session = state.session_factory()
    try:
        cfg = state.settings.market
        rows = repo.list_market_rows(session)
        prices = await _prices(state)
        pairs = [r.market for r in rows]
        decisions = repo.latest_decisions(session, state.account_id, pairs)
        statuses: list[tuple[int, MarketStatusOut, MarketRow, UniverseMarketOut]] = []
        for row in rows:
            run = _backfill_run(session, row.market) if row.active else None
            failures, nxt = _backfill_failures(run)
            status = market_status(
                session,
                state,
                row,
                cfg,
                counts=repo.candle_counts(session, row.market),
                backfill_failures=failures,
                backfill_next_retry=nxt,
            )
            drow = decisions.get(row.market)
            regime = None
            if row.active and drow is not None:
                try:
                    regime = Regime.model_validate_json(drow.regime)
                except (ValueError, TypeError):
                    regime = None
            price_at = repo.aware_utc(row.price_at) if row.price_at else None
            out = UniverseMarketOut(
                market=row.market,
                name=row.name,
                status=status,
                eligible=row.eligible,
                active=row.active,
                failed_rule=row.failed_rule,
                below_streak=row.below_streak or 0,
                within_streak=row.within_streak or 0,
                checked_at=repo.aware_utc(row.checked_at) if row.checked_at else None,
                activated_at=repo.aware_utc(row.activated_at) if row.activated_at else None,
                last_price=prices.get(row.market, row.last_price),
                price_at=price_at,
                volume_30d_eur=row.volume_30d_eur,
                spread_pct=row.spread_pct,
                vs_btc_30d_pct=row.vs_btc_30d_pct,
                corr_btc_30d=row.corr_btc_30d,
                regime=regime,
            )
            group = (
                0
                if row.active
                else (1 if row.eligible else (2 if status.kind == "recovering" else 3))
            )
            statuses.append((group, status, row, out))

        statuses.sort(key=lambda t: _sort_key((t[0], t[1], t[2])))
        markets = [t[3] for t in statuses]

        # Tiles.
        eur = len(rows)
        eligible_rows = [r for r in rows if r.eligible]
        active_rows = [r for r in rows if r.active]
        held_markets = sorted(p.pair for p in repo.list_position_rows(session, state.account_id))
        failing = [r for r in active_rows if r.failed_rule is not None]
        failing_info = {
            "count": len(failing),
            "names": [r.market for r in failing],
            "days": {
                r.market: (
                    max(1, (utcnow() - repo.aware_utc(r.failing_since)).days + 1)
                    if r.failing_since is not None
                    else (r.below_streak or 1)
                )
                for r in failing
            },
        }
        warming = []
        for r in active_rows:
            if warming_up(session, r.market, cfg):
                counts = repo.candle_counts(session, r.market)
                missing = [
                    tf
                    for tf, need in (
                        ("1h", cfg.history_1h),
                        ("4h", cfg.history_4h),
                        ("1d", cfg.history_1d),
                    )
                    if counts.get(tf, 0) < need
                ]
                warming.append({"market": r.market, "missing": missing})
        warming_info = {
            "count": len(warming),
            "first": warming[0]["market"] if warming else None,
            "reason": ", ".join(warming[0]["missing"]) if warming else None,
        }

        latest_check = _latest_check(session)
        in_use_check = _latest_check(session, status="ok")
        check = CheckLineOut(
            last_check_at=(
                repo.aware_utc(latest_check.finished_at or latest_check.started_at)
                if latest_check is not None
                else None
            ),
            trigger=latest_check.trigger if latest_check is not None else None,
            status=(
                "running"
                if latest_check is not None and latest_check.finished_at is None
                else (latest_check.status if latest_check is not None else None)
            ),
            error=latest_check.error if latest_check is not None else None,
            result_in_use_at=(
                repo.aware_utc(in_use_check.finished_at) if in_use_check is not None else None
            ),
        )

        per_market = 86400 // CYCLE_S
        return UniverseOut(
            check=check,
            tiles=UniverseTilesOut(
                eur_markets=eur,
                eur_excluded=eur - len(eligible_rows),
                eligible=len(eligible_rows),
                active=len(active_rows),
                active_calls_per_day=len(active_rows) * per_market,
                held=held_markets,
                failing=failing_info,
                warming=warming_info,
            ),
            calls=UniverseCallsOut(
                per_market_per_day=per_market,
                active_per_day=len(active_rows) * per_market,
                daily_call_budget=state.settings.model.daily_call_budget,
            ),
            markets=markets,
        )
    finally:
        session.close()


@router.get("/universe/check")
def get_universe_check(
    state: Annotated[AppState, Depends(get_state)],
) -> CheckDetailOut:
    session = state.session_factory()
    try:
        latest = _latest_check(session)
        in_use = _latest_check(session, status="ok")
        return CheckDetailOut(
            latest=_check_run_out(latest) if latest is not None else None,
            in_use=_check_run_out(in_use) if in_use is not None else None,
        )
    finally:
        session.close()


def _sync_job_out(row: SyncRunRow, progress: dict[str, Any] | None = None) -> SyncJobOut:
    try:
        failures = json.loads(row.failures) if row.failures else []
    except (TypeError, ValueError):
        failures = []
    market = row.job[len("backfill:") :] if row.job.startswith("backfill:") else None
    return SyncJobOut(
        job=row.job.split(":", 1)[0] if row.job.startswith("backfill:") else row.job,
        market=market,
        status=row.status,
        started_at=repo.aware_utc(row.started_at),
        finished_at=repo.aware_utc(row.finished_at) if row.finished_at else None,
        duration_seconds=(
            (repo.aware_utc(row.finished_at) - repo.aware_utc(row.started_at)).total_seconds()
            if row.finished_at is not None
            else None
        ),
        done=row.done,
        total=row.total,
        failures=[
            SyncFailureOut(
                what=str(f.get("what", "")),
                tf=f.get("tf"),
                error=str(f.get("error", "")),
                next_retry=f.get("next_retry"),
            )
            for f in failures
            if isinstance(f, dict)
        ],
        progress=progress,
    )


def _backfill_progress(session: Session, state: AppState, row: SyncRunRow) -> dict[str, Any] | None:
    """While a backfill runs: candles loaded of needed and the seconds
    left from the job's own pace (none in its first 5 s)."""
    if row.status != "running" or not row.job.startswith("backfill:"):
        return None
    market = row.job[len("backfill:") :]
    cfg = state.settings.market
    counts = repo.candle_counts(session, market)
    loaded = sum(counts.get(tf, 0) for tf in ("1h", "4h", "1d"))
    needed = cfg.history_1h + cfg.history_4h + cfg.history_1d
    eta = None
    started = repo.aware_utc(row.started_at)
    elapsed = (utcnow() - started).total_seconds()
    if elapsed > 5 and row.done and row.total and row.done < row.total:
        rate = row.done / elapsed
        if rate > 0:
            eta = (row.total - row.done) / rate
    return {"loaded": loaded, "needed": needed, "eta_seconds": eta}


@router.get("/universe/sync")
def get_universe_sync(state: Annotated[AppState, Depends(get_state)]) -> SyncOut:
    session = state.session_factory()
    try:
        jobs: list[SyncJobOut] = []
        for job in ("exchange_rules", "daily_check", "candles"):
            row = state.syncs.latest(job) if state.syncs is not None else None
            if row is not None:
                jobs.append(_sync_job_out(row))
        # Backfill: the latest run per market.
        seen: set[str] = set()
        for row in state.syncs.runs_for("backfill") if state.syncs is not None else []:
            market = row.job[len("backfill:") :]
            if market in seen:
                continue
            seen.add(market)
            jobs.append(_sync_job_out(row, _backfill_progress(session, state, row)))
        engine = state.agent_engine
        watcher = None
        if engine is not None:
            watcher = WatcherStateOut(
                checked_at=engine.watcher_checked_at,
                errors=getattr(engine, "_watch_errors", 0),
            )
        return SyncOut(jobs=jobs, watcher=watcher)
    finally:
        session.close()


# --------------------------------------------------------------------- retry --


def _spawn(state: AppState, fn, name: str) -> None:
    """A tracked one-shot task for a sync retry."""

    async def wrapped() -> None:
        await fn()

    tasks_mod.spawn(wrapped, name=name, restart=False, track=lambda t: deps._track(state, t))


async def _retry_exchange_rules(state: AppState) -> None:
    from app.main import _exchange_rules_job  # lazy: main imports this router

    run = state.syncs.begin("exchange_rules")
    try:
        await _exchange_rules_job(state, run)
    except Exception as exc:
        run.abort(plain_error(exc))
        raise
    finally:
        run.finish()


async def _retry_daily_check(state: AppState) -> None:
    session = state.session_factory()
    run = state.syncs.begin("daily_check")
    try:
        await run_eligibility_check(
            state.market_client,
            session,
            state.settings.market,
            trigger="retry",
            on_progress=run.progress,
        )
        latest = session.scalar(select(MarketCheckRow).order_by(MarketCheckRow.id.desc()).limit(1))
        if latest is not None and latest.status == "failed":
            run.abort(latest.error or "check failed")
    except Exception as exc:
        run.abort(plain_error(exc))
        raise
    finally:
        run.finish()
        session.close()


async def _retry_candles(state: AppState, markets: set[str]) -> None:
    """Refetch the failed markets' 1h/4h/1d candles now."""
    session = state.session_factory()
    run = state.syncs.begin("candles", total=len(markets) * 3)
    done = 0
    try:
        now = utcnow()
        for market in sorted(markets):
            for tf in ("1h", "4h", "1d"):
                try:
                    candles = drop_forming(
                        await state.market_client.candles(market, tf, TF_LIMITS[tf]), tf, now
                    )
                    repo.upsert_candles(session, market, tf, candles)
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - per-market failure
                    run.fail(market, plain_error(exc), tf=tf)
                done += 1
                run.progress(done, len(markets) * 3)
    finally:
        run.finish()
        session.close()


async def _retry_backfill(state: AppState, wanted: dict[str, set[str]]) -> None:
    for market, tfs in wanted.items():
        await backfill_job(
            state.market_client,
            state.session_factory,
            market,
            state.settings.market,
            tfs=tfs,
            syncs=state.syncs,
            cycle_offset_s=state.settings.cycle_offset_s,
        )


def _failed_backfills(session: Session, market: str | None) -> dict[str, set[str]]:
    """``{market: {tf}}`` from failed/partial backfill runs, optionally
    for one market."""
    stmt = (
        select(SyncRunRow)
        .where(
            SyncRunRow.job.like("backfill:%"),
            SyncRunRow.status.in_(("failed", "partial")),
        )
        .order_by(SyncRunRow.id)
    )
    out: dict[str, set[str]] = {}
    for row in session.execute(stmt).scalars().all():
        m = row.job[len("backfill:") :]
        if market is not None and m != market:
            continue
        try:
            failures = json.loads(row.failures) if row.failures else []
        except (TypeError, ValueError):
            continue
        tfs = {str(f["tf"]) for f in failures if isinstance(f, dict) and f.get("tf")}
        if tfs:
            out.setdefault(m, set()).update(tfs)
    return out


@router.post("/universe/sync/{job}/retry", dependencies=[Depends(require_token)])
async def retry_sync_job(
    job: str, state: Annotated[AppState, Depends(get_state)]
) -> dict[str, str]:
    family = job.split(":", 1)[0]
    market_filter = job.split(":", 1)[1] if ":" in job else None
    if family not in ("exchange_rules", "daily_check", "candles", "backfill"):
        raise HTTPException(status_code=404, detail="unknown sync job")
    engine = state.agent_engine
    if engine is not None and engine.cycle_in_progress:
        raise HTTPException(status_code=409, detail="a cycle is running")
    if state.syncs is None:
        raise HTTPException(status_code=409, detail="sync tracking is not running")

    session = state.session_factory()
    try:
        if family == "backfill":
            wanted = _failed_backfills(session, market_filter)
            if not wanted:
                raise HTTPException(status_code=409, detail="the job did not fail")
            if any(t.done() is False for t in state.backfill_tasks.values()):
                raise HTTPException(status_code=409, detail="a backfill is running")
            _spawn(state, lambda: _retry_backfill(state, wanted), f"retry-backfill-{family}")
        else:
            latest = state.syncs.latest(family)
            if latest is None or latest.status not in ("failed", "partial"):
                raise HTTPException(status_code=409, detail="the job did not fail")
            if state.syncs.running(family):
                raise HTTPException(status_code=409, detail="the job is running")
            if family == "exchange_rules":
                _spawn(state, lambda: _retry_exchange_rules(state), "retry-exchange-rules")
            elif family == "daily_check":
                _spawn(state, lambda: _retry_daily_check(state), "retry-daily-check")
            else:  # candles: rerun for its failed markets
                try:
                    failures = json.loads(latest.failures) if latest.failures else []
                except (TypeError, ValueError):
                    failures = []
                markets = {
                    str(f.get("what")) for f in failures if isinstance(f, dict) and f.get("what")
                }
                if not markets:
                    raise HTTPException(status_code=409, detail="the job did not fail")
                _spawn(
                    state,
                    lambda: _retry_candles(state, markets),
                    "retry-candles",
                )
    finally:
        session.close()
    return {"job": family, "status": "started"}
