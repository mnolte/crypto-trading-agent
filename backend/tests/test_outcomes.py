from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.broker.paper import HeldPosition, PaperBroker
from app.agent.market.base import Candle
from app.agent.outcomes import MAX_OUTCOME_ATTEMPTS, _label, compute_outcome, run_outcomes
from app.db import (
    ActivityRow,
    DecisionRow,
    ErrorRow,
    OutcomeFailureRow,
    TradeRow,
    make_engine,
    make_session_factory,
    run_migrations,
)
from app.models import PaperSettings

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _decision() -> DecisionRow:
    return DecisionRow(
        id="sell-1",
        cycle=1,
        at=NOW,
        account_id=1,
        pair="BTC-EUR",
        decider="rules",
        model=None,
        prompt_version=None,
        action="SELL",
        confidence=0.8,
        reasoning="exit",
        invalidation=None,
        signals="[]",
        plan=None,
        gates="[]",
        status="executed",
        execute_at=NOW,
        error=None,
        regime='{"trend":"up","volatility":"normal","age_4h":2}',
        btc_regime='{"trend":"up","volatility":"normal","age_4h":2}',
        invalidation_price=None,
        risk_pct_used="1",
        settings_version=1,
        shadow_id=None,
        stand_in=False,
        raw_brief=None,
        raw_response=None,
        prefilter=True,
    )


def _session(path: Path) -> Session:
    run_migrations(path)
    return make_session_factory(make_engine(path))()


@pytest.mark.parametrize("moved_stop", [Decimal("100"), Decimal("108")])
def test_sell_outcome_keeps_original_r_when_stop_moves(tmp_path: Path, moved_stop: Decimal) -> None:
    session = _session(tmp_path / "agent.db")
    account = repo.current_account(session, Decimal("10000"), "2026-10-11")
    broker = PaperBroker(
        account, PaperSettings(taker_fee_pct=Decimal("0"), slippage_pct=Decimal("0"))
    )
    broker.positions["BTC-EUR"] = HeldPosition(
        pair="BTC-EUR",
        qty=Decimal("1"),
        entry=Decimal("100"),
        stop=moved_stop,
        initial_stop=Decimal("90"),
        target=Decimal("120"),
        opened_at=NOW - timedelta(hours=2),
        decision_id="buy-1",
        last=Decimal("110"),
        r_unit=Decimal("10"),
    )
    trade = broker.close(session, "BTC-EUR", Decimal("110"), NOW, "model_sell")
    assert trade is not None
    assert trade.initial_stop == "90"
    assert trade.r_unit == "10"


def test_sell_outcome_uses_trade_original_r_after_breakeven(tmp_path: Path) -> None:
    session = _session(tmp_path / "agent.db")
    decision = _decision()
    session.add(decision)
    session.add(
        TradeRow(
            account_id=1,
            pair="BTC-EUR",
            qty="1",
            entry="100",
            exit="110",
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW,
            fee_eur="0",
            realised_eur="10",
            realised_r="1",
            initial_stop="90",
            r_unit="10",
            exit_reason="model_sell",
            decision_id="buy-1",
        )
    )
    candles = [
        Candle(
            open_time=NOW + timedelta(hours=i),
            open=Decimal("110"),
            high=Decimal("121"),
            low=Decimal("109"),
            close=Decimal("121" if i == 23 else "110"),
            volume=Decimal("1"),
        )
        for i in range(24)
    ]
    repo.upsert_candles(session, "BTC-EUR", "1h", candles)
    outcome = compute_outcome(session, decision, 24, NOW + timedelta(hours=24))
    assert outcome.label == "early exit"


