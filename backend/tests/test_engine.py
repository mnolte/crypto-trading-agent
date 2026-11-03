"""Engine tests on the simulated/scripted market with a fake clock.

Covers the spec's engine-test list: open-risk sizing, the 6-hour stop-out
cooldown, the 5-day time stop, the kill drawdown with acknowledgement, the
stale retry, a stop hit between cycles, restart-paused recovery, pending
expiry, and the long-run no-NaN soak.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.broker.paper import HeldPosition, PaperBroker
from app.agent.deciders.base import DecideContext, Draft
from app.agent.deciders.rules import RulesDecider
from app.agent.engine import Engine, KilledRefusal
from app.agent.market.base import (
    BookLevel,
    Candle,
    MarketDataError,
    MarketInfo,
    OrderBook,
    Ticker24h,
)
from app.config import get_env
from app.db import (
    AccountRow,
    DecisionRow,
    ErrorRow,
    LlmCallRow,
    MarketRow,
    TradeRow,
    make_engine,
    make_session_factory,
    run_migrations,
)
from app.models import PaperSettings, Settings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


async def _no_sleep(_: float) -> None:
    return None


def _series(
    n: int,
    step: timedelta,
    end: datetime,
    *,
    start_price: float,
    rate: float,
    wick: float = 0.004,
    volume: float = 100.0,
) -> list[Candle]:
    """A synthetic closed-candle series ending at ``end``."""
    out: list[Candle] = []
    price = start_price
    for i in range(n):
        price *= rate
        out.append(
            Candle(
                open_time=end - (n - i) * step,
                open=Decimal(f"{price / rate:.6f}"),
                high=Decimal(f"{price * (1 + wick):.6f}"),
                low=Decimal(f"{price * (1 - wick):.6f}"),
                close=Decimal(f"{price:.6f}"),
                volume=Decimal(f"{volume}"),
            )
        )
    return out


def _zigzag(n: int, step: timedelta, end: datetime, price: float = 100.0) -> list[Candle]:
    """A mean-reverting series — low correlation with an uptrend."""
    out: list[Candle] = []
    for i in range(n):
        p = price * (1 + 0.01 * math.sin(i * 1.7))
        out.append(
            Candle(
                open_time=end - (n - i) * step,
                open=Decimal(f"{price:.6f}"),
                high=Decimal(f"{p * 1.003:.6f}"),
                low=Decimal(f"{p * 0.997:.6f}"),
                close=Decimal(f"{p:.6f}"),
                volume=Decimal("100"),
            )
        )
        price = p
    return out


class ScriptMarket:
    """A canned ``MarketClient``: precomputed candles per (market, tf) and
    settable ticker prices. Counts 1h fetches for the stale-retry test."""

    def __init__(self, now: datetime, sessions: sessionmaker[Session] | None = None) -> None:
        self.series: dict[tuple[str, str], list[Candle]] = {}
        self.tickers: dict[str, Decimal] = {}
        self.calls_1h: dict[str, int] = {}
        self.books: dict[str, OrderBook] = {}
        self.now = now
        self._sessions = sessions

    def _store(self, market: str) -> None:
        """Simulate the startup backfill: eligibility rows plus the candle
        history the check/backfill would have stored before a first cycle."
        """
        if self._sessions is None:
            return
        session = self._sessions()
        try:
            for tf in ("1h", "4h", "1d"):
                repo.upsert_candles(session, market, tf, self.series.get((market, tf), []))
        finally:
            session.close()

    def add_uptrend(self, market: str, base: float = 100.0) -> None:
        self.series[(market, "1h")] = _series(
            800,
            HOUR,
            self.now,
            start_price=base,
            rate=1.0008,
            wick=0.006,
        )
        self.series[(market, "4h")] = _series(
            300,
            4 * HOUR,
            self.now,
            start_price=base * 0.9,
            rate=1.002,
            wick=0.004,
        )
        self.series[(market, "1d")] = _series(
            300,
            DAY,
            self.now,
            start_price=base * 0.7,
            rate=1.001,
            wick=0.003,
        )
        self.tickers[market] = self.series[(market, "1h")][-1].close
        self._store(market)

    def add_zigzag(self, market: str) -> None:
        self.series[(market, "1h")] = _zigzag(800, HOUR, self.now)
        self.series[(market, "4h")] = _zigzag(300, 4 * HOUR, self.now)
        self.series[(market, "1d")] = _zigzag(300, DAY, self.now)
        self.tickers[market] = self.series[(market, "1h")][-1].close
        self._store(market)

    def add_downtrend(self, market: str) -> None:
        self.series[(market, "1h")] = _series(
            800,
            HOUR,
            self.now,
            start_price=140.0,
            rate=0.999,
            wick=0.004,
        )
        self.series[(market, "4h")] = _series(
            300,
            4 * HOUR,
            self.now,
            start_price=160.0,
            rate=0.998,
            wick=0.003,
        )
        self.series[(market, "1d")] = _series(
            300,
            DAY,
            self.now,
            start_price=180.0,
            rate=0.999,
            wick=0.002,
        )
        self.tickers[market] = self.series[(market, "1h")][-1].close
        self._store(market)

    async def candles(self, market: str, interval: str, limit: int, **kw: object) -> list[Candle]:
        if interval == "1h":
            self.calls_1h[market] = self.calls_1h.get(market, 0) + 1
        bars = [c for c in self.series.get((market, interval), []) if c.open_time <= self.now]
        return bars[-limit:]

    def set_book(self, market: str, bid: Decimal, ask: Decimal) -> None:
        self.books[market] = OrderBook(
            market=market,
            bids=[BookLevel(price=bid, size=Decimal("5"))],
            asks=[BookLevel(price=ask, size=Decimal("5"))],
        )

    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        return self.books.get(market) or OrderBook(market=market, bids=[], asks=[])

    async def ticker_price(self, market: str, **_kw: Any) -> Decimal:
        return self.tickers.get(market, Decimal("0"))

    async def ticker_24h(self, market: str | None = None) -> list[Ticker24h]:
        return []

    async def markets(self) -> list[MarketInfo]:
        return [
            MarketInfo(
                market=m,
                status="trading",
                base=m.split("-")[0],
                quote="EUR",
                min_order_base=Decimal("0.0001"),
                min_order_quote=Decimal("5"),
                tick_size=Decimal("0.01"),
                quantity_decimals=4,
            )
            for (m, _) in self.series
        ]

    async def aclose(self) -> None:
        pass


def _account(day: str = "2026-10-11") -> AccountRow:
    return AccountRow(
        id=1,
        started_at=NOW,
        start_balance="10000",
        cash="10000",
        peak_equity="10000",
        day_start_equity="10000",
        day_start_date=day,
        active=True,
    )


class EngineEnv:
    """Engine + broker + script market on a throwaway database."""

    def __init__(
        self,
        db_path: Path,
        *,
        markets: list[str],
        settings: Settings | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        run_migrations(db_path)
        self.db_engine = make_engine(db_path)
        self.sessions: sessionmaker[Session] = make_session_factory(self.db_engine)
        session = self.sessions()
        account = repo.current_account(session, Decimal("10000"), "2026-10-11")
        # Markets in the env stand in for ones that passed the startup
        # eligibility check — entries_allowed requires an eligible row.
        for m in markets:
            row = session.get(MarketRow, m)
            if row is None:
                session.add(MarketRow(market=m, active=True, eligible=True))
        session.commit()
        self.broker = PaperBroker(account, PaperSettings())
        self.broker.load_positions(session)
        session.close()
        self.market = ScriptMarket(NOW, self.sessions)
        self.clock = clock or FakeClock()
        self.settings = settings or Settings(
            active_markets=markets, data_source="sim", execution_mode="auto"
        )
        infos = {
            m: MarketInfo(
                market=m,
                status="trading",
                base=m.split("-")[0],
                quote="EUR",
                min_order_base=Decimal("0.0001"),
                min_order_quote=Decimal("5"),
                tick_size=Decimal("0.01"),
                quantity_decimals=4,
            )
            for m in markets
        }
        self.engine = Engine(
            session_factory=self.sessions,
            market_client=self.market,
            market_rules=infos,
            settings=lambda: self.settings,
            settings_version=lambda: 1,
            broker=self.broker,
            decider=RulesDecider(),
            clock=self.clock,
            sleep=_no_sleep,
        )

    def session(self) -> Session:
        return self.sessions()

    def held(
        self,
        pair: str,
        *,
        qty: str = "25",
        entry: str = "100",
        stop: str = "96",
        target: str = "110",
        opened_at: datetime = NOW,
    ) -> HeldPosition:
        pos = HeldPosition(
            pair=pair,
            qty=Decimal(qty),
            entry=Decimal(entry),
            stop=Decimal(stop),
            target=Decimal(target),
            opened_at=opened_at,
            decision_id="seed",
            last=Decimal(entry),
            highest_close=Decimal(entry),
            r_unit=Decimal(entry) - Decimal(stop),
        )
        self.broker.positions[pair] = pos
        self.broker.cash -= pos.qty * pos.entry
        return pos


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


def _decisions(session: Session, account_id: int = 1) -> list[DecisionRow]:
    return list(
        session.scalars(select(DecisionRow).where(DecisionRow.account_id == account_id)).all()
    )


# --------------------------------------------------------------------- flow ==


async def test_cycle_buys_on_uptrend(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    await e.engine.run_cycle()
    assert "SOL-EUR" in e.broker.positions
    pos = e.broker.positions["SOL-EUR"]
    assert pos.stop < pos.entry < pos.target
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows and rows[0].action == "BUY" and rows[0].status == "executed"


async def test_decision_records_the_real_btc_regime(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_zigzag("SOL-EUR")
    e.market.add_downtrend("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows
    btc_regime = json.loads(rows[0].btc_regime)
    assert btc_regime["trend"] == "down"


async def test_decision_signals_fill_all_timeframes(env: Path) -> None:
    """Stored signals lead with the decider's chips, then the computed
    per-tf reads, so the drawer's signal stack fills 1h, 4h and 1d."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    s = e.session()
    rows = _decisions(s)
    s.close()
    stored = json.loads(rows[0].signals)
    assert [sig["name"] for sig in stored[:6]] == [f"C{i}" for i in range(1, 7)]
    cells = {(sig["name"], sig["tf"]) for sig in stored}
    for name in (
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "C6",
        "RSI 14",
        "EMA 20 vs 50",
        "Price vs VWAP",
        "OBV trend",
        "ADX 14",
    ):
        for tf in ("1h", "4h", "1d"):
            assert (name, tf) in cells, f"missing {name} on {tf}"


