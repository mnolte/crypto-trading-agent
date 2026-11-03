"""Hourly, idempotent forward-outcome tracking from stored closed 1h candles."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.signals import compute_tf_values
from app.db import (
    DecisionRow,
    LlmCallRow,
    OutcomeFailureRow,
    OutcomeRow,
    PositionRow,
    TradeRow,
    utcnow,
)
from app.models import Plan

HORIZONS = (4, 24, 72)
METHOD_VERSION = "outcome.v1"
HOUR = timedelta(hours=1)
MAX_OUTCOME_ATTEMPTS = 24


def _trade_for_sell(session: Session, decision: DecisionRow) -> TradeRow | None:
    return session.scalar(
        select(TradeRow)
        .where(
            TradeRow.account_id == decision.account_id,
            TradeRow.pair == decision.pair,
            TradeRow.exit_reason == "model_sell",
            TradeRow.closed_at >= decision.at - timedelta(seconds=1),
            TradeRow.closed_at <= decision.at + timedelta(minutes=5),
        )
        .order_by(TradeRow.closed_at.asc(), TradeRow.id.asc())
        .limit(1)
    )


def _levels(
    session: Session, decision: DecisionRow
) -> tuple[
    Decimal,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    datetime,
]:
    plan = Plan.model_validate_json(decision.plan) if decision.plan else None
    trade = session.scalar(
        select(TradeRow).where(
            TradeRow.account_id == decision.account_id,
            TradeRow.decision_id == decision.id,
        )
    )
    position = session.scalar(
        select(PositionRow).where(
            PositionRow.account_id == decision.account_id,
            PositionRow.decision_id == decision.id,
        )
    )
    if decision.action == "BUY" and trade is not None:
        entry = Decimal(trade.entry)
        stop = Decimal(trade.initial_stop)
        target = plan.target if plan is not None else entry + Decimal(trade.r_unit) * 2
        return (
            entry,
            stop,
            target,
            Decimal(trade.r_unit),
            Decimal(trade.realised_r),
            repo.aware_utc(decision.at),
        )
    if decision.action == "BUY" and position is not None:
        entry = Decimal(position.entry)
        stop = Decimal(position.initial_stop)
        target = plan.target if plan is not None else Decimal(position.target)
        return entry, stop, target, entry - stop, None, repo.aware_utc(decision.at)
    if decision.action == "SELL":
        closed = _trade_for_sell(session, decision)
        if closed is not None:
            return (
                Decimal(closed.exit),
                None,
                None,
                Decimal(closed.r_unit),
                None,
                repo.aware_utc(closed.closed_at),
            )
    if plan is not None:
        return (
            plan.entry,
            plan.stop,
            plan.target,
            plan.entry - plan.stop,
            None,
            repo.aware_utc(decision.at),
        )
    # A gate-skipped or check-rejected BUY carries no plan: the levels the
    # model actually proposed live on its stored call — not a synthetic
    # 1.5xATR hypothetical — so "missed vs avoided" is measured correctly.
    proposed = _model_levels(session, decision)
    if proposed is not None:
        entry, stop, target = proposed
        return entry, stop, target, entry - stop, None, repo.aware_utc(decision.at)
    history = repo.load_candles(session, decision.pair, "1h", 60, end=repo.aware_utc(decision.at))
    if not history:
        raise ValueError("no reference candle")
    entry = history[-1].close
    values = compute_tf_values(history, "1h")
    if values.atr14 is None:
        raise ValueError("no ATR for hypothetical outcome")
    risk = Decimal(str(values.atr14)) * Decimal("1.5")
    return (
        entry,
        entry - risk,
        entry + risk * 2,
        risk,
        None,
        repo.aware_utc(decision.at),
    )


def _model_levels(
    session: Session, decision: DecisionRow
) -> tuple[Decimal, Decimal, Decimal] | None:
    """The model's proposed (entry, stop, target) for a BUY that never got
    a plan, read from the parsed output stored on its call."""
    row = session.scalar(
        select(LlmCallRow)
        .where(LlmCallRow.decision_id == decision.id)
        .order_by(LlmCallRow.at.desc())
        .limit(1)
    )
    if row is None or not row.parsed:
        return None
    try:
        parsed = json.loads(row.parsed)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("action") != "BUY":
        return None
    try:
        entry = Decimal(str(parsed["entry"])) if parsed.get("entry") is not None else None
        stop = Decimal(str(parsed["stop"])) if parsed.get("stop") is not None else None
        target = Decimal(str(parsed["target"])) if parsed.get("target") is not None else None
    except (InvalidOperation, ValueError):
        return None
    if entry is None or stop is None or target is None or not (stop < entry < target):
        return None
    return entry, stop, target


def _label(
    action: str,
    status: str,
    first_touch: str,
    reference: Decimal,
    entry: Decimal,
    risk: Decimal | None,
    horizon: int,
) -> str:
    # An unfilled limit entry is tracked like a BUY that was not taken.
    if status == "unfilled" or action in ("HOLD", "SKIP"):
        return (
            "missed" if first_touch == "target" else "avoided" if first_touch == "stop" else "flat"
        )
    if action == "BUY":
        return "win" if first_touch == "target" else "loss" if first_touch == "stop" else "open"
    if action == "SELL" and horizon >= 24 and risk is not None:
        if reference > entry + risk:
            return "early exit"
        if reference < entry:
            return "good exit"
    return "neutral"


def compute_outcome(
    session: Session, decision: DecisionRow, horizon: int, computed_at: datetime
) -> OutcomeRow:
    entry, stop, target, risk, realised, anchor = _levels(session, decision)
    end = anchor + timedelta(hours=horizon)
    if end > computed_at:
        raise ValueError("outcome horizon has not elapsed")
    candles = repo.load_candles(session, decision.pair, "1h", horizon + 4, end=end - HOUR)
    candles = [candle for candle in candles if anchor < candle.open_time + HOUR <= end]
    if not candles:
        raise ValueError("no closed candles in outcome horizon")
    reference = candles[-1].close
    best = max(c.high for c in candles)
    worst = min(c.low for c in candles)
    first_touch = "neither"
    if stop is not None and target is not None:
        for candle in candles:
            if candle.low <= stop:
                first_touch = "stop"
                break
            if candle.high >= target:
                first_touch = "target"
                break
    invalidation = Decimal(decision.invalidation_price) if decision.invalidation_price else None
    crossed_at: int | None = None
    if invalidation is not None:
        for index, candle in enumerate(candles):
            if candle.low <= invalidation <= candle.high:
                crossed_at = index
                break
    followup = None
    if crossed_at is not None and crossed_at + 24 < len(candles):
        crossed = candles[crossed_at].close
        followup = (candles[crossed_at + 24].close - crossed) / crossed * 100
    forward = (reference - entry) / entry * 100
    best_pct = (best - entry) / entry * 100
    worst_pct = (worst - entry) / entry * 100
    return OutcomeRow(
        decision_id=decision.id,
        horizon=horizon,
        account_id=decision.account_id,
        pair=decision.pair,
        action=decision.action,
        reference_at=end,
        reference_price=str(reference),
        forward_return_pct=str(forward),
        best_move_pct=str(best_pct),
        worst_move_pct=str(worst_pct),
        best_move_r=str((best - entry) / risk) if risk and risk > 0 else None,
        worst_move_r=str((worst - entry) / risk) if risk and risk > 0 else None,
        first_touch=first_touch,
        invalidated=crossed_at is not None,
        invalidation_followup_pct=str(followup) if followup is not None else None,
        label=_label(
            decision.action, decision.status, first_touch, reference, entry, risk, horizon
        ),
        realised_r=str(realised) if realised is not None else None,
        computed_at=computed_at,
        method_version=METHOD_VERSION,
    )


def run_outcomes(session: Session, now: datetime) -> int:
    count = 0
    for horizon in HORIZONS:
        existing = select(OutcomeRow.decision_id).where(
            OutcomeRow.decision_id == DecisionRow.id,
            OutcomeRow.horizon == horizon,
        )
        terminal = select(OutcomeFailureRow.decision_id).where(
            OutcomeFailureRow.decision_id == DecisionRow.id,
            OutcomeFailureRow.horizon == horizon,
            OutcomeFailureRow.terminal.is_(True),
        )
        decisions = session.scalars(
            select(DecisionRow).where(
                DecisionRow.at <= (now - timedelta(hours=horizon)).replace(tzinfo=None),
                ~existing.exists(),
                ~terminal.exists(),
            )
        ).all()
        for decision in decisions:
            try:
                session.add(compute_outcome(session, decision, horizon, now))
            except (ValueError, ArithmeticError) as exc:
                failure = session.get(OutcomeFailureRow, (decision.id, horizon))
                if failure is None:
                    failure = OutcomeFailureRow(
                        decision_id=decision.id,
                        horizon=horizon,
                        attempts=0,
                        last_error=str(exc),
                        last_at=now,
                        terminal=False,
                    )
                    session.add(failure)
                failure.attempts += 1
                failure.last_error = str(exc)
                failure.last_at = now
                became_terminal = failure.attempts == MAX_OUTCOME_ATTEMPTS
                failure.terminal = failure.attempts >= MAX_OUTCOME_ATTEMPTS
                if became_terminal:
                    repo.add_activity(
                        session,
                        "error",
                        f"outcome unavailable at {horizon}h: {exc}",
                        pair=decision.pair,
                        decision_id=decision.id,
                        at=now,
                    )
                continue
            failure = session.get(OutcomeFailureRow, (decision.id, horizon))
            if failure is not None:
                session.delete(failure)
            count += 1
    session.commit()
    return count


async def outcome_loop(
    sessions: sessionmaker[Session], stop: asyncio.Event, clock: Callable[[], datetime] = utcnow
) -> None:
    while not stop.is_set():
        session = sessions()
        try:
            run_outcomes(session, clock())
        finally:
            session.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except TimeoutError:
            pass