def test_sell_outcome_horizon_is_anchored_to_exit_fill(tmp_path: Path) -> None:
    session = _session(tmp_path / "agent.db")
    decision = _decision()
    decision.at = NOW + timedelta(minutes=59)
    session.add(decision)
    session.add(
        TradeRow(
            account_id=1,
            pair="BTC-EUR",
            qty="1",
            entry="100",
            exit="110",
            opened_at=NOW - timedelta(hours=2),
            closed_at=NOW + timedelta(hours=1, minutes=1),
            fee_eur="0",
            realised_eur="10",
            realised_r="1",
            initial_stop="90",
            r_unit="10",
            exit_reason="model_sell",
            decision_id="buy-1",
        )
    )
    repo.upsert_candles(
        session,
        "BTC-EUR",
        "1h",
        [
            Candle(
                open_time=NOW + timedelta(hours=i),
                open=Decimal("110"),
                high=Decimal("110"),
                low=Decimal("110"),
                close=Decimal(str(110 + i)),
                volume=Decimal("1"),
            )
            for i in range(25)
        ],
    )
    outcome = compute_outcome(
        session,
        decision,
        24,
        NOW + timedelta(hours=25, minutes=1),
    )
    assert outcome.reference_price == "134"
    assert outcome.reference_at == NOW + timedelta(hours=25, minutes=1)


@pytest.mark.parametrize("reference", [Decimal("120"), Decimal("100")])
def test_sell_exact_one_r_boundaries_are_neutral(reference: Decimal) -> None:
    assert (
        _label("SELL", "executed", "neither", reference, Decimal("110"), Decimal("10"), 24)
        == "neutral"
    )


def test_permanently_unavailable_outcome_stops_retrying(tmp_path: Path) -> None:
    session = _session(tmp_path / "agent.db")
    decision = _decision()
    decision.action = "HOLD"
    session.add(decision)
    session.commit()
    now = NOW + timedelta(hours=100)

    for _ in range(MAX_OUTCOME_ATTEMPTS + 2):
        run_outcomes(session, now)

    failures = session.scalars(select(OutcomeFailureRow)).all()
    assert len(failures) == 3
    assert all(failure.attempts == MAX_OUTCOME_ATTEMPTS for failure in failures)
    assert all(failure.terminal for failure in failures)


def test_terminal_outcome_records_one_error_per_horizon(tmp_path: Path) -> None:
    """When a horizon turns terminal, the user sees it: one aggregated error,
    flagged as affecting a decision, carrying the decision id and reason."""
    session = _session(tmp_path / "agent.db")
    decision = _decision()
    decision.action = "HOLD"
    session.add(decision)
    session.commit()
    now = NOW + timedelta(hours=100)

    for _ in range(MAX_OUTCOME_ATTEMPTS + 1):
        run_outcomes(session, now)

    errors = session.scalars(select(ErrorRow)).all()
    assert len(errors) == 3
    assert all(error.affected_decision_or_fill for error in errors)
    assert all(error.resolved_at is None for error in errors)
    activities = session.scalars(select(ActivityRow).where(ActivityRow.kind == "error")).all()
    assert len(activities) == 3
    assert all(activity.decision_id == decision.id for activity in activities)
    assert {activity.text.split(":")[0] for activity in activities} == {
        f"outcome unavailable at {horizon}h" for horizon in (4, 24, 72)
    }


def test_same_candle_touching_stop_and_target_counts_stop_first(tmp_path: Path) -> None:
    session = _session(tmp_path / "agent.db")
    decision = _decision()
    decision.id = "buy-pending"
    decision.action = "BUY"
    decision.status = "expired"
    decision.plan = (
        '{"entry":"100","fill_entry":"100","stop":"90","target":"120",'
        '"qty":"1","notional":"100","rr":"2"}'
    )
    session.add(decision)
    repo.upsert_candles(
        session,
        "BTC-EUR",
        "1h",
        [
            Candle(
                open_time=NOW,
                open=Decimal("100"),
                high=Decimal("121"),
                low=Decimal("89"),
                close=Decimal("105"),
                volume=Decimal("1"),
            )
        ],
    )
    outcome = compute_outcome(session, decision, 4, NOW + timedelta(hours=4))
    assert outcome.first_touch == "stop"
    assert outcome.label == "loss"
