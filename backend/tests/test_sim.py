"""The simulated market: deterministic per seed, sane OHLC, same interface
as Bitvavo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agent.market.base import drop_forming
from app.agent.market.sim import SimMarket

NOW = datetime(2026, 10, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


async def test_deterministic_per_seed() -> None:
    a = SimMarket(seed=5, now=NOW)
    b = SimMarket(seed=5, now=NOW)
    ca = await a.candles("BTC-EUR", "1h", 100)
    cb = await b.candles("BTC-EUR", "1h", 100)
    assert [c.close for c in ca] == [c.close for c in cb]

    other = SimMarket(seed=6, now=NOW)
    co = await other.candles("BTC-EUR", "1h", 100)
    assert [c.close for c in ca] != [c.close for c in co]


async def test_candles_are_sane_and_timed() -> None:
    sim = SimMarket(seed=3, now=NOW)
    candles = await sim.candles("BTC-EUR", "1h", 50)
    assert len(candles) == 50
    for i, c in enumerate(candles):
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.volume > 0
        if i:
            assert c.open_time - candles[i - 1].open_time == timedelta(hours=1)
    # Next bar's open is the previous close — a continuous walk.
    assert candles[-1].open == candles[-2].close


async def test_timeframes_agree() -> None:
    sim = SimMarket(seed=3, now=NOW)
    h1 = drop_forming(await sim.candles("BTC-EUR", "1h", 200), "1h", sim.now)
    h4 = drop_forming(await sim.candles("BTC-EUR", "4h", 60), "4h", sim.now)
    d1 = drop_forming(await sim.candles("BTC-EUR", "1d", 30), "1d", sim.now)
    # The last closed 4h bar's close equals the matching 1h close.
    last_4h = h4[-1]
    matching_1h = [c for c in h1 if last_4h.open_time <= c.open_time < last_4h.close_time("4h")]
    assert len(matching_1h) == 4
    assert matching_1h[-1].close == last_4h.close
    assert sum(c.volume for c in matching_1h) == last_4h.volume
    # Daily bars land on UTC midnight.
    assert all(c.open_time.hour == 0 and c.open_time.minute == 0 for c in d1)


async def test_ticker_book_markets() -> None:
    sim = SimMarket(seed=3, now=NOW)
    price = await sim.ticker_price("BTC-EUR")
    assert price > 0
    book = await sim.order_book("BTC-EUR")
    assert book.best_bid and book.best_ask and book.best_ask > book.best_bid
    tickers = await sim.ticker_24h()
    markets = {t.market for t in tickers}
    assert "BTC-EUR" in markets and "USDC-EUR" in markets
    infos = await sim.markets()
    assert all(i.quote == "EUR" and i.status == "trading" for i in infos)


async def test_history_limits_respected() -> None:
    sim = SimMarket(seed=3, now=NOW)
    assert len(await sim.candles("BTC-EUR", "1h", 800)) == 800
    assert len(await sim.candles("BTC-EUR", "4h", 300)) == 300
    assert len(await sim.candles("BTC-EUR", "1d", 300)) == 300
    last = drop_forming(await sim.candles("BTC-EUR", "1h", 800), "1h", sim.now)[-1]
    # A candle whose close lands exactly on `now` is closed, not forming.
    assert last.close_time("1h") <= sim.now < last.close_time("1h") + HOUR


async def test_advance_extends_walk() -> None:
    sim = SimMarket(seed=3, now=NOW)
    before = await sim.candles("BTC-EUR", "1h", 5)
    sim.advance(3600 * 3)
    after = await sim.candles("BTC-EUR", "1h", 5)
    assert after[-1].open_time > before[-1].open_time


async def test_different_markets_differ() -> None:
    sim = SimMarket(seed=3, now=NOW)
    a = await sim.candles("BTC-EUR", "1h", 50)
    b = await sim.candles("ETH-EUR", "1h", 50)
    assert [c.close for c in a] != [c.close for c in b]
