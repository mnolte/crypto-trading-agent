"""Limit entries (PR 12): post-only placement, the through-the-tick fill
rule, next-cycle and gate cancellations, maker/taker fees, the fee-gate
thresholds per order type, fill-time gate rechecks, Shadow B, restart
recovery and the settings migration.

Everything runs on the scripted/simulated market with a fake clock — no
network, no real orders.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import command
from app import repo
from app.agent.broker.paper import PaperBroker
from app.agent.market.base import Candle, MarketInfo
from app.agent.outcomes import run_outcomes
from app.agent.risk import BuyContext, check_buy
from app.config import get_env
from app.db import (
    ALEMBIC_DIR,
    ALEMBIC_INI,
    DecisionRow,
    OutcomeRow,
    PaperOrderRow,
    PositionRow,
    TradeRow,
    run_migrations,
)
from app.models import PaperSettings, Plan, Settings
from tests.test_engine import NOW, EngineEnv

HOUR = timedelta(hours=1)


def _limit_env(db_path: Path, markets: list[str], **paper_kw: object) -> EngineEnv:
    settings = Settings(
        active_markets=markets,
        data_source="sim",
        execution_mode="auto",
        paper=PaperSettings(entry_orders="limit", **paper_kw),
    )
    return EngineEnv(db_path, markets=markets, settings=settings)


def _uptrend_env(env: Path, **paper_kw: object) -> EngineEnv:
    e = _limit_env(env / "agent.db", ["SOL-EUR"], **paper_kw)
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    return e


def _orders(session: Session, account_id: int = 1) -> list[PaperOrderRow]:
    return list(
        session.scalars(
            select(PaperOrderRow)
            .where(PaperOrderRow.account_id == account_id)
            .order_by(PaperOrderRow.id)
        ).all()
    )


def _decision(session: Session, decision_id: str) -> DecisionRow:
    row = session.get(DecisionRow, decision_id)
    assert row is not None
    return row


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Path) -> Iterator[Path]:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(migrated_db, data / "agent.db")
    monkeypatch.setenv("TA_DB_PATH", str(data / "agent.db"))
    monkeypatch.setenv("TA_DATA_DIR", str(data))
    get_env.cache_clear()
    yield data
    get_env.cache_clear()


# ---------------------------------------------------------------- placement ==


async def test_limit_buy_rests_as_open_order(env: Path) -> None:
    """An auto BUY under limit entries places a post-only order instead of
    filling — the decision waits, no position opens."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    try:
        orders = _orders(s)
        assert len(orders) == 1
        order = orders[0]
        assert order.leg == "entry" and order.status == "open"
        assert order.expires_at is not None
        row = _decision(s, order.decision_id)
        assert row.action == "BUY" and row.status == "none"
        assert row.plan is not None
    finally:
        s.close()