async def test_third_position_sized_to_half(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    # Two full-size positions: 1% risk each (stops 4% away).
    e.held("AAA-EUR", qty="25", entry="100", stop="96")
    e.held("BBB-EUR", qty="25", entry="100", stop="96")
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    # Stored candles for the held pairs feed the correlation gate.
    s = e.session()
    repo.upsert_candles(s, "AAA-EUR", "1h", _zigzag(800, HOUR, NOW, price=100.0))
    repo.upsert_candles(s, "BBB-EUR", "1h", _zigzag(800, HOUR, NOW, price=80.0))
    s.commit()
    s.close()
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    pos = e.broker.positions.get("SOL-EUR")
    assert pos is not None
    # Room left: 250 - 200 = 50 EUR risk; stop ~2% away → notional ~2500.
    risk = pos.qty * (pos.entry - pos.stop)
    assert risk <= Decimal("50.01")
    assert risk > Decimal("45")


async def test_stop_out_cooldown_blocks_reentry(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    # Seed a stop-out trade 3h ago.
    s = e.session()
    from app.db import TradeRow

    s.add(
        TradeRow(
            account_id=1,
            pair="SOL-EUR",
            qty="1",
            entry="100",
            exit="90",
            opened_at=NOW - 4 * HOUR,
            closed_at=NOW - 3 * HOUR,
            fee_eur="0.5",
            realised_eur="-10",
            realised_r="-1",
            exit_reason="stop",
            decision_id="old",
        )
    )
    s.commit()
    await e.engine.run_cycle()
    s.close()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows[0].action == "SKIP" and rows[0].error == "Cooldown after stop-out"


async def test_time_stop_closes_old_position(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR", opened_at=NOW - timedelta(days=5, seconds=1))
    e.market.tickers["SOL-EUR"] = Decimal("105")
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    trade = repo.last_exit_for_pair(s, 1, "SOL-EUR", "time_stop")
    s.close()
    assert trade is not None


async def test_stop_hit_between_cycles(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR", stop="96")
    e.market.tickers["SOL-EUR"] = Decimal("95")
    await e.engine.tick()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    trade = repo.last_exit_for_pair(s, 1, "SOL-EUR", "stop")
    s.close()
    assert trade is not None


async def test_breakeven_stop_moves(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    pos = e.held("SOL-EUR", entry="100", stop="96")
    e.market.tickers["SOL-EUR"] = Decimal("105")  # >= entry + 1R
    await e.engine.tick()
    assert pos.stop == Decimal("100") * Decimal("1.005")


async def test_stale_pair_holds_and_marks(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    # Last 1h candle closed 3h ago → beyond the 75-minute freshness limit.
    old_end = NOW - 3 * HOUR
    e.market.series[("SOL-EUR", "1h")] = _series(
        800,
        HOUR,
        old_end,
        start_price=100.0,
        rate=1.0008,
        wick=0.006,
    )
    e.market.series[("SOL-EUR", "4h")] = _series(
        300,
        4 * HOUR,
        NOW,
        start_price=90.0,
        rate=1.002,
        wick=0.004,
    )
    e.market.series[("SOL-EUR", "1d")] = _series(
        300,
        DAY,
        NOW,
        start_price=70.0,
        rate=1.001,
        wick=0.003,
    )
    e.market._store("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows and rows[0].action == "HOLD" and "stale" in (rows[0].error or "")
    assert e.engine.prices["SOL-EUR"].stale
    # 1 initial + 3 retries for the missing closed candle.
    assert e.market.calls_1h["SOL-EUR"] == 4


async def test_kill_drawdown_flattens_and_blocks(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    # Big losing position: equity 1000 + 90*80 = 8200 vs peak 10000 → 18%.
    e.held("SOL-EUR", qty="90", entry="100", stop="50")
    e.market.tickers["SOL-EUR"] = Decimal("80")
    await e.engine.tick()
    assert e.engine.status == "killed"
    assert not e.broker.positions
    assert e.engine.kill is not None and "SOL-EUR" in e.engine.kill.closed
    with pytest.raises(KilledRefusal):
        e.engine.resume()
    await e.engine.acknowledge_kill()
    assert str(e.engine.status) == "paused"
    e.engine.resume()
    assert str(e.engine.status) == "running"


async def test_restart_paused_with_positions(env: Path) -> None:
    e1 = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e1.market.add_uptrend("SOL-EUR")
    e1.market.add_uptrend("BTC-EUR", base=60_000.0)
    e1.market.add_uptrend("USDC-EUR", base=0.92)
    await e1.engine.run_cycle()
    assert "SOL-EUR" in e1.broker.positions
    e1.db_engine.dispose()

    # "Restart": new broker + engine on the same database.
    e2 = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    assert "SOL-EUR" in e2.broker.positions
    assert e2.engine.status == "paused"


async def test_restart_refreshes_prices_while_paused(env: Path) -> None:
    """Spec restart behaviour: the backend refreshes prices at startup and
    waits Paused — pair badges show a live 24h change before any cycle."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    assert e.engine.status == "paused" and not e.engine.prices
    await e.engine.refresh_prices()
    info = e.engine.prices["SOL-EUR"]
    assert not info.stale
    assert info.last == e.market.series[("SOL-EUR", "1h")][-1].close
    assert info.chg_24h_pct != 0


async def test_restart_price_refresh_falls_back_to_stored(env: Path) -> None:
    """A failing source can't stop startup: stored candles still give a
    last price, marked stale when beyond the freshness limit; a pair with
    no data at all is simply skipped."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR", "LTC-EUR"])
    s = e.session()
    repo.upsert_candles(s, "SOL-EUR", "1h", _zigzag(30, HOUR, NOW - 3 * HOUR))
    s.commit()
    s.close()

    class FailingMarket(ScriptMarket):
        async def candles(
            self, market: str, interval: str, limit: int, **kw: object
        ) -> list[Candle]:
            raise MarketDataError("simulated outage")

    e.engine.client = FailingMarket(NOW)
    await e.engine.refresh_prices()
    info = e.engine.prices["SOL-EUR"]
    assert info.stale and info.last > 0
    assert "LTC-EUR" not in e.engine.prices
    assert e.engine.status == "paused"


async def test_pending_expires_at_next_cycle(env: Path) -> None:
    settings = Settings(active_markets=["SOL-EUR"], data_source="sim", execution_mode="manual")
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"], settings=settings)
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert e.engine.pending  # pending decision recorded, not filled
    assert "SOL-EUR" not in e.broker.positions
    first_id = next(iter(e.engine.pending))
    await e.engine.run_cycle()
    s = e.session()
    rows = _decisions(s)
    s.close()
    first = next(r for r in rows if r.id == first_id)
    assert first.status == "expired"


async def test_pending_expires_on_price_drift(env: Path) -> None:
    settings = Settings(active_markets=["SOL-EUR"], data_source="sim", execution_mode="manual")
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"], settings=settings)
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert e.engine.pending
    entry = next(iter(e.engine.pending.values())).plan.entry  # type: ignore[union-attr]
    e.market.tickers["SOL-EUR"] = entry * Decimal("1.01")  # > 0.5% drift
    await e.engine.tick()
    assert not e.engine.pending


async def test_supervised_executes_after_veto_window(env: Path) -> None:
    settings = Settings(active_markets=["SOL-EUR"], data_source="sim", execution_mode="supervised")
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"], settings=settings)
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert e.engine.pending and "SOL-EUR" not in e.broker.positions
    e.clock.advance(seconds=60)
    await e.engine.tick()
    assert not e.engine.pending and "SOL-EUR" in e.broker.positions


async def test_veto_prevents_supervised_fill(env: Path) -> None:
    settings = Settings(active_markets=["SOL-EUR"], data_source="sim", execution_mode="supervised")
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"], settings=settings)
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    decision_id = next(iter(e.engine.pending))
    decision = await e.engine.veto_pending(decision_id)
    e.clock.advance(seconds=60)
    await e.engine.tick()
    assert decision.status == "vetoed"
    assert "SOL-EUR" not in e.broker.positions


async def test_manual_approval_fills_immediately(env: Path) -> None:
    settings = Settings(active_markets=["SOL-EUR"], data_source="sim", execution_mode="manual")
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"], settings=settings)
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    decision_id = next(iter(e.engine.pending))
    decision = await e.engine.approve_pending(decision_id)
    assert decision.status == "approved"
    assert "SOL-EUR" in e.broker.positions


async def test_concurrent_manual_approvals_recheck_portfolio_risk(env: Path) -> None:
    settings = Settings(
        active_markets=["SOL-EUR", "ETH-EUR"],
        data_source="sim",
        execution_mode="manual",
        risk={"total_open_risk_pct": "1.5"},
    )
    e = EngineEnv(
        env / "agent.db",
        markets=["SOL-EUR", "ETH-EUR"],
        settings=settings,
    )
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("ETH-EUR", base=3000)
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert len(e.engine.pending) == 2
    await asyncio.gather(
        *(e.engine.approve_pending(decision_id) for decision_id in list(e.engine.pending))
    )
    cap = (
        Decimal(e.broker.account.start_balance) * settings.risk.total_open_risk_pct / Decimal("100")
    )
    assert e.broker.open_risk() <= cap


async def test_concurrent_auto_fills_recheck_portfolio_risk(env: Path) -> None:
    settings = Settings(
        active_markets=["SOL-EUR", "ETH-EUR"],
        data_source="sim",
        execution_mode="auto",
        risk={"total_open_risk_pct": "1.5"},
    )
    e = EngineEnv(
        env / "agent.db",
        markets=["SOL-EUR", "ETH-EUR"],
        settings=settings,
    )
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("ETH-EUR", base=3000)
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    cap = (
        Decimal(e.broker.account.start_balance) * settings.risk.total_open_risk_pct / Decimal("100")
    )
    assert e.broker.open_risk() <= cap


async def test_pause_resume_run_now(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.engine.resume()
    assert e.engine.status == "running"
    # Resume schedules the next run so the countdown shows right away.
    assert e.engine.next_run_at == NOW + timedelta(seconds=45)
    e.engine.pause()
    assert str(e.engine.status) == "paused"
    assert e.engine.next_run_at is None
    assert await e.engine.run_now()
    assert e.engine.cycle == 1
    # Still paused after a manual run: nothing is scheduled.
    assert e.engine.next_run_at is None


async def test_flatten_closes_and_pauses(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR")
    e.held("ETH-EUR")
    e.market.tickers["SOL-EUR"] = Decimal("100")
    e.market.tickers["ETH-EUR"] = Decimal("3000")
    e.engine.status = "running"
    closed = await e.engine.flatten()
    assert set(closed) == {"SOL-EUR", "ETH-EUR"}
    assert not e.broker.positions
    assert str(e.engine.status) == "paused"


async def test_flatten_keeps_position_when_price_is_unavailable(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    pos = e.held("SOL-EUR")
    e.market.tickers["SOL-EUR"] = Decimal("0")
    e.engine.status = "running"
    closed = await e.engine.flatten()
    assert closed == []
    assert e.broker.positions["SOL-EUR"] is pos
    assert str(e.engine.status) == "paused"
    s = e.session()
    trades = list(s.scalars(select(TradeRow)).all())
    s.close()
    assert trades == []


async def test_manual_close_refuses_a_missing_price(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    pos = e.held("SOL-EUR")
    e.market.tickers["SOL-EUR"] = Decimal("0")
    cash = e.broker.cash
    with pytest.raises(MarketDataError):
        await e.engine.close_position("SOL-EUR")
    assert e.broker.positions["SOL-EUR"] is pos
    assert e.broker.cash == cash


async def test_sell_decision_closes_position(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR")
    e.market.add_downtrend("SOL-EUR")
    e.market.add_downtrend("BTC-EUR")
    e.market.add_downtrend("USDC-EUR")
    await e.engine.run_cycle()
    assert "SOL-EUR" not in e.broker.positions


async def test_shock_brake_blocks_new_entries(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.engine.shock_brake_until = NOW + 2 * HOUR
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows[0].action == "SKIP" and rows[0].error == "Shock brake"


async def test_shock_brake_btc_move_triggers(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    # BTC ticker +5% inside the 60-minute window.
    e.market.tickers["BTC-EUR"] = Decimal("100")
    await e.engine.tick()
    e.clock.advance(minutes=30)
    e.market.tickers["BTC-EUR"] = Decimal("105")
    await e.engine.tick()
    assert e.engine.shock_brake_until is not None
    first_until = e.engine.shock_brake_until
    e.clock.advance(seconds=30)
    await e.engine.tick()
    # A new trigger inside the window extends the brake 2h from the last
    # trigger rather than expiring on the original clock.
    assert e.engine.shock_brake_until > first_until


async def test_shock_brake_survives_restart(env: Path) -> None:
    """The brake is persisted on the account row: a restart restores it
    instead of silently lifting it and re-alerting."""
    e1 = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e1.market.tickers["BTC-EUR"] = Decimal("100")
    await e1.engine.tick()
    e1.clock.advance(minutes=30)
    e1.market.tickers["BTC-EUR"] = Decimal("105")
    await e1.engine.tick()
    until = e1.engine.shock_brake_until
    assert until is not None
    e1.db_engine.dispose()

    e2 = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    session = e2.session()
    try:
        e2.engine._recover_startup(session)
        session.commit()
    finally:
        session.close()
    assert e2.engine.shock_brake_until == until


async def test_cycle_number_survives_restart(env: Path) -> None:
    """Cycle numbering is monotonic across restarts — decisions, snapshots
    and llm calls key on it."""
    e1 = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e1.market.add_zigzag("SOL-EUR")
    e1.market.add_zigzag("BTC-EUR")
    e1.market.add_zigzag("USDC-EUR")
    await e1.engine.run_cycle()
    await e1.engine.run_cycle()
    assert e1.engine.cycle == 2
    e1.db_engine.dispose()

    e2 = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e2.market.add_zigzag("SOL-EUR")
    e2.market.add_zigzag("BTC-EUR")
    e2.market.add_zigzag("USDC-EUR")
    session = e2.session()
    try:
        e2.engine._recover_startup(session)
        session.commit()
    finally:
        session.close()
    assert e2.engine.cycle == 2
    await e2.engine.run_cycle()
    assert e2.engine.cycle == 3
    s = e2.session()
    try:
        assert 3 in {row.cycle for row in _decisions(s)}
    finally:
        s.close()


async def test_throttle_halves_risk_after_three_losses(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    s = e.session()
    from app.db import TradeRow

    for i in range(3):
        s.add(
            TradeRow(
                account_id=1,
                pair="X-EUR",
                qty="1",
                entry="100",
                exit="90",
                opened_at=NOW - (10 - i) * HOUR,
                closed_at=NOW - (9 - i) * HOUR,
                fee_eur="0.5",
                realised_eur="-10",
                realised_r="-1",
                exit_reason="stop",
                decision_id=f"t{i}",
            )
        )
    s.commit()
    s.close()
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    e.market.series[("X-EUR", "1h")] = _zigzag(800, HOUR, NOW)
    e.held("X-EUR", qty="1", entry="100", stop="99")
    await e.engine.run_cycle()
    pos = e.broker.positions.get("SOL-EUR")
    assert pos is not None
    # Throttled: 0.5% of equity risk instead of 1%.
    risk = pos.qty * (pos.entry - pos.stop)
    assert risk <= Decimal("50.01")


@pytest.mark.parametrize(
    "cycles",
    [200, pytest.param(2000, marks=pytest.mark.slow)],
)
async def test_n_cycles_no_nan(env: Path, cycles: int) -> None:
    """Spec soak: N cycles on the simulated market, no NaN anywhere.

    200 runs in the normal suite; the 2000-cycle spec soak is ``slow``
    and runs via ``make soak``."""
    e = EngineEnv(
        env / "agent.db",
        markets=["SOL-EUR"],
        settings=Settings(
            active_markets=["SOL-EUR"],
            data_source="sim",
            execution_mode="auto",
        ),
    )
    # One long series covering the whole cycle span; the market serves
    # only bars already closed at the fake now.
    horizon = NOW + timedelta(seconds=cycles * 900 + 7200)
    e.market.series[("SOL-EUR", "1h")] = _series(
        1400,
        HOUR,
        horizon,
        start_price=100.0,
        rate=1.0002,
        wick=0.006,
    )
    e.market.series[("SOL-EUR", "4h")] = _series(
        500,
        4 * HOUR,
        horizon,
        start_price=90.0,
        rate=1.0008,
        wick=0.004,
    )
    e.market.series[("SOL-EUR", "1d")] = _series(
        300,
        DAY,
        horizon,
        start_price=70.0,
        rate=1.0003,
        wick=0.003,
    )
    e.market.series[("BTC-EUR", "1h")] = _series(
        1400,
        HOUR,
        horizon,
        start_price=60_000.0,
        rate=1.0002,
        wick=0.004,
    )
    e.market.series[("BTC-EUR", "4h")] = _series(
        500,
        4 * HOUR,
        horizon,
        start_price=55_000.0,
        rate=1.0008,
        wick=0.003,
    )
    e.market.series[("BTC-EUR", "1d")] = _series(
        300,
        DAY,
        horizon,
        start_price=45_000.0,
        rate=1.0003,
        wick=0.002,
    )
    e.market._store("SOL-EUR")
    e.market._store("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")

    def finite() -> bool:
        for v in (
            float(e.broker.equity()),
            float(e.broker.cash),
            float(e.broker.drawdown_pct()),
            float(e.broker.today_loss_pct(e.clock())),
        ):
            if not math.isfinite(v):
                return False
        rb = e.engine.risk_budget()
        for v in (
            float(rb.open_risk_pct),
            float(rb.today_loss_pct),
            float(rb.drawdown_pct),
            float(rb.risk_pct_now),
            rb.full_trades_left,
        ):
            if not math.isfinite(float(v)):
                return False
        return True

    for i in range(cycles):
        e.clock.advance(seconds=900)
        e.market.now = e.clock()
        await e.engine.run_cycle()
        if i % 8 == 0:
            await e.engine.tick()
        assert finite(), f"NaN in snapshot after cycle {i + 1}"
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows
    assert all(math.isfinite(r.confidence) for r in rows)


# ------------------------------------------------------------- openai path ==


class _StubOpenAIDecider:
    """A decider that answers like the OpenAI layer did: its draft carries
    the audit fields the decision row and llm_call link need."""

    name = "openai"

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.calls = 0

    async def decide(self, ctx: DecideContext) -> Draft:
        self.calls += 1
        session = self.sessions()
        try:
            row = LlmCallRow(
                id=f"stub-call-{self.calls}",
                at=ctx.now,
                cycle=ctx.cycle,
                account_id=1,
                pair=ctx.pair,
                decision_id=None,
                feature="decision",
                replay_of=None,
                model="gpt-4o-mini",
                prompt_version="decision.v1",
                status="ok",
                attempts="[]",
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
                cost_usd="0.0001",
                cost_eur="0.0001",
                request_id=None,
                request_json='{"input":"{}"}',
                response_raw="{}",
                parsed='{"action":"HOLD"}',
                input_checks="[]",
                output_checks="[]",
                verdict=None,
                verdict_note=None,
                verdict_at=None,
            )
            repo.add_llm_call(session, row)
            session.commit()
        finally:
            session.close()
        return Draft(
            action="HOLD",
            confidence=0.4,
            reasoning="model said hold",
            signals=[],
            prefilter=True,
            decider="openai",
            model="gpt-4o-mini",
            prompt_version="decision.v1",
            llm_call_id=f"stub-call-{self.calls}",
            raw_brief='{"input":"{}"}',
            raw_response="{}",
        )


async def test_openai_decider_path_records_model_and_links_call(env: Path) -> None:
    e = EngineEnv(
        env / "agent.db",
        markets=["SOL-EUR"],
        settings=Settings(
            active_markets=["SOL-EUR"],
            data_source="sim",
            execution_mode="auto",
            model={"decider": "openai"},
        ),
    )
    stub = _StubOpenAIDecider(e.sessions)
    e.engine.openai_decider = stub
    e.market.add_zigzag("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert stub.calls == 1
    s = e.session()
    try:
        rows = _decisions(s)
        assert len(rows) == 1
        row = rows[0]
        assert row.decider == "openai"
        assert row.model == "gpt-4o-mini"
        assert row.prompt_version == "decision.v1"
        assert row.prefilter is True
        assert row.raw_brief == '{"input":"{}"}'
        call = repo.get_llm_call(s, "stub-call-1")
        assert call is not None
        assert call.decision_id == row.id
    finally:
        s.close()


async def test_openai_decider_not_used_when_decider_is_rules(env: Path) -> None:
    e = EngineEnv(
        env / "agent.db",
        markets=["SOL-EUR"],
        settings=Settings(
            active_markets=["SOL-EUR"],
            data_source="sim",
            execution_mode="auto",
            model={"decider": "rules"},
        ),
    )
    stub = _StubOpenAIDecider(e.sessions)
    e.engine.openai_decider = stub
    e.market.add_zigzag("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert stub.calls == 0


# ------------------------------------------------------------- resilience --


class _CrashyDecider:
    """Decides like Rules except for one pair, where it blows up a set
    number of times before recovering."""

    name = "rules"

    def __init__(self, crash_on: str, crashes: int = 1) -> None:
        self._crash_on = crash_on
        self._left = crashes
        self._inner = RulesDecider()

    async def decide(self, ctx: DecideContext) -> Draft:
        if ctx.pair == self._crash_on and self._left > 0:
            self._left -= 1
            raise RuntimeError("decider exploded")
        return await self._inner.decide(ctx)


async def test_pair_crash_records_a_hold_and_the_cycle_lives(env: Path) -> None:
    """A pair whose decider raises ends as a recorded HOLD with the error;
    the other pair still decides and the next cycle runs normally."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR", "ETH-EUR"])
    e.engine.decider = _CrashyDecider("ETH-EUR")
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("ETH-EUR", base=3_000.0)
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    await e.engine.run_cycle()
    assert "SOL-EUR" in e.broker.positions
    assert "ETH-EUR" not in e.broker.positions
    s = e.session()
    rows = {r.pair: r for r in _decisions(s)}
    errors = list(s.scalars(select(ErrorRow).where(ErrorRow.pair == "ETH-EUR")))
    s.close()
    assert rows["ETH-EUR"].action == "HOLD"
    assert "cycle error" in (rows["ETH-EUR"].error or "")
    assert errors and errors[0].affected_decision_or_fill
    # The next cycle runs: the crash did not poison the engine.
    e.clock.advance(minutes=15)
    await e.engine.run_cycle()
    assert e.engine.cycle == 2
    s = e.session()
    eth = [r for r in _decisions(s) if r.pair == "ETH-EUR"]
    s.close()
    assert len(eth) == 2 and eth[1].action == "BUY"


async def test_scheduler_survives_a_crashed_cycle(env: Path) -> None:
    """An exception out of run_cycle is recorded as an error and the
    scheduler keeps running — the next cycle still trades."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    real_cycle = e.engine.run_cycle
    calls = 0

    async def flaky_cycle() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Mimic run_cycle's finally: leave next_run_at sane.
            e.engine.next_run_at = e.engine._next_due(e.clock() + timedelta(seconds=1))
            raise RuntimeError("boom")
        await real_cycle()
        if calls >= 3:
            e.engine._stop.set()

    e.engine.run_cycle = flaky_cycle  # type: ignore[method-assign]
    e.engine.status = "running"
    e.engine.next_run_at = e.clock() - timedelta(seconds=1)

    async def sleeper(seconds: float) -> None:
        e.clock.advance(seconds=max(seconds, 1.0))

    e.engine._sleep = sleeper
    await e.engine._scheduler_loop()
    assert calls == 3
    assert "SOL-EUR" in e.broker.positions
    s = e.session()
    errors = list(s.scalars(select(ErrorRow)).all())
    s.close()
    assert errors and "boom" in errors[0].text
    assert e.engine.status == "running"


# ----------------------------------------------------------- eligibility ---


async def test_failing_market_takes_no_entries(env: Path) -> None:
    """An active market that fails the eligibility check stays in the
    cycle — price, 24h change and a recorded decision keep flowing — but
    a BUY ends as SKIP named by the eligibility gate."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    s = e.session()
    row = s.get(MarketRow, "SOL-EUR")
    assert row is not None
    row.eligible = False
    row.failed_rule = "volume"
    # Full history so warming_up doesn't hide the eligibility gate.
    repo.upsert_candles(s, "SOL-EUR", "1h", _zigzag(800, HOUR, NOW))
    repo.upsert_candles(s, "SOL-EUR", "4h", _zigzag(300, 4 * HOUR, NOW))
    repo.upsert_candles(s, "SOL-EUR", "1d", _zigzag(300, DAY, NOW))
    s.commit()
    s.close()
    await e.engine.run_cycle()
    assert "SOL-EUR" not in e.broker.positions
    # The cycle still ran: the pair got its price and 24h change — dropping
    # it from the cycle froze both at nothing and showed +0.00% in the GUI.
    assert "SOL-EUR" in e.engine.prices
    assert not e.engine.prices["SOL-EUR"].stale
    assert e.engine.prices["SOL-EUR"].chg_24h_pct != 0
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows[0].action == "SKIP" and rows[0].error == "Market eligibility"


async def test_held_position_on_failing_market_still_gets_decided(env: Path) -> None:
    """A failing market takes no new entries, but an open position on it
    keeps its cycle — the pair still gets its decision call so SELL stays
    possible."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR")
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    s = e.session()
    row = s.get(MarketRow, "SOL-EUR")
    assert row is not None
    row.eligible = False
    row.failed_rule = "volume"
    s.commit()
    s.close()
    await e.engine.run_cycle()
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows and rows[0].pair == "SOL-EUR" and rows[0].action == "HOLD"
    assert "SOL-EUR" in e.broker.positions


async def test_held_position_on_deactivated_market_still_gets_decided(env: Path) -> None:
    """Deactivation removes the market from settings and flips its row
    inactive, but an open position keeps its cycle until it closes."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR")
    e.market.add_uptrend("SOL-EUR")
    e.market.add_uptrend("BTC-EUR", base=60_000.0)
    e.market.add_uptrend("USDC-EUR", base=0.92)
    s = e.session()
    row = s.get(MarketRow, "SOL-EUR")
    assert row is not None
    row.active = False
    s.commit()
    s.close()
    e.settings.active_markets = []
    await e.engine.run_cycle()
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows and rows[0].pair == "SOL-EUR" and rows[0].action == "HOLD"
    assert "SOL-EUR" in e.broker.positions


# ------------------------------------------------- invalidation re-eval ---


class _SellDecider:
    """Always answers SELL — the draft that deadlocked the watcher when an
    invalidation re-evaluation ran inside the execution lock."""

    name = "rules"

    async def decide(self, ctx: DecideContext) -> Draft:
        return Draft(
            action="SELL",
            confidence=0.9,
            reasoning="test sell",
            signals=[],
            decider="rules",
        )


async def test_invalidation_reeval_sell_does_not_deadlock(env: Path) -> None:
    """Crossing a position's invalidation price queues a re-evaluation that
    runs AFTER the execution lock is released. When it ran inside the tick,
    a SELL draft re-acquired the lock and froze the whole engine."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_zigzag("SOL-EUR")
    e.engine.decider = _SellDecider()
    ticker = e.market.tickers["SOL-EUR"]
    pos = e.held("SOL-EUR", qty="1", entry="100", stop="1", target="100000")
    pos.invalidation_price = ticker + Decimal("1")
    await asyncio.wait_for(e.engine.tick(), timeout=15)
    assert "SOL-EUR" not in e.broker.positions
    s = e.session()
    rows = _decisions(s)
    s.close()
    assert rows and rows[0].action == "SELL" and rows[0].status == "executed"
