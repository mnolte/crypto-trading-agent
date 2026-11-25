"""Paper broker: slippage and fees on both sides, equity = cash + positions
at last price, open risk, peak and day-start equity (00:00 Amsterdam)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.broker.paper import HeldPosition, PaperBroker
from app.config import get_env
from app.db import (
    AccountRow,
    PositionRow,
    TradeRow,
    make_engine,
    make_session_factory,
)
from app.models import PaperSettings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _account(
    cash: str = "10000",
    peak: str = "10000",
    day_start: str = "10000",
    day: str = "2026-10-11",
    account_id: int = 1,
) -> AccountRow:
    return AccountRow(
        id=account_id,
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


def test_today_pnl_pct_is_backend_decimal_math(session: Session) -> None:
    broker = PaperBroker(_account(day_start="10000"), PaperSettings())
    broker.cash = Decimal("10250")
    assert broker.today_pnl_pct(NOW) == Decimal("2.5")
    broker.cash = Decimal("9750")
    assert broker.today_pnl_pct(NOW) == Decimal("-2.5")


def test_day_start_rolls_once_across_dst_fallback() -> None:
    """DST fallback on 2026-10-25 (CEST→CET): the day is 25 hours long and
    day-start must roll exactly once at 00:00 Europe/Amsterdam — not at UTC
    midnight and not again inside the repeated 02:00 hour."""
    broker = PaperBroker(_account(day_start="10000", day="2026-10-24"), PaperSettings())
    # 00:00 Amsterdam on 25 Oct = 22:00 UTC 24 Oct (still CEST, UTC+2).
    assert broker.today_pnl(datetime(2026, 10, 24, 21, 59, 59, tzinfo=UTC)) == Decimal("0")
    assert broker.day_start_equity == Decimal("10000")  # still the 24th there
    broker.today_pnl(datetime(2026, 10, 24, 22, 0, tzinfo=UTC))  # 00:00 CEST 25th
    # Sentinel: any further roll inside the 25th would overwrite with equity.
    broker.day_start_equity = Decimal("42")
    # The repeated local hour: 00:30 UTC is 02:30 CEST, 01:30 UTC is 02:30
    # CET — same Amsterdam date, so no second roll anywhere in it.
    for ts in (
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),  # the fallback instant
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 22, 59, 59, tzinfo=UTC),  # 23:59:59 CET
    ):
        broker.today_pnl(ts)
        assert broker.day_start_equity == Decimal("42"), f"re-rolled at {ts}"
    # Next roll at 00:00 CET 26 Oct = 23:00 UTC 25 Oct — once per Amsterdam day.
    broker.today_pnl(datetime(2026, 10, 25, 23, 0, tzinfo=UTC))
    assert broker.day_start_equity == broker.equity()
    broker.day_start_equity = Decimal("43")
    broker.today_pnl(datetime(2026, 10, 25, 23, 30, tzinfo=UTC))
    assert broker.day_start_equity == Decimal("43")


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
    session.expire_all()
    other = PaperBroker(_account(), PaperSettings())
    other.load_positions(session)
    pos = other.positions["BTC-EUR"]
    assert pos.qty == Decimal("5") and pos.entry == Decimal("100.05")
    assert pos.stop == Decimal("90") and pos.invalidation_price == Decimal("95")
    assert pos.r_unit == pos.entry - Decimal("90")
    assert pos.opened_at == NOW and pos.opened_at.tzinfo is UTC


def test_position_model_normalises_naive_opened_at(session: Session) -> None:
    pos = HeldPosition(
        pair="BTC-EUR",
        qty=Decimal("1"),
        entry=Decimal("100"),
        stop=Decimal("90"),
        target=Decimal("120"),
        opened_at=NOW.replace(tzinfo=None),
        decision_id="d1",
        last=Decimal("100"),
    )
    assert pos.to_model().opened_at == NOW


def test_close_refuses_a_non_positive_price(session: Session) -> None:
    broker = PaperBroker(_account(), PaperSettings())
    broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("5"), Decimal("90"), Decimal("120"), "d1", NOW
    )
    cash = broker.cash
    assert broker.close(session, "BTC-EUR", Decimal("0"), NOW, "manual") is None
    assert "BTC-EUR" in broker.positions and broker.cash == cash


def test_position_rows_stay_scoped_to_their_account(session: Session) -> None:
    old = PaperBroker(_account(account_id=1), PaperSettings())
    old.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("5"), Decimal("90"), Decimal("120"), "d1", NOW
    )
    current = PaperBroker(_account(account_id=2), PaperSettings())
    current.buy(
        session, "BTC-EUR", Decimal("101"), Decimal("4"), Decimal("95"), Decimal("120"), "d2", NOW
    )
    session.commit()

    # (pair, account_id) is the key: two accounts can hold the same pair —
    # a shadow may sit on the same market as live.
    old_row = session.get(PositionRow, ("BTC-EUR", 1))
    new_row = session.get(PositionRow, ("BTC-EUR", 2))
    assert old_row is not None and old_row.decision_id == "d1"
    assert new_row is not None and new_row.decision_id == "d2"

    repo.delete_position_row(session, "BTC-EUR", account_id=1)
    assert session.get(PositionRow, ("BTC-EUR", 2)) is not None
    repo.delete_position_row(session, "BTC-EUR", account_id=2)
    session.flush()
    assert session.get(PositionRow, ("BTC-EUR", 2)) is None


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


def test_failed_buy_commit_restores_broker_state(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit that fails after buy() mutated memory must not leave cash
    deducted or a phantom position in the in-memory broker."""
    broker = PaperBroker(_account(), PaperSettings())
    snapshot = broker.capture_state()
    broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("10"), Decimal("95"), Decimal("110"), "d1", NOW
    )
    assert "BTC-EUR" in broker.positions

    def fail() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(session, "commit", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        session.commit()
    session.rollback()
    broker.restore_state(snapshot)

    assert broker.cash == Decimal("10000")
    assert "BTC-EUR" not in broker.positions
    assert broker.equity() == Decimal("10000")
    assert session.scalars(select(PositionRow)).all() == []


def test_failed_close_commit_restores_broker_state(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit that fails after close() removed the position must bring the
    position and the pre-close cash back in memory."""
    broker = PaperBroker(_account(), PaperSettings())
    broker.buy(
        session, "BTC-EUR", Decimal("100"), Decimal("5"), Decimal("90"), Decimal("120"), "d1", NOW
    )
    session.commit()
    snapshot = broker.capture_state()
    cash_before = broker.cash

    broker.close(session, "BTC-EUR", Decimal("110"), NOW, "target")
    assert "BTC-EUR" not in broker.positions

    def fail() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(session, "commit", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        session.commit()
    session.rollback()
    broker.restore_state(snapshot)

    assert broker.cash == cash_before
    pos = broker.positions["BTC-EUR"]
    assert pos.qty == Decimal("5") and pos.stop == Decimal("90")
    # The database still holds the committed open position and no trade.
    assert session.get(PositionRow, ("BTC-EUR", 1)) is not None
    assert session.scalars(select(TradeRow)).all() == []
