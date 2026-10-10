"""Paper broker: slippage and fees on both sides, equity = cash + positions
at last price, open risk, peak and day-start equity (00:00 Amsterdam)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent.broker.paper import HeldPosition, PaperBroker
from app.config import get_env
from app.db import AccountRow, make_engine, make_session_factory, run_migrations
from app.models import PaperSettings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _account(
    cash: str = "10000", peak: str = "10000", day_start: str = "10000", day: str = "2026-10-11"
) -> AccountRow:
    return AccountRow(
        id=1,
        started_at=NOW,
        start_balance="10000",
        cash=cash,
        peak_equity=peak,
        day_start_equity=day_start,
        day_start_date=day,
        active=True,
    )


@pytest.fixture
def session(env_paths: Path) -> Iterator[Session]:
    run_migrations(get_env().ta_db_path)
    engine = make_engine(get_env().ta_db_path)
    s = make_session_factory(engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_buy_applies_slippage_and_fee(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())  # 0.25% fee, 0.05% slip
    pos = broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("10"), Decimal("95"), Decimal("110"), "d1", NOW
    )
    # Fill is 0.05% against us; fee 0.25% on top.
    assert pos.entry == Decimal("100.05")
    fee = Decimal("10") * pos.entry * Decimal("0.0025")
    assert broker.cash == Decimal("10000") - Decimal("10") * pos.entry - fee
    assert pos.r_unit == pos.entry - Decimal("95")


def test_sell_applies_slippage_and_fee_both_sides(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())
    broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("10"), Decimal("95"), Decimal("110"), "d1", NOW
    )
    trade = broker.close(session, "BTC-EUR", Decimal("110"), NOW, "target")
    assert trade is not None
    exit_price = Decimal(trade.exit)
    assert exit_price == Decimal("110") * Decimal("0.9995")
    buy_fee = Decimal("10") * Decimal("100.05") * Decimal("0.0025")
    sell_fee = Decimal("10") * exit_price * Decimal("0.0025")
    expected = Decimal("10") * (exit_price - Decimal("100.05")) - buy_fee - sell_fee
    assert Decimal(trade.realised_eur) == expected
    assert Decimal(trade.fee_eur) == buy_fee + sell_fee


def test_equity_is_cash_plus_positions_at_last(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())
    broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("10"), Decimal("95"), Decimal("110"), "d1", NOW
    )
    broker.mark("BTC-EUR", Decimal("120"), NOW)
    # equity = cash + qty * last
    assert broker.equity() == broker.cash + Decimal("10") * Decimal("120")
    assert broker.exposure() == Decimal("1200")


def test_open_risk_counts_only_stop_below_entry(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())
    broker.positions["A-EUR"] = HeldPosition(
        pair="A-EUR",
        qty=Decimal("1"),
        entry=Decimal("100"),
        stop=Decimal("90"),
        target=Decimal("120"),
        opened_at=NOW,
        decision_id="d",
        last=Decimal("100"),
    )
    broker.positions["B-EUR"] = HeldPosition(
        pair="B-EUR",
        qty=Decimal("2"),
        entry=Decimal("50"),
        stop=Decimal("60"),
        target=Decimal("80"),
        opened_at=NOW,
        decision_id="d",
        last=Decimal("50"),
    )
    # B's stop sits above entry: zero risk.
    assert broker.open_risk() == Decimal("1") * Decimal("10")


def test_peak_and_drawdown(session: Session) -> None:
    broker = PaperBroker(_account(peak="11000"), PaperSettings())
    broker.mark_all({"X-EUR": Decimal("1")}, NOW)
    # equity 10000 vs peak 11000 → drawdown ~9.09%.
    assert float(broker.drawdown_pct()) == pytest.approx(9.0909, abs=0.001)


def test_day_start_rolls_at_amsterdam_midnight(session: Session) -> None:
    broker = PaperBroker(_account(day_start="10000", day="2026-10-10"), PaperSettings())
    # 2026-10-11 00:30 Amsterdam = 2026-10-10 23:30 UTC (CEST, UTC+2).
    after = datetime(2026, 10, 10, 23, 30, tzinfo=UTC)
    pnl = broker.today_pnl(after)
    assert pnl == Decimal("0")
    assert broker.day_start_equity == broker.equity()
    # Same Amsterdam day: no reset.
    assert broker.today_pnl(after - timedelta(hours=2)) == Decimal("0")


def test_positions_roundtrip_through_db(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())
    broker.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("5"),
        Decimal("90"),
        Decimal("120"),
        "d1",
        NOW,
        invalidation_price=Decimal("95"),
    )
    session.commit()
    other = PaperBroker(_account(), PaperSettings())
    other.load_positions(session)
    pos = other.positions["BTC-EUR"]
    assert pos.qty == Decimal("5") and pos.entry == Decimal("100.05")
    assert pos.stop == Decimal("90") and pos.invalidation_price == Decimal("95")
    assert pos.r_unit == pos.entry - Decimal("90")


def test_close_removes_position_and_books_trade(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())
    broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("5"), Decimal("90"), Decimal("120"), "d1", NOW
    )
    session.commit()
    trade = broker.close(session, "BTC-EUR", Decimal("80"), NOW, "stop")
    session.commit()
    assert "BTC-EUR" not in broker.positions
    assert trade is not None
    assert Decimal(trade.realised_r) < 0
    from app.repo import last_exit_for_pair

    row = last_exit_for_pair(session, 1, "BTC-EUR", "stop")
    assert row is not None
