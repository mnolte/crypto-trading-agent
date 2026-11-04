"""The cycles card payload: the rolling quarter-hour timeline, the
scheduled jobs, the ran/missed summary and the grouped decisions for
"Decisions · last 24 hours".

Everything is bucketed in Python from a handful of windowed queries —
never one query per slot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from sqlalchemy.orm import Session

from app import repo
from app.agent.engine import CYCLE_S, slot_floor
from app.db import AMSTERDAM, DecisionRow, PositionRow, StopMoveRow, TradeRow
from app.features.briefings import WEEKLY_SCHEDULE
from app.models import (
    CycleDecisions,
    CycleGroup,
    CycleSlot,
    CycleSlotChange,
    CyclesRecent,
    CyclesSummary,
    MissedRange,
    Plan,
)

if TYPE_CHECKING:
    from app.api.deps import AppState

FUTURE_HOURS = 3
ELIGIBILITY_HOUR_UTC = 1  # daily_market_loop's schedule
BACKUP_HOUR_UTC = 3  # operations.backup_loop's schedule
_GROUP_RANK = {"BUY": 0, "SELL": 1, "HOLD": 2, "SKIP": 3, "NO CALL": 4}

SlotState = Literal[
    "entry",
    "exit_gain",
    "exit_loss",
    "stop_moved",
    "error",
    "no_change",
    "missed",
    "paused",
    "next",
    "future",
]


def _group_label_reason(row: DecisionRow, holding: bool) -> tuple[str, str | None]:
    if not row.prefilter:
        return "NO CALL", "pre-filter"
    if row.action == "SKIP":
        return "SKIP", row.error
    if row.action == "HOLD":
        return "HOLD", "positions kept" if holding else None
    return row.action, None


def _jobs(
    settings_time: str,
    weekly: str,
    start: datetime,
    end: datetime,
) -> dict[datetime, list[str]]:
    """slot boundary → scheduled job names, for every job due in [start, end]."""
    out: dict[datetime, list[str]] = {}

    def mark(at: datetime, name: str) -> None:
        if start <= at <= end:
            out.setdefault(slot_floor(at), []).append(name)

    day = start.date() - timedelta(days=1)
    while day <= end.date() + timedelta(days=1):
        mark(
            datetime(day.year, day.month, day.day, ELIGIBILITY_HOUR_UTC, tzinfo=UTC),
            "eligibility",
        )
        mark(
            datetime(day.year, day.month, day.day, BACKUP_HOUR_UTC, tzinfo=UTC),
            "backup",
        )
        day += timedelta(days=1)

    hh, _, mm = settings_time.partition(":")
    sched = WEEKLY_SCHEDULE.get(weekly)
    day = start.astimezone(AMSTERDAM).date() - timedelta(days=1)
    end_day = end.astimezone(AMSTERDAM).date() + timedelta(days=1)
    while day <= end_day:
        local = datetime(
            day.year, day.month, day.day, int(hh or "8"), int(mm or "0"), tzinfo=AMSTERDAM
        )
        mark(local.astimezone(UTC), "briefing")
        if sched is not None and day.weekday() == sched[0]:
            weekly_at = datetime(
                day.year, day.month, day.day, sched[1], tzinfo=AMSTERDAM
            ).astimezone(UTC)
            mark(weekly_at, "weekly_review")
        day += timedelta(days=1)
    return out


def build_cycles_recent(
    state: AppState, session: Session, now: datetime, hours: int
) -> CyclesRecent:
    """Slots, summary and grouped decisions for the last ``hours`` hours."""
    settings = state.settings
    engine = state.agent_engine
    account_id = state.broker.account_id if state.broker is not None else state.account_id
    offset_s = settings.cycle_offset_s
    step = timedelta(seconds=CYCLE_S)
    window = now - timedelta(hours=hours)

    # The grid: past boundaries (now-hours, slot_floor(now)] plus the next
    # three hours. Exactly hours*4 past slots — UTC, so DST never shifts it.
    grid_start = slot_floor(window)
    last_boundary = slot_floor(now) + timedelta(hours=FUTURE_HOURS)
    boundaries: list[datetime] = []
    b = grid_start + step
    while b <= last_boundary:
        boundaries.append(b)
        b += step
    past = [b for b in boundaries if b <= now]
    # The next run: the earliest boundary whose cycle is not yet due.
    next_slot = slot_floor(now - timedelta(seconds=offset_s)) + step

    # ------------------------------------------------------- windowed fetch
    decisions = [
        row
        for row in repo.list_decision_rows(
            session, account_id=account_id, after=window, before=now, limit=None
        )
        if row.shadow_id is None
    ]
    trades = repo.trades_in_window(session, account_id, window, now)
    entries_open = repo.positions_opened_since(session, account_id, window)
    moves = repo.list_stop_moves(session, account_id, window, now)
    skipped = {
        repo.aware_utc(row.slot_at): row.reason
        for row in repo.list_skipped_slots(session, after=window, before=now)
    }
    snapshots = repo.list_equity_snapshots(session, account_id, window, now)
    error_activity = repo.list_activity(session, kind="error", since=window, limit=2000)

    # ------------------------------------------------------------ bucketing
    decisions_by_slot: dict[datetime, list[DecisionRow]] = {}
    for row in decisions:
        decisions_by_slot.setdefault(slot_floor(repo.aware_utc(row.at)), []).append(row)
    ran: dict[datetime, int] = {}
    for snap in snapshots:
        boundary = slot_floor(repo.aware_utc(snap.at))
        ran[boundary] = max(ran.get(boundary, 0), snap.cycle)
    exits_by_slot: dict[datetime, list[TradeRow]] = {}
    entries_by_slot: dict[datetime, list[TradeRow | PositionRow]] = {}
    for trade in trades:
        exits_by_slot.setdefault(slot_floor(repo.aware_utc(trade.closed_at)), []).append(trade)
        opened = slot_floor(repo.aware_utc(trade.opened_at))
        entries_by_slot.setdefault(opened, []).append(trade)
    for pos in entries_open:
        opened = slot_floor(repo.aware_utc(pos.opened_at))
        entries_by_slot.setdefault(opened, []).append(pos)
    moves_by_slot: dict[datetime, list[StopMoveRow]] = {}
    for move in moves:
        moves_by_slot.setdefault(slot_floor(repo.aware_utc(move.at)), []).append(move)

    jobs = _jobs(
        settings.learning.briefing_time,
        settings.learning.weekly_review,
        grid_start,
        last_boundary,
    )

    def error_changes(boundary: datetime) -> list[CycleSlotChange]:
        out: list[CycleSlotChange] = []
        seen: set[tuple[str | None, str]] = set()
        for row in decisions_by_slot.get(boundary, []):
            detail: str | None = None
            if row.error:
                detail = row.error
            elif row.decider == "rules" and row.reasoning.startswith("OpenAI call failed"):
                detail = row.reasoning.split(".", 1)[0]
            if detail and (row.pair, detail) not in seen:
                seen.add((row.pair, detail))
                out.append(
                    CycleSlotChange(kind="error", pair=row.pair, detail=detail, decision_id=row.id)
                )
        for act in error_activity:
            if slot_floor(repo.aware_utc(act.at)) != boundary:
                continue
            if (act.pair, act.text) in seen:
                continue
            seen.add((act.pair, act.text))
            out.append(
                CycleSlotChange(
                    kind="error",
                    pair=act.pair,
                    detail=act.text,
                    decision_id=act.decision_id,
                )
            )
        return out

    def slot_changes(boundary: datetime) -> list[CycleSlotChange]:
        out = error_changes(boundary)
        for item in entries_by_slot.get(boundary, []):
            out.append(
                CycleSlotChange(
                    kind="entry",
                    pair=item.pair,
                    detail=f"{item.pair} {item.qty} @ {item.entry}",
                    decision_id=item.decision_id or None,
                )
            )
        for trade in exits_by_slot.get(boundary, []):
            out.append(
                CycleSlotChange(
                    kind="exit",
                    pair=trade.pair,
                    detail=(f"{trade.pair} {trade.exit_reason} {Decimal(trade.realised_r):+.1f}R"),
                    decision_id=trade.decision_id or None,
                    exit_gain=Decimal(trade.realised_eur) > 0,
                )
            )
        for move in moves_by_slot.get(boundary, []):
            out.append(
                CycleSlotChange(
                    kind="stop_move",
                    pair=move.pair,
                    detail=f"{move.pair} {move.old_stop} → {move.new_stop} · {move.reason}",
                )
            )
        return out

    # --------------------------------------------------------------- slots
    slots: list[CycleSlot] = []
    for boundary in boundaries:
        changes = slot_changes(boundary) if boundary <= now else []
        kinds = {c.kind for c in changes}
        ran_cycle = ran.get(boundary)
        state_name: SlotState
        if "error" in kinds:
            state_name = "error"
        elif "exit" in kinds:
            gains = [Decimal(t.realised_eur) for t in exits_by_slot[boundary]]
            state_name = "exit_loss" if sum(gains) < 0 else "exit_gain"
        elif "entry" in kinds:
            state_name = "entry"
        elif "stop_move" in kinds:
            state_name = "stop_moved"
        elif ran_cycle is not None:
            state_name = "no_change"
        elif boundary == next_slot:
            state_name = "next"
        elif boundary > now:
            state_name = "future"
        elif boundary in skipped:
            state_name = "paused" if skipped[boundary] == "paused" else "missed"
        else:
            state_name = "missed" if boundary < next_slot else "future"
        reason = skipped.get(boundary) if state_name in ("missed", "paused") else None
        slots.append(
            CycleSlot(
                at=boundary,
                state=state_name,
                missed_reason=reason,
                cycle=ran_cycle,
                changes=changes,
                jobs=sorted(jobs.get(boundary, [])),
            )
        )

    # ------------------------------------------------------------- summary
    missed_ranges: list[MissedRange] = []
    group: list[datetime] = []
    for b in past:
        if b in ran:
            if group:
                missed_ranges.append(
                    MissedRange(
                        from_at=group[0],
                        to_at=group[-1],
                        reason=skipped.get(group[-1]),
                        count=len(group),
                    )
                )
                group = []
            continue
        group.append(b)
    if group:
        missed_ranges.append(
            MissedRange(
                from_at=group[0],
                to_at=group[-1],
                reason=skipped.get(group[-1]),
                count=len(group),
            )
        )
    summary = CyclesSummary(
        ran=len([b for b in past if b in ran]),
        scheduled=len(past),
        missed=missed_ranges,
        errors=repo.error_count_since(session, window),
        cycle_id=engine.cycle if engine is not None else state.cycle,
        running_since=engine.started_at if engine is not None else None,
    )

    # ------------------------------------------------------------ decisions
    # Pairs held at decision time: open now or a trade straddling the row.
    held_now = set(state.broker.positions) if state.broker is not None else set()

    def held_at(row: DecisionRow) -> bool:
        if row.pair in held_now:
            return True
        at = repo.aware_utc(row.at)
        return any(
            t.pair == row.pair and repo.aware_utc(t.opened_at) <= at < repo.aware_utc(t.closed_at)
            for t in trades
        )

    decision_items: list[CycleDecisions] = []
    for boundary in reversed(past):
        rows = sorted(decisions_by_slot.get(boundary, []), key=lambda r: (r.at, r.id), reverse=True)
        changes = [c for c in slot_changes(boundary) if c.kind != "error"]
        ran_cycle = ran.get(boundary) or (max(r.cycle for r in rows) if rows else None)
        if not rows and not changes and ran_cycle is None:
            continue
        groups: dict[tuple[str, str | None], list[DecisionRow]] = {}
        for row in rows:
            key = _group_label_reason(row, held_at(row))
            groups.setdefault(key, []).append(row)
        out_groups: list[CycleGroup] = []
        for (label, reason), group_rows in sorted(
            groups.items(), key=lambda kv: _GROUP_RANK.get(kv[0][0], 9)
        ):
            detail = None
            change = label in ("BUY", "SELL") and any(r.status == "executed" for r in group_rows)
            if len(group_rows) == 1:
                row = group_rows[0]
                plan = Plan.model_validate_json(row.plan) if row.plan else None
                if label == "BUY" and plan is not None:
                    detail = f"{plan.qty} @ {plan.fill_entry} · R/R {plan.rr}"
                elif label == "SELL":
                    matching = next((t for t in trades if t.decision_id == row.id), None)
                    detail = (
                        f"{matching.exit_reason} · {Decimal(matching.realised_r):+.1f}R"
                        if matching is not None
                        else row.reasoning[:80]
                    )
            out_groups.append(
                CycleGroup(
                    label=label,
                    reason=reason,
                    pairs=sorted({r.pair for r in group_rows}),
                    count=len(group_rows),
                    decision_id=group_rows[0].id,
                    detail=detail,
                    change=change,
                )
            )
        decision_items.append(
            CycleDecisions(
                slot_at=boundary,
                cycle=ran_cycle,
                groups=out_groups,
                changes=changes,
                decision_count=len(rows),
                missed=boundary not in ran,
            )
        )

    return CyclesRecent(slots=slots, summary=summary, decisions=decision_items)


__all__ = ["build_cycles_recent"]
