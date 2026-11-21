"""016 Positions & orders: fill rows from the paper broker, the one-time
rebuild, and the /api/positions + /api/fills endpoints."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import repo
from app.agent.broker.paper import PaperBroker
from app.api.deps import AppState
from app.config import get_env
from app.db import (
    AccountRow,
    FillRow,
    PositionRow,
    ShadowRow,
    TradeRow,
    make_engine,
    make_session_factory,
)
from app.features import fills as fills_mod
from app.models import PaperSettings

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _account(
    cash: str = "10000",
    account_id: int = 1,
    started: datetime = NOW,
) -> AccountRow:
    return AccountRow(
        id=account_id,
        started_at=started,
        start_balance="10000",
        cash=cash,
        peak_equity="10000",
        day_start_equity="10000",
        day_start_date="2026-10-14",
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


def _broker(session: Session, account_id: int = 1, cash: str = "10000") -> PaperBroker:
    account = session.get(AccountRow, account_id)
    if account is None:
        account = _account(cash=cash, account_id=account_id)
        session.add(account)
        session.flush()
    return PaperBroker(account, PaperSettings())


# ------------------------------------------------------------- broker fills --


def test_market_buy_writes_a_decision_fill(session: Session) -> None:
    broker = _broker(session)
    broker.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("10"),
        Decimal("95"),
        Decimal("110"),
        "d1",
        NOW,
    )
    session.flush()
    fill = session.scalar(select(FillRow).where(FillRow.pair == "BTC-EUR"))
    assert fill is not None
    assert fill.side == "buy" and fill.reason == "decision" and fill.decision_id == "d1"
    assert fill.source == "live" and fill.order_type == "market"
    assert fill.fee_kind == "taker"
    assert Decimal(fill.reference_price) == Decimal("100")
    assert Decimal(fill.fill_price) > Decimal("100")  # slippage against us
    assert Decimal(fill.slippage_eur) > 0
    assert Decimal(fill.fee_eur) > 0


def test_limit_buy_writes_a_maker_fill_without_slippage(session: Session) -> None:
    broker = _broker(session)
    broker.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("10"),
        Decimal("95"),
        Decimal("110"),
        "d1",
        NOW,
        order_type="limit",
        last_price=Decimal("99"),
    )
    session.flush()
    fill = session.scalar(select(FillRow))
    assert fill is not None
    assert fill.order_type == "limit" and fill.fee_kind == "maker"
    assert Decimal(fill.fill_price) == Decimal("100")
    assert Decimal(fill.slippage_eur) == 0


def test_close_writes_exit_fill_and_links_both_to_the_trade(session: Session) -> None:
    broker = _broker(session)
    broker.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("10"),
        Decimal("95"),
        Decimal("110"),
        "d1",
        NOW,
    )
    session.flush()
    trade = broker.close(session, "BTC-EUR", Decimal("110"), NOW + HOUR, "target")
    assert trade is not None
    session.flush()
    fills = list(session.scalars(select(FillRow).order_by(FillRow.id)).all())
    assert len(fills) == 2
    entry_fill, exit_fill = fills
    assert entry_fill.side == "buy" and exit_fill.side == "sell"
    assert exit_fill.reason == "target"
    assert exit_fill.trade_id == trade.id
    assert entry_fill.trade_id == trade.id  # linked once the trade exists
    assert Decimal(exit_fill.fill_price) < Decimal("110")  # sell slips down


def test_exit_reasons_are_recorded_verbatim(env_paths: Path) -> None:
    engine = make_engine(get_env().ta_db_path)
    factory = make_session_factory(engine)
    try:
        for reason in ("stop", "time_stop", "model_sell", "flatten", "manual", "kill"):
            s = factory()
            try:
                broker = _broker(s)
                broker.buy(
                    s,
                    "ETH-EUR",
                    Decimal("50"),
                    Decimal("4"),
                    Decimal("45"),
                    Decimal("60"),
                    f"d-{reason}",
                    NOW,
                )
                s.flush()
                broker.close(s, "ETH-EUR", Decimal("55"), NOW + HOUR, reason)
                s.flush()
                fill = s.scalar(
                    select(FillRow).where(FillRow.side == "sell", FillRow.reason == reason)
                )
                assert fill is not None
            finally:
                s.close()
    finally:
        engine.dispose()


def test_fills_carry_their_own_account(session: Session) -> None:
    live = _broker(session, account_id=1)
    shadow = _broker(session, account_id=2)
    live.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("1"),
        Decimal("90"),
        Decimal("120"),
        "d1",
        NOW,
    )
    shadow.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("2"),
        Decimal("90"),
        Decimal("120"),
        "d2",
        NOW,
    )
    session.flush()
    assert repo.count_fills(session, 1) == 1
    assert repo.count_fills(session, 2) == 1
    assert repo.count_fills(session, 1, pair="ETH-EUR") == 0


def test_failed_commit_leaves_neither_trade_nor_fill(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = _broker(session)
    broker.buy(
        session,
        "BTC-EUR",
        Decimal("100"),
        Decimal("10"),
        Decimal("95"),
        Decimal("110"),
        "d1",
        NOW,
    )
    broker.close(session, "BTC-EUR", Decimal("110"), NOW + HOUR, "target")

    def fail() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(session, "commit", fail)
    with pytest.raises(RuntimeError):
        session.commit()
    session.rollback()
    monkeypatch.undo()
    assert session.scalar(select(func.count()).select_from(FillRow)) == 0
    assert session.scalar(select(func.count()).select_from(TradeRow)) == 0


# ------------------------------------------------------------------ rebuild --


def _seed_trade(session: Session, *, account_id: int = 1) -> TradeRow:
    trade = TradeRow(
        account_id=account_id,
        pair="BTC-EUR",
        qty="10",
        entry="100",
        exit="110",
        opened_at=NOW,
        closed_at=NOW + 2 * HOUR,
        fee_eur="5.50",
        realised_eur="94.50",
        realised_r="1.89",
        initial_stop="95",
        r_unit="5",
        exit_reason="target",
        decision_id="d-old",
    )
    session.add(trade)
    session.flush()
    return trade


def test_rebuild_writes_both_sides_once(session: Session) -> None:
    trade = _seed_trade(session)
    session.add(
        PositionRow(
            pair="ETH-EUR",
            account_id=1,
            qty="4",
            entry="50",
            stop="45",
            initial_stop="45",
            target="60",
            opened_at=NOW,
            decision_id="d-open",
            last="52",
            invalidation_price=None,
            highest_close=None,
            r_unit="5",
            entry_maker=False,
        )
    )
    session.commit()

    paper = PaperSettings()
    assert fills_mod.rebuild_fills(session, paper) == 3  # 2 for the trade + 1 open
    session.commit()
    rows = list(session.scalars(select(FillRow).order_by(FillRow.at, FillRow.id)).all())
    assert len(rows) == 3
    for row in rows:
        assert row.source == "rebuilt"
        assert row.reference_price is None and row.slippage_eur is None
    buy = next(r for r in rows if r.pair == "BTC-EUR" and r.side == "buy")
    sell = next(r for r in rows if r.pair == "BTC-EUR" and r.side == "sell")
    assert buy.trade_id == trade.id and sell.trade_id == trade.id
    assert buy.reason == "decision" and sell.reason == "target"
    # The trade's €5.50 fee splits proportionally to each side's fee weight.
    assert abs(Decimal(buy.fee_eur) + Decimal(sell.fee_eur) - Decimal("5.50")) < Decimal("0.01")
    open_fill = next(r for r in rows if r.pair == "ETH-EUR")
    assert open_fill.trade_id is None and open_fill.side == "buy"

    # A second startup adds nothing.
    assert fills_mod.rebuild_fills(session, paper) == 0


def test_rebuild_is_idempotent_across_sessions(session: Session) -> None:
    _seed_trade(session)
    session.commit()
    assert fills_mod.rebuild_fills(session, PaperSettings()) == 2
    session.commit()
    assert fills_mod.rebuild_fills(session, PaperSettings()) == 0


# -------------------------------------------------------------------- API --


def _state(client: TestClient) -> AppState:
    app = client.app
    assert isinstance(app, FastAPI)
    state: AppState = app.state.app_state
    return state


def _db(client: TestClient) -> Session:
    return _state(client).session_factory()


def _add_fill(
    session: Session,
    *,
    account_id: int,
    at: datetime,
    pair: str = "BTC-EUR",
    side: str = "buy",
    reason: str = "decision",
    decision_id: str = "d1",
    trade_id: int | None = None,
    source: str = "live",
) -> FillRow:
    row = FillRow(
        account_id=account_id,
        at=at,
        pair=pair,
        side=side,
        qty="1.5",
        reference_price="100",
        fill_price="100.05",
        order_type="market",
        fee_eur="0.38",
        fee_kind="taker",
        slippage_eur="0.08",
        reason=reason,
        decision_id=decision_id,
        trade_id=trade_id,
        paper_order_id=None,
        source=source,
    )
    session.add(row)
    session.commit()
    return row


def test_positions_live_shape(sim_client: TestClient) -> None:
    client = sim_client
    res = client.get("/api/positions?account=live")
    assert res.status_code == 200, res.text
    body = res.json()
    for key in ("account", "shadows", "totals", "positions", "orders", "today", "paper_account"):
        assert key in body
    assert body["account"] == "live"
    assert body["totals"]["count"] == 0
    assert body["positions"] == [] and body["orders"] == []
    assert body["today"]["items"] == []
    assert body["paper_account"]["id"] == _state(client).account_id
    assert isinstance(body["shadows"], list)


def test_positions_unknown_shadow_is_404(sim_client: TestClient) -> None:
    res = sim_client.get("/api/positions?account=nope")
    assert res.status_code == 404


def test_positions_shadow_account(sim_client: TestClient) -> None:
    client = sim_client
    session = _db(client)
    try:
        account = AccountRow(
            started_at=NOW,
            start_balance="5000",
            cash="5000",
            peak_equity="5000",
            day_start_equity="5000",
            day_start_date="2026-10-14",
            active=False,
        )
        session.add(account)
        session.flush()
        shadow = ShadowRow(
            id="sh-1",
            name="Shadow A",
            model="gpt-5",
            prompt_version="decision.v1",
            overrides="{}",
            status="stopped",
            created_at=NOW,
            stopped_at=NOW,
            account_id=account.id,
            start_equity="10000",
        )
        session.add(shadow)
        session.commit()
        sid = shadow.id
    finally:
        session.close()

    res = client.get(f"/api/positions?account={sid}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account"] == sid
    assert body["today"] is None and body["paper_account"] is None
    refs = {s["id"]: s for s in body["shadows"]}
    assert refs[sid]["name"] == "Shadow A" and refs[sid]["status"] == "stopped"
    assert refs[sid]["entry_orders"] in ("market", "limit")


def test_fills_filters_order_and_pagination(sim_client: TestClient) -> None:
    client = sim_client
    account_id = _state(client).account_id
    base = NOW - 3 * HOUR
    session = _db(client)
    try:
        for i in range(55):
            _add_fill(
                session,
                account_id=account_id,
                at=base + timedelta(minutes=i),
                pair="BTC-EUR" if i % 2 == 0 else "ETH-EUR",
                side="buy" if i % 3 else "sell",
                reason="decision" if i % 4 else "stop",
                decision_id=f"d-{i}",
            )
        # A shadow fill must never appear in the live history.
        _add_fill(
            session,
            account_id=account_id + 100,
            at=base,
            pair="BTC-EUR",
            side="buy",
            reason="decision",
            decision_id="d-shadow",
        )
    finally:
        session.close()

    res = client.get("/api/fills")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 55
    assert len(body["items"]) == 50
    times = [i["at"] for i in body["items"]]
    assert times == sorted(times, reverse=True)  # newest first
    assert all(i["decision_id"] != "d-shadow" for i in body["items"])

    # Page 2 via the opaque cursor continues where page 1 stopped.
    cursor = body["next_cursor"]
    assert cursor
    res2 = client.get(f"/api/fills?cursor={cursor}")
    body2 = res2.json()
    assert len(body2["items"]) == 5 and body2["next_cursor"] is None
    assert {i["id"] for i in body["items"]}.isdisjoint({i["id"] for i in body2["items"]})

    # Filters narrow the list.
    res3 = client.get("/api/fills?pair=ETH-EUR&side=sell&reason=stop")
    items3 = res3.json()["items"]
    assert all(
        i["pair"] == "ETH-EUR" and i["side"] == "sell" and i["reason"] == "stop" for i in items3
    )
    assert res3.json()["total"] == len(items3)


def test_fills_csv_export(sim_client: TestClient) -> None:
    client = sim_client
    account_id = _state(client).account_id
    session = _db(client)
    try:
        _add_fill(
            session,
            account_id=account_id,
            at=NOW - HOUR,
            pair="BTC-EUR",
            side="buy",
            reason="decision",
            decision_id="d-csv",
        )
        _add_fill(
            session,
            account_id=account_id,
            at=NOW - 2 * HOUR,
            pair="ETH-EUR",
            side="sell",
            reason="stop",
            decision_id="d-csv2",
        )
    finally:
        session.close()

    res = client.get("/api/fills/export.csv?pair=BTC-EUR")
    assert res.status_code == 200
    assert "fills-" in res.headers.get("content-disposition", "")
    rows = list(csv.reader(io.StringIO(res.text)))
    assert rows[0] == [
        "at_utc",
        "at_amsterdam",
        "pair",
        "side",
        "qty",
        "reference_price",
        "fill_price",
        "order_type",
        "slippage_eur",
        "fee_eur",
        "fee_kind",
        "reason",
        "decision_id",
        "trade_id",
        "source",
    ]
    assert len(rows) == 2  # header + the one BTC fill
    assert rows[1][2] == "BTC-EUR" and rows[1][5] == "100"


def test_today_uses_amsterdam_midnight(session: Session) -> None:
    """A fill at 23:30 UTC is still 'yesterday' in Amsterdam (CEST, UTC+2);
    one at 22:30 UTC the next day is 'today'."""
    from app.api.positions import _today_block

    account = _account(account_id=1)
    session.add(account)
    session.flush()
    # 2026-10-14 22:30 UTC = 2026-10-15 00:30 Amsterdam -> today.
    today_fill = _add_fill(
        session,
        account_id=1,
        at=datetime(2026, 10, 14, 22, 30, tzinfo=UTC),
    )
    # 2026-10-14 21:30 UTC = 2026-10-14 23:30 Amsterdam -> yesterday.
    _add_fill(
        session,
        account_id=1,
        at=datetime(2026, 10, 14, 21, 30, tzinfo=UTC),
        pair="ETH-EUR",
    )
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)  # 14:00 Amsterdam
    out = _today_block(session, 1, now)
    assert out is not None
    assert out.entries == 1 and out.exits == 0
    assert [i.pair for i in out.items] == ["BTC-EUR"]
    assert out.items[0].at == repo.aware_utc(today_fill.at)
