"""The live-readiness checklist: nine criteria that must all pass before a
Live switch could unlock (live trading itself is a later phase).

Counts and dates come from the current paper account only. The four
thresholds editable in Settings live in ``settings.readiness``; the other
five use the spec defaults in ``DEFAULTS`` below — a loosened editable
threshold is reported with the date it changed, from the settings history.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import repo
from app.db import AMSTERDAM, AccountRow, DecisionRow, OutcomeRow
from app.models import ReadinessOut, ReadinessRowOut, Settings

# Spec defaults for the criteria that are not editable in Settings.
MIN_AVG_R = Decimal("0.2")
MIN_PROFIT_FACTOR = Decimal("1.3")
CALIBRATION_MIN_BUYS = 50
BUCKET_MIN_BUYS = 10
KILL_FREE_DAYS = 30
ERROR_FREE_DAYS = 7

# Editable thresholds: key → (row id, "lower is looser" direction).
_EDITABLE: dict[str, tuple[str, Literal["min", "max"], Decimal]] = {
    "readiness.min_closed_trades": ("closed_trades", "min", Decimal("100")),
    "readiness.min_days_running": ("days_running", "min", Decimal("30")),
    "readiness.max_drawdown_pct": ("max_drawdown", "max", Decimal("6")),
    "readiness.calibration_gap_pts": ("calibration", "max", Decimal("10")),
}

BUCKETS: tuple[tuple[float, float], ...] = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _ams_date(at: datetime) -> str:
    return at.astimezone(AMSTERDAM).strftime("%d %b")


def _month(at: datetime) -> str:
    return at.astimezone(AMSTERDAM).strftime("early %B")


def _estimate_date(now: datetime, have: int, need: int, per_day: float) -> datetime | None:
    """Estimated completion date at the current pace (None when the pace is
    zero — no trades yet means no projection)."""
    if per_day <= 0 or have >= need:
        return None
    return now + timedelta(days=(need - have) / per_day)


def _loosened_note(session: Session, key: str, current: Decimal) -> str | None:
    """ "was 120, changed 10 Oct" when the current value is looser than the
    spec default and a settings-history change recorded it."""
    _row_id, direction, default = _EDITABLE[key]
    looser = current < default if direction == "min" else current > default
    if not looser:
        return None
    change = repo.latest_settings_change(session, key)
    when = _ams_date(repo.aware_utc(change.at)) if change is not None else "an earlier save"
    old = ""
    if change is not None and change.old is not None:
        try:
            old = f"was {json.loads(change.old)}, "
        except (TypeError, json.JSONDecodeError):
            old = ""
    return f"loosened: {old}changed {when}"


def _max_drawdown(
    session: Session, account_id: int, current_equity: Decimal, now: datetime
) -> float:
    """Worst equity fall from a running peak, over stored snapshots plus
    the live reading."""
    first = repo.first_equity_snapshot(session, account_id)
    if first is None:
        return 0.0
    snaps = repo.list_equity_snapshots(session, account_id, repo.aware_utc(first.at), now)
    peak = Decimal(first.equity)
    worst = Decimal("0")
    for snap in snaps:
        eq = Decimal(snap.equity)
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak * 100)
    peak = max(peak, current_equity)
    if peak > 0:
        worst = max(worst, (peak - current_equity) / peak * 100)
    return float(worst)


def _calibration(session: Session, account_id: int) -> tuple[float | None, int, int]:
    """(weighted gap in points, BUYs scored, total BUYs) — spec buckets by
    stated confidence; a bucket under 10 scored BUYs is skipped."""
    rows = session.execute(
        select(DecisionRow.confidence, OutcomeRow.label)
        .join(OutcomeRow, OutcomeRow.decision_id == DecisionRow.id)
        .where(
            DecisionRow.account_id == account_id,
            DecisionRow.action == "BUY",
            OutcomeRow.horizon == 72,
        )
    ).all()
    total_buys = (
        session.scalar(
            select(func.count())
            .select_from(DecisionRow)
            .where(DecisionRow.account_id == account_id, DecisionRow.action == "BUY")
        )
        or 0
    )
    weighted = Decimal("0")
    weight = 0
    for lo, hi in BUCKETS:
        bucket = [(conf, label) for conf, label in rows if lo <= conf < hi]
        if len(bucket) < BUCKET_MIN_BUYS:
            continue
        stated = sum(conf for conf, _ in bucket) / len(bucket) * 100
        wins = sum(1 for _, label in bucket if label == "win")
        gap = abs(Decimal(str(stated - wins / len(bucket) * 100)))
        weighted += gap * len(bucket)
        weight += len(bucket)
    if weight == 0:
        return None, len(rows), total_buys
    return float(weighted / weight), len(rows), total_buys


def compute_readiness(
    session: Session,
    *,
    now: datetime,
    account: AccountRow,
    equity: Decimal,
    settings: Settings,
) -> ReadinessOut:
    """All nine criteria with their current value, target, pass/fail and,
    for counts, an estimated date at the current pace."""
    rs = settings.readiness
    account_id = account.id
    started = repo.aware_utc(account.started_at)
    running_days = (now - started).total_seconds() / 86400
    trades = repo.list_trade_rows(session, account_id, limit=10000)
    closed_count = len(trades)
    per_day = closed_count / running_days if running_days > 0 else 0.0

    rows: list[ReadinessRowOut] = []
    estimates: list[tuple[str, datetime]] = []

    # 1. Closed trades ------------------------------------------------------
    est = _estimate_date(now, closed_count, rs.min_closed_trades, per_day)
    met = closed_count >= rs.min_closed_trades
    rows.append(
        ReadinessRowOut(
            id="closed_trades",
            name="Closed trades",
            why="Enough trades to tell skill from luck",
            value_text=str(closed_count),
            target_text=f"≥ {rs.min_closed_trades}",
            progress=_clamp(closed_count / rs.min_closed_trades),
            met=met,
            hint=(f"≈ {_ams_date(est)}" if est else None) or ("no pace yet" if not met else None),
            estimated_at=est,
            loosened=_loosened_note(
                session, "readiness.min_closed_trades", Decimal(rs.min_closed_trades)
            ),
        )
    )
    if est:
        estimates.append(("the trade count", est))

    # 2. Days running --------------------------------------------------------
    met = running_days >= rs.min_days_running
    est2 = started + timedelta(days=float(rs.min_days_running))
    rows.append(
        ReadinessRowOut(
            id="days_running",
            name="Days running",
            why="Seen more than one kind of market",
            value_text=str(int(running_days)),
            target_text=f"≥ {rs.min_days_running}",
            progress=_clamp(running_days / float(rs.min_days_running)),
            met=met,
            hint=f"≈ {_ams_date(est2)}" if not met else None,
            estimated_at=est2 if not met else None,
            loosened=_loosened_note(
                session, "readiness.min_days_running", Decimal(rs.min_days_running)
            ),
        )
    )
    if not met:
        estimates.append(("the time running", est2))

    # 3. Beats holding BTC ----------------------------------------------------
    start_balance = Decimal(account.start_balance)
    agent_pct = float((equity / start_balance - 1) * 100) if start_balance > 0 else 0.0
    btc_then = repo.load_candles(session, "BTC-EUR", "1d", 400, end=started)
    btc_now = repo.load_candles(session, "BTC-EUR", "1d", 5, end=now)
    btc_pct: float | None = None
    if btc_then and btc_now and btc_then[-1].close > 0:
        btc_pct = float((btc_now[-1].close / btc_then[-1].close - 1) * 100)
    met = btc_pct is not None and agent_pct > btc_pct
    rows.append(
        ReadinessRowOut(
            id="beats_btc",
            name="Beats holding BTC",
            why="Since start, after fees",
            value_text=(
                f"{agent_pct:+.2f}% vs {btc_pct:+.2f}%"
                if btc_pct is not None
                else f"{agent_pct:+.2f}% vs —"
            ),
            target_text="ahead",
            progress=1.0 if met else _clamp(0.5 + (agent_pct - (btc_pct or 0)) / 20),
            met=met,
            hint=None if met else ("no BTC history yet" if btc_pct is None else None),
        )
    )

    # 4. Max drawdown ----------------------------------------------------------
    worst_dd = _max_drawdown(session, account_id, equity, now)
    met = worst_dd < float(rs.max_drawdown_pct)
    rows.append(
        ReadinessRowOut(
            id="max_drawdown",
            name="Max drawdown",
            why="Worst fall from a peak",
            value_text=f"{worst_dd:.1f}%",
            target_text=f"< {rs.max_drawdown_pct.normalize():f}%",
            progress=_clamp(float(rs.max_drawdown_pct) / worst_dd) if worst_dd else 1.0,
            met=met,
            hint=None,
            loosened=_loosened_note(
                session, "readiness.max_drawdown_pct", Decimal(rs.max_drawdown_pct)
            ),
        )
    )

    # 5. Average R -------------------------------------------------------------
    r_values = [Decimal(t.realised_r) for t in trades]
    avg_r = float(sum(r_values) / len(r_values)) if r_values else None
    met = avg_r is not None and avg_r > float(MIN_AVG_R)
    rows.append(
        ReadinessRowOut(
            id="avg_r",
            name="Average R per trade",
            why="Average win/loss in units of risk",
            value_text=f"{avg_r:+.2f}" if avg_r is not None else "—",
            target_text=f"> +{MIN_AVG_R}",
            progress=_clamp((avg_r or 0) / float(MIN_AVG_R)),
            met=met,
            hint=None if met else ("needs better trades, not time" if trades else None),
        )
    )

    # 6. Profit factor -----------------------------------------------------------
    won = sum(Decimal(t.realised_eur) for t in trades if Decimal(t.realised_eur) > 0)
    lost = sum(Decimal(t.realised_eur) for t in trades if Decimal(t.realised_eur) < 0)
    pf = float(won / abs(lost)) if lost != 0 else (None if not trades else 99.9)
    met = pf is not None and pf > float(MIN_PROFIT_FACTOR)
    rows.append(
        ReadinessRowOut(
            id="profit_factor",
            name="Profit factor",
            why="Total won ÷ total lost",
            value_text=f"{pf:.2f}" if pf is not None else "—",
            target_text=f"> {MIN_PROFIT_FACTOR}",
            progress=_clamp((pf or 0) / float(MIN_PROFIT_FACTOR)),
            met=met,
            hint=None if met else ("needs better trades, not time" if trades else None),
        )
    )

    # 7. Calibration -------------------------------------------------------------
    gap, _scored, total_buys = _calibration(session, account_id)
    enough = total_buys >= CALIBRATION_MIN_BUYS
    met = enough and gap is not None and gap <= float(rs.calibration_gap_pts)
    hint = None
    if not met:
        if not enough:
            hint = f"needs {CALIBRATION_MIN_BUYS} BUYs (have {total_buys})"
        elif gap is None:
            hint = f"needs ≥{BUCKET_MIN_BUYS} BUYs per bucket"
        else:
            hint = "see Insights"
    rows.append(
        ReadinessRowOut(
            id="calibration",
            name="Confidence calibration",
            why=f"Weighted gap over confidence buckets · needs {CALIBRATION_MIN_BUYS} BUYs",
            value_text=f"{gap:.0f} pts" if gap is not None else "—",
            target_text=f"≤ {rs.calibration_gap_pts.normalize():f} pts",
            progress=(
                _clamp(float(rs.calibration_gap_pts) / gap)
                if gap
                else _clamp(total_buys / CALIBRATION_MIN_BUYS)
            ),
            met=met,
            hint=hint,
            loosened=_loosened_note(
                session, "readiness.calibration_gap_pts", Decimal(rs.calibration_gap_pts)
            ),
        )
    )

    # 8. No kill switch ------------------------------------------------------------
    last_kill = account.last_kill_at
    kill_free = (
        repo.aware_utc(last_kill) < now - timedelta(days=KILL_FREE_DAYS)
        if last_kill is not None
        else True
    )
    met = kill_free
    rows.append(
        ReadinessRowOut(
            id="no_kill",
            name="No kill switch",
            why=f"In the last {KILL_FREE_DAYS} days",
            value_text=(
                "0 kills"
                if last_kill is None or kill_free
                else f"killed {_ams_date(repo.aware_utc(last_kill))}"
            ),
            target_text="0",
            progress=1.0 if met else 0.0,
            met=met,
            hint=(
                None
                if met or last_kill is None
                else f"free {_ams_date(repo.aware_utc(last_kill) + timedelta(days=KILL_FREE_DAYS))}"
            ),
        )
    )

    # 9. No unresolved errors -------------------------------------------------------
    errors = repo.unresolved_errors_affecting(session, now - timedelta(days=ERROR_FREE_DAYS))
    met = not errors
    rows.append(
        ReadinessRowOut(
            id="no_errors",
            name="No unresolved errors",
            why=f"Errors that affected a decision or fill, last {ERROR_FREE_DAYS} days",
            value_text=f"{len(errors)} open" if errors else "0 open",
            target_text="0",
            progress=1.0 if met else 0.0,
            met=met,
            hint=None if met else "resolve on Dashboard",
        )
    )

    # --- summary -------------------------------------------------------------------
    met_count = sum(1 for r in rows if r.met)
    unmet = [r for r in rows if not r.met]
    if not unmet:
        status = f"All {len(rows)} criteria met"
        summary = "Every readiness criterion passes on the current paper account."
        estimate_text = None
    else:
        status = "Not ready for real money yet"
        bits = []
        beats = next((r for r in rows if r.id == "beats_btc"), None)
        if beats is not None and beats.met:
            bits.append("the agent beats holding BTC")
        names = ", ".join(r.name.lower() for r in unmet[:3])
        summary = (
            ", but ".join(bits) + ", but " if bits else "Still missing: "
        ) + f"{len(unmet)} of {len(rows)} criteria are unmet ({names})."
        if estimates:
            slowest = max(estimates, key=lambda e: e[1])
            estimate_text = (
                f"At the current pace, all criteria could be met around "
                f"{_month(slowest[1])}. The slowest one is {slowest[0]}."
            )
        else:
            estimate_text = None
    return ReadinessOut(
        met=met_count,
        total=len(rows),
        status=status,
        summary=summary,
        estimate=estimate_text,
        account_id=account_id,
        account_started_at=started,
        checked_at=now,
        rows=rows,
    )