async def test_post_only_moves_to_best_bid(env: Path) -> None:
    """An entry at or above the best ask is placed on the bid instead."""
    e = _uptrend_env(env)
    # A deep low inside the stop lookback gives the stop enough slack for
    # the entry to move down to the bid and still clear the fee gate.
    candle = e.market.series[("SOL-EUR", "1h")][-3]
    candle.low = candle.close * Decimal("0.97")
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    # The ask sits below the decision's entry → the limit would cross.
    e.market.set_book("SOL-EUR", last * Decimal("0.99"), last * Decimal("0.995"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        assert Decimal(order.limit_price) == last * Decimal("0.99")
    finally:
        s.close()


async def test_post_only_keeps_entry_below_ask(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        row = _decision(s, order.decision_id)
        assert row.plan is not None
        plan = Plan.model_validate_json(row.plan)
        assert Decimal(order.limit_price) == plan.fill_entry
        assert Decimal(order.limit_price) < last * Decimal("1.20")
    finally:
        s.close()


async def test_no_book_means_no_order(env: Path) -> None:
    """Without a readable book the BUY does not place (SKIP, not HOLD)."""
    e = _uptrend_env(env)  # BookMarket but no book set → empty book
    await e.engine.run_cycle()
    s = e.session()
    try:
        assert _orders(s) == []
        row = s.scalars(select(DecisionRow).where(DecisionRow.action == "SKIP")).first()
        assert row is not None and row.error == "order book unavailable"
    finally:
        s.close()


# ------------------------------------------------------------------ fills ==


async def test_fills_only_when_price_trades_through_a_tick(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
    finally:
        s.close()
    limit = Decimal(order.limit_price)
    tick = Decimal("0.01")

    # A touch at the limit — even half a tick below — is not a fill.
    e.market.tickers["SOL-EUR"] = limit
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    e.market.tickers["SOL-EUR"] = limit - tick / 2
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions

    # One full tick below fills at the exact limit price (maker fee).
    e.market.tickers["SOL-EUR"] = limit - tick
    await e.engine.tick()
    pos = e.broker.positions.get("SOL-EUR")
    assert pos is not None
    assert pos.entry == limit
    s = e.session()
    try:
        fresh = s.get(PaperOrderRow, order.id)
        assert fresh is not None and fresh.status == "filled"
        assert Decimal(fresh.fill_price or "0") == limit
        assert _decision(s, fresh.decision_id).status == "executed"
    finally:
        s.close()


async def test_filled_entry_is_not_stopped_same_tick(env: Path) -> None:
    """A trade-through below the stop fills the entry but never stops the
    new position in the same check."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        limit = Decimal(order.limit_price)
        row = _decision(s, order.decision_id)
        assert row.plan is not None
        stop = Plan.model_validate_json(row.plan).stop
    finally:
        s.close()
    # Price trades far through the limit — under the planned stop.
    e.market.tickers["SOL-EUR"] = min(limit - Decimal("0.01"), stop - Decimal("0.01"))
    await e.engine.tick()
    assert "SOL-EUR" in e.broker.positions  # filled, not stopped
    await e.engine.tick()  # next check: now the stop applies
    assert "SOL-EUR" not in e.broker.positions


async def test_watcher_fill_broadcasts_a_snapshot(env: Path) -> None:
    """A fill lands between cycle pushes — the watcher pushes its own
    snapshot or connected clients keep showing no position until the
    next cycle ends."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        limit = Decimal(_orders(s)[0].limit_price)
    finally:
        s.close()

    pushes = 0

    async def spy() -> None:
        nonlocal pushes
        pushes += 1

    e.engine._broadcast = spy
    e.market.tickers["SOL-EUR"] = limit - Decimal("0.01")
    await e.engine.tick()
    assert "SOL-EUR" in e.broker.positions
    assert pushes == 1


# --------------------------------------------------------------- cancels ==


async def test_unfilled_entry_cancels_at_next_cycle(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        decision_id = order.decision_id
        target = Plan.model_validate_json(_decision(s, decision_id).plan or "{}").target
    finally:
        s.close()

    e.clock.advance(minutes=15)
    await e.engine.run_cycle()

    s = e.session()
    try:
        fresh = s.get(PaperOrderRow, order.id)
        assert fresh is not None
        assert fresh.status == "cancelled" and fresh.cancel_reason == "next cycle"
        assert _decision(s, decision_id).status == "unfilled"
        # An unfilled entry is outcome-tracked like a BUY not taken: the
        # first touch of the planned target reads "missed".
        repo.upsert_candles(
            s,
            "SOL-EUR",
            "1h",
            [
                Candle(
                    open_time=NOW + timedelta(hours=i),
                    open=target,
                    high=target + Decimal("1"),
                    low=last,
                    close=target,
                    volume=Decimal("1"),
                )
                for i in range(4)
            ],
        )
        run_outcomes(s, NOW + timedelta(hours=4))
        outcome = s.scalar(
            select(OutcomeRow).where(
                OutcomeRow.decision_id == decision_id,
                OutcomeRow.horizon == 4,
            )
        )
        assert outcome is not None
        assert outcome.action == "BUY" and outcome.label == "missed"
    finally:
        s.close()


async def test_entry_pause_cancels_open_order(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        repo.set_entry_pause(s, "SOL-EUR", None, at=NOW)
        s.commit()
    finally:
        s.close()
    await e.engine.tick()
    s = e.session()
    try:
        fresh = s.get(PaperOrderRow, order.id)
        assert fresh is not None
        assert fresh.status == "cancelled" and fresh.cancel_reason == "entry pause"
        assert _decision(s, fresh.decision_id).status == "unfilled"
    finally:
        s.close()


async def test_shock_brake_cancels_open_order(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    e.engine.shock_brake_until = e.clock() + timedelta(hours=2)
    s = e.session()
    try:
        order_id = _orders(s)[0].id
    finally:
        s.close()
    await e.engine.tick()
    s = e.session()
    try:
        order = s.get(PaperOrderRow, order_id)
        assert order is not None and order.cancel_reason == "shock brake"
    finally:
        s.close()


async def test_daily_loss_limit_cancels_open_order(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order_id = _orders(s)[0].id
    finally:
        s.close()
    # A day-start far above current equity trips the 3% daily loss limit.
    e.broker.day_start_equity = Decimal("20000")
    await e.engine.tick()
    s = e.session()
    try:
        order = s.get(PaperOrderRow, order_id)
        assert order is not None and order.cancel_reason == "daily loss limit"
    finally:
        s.close()


async def test_kill_switch_cancels_open_order(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order_id = _orders(s)[0].id
    finally:
        s.close()
    # A losing held position on another pair trips the 8% kill drawdown.
    # Day-start equity is moved so the daily-loss gate does not fire first.
    e.held("AAA-EUR", qty="90", entry="100", stop="50")
    e.market.add_zigzag("AAA-EUR")
    e.market.tickers["AAA-EUR"] = Decimal("80")
    e.broker.day_start_equity = Decimal("8200")
    await e.engine.tick()
    assert e.engine.status == "killed"
    s = e.session()
    try:
        order = s.get(PaperOrderRow, order_id)
        assert order is not None and order.cancel_reason == "kill switch"
    finally:
        s.close()


# ------------------------------------------------------------- fill gates ==


async def test_open_risk_cap_filled_meanswhile_cancels(env: Path) -> None:
    """Gates re-run at fill: an open-risk cap that filled meanwhile cancels
    the order instead of filling it."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        limit = Decimal(order.limit_price)
    finally:
        s.close()
    # Another position takes the whole 2.5% open-risk budget meanwhile.
    e.held("AAA-EUR", qty="90", entry="100", stop="50")  # risk 4500 EUR
    e.market.add_zigzag("AAA-EUR")
    e.market.tickers["SOL-EUR"] = limit - Decimal("0.01")
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    try:
        fresh = s.get(PaperOrderRow, order.id)
        assert fresh is not None
        assert fresh.status == "cancelled" and fresh.cancel_reason == "Total open risk"
        assert _decision(s, fresh.decision_id).status == "unfilled"
    finally:
        s.close()


# ------------------------------------------------------------- limit exits ==


async def test_limit_entry_then_limit_target_maker_fees(env: Path) -> None:
    """Entry fills at the limit (maker, no slip); the target rests and
    fills through the tick at the exact target — exact-cent P&L."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        limit = Decimal(order.limit_price)
    finally:
        s.close()
    e.market.tickers["SOL-EUR"] = limit - Decimal("0.01")
    await e.engine.tick()
    pos = e.broker.positions.get("SOL-EUR")
    assert pos is not None
    cash_after_buy = e.broker.cash
    # Maker fee on the entry: qty * limit * 0.15%.
    entry_fee = pos.qty * pos.entry * Decimal("0.0015")
    assert cash_after_buy == Decimal("10000") - pos.qty * pos.entry - entry_fee

    # A touch at the target does not fill; a tick above does, at the limit.
    e.market.tickers["SOL-EUR"] = pos.target
    await e.engine.tick()
    assert "SOL-EUR" in e.broker.positions
    e.market.tickers["SOL-EUR"] = pos.target + Decimal("0.01")
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    try:
        trade = s.scalars(select(TradeRow).where(TradeRow.decision_id == pos.decision_id)).one()
        assert trade.exit_reason == "target"
        assert Decimal(trade.exit) == pos.target  # exact limit, no slippage
        sell_fee = pos.qty * pos.target * Decimal("0.0015")
        expected = pos.qty * (pos.target - pos.entry) - entry_fee - sell_fee
        assert Decimal(trade.realised_eur) == expected
        assert Decimal(trade.fee_eur) == entry_fee + sell_fee
    finally:
        s.close()


async def test_limit_entry_stop_exits_at_market(env: Path) -> None:
    """A stop always exits at market: taker fee plus slippage, whatever
    the entry setting."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        limit = Decimal(order.limit_price)
    finally:
        s.close()
    e.market.tickers["SOL-EUR"] = limit - Decimal("0.01")
    await e.engine.tick()  # entry fills (exits start next tick)
    pos = e.broker.positions.get("SOL-EUR")
    assert pos is not None
    e.market.tickers["SOL-EUR"] = pos.stop
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    try:
        trade = s.scalars(select(TradeRow).where(TradeRow.decision_id == pos.decision_id)).one()
        assert trade.exit_reason == "stop"
        slipped = pos.stop * (1 - Decimal("0.0005"))  # 0.05% slippage
        assert Decimal(trade.exit) == slipped
        entry_fee = pos.qty * pos.entry * Decimal("0.0015")  # maker on entry
        sell_fee = pos.qty * slipped * Decimal("0.0025")  # taker on exit
        expected = pos.qty * (slipped - pos.entry) - entry_fee - sell_fee
        assert Decimal(trade.realised_eur) == expected
    finally:
        s.close()


async def test_switch_to_market_cancels_resting_entry(env: Path) -> None:
    """Flipping Entry orders back to Market cancels a resting limit entry —
    it can no longer be managed post-only."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
    finally:
        s.close()
    e.settings = e.settings.model_copy(
        update={"paper": e.settings.paper.model_copy(update={"entry_orders": "market"})}
    )
    await e.engine.tick()
    s = e.session()
    try:
        fresh = s.get(PaperOrderRow, order.id)
        assert fresh is not None
        assert fresh.status == "cancelled" and fresh.cancel_reason == "switched to market"
        assert _decision(s, fresh.decision_id).status == "unfilled"
    finally:
        s.close()


async def test_target_style_follows_the_position(env: Path) -> None:
    """A limit-filled position keeps its resting limit target after the
    account switches back to market entries — and entry_maker survives
    every watcher save of the position row."""
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        limit = Decimal(order.limit_price)
    finally:
        s.close()
    e.market.tickers["SOL-EUR"] = limit - Decimal("0.01")
    await e.engine.tick()
    pos = e.broker.positions.get("SOL-EUR")
    assert pos is not None and pos.entry_maker
    # The target rests on the book from the fill tick.
    s = e.session()
    try:
        assert any(o.leg == "target" and o.status == "open" for o in _orders(s))
    finally:
        s.close()
    # A neutral tick persists the position; entry_maker must survive.
    e.market.tickers["SOL-EUR"] = pos.entry
    await e.engine.tick()
    s = e.session()
    try:
        row = s.get(PositionRow, ("SOL-EUR", order.account_id))
        assert row is not None and row.entry_maker
    finally:
        s.close()
    # Switching to market entries does not touch the open position's exit:
    # a touch is not a fill, a tick above fills at the exact limit.
    e.settings = e.settings.model_copy(
        update={"paper": e.settings.paper.model_copy(update={"entry_orders": "market"})}
    )
    e.market.tickers["SOL-EUR"] = pos.target
    await e.engine.tick()
    assert "SOL-EUR" in e.broker.positions
    e.market.tickers["SOL-EUR"] = pos.target + Decimal("0.01")
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    try:
        trade = s.scalars(select(TradeRow).where(TradeRow.decision_id == pos.decision_id)).one()
        assert trade.exit_reason == "target"
        assert Decimal(trade.exit) == pos.target
    finally:
        s.close()


# ---------------------------------------------------------------- fee gate ==


def _ctx(stop_dist_pct: Decimal, entry_orders: str) -> str:
    """Run just the gate chain for a 100 EUR entry with a stop_dist_pct
    stop and a 4R target."""
    entry = Decimal("100")
    stop = entry * (1 - stop_dist_pct / 100)
    target = entry * (1 + Decimal("4") * stop_dist_pct / 100)
    settings = Settings(
        active_markets=["SOL-EUR"],
        paper=PaperSettings(entry_orders=entry_orders),
    )
    from tests.test_broker import _account

    broker = PaperBroker(_account(), settings.paper)
    verdict = check_buy(
        BuyContext(
            pair="SOL-EUR",
            now=NOW,
            entry=entry,
            stop=stop,
            target=target,
            atr_1h=None,
            has_position=False,
            last_stop_out_at=None,
        ),
        broker,
        MarketInfo(
            market="SOL-EUR",
            status="trading",
            base="SOL",
            quote="EUR",
            tick_size=Decimal("0.01"),
            quantity_decimals=4,
        ),
        settings,
    )
    return verdict.outcome


def test_fee_gate_16_pct_stop_limit_passes_market_fails() -> None:
    """Costs 0.45% (limit) vs 0.6% (market) at a 30% cost cap → the 1.6%
    stop only fits with maker entries."""
    assert _ctx(Decimal("1.6"), "limit") == "fill"
    assert _ctx(Decimal("1.6"), "market") == "skip"


def test_fee_gate_14_pct_stop_fails_both() -> None:
    assert _ctx(Decimal("1.4"), "limit") == "skip"
    assert _ctx(Decimal("1.4"), "market") == "skip"


# ---------------------------------------------------------------- restart ==


async def test_restart_cancels_open_limit_order(env: Path) -> None:
    e = _uptrend_env(env)
    last = e.market.series[("SOL-EUR", "1h")][-1].close
    e.market.set_book("SOL-EUR", last * Decimal("0.90"), last * Decimal("1.20"))
    await e.engine.run_cycle()
    s = e.session()
    try:
        order = _orders(s)[0]
        order_id, decision_id = order.id, order.decision_id
    finally:
        s.close()
    e.db_engine.dispose()

    e2 = EngineEnv(
        env / "agent.db",
        markets=["SOL-EUR"],
        settings=Settings(
            active_markets=["SOL-EUR"],
            data_source="sim",
            execution_mode="auto",
            paper=PaperSettings(entry_orders="limit"),
        ),
    )
    # start() spawns the scheduler/watcher loops; the recovery half runs
    # on its own (engine.start calls _recover_startup first).
    session = e2.session()
    try:
        e2.engine._recover_startup(session)
        session.commit()
    finally:
        session.close()
    s = e2.session()
    try:
        fresh = s.get(PaperOrderRow, order_id)
        assert fresh is not None
        assert fresh.status == "cancelled" and fresh.cancel_reason == "restart"
        assert _decision(s, decision_id).status == "unfilled"
    finally:
        s.close()


# --------------------------------------------------------------- migration ==


def test_migration_keeps_configured_fee_as_taker_fee(tmp_path: Path) -> None:
    """The pre-split paper.fee_pct survives the migration as taker_fee_pct."""
    db = tmp_path / "agent.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "0008")
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO settings (id, version, doc, updated_at) "
                    "VALUES (1, 3, :doc, '2026-10-20 00:00:00')"
                ),
                {"doc": '{"paper": {"fee_pct": "0.31", "slippage_pct": "0.07"}}'},
            )
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            doc = conn.execute(sa.text("SELECT doc FROM settings WHERE id = 1")).scalar()
    finally:
        engine.dispose()
    paper = json.loads(str(doc))["paper"]
    assert paper["taker_fee_pct"] == "0.31"
    assert paper["slippage_pct"] == "0.07"
    assert "fee_pct" not in paper
    settings = Settings.model_validate_json(str(doc))
    assert settings.paper.taker_fee_pct == Decimal("0.31")
    assert settings.paper.maker_fee_pct == Decimal("0.15")
    assert settings.paper.entry_orders == "market"


def test_migration_creates_paper_orders_and_entry_maker(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    run_migrations(db)
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        cols = {c["name"] for c in sa.inspect(engine).get_columns("positions")}
        tables = sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
    assert "paper_orders" in tables
    assert "entry_maker" in cols
