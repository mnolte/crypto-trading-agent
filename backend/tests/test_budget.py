"""Daily call budget, monthly EUR cap, model pricing and USD→EUR cost.

Boundaries come from the spec: warn at 80% of the monthly cap, cap at 100%;
the daily budget counts calls that actually hit the wire; unknown model
prices report an unknown cost — never a guess.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent import budget
from app.agent.market.base import Candle
from app.db import LlmCallRow, make_engine, make_session_factory

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # a Sunday, Amsterdam CEST


@pytest.fixture
def sessions(tmp_path: Path, migrated_db: Path) -> sessionmaker[Session]:
    db = tmp_path / "agent.db"
    shutil.copyfile(migrated_db, db)
    return make_session_factory(make_engine(db))


def _call(
    session: Session,
    *,
    at: datetime,
    feature: str = "decision",
    status: str = "ok",
    cost_eur: str | None = None,
    pair: str = "SOL-EUR",
) -> LlmCallRow:
    row = LlmCallRow(
        id=f"call-{at.timestamp()}-{feature}-{status}",
        at=at,
        cycle=None,
        account_id=1,
        pair=pair,
        decision_id=None,
        feature=feature,
        replay_of=None,
        model="gpt-4o-mini",
        prompt_version="decision.v1",
        status=status,
        attempts="[]",
        latency_ms=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        cost_eur=cost_eur,
        request_id=None,
        request_json=None,
        response_raw=None,
        parsed=None,
        input_checks="[]",
        output_checks="[]",
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )
    repo.add_llm_call(session, row)
    session.commit()
    return row


# ----------------------------------------------------------------- pricing --


def test_known_model_priced() -> None:
    prices = budget.ModelPrices()
    price = prices.price("gpt-4o-mini")
    assert price is not None
    assert price[0] > 0 and price[1] > 0


def test_unknown_model_reports_no_price() -> None:
    assert budget.ModelPrices().price("some-future-model") is None


def test_cost_estimates_known_model() -> None:
    prices = budget.ModelPrices()
    pin, pout = prices.price("gpt-4o-mini") or (Decimal("0"), Decimal("0"))
    usd, eur = budget.cost_estimates(prices, "gpt-4o-mini", 1000, 500, Decimal("0.9"))
    expected_usd = (Decimal(1000) * pin + Decimal(500) * pout) / Decimal("1000000")
    assert usd == expected_usd.quantize(Decimal("0.0001"))
    assert eur == (expected_usd * Decimal("0.9")).quantize(Decimal("0.0001"))


def test_cost_estimates_unknown_model_is_none() -> None:
    usd, eur = budget.cost_estimates(budget.ModelPrices(), "mystery", 1000, 500, Decimal("0.9"))
    assert usd is None and eur is None


def test_cost_estimates_without_rate_leaves_eur_none() -> None:
    usd, eur = budget.cost_estimates(budget.ModelPrices(), "gpt-4o-mini", 1000, 500, None)
    assert usd is not None and eur is None


def test_cost_estimates_without_tokens_is_none() -> None:
    usd, eur = budget.cost_estimates(budget.ModelPrices(), "gpt-4o-mini", None, 500, Decimal("0.9"))
    assert usd is None and eur is None


# ------------------------------------------------------------------- clock --


def test_amsterdam_day_start_is_local_midnight() -> None:
    # 2026-10-11 is CEST (UTC+2): local midnight = 22:00 UTC the day before.
    start = budget.amsterdam_day_start(NOW)
    assert start == datetime(2026, 10, 10, 22, 0, tzinfo=UTC)


def test_amsterdam_month_bounds_rollover() -> None:
    start, end = budget.amsterdam_month_bounds(datetime(2026, 12, 15, tzinfo=UTC))
    assert start == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)  # CET = UTC+1
    assert end == datetime(2026, 12, 31, 23, 0, tzinfo=UTC)


def test_cap_state_boundaries() -> None:
    cap = Decimal("10")
    assert budget.cap_state(Decimal("7.99"), cap) == "ok"
    assert budget.cap_state(Decimal("8.00"), cap) == "warn"
    assert budget.cap_state(Decimal("9.99"), cap) == "warn"
    assert budget.cap_state(Decimal("10.00"), cap) == "cap"
    assert budget.cap_state(Decimal("11"), cap) == "cap"


def test_zero_or_negative_cap_means_capped() -> None:
    assert budget.cap_state(Decimal("0"), Decimal("0")) == "cap"
    assert budget.cap_state(Decimal("0"), Decimal("-1")) == "cap"


# ------------------------------------------------------------------ budget --


def test_calls_today_counts_only_sent_calls_today(
    sessions: sessionmaker[Session],
) -> None:
    session = sessions()
    try:
        day_start = budget.amsterdam_day_start(NOW)
        _call(session, at=day_start + timedelta(hours=1))
        _call(session, at=day_start + timedelta(hours=2), feature="replay")
        _call(session, at=day_start + timedelta(hours=3), status="input_blocked")
        _call(session, at=day_start - timedelta(minutes=1))  # yesterday
        _call(session, at=day_start + timedelta(hours=4), feature="ask")
        assert budget.calls_today(session, 1, NOW) == 2
    finally:
        session.close()


def test_month_spend_sums_only_the_current_month(
    sessions: sessionmaker[Session],
) -> None:
    session = sessions()
    try:
        start, _ = budget.amsterdam_month_bounds(NOW)
        _call(session, at=start + timedelta(hours=1), cost_eur="0.0100")
        _call(session, at=start + timedelta(hours=2), cost_eur="0.0200")
        _call(session, at=start + timedelta(hours=3), cost_eur=None)  # unknown
        _call(session, at=start - timedelta(days=1), cost_eur="9.99")
        assert budget.month_spend_eur(session, 1, NOW) == Decimal("0.0300")
    finally:
        session.close()


# ------------------------------------------------------------------ FX rate --


def test_usdc_eur_rate_from_latest_daily_close(
    sessions: sessionmaker[Session],
) -> None:
    session = sessions()
    try:
        candles = [
            Candle(
                open_time=NOW - timedelta(days=i),
                open="0.90",
                high="0.91",
                low="0.89",
                close=Decimal("0.90") + Decimal(i) / 100,
                volume="1",
            )
            for i in range(3, 0, -1)
        ]
        repo.upsert_candles(session, "USDC-EUR", "1d", candles)
        session.commit()
        assert budget.usdc_eur_rate(session, NOW) == Decimal("0.91")
    finally:
        session.close()


def test_usdc_eur_rate_none_without_candles(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        assert budget.usdc_eur_rate(session, NOW) is None
    finally:
        session.close()
