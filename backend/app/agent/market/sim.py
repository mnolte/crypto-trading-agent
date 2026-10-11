"""Seeded simulated market with the same interface as the Bitvavo client.

Used by tests and selectable in Settings for offline demos (the GUI shows a
SIMULATED badge while active). Each market is a lognormal random walk with a
realistic per-pair hourly volatility, generated lazily and deterministically
from ``seed`` — the same seed always produces the same prices.

Time is controlled by ``set_now`` / ``advance`` so engine tests can run on a
fake clock. 1h bars are the source of truth; 4h and 1d bars are aggregated
from them so the timeframes always agree.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np

from app.agent.market.base import (
    BookLevel,
    Candle,
    MarketInfo,
    OrderBook,
    Ticker24h,
    drop_forming,
)
from app.models import DEFAULT_MARKETS, Tf

GENESIS = datetime(2025, 1, 1, tzinfo=UTC)
DEFAULT_NOW = datetime(2026, 10, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)

# (start price in EUR, hourly volatility, average hourly base volume)
_SIM_PARAMS: dict[str, tuple[float, float, float]] = {
    "BTC-EUR": (60_000.0, 0.006, 6.0),
    "ETH-EUR": (3_000.0, 0.008, 60.0),
    "SOL-EUR": (150.0, 0.012, 900.0),
    "XRP-EUR": (0.55, 0.010, 200_000.0),
    "ADA-EUR": (0.45, 0.011, 250_000.0),
    "LINK-EUR": (13.0, 0.011, 8_000.0),
    "AVAX-EUR": (30.0, 0.013, 4_000.0),
    "DOGE-EUR": (0.12, 0.014, 800_000.0),
    "LTC-EUR": (70.0, 0.009, 1_500.0),
    "USDC-EUR": (0.92, 0.0008, 500_000.0),
}
_FALLBACK_PARAMS = (1.0, 0.012, 1_000.0)


def _stable_seed(seed: int, market: str) -> int:
    digest = hashlib.sha256(f"{seed}:{market}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**32)


class _MarketWalk:
    """Lazily extended 1h random walk for one simulated market."""

    def __init__(self, seed: int, market: str) -> None:
        price, vol, base_vol = _SIM_PARAMS.get(market, _FALLBACK_PARAMS)
        self.rng = np.random.default_rng(_stable_seed(seed, market))
        self.vol = vol
        self.base_vol = base_vol
        self.candles: list[Candle] = []
        self.next_open = GENESIS
        self.next_close = price

    def ensure(self, through: datetime) -> None:
        """Generate 1h bars until the walk covers ``through``."""
        while self.next_open <= through:
            self._append(self.next_open)

    def _append(self, open_time: datetime) -> None:
        rng = self.rng
        open_ = self.next_close
        ret = float(rng.normal(0.0, self.vol))
        close = open_ * math.exp(ret)
        span = abs(float(rng.normal(0.0, self.vol))) * 0.5 + self.vol * 0.2
        high = max(open_, close) * math.exp(span * float(rng.uniform(0.4, 1.0)))
        low = min(open_, close) * math.exp(-span * float(rng.uniform(0.4, 1.0)))
        volume = self.base_vol * math.exp(float(rng.normal(0.0, 0.5)))
        self.candles.append(
            Candle(
                open_time=open_time,
                open=Decimal(f"{open_:.10g}"),
                high=Decimal(f"{high:.10g}"),
                low=Decimal(f"{low:.10g}"),
                close=Decimal(f"{close:.10g}"),
                volume=Decimal(f"{volume:.10g}"),
            )
        )
        self.next_close = close
        self.next_open = open_time + HOUR


def _aggregate(candles: list[Candle], tf: Tf) -> list[Candle]:
    """Aggregate 1h bars into 4h or 1d bars (aligned to UTC boundaries)."""

    def bucket(c: Candle) -> datetime:
        t = c.open_time
        if tf == "4h":
            return t.replace(hour=(t.hour // 4) * 4, minute=0, second=0, microsecond=0)
        return t.replace(hour=0, minute=0, second=0, microsecond=0)

    out: list[Candle] = []
    for c in candles:
        start = bucket(c)
        if out and out[-1].open_time == start:
            last = out[-1]
            out[-1] = Candle(
                open_time=start,
                open=last.open,
                high=max(last.high, c.high),
                low=min(last.low, c.low),
                close=c.close,
                volume=last.volume + c.volume,
            )
        else:
            out.append(
                Candle(
                    open_time=start,
                    open=c.open,
                    high=c.high,
                    low=c.low,
                    close=c.close,
                    volume=c.volume,
                )
            )
    return out


class SimMarket:
    """A ``MarketClient`` backed by seeded random walks."""

    def __init__(
        self,
        seed: int = 1,
        markets: list[str] | None = None,
        now: datetime | None = DEFAULT_NOW,
    ) -> None:
        self.seed = seed
        # ``now=None`` tracks the wall clock (the app factory uses this for
        # demos/e2e); a datetime pins the walk to a fixed instant (tests).
        self._now = (now if now.tzinfo is not None else now.replace(tzinfo=UTC)) if now else None
        self.market_names = list(markets or [*DEFAULT_MARKETS, "USDC-EUR"])
        self._walks = {m: _MarketWalk(seed, m) for m in self.market_names}

    # -- fake clock ---------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def set_now(self, now: datetime) -> None:
        self._now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self._now = self.now + timedelta(seconds=seconds)

    # -- MarketClient --------------------------------------------------------

    async def aclose(self) -> None:
        pass

    def _walk(self, market: str) -> _MarketWalk:
        if market not in self._walks:
            self._walks[market] = _MarketWalk(self.seed, market)
            if market not in self.market_names:
                self.market_names.append(market)
        return self._walks[market]

    async def candles(
        self,
        market: str,
        interval: Tf,
        limit: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        walk = self._walk(market)
        needed = end or self.now
        if start is not None:
            needed = max(needed, start)
        walk.ensure(needed)
        bars: list[Candle] = walk.candles
        if interval != "1h":
            bars = _aggregate(bars, interval)
        if start is not None:
            bars = [c for c in bars if c.open_time >= start]
        if end is not None:
            bars = [c for c in bars if c.open_time <= end]
        else:
            bars = [c for c in bars if c.open_time <= self.now]
        return bars[-limit:]

    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        last = float(await self.ticker_price(market))
        bids = [
            BookLevel(
                price=Decimal(f"{last * (1 - 0.0005 * i):.10g}"),
                size=Decimal("1.5"),
            )
            for i in range(1, depth + 1)
        ]
        asks = [
            BookLevel(
                price=Decimal(f"{last * (1 + 0.0005 * i):.10g}"),
                size=Decimal("1.5"),
            )
            for i in range(1, depth + 1)
        ]
        return OrderBook(market=market, bids=bids, asks=asks)

    async def ticker_price(self, market: str) -> Decimal:
        closed = drop_forming(await self.candles(market, "1h", 2), "1h", self.now)
        if not closed:
            return Decimal("0")
        return closed[-1].close

    async def ticker_24h(self, market: str | None = None) -> list[Ticker24h]:
        names = [market] if market else self.market_names
        out: list[Ticker24h] = []
        for name in names:
            closed = drop_forming(await self.candles(name, "1h", 25), "1h", self.now)
            if not closed:
                continue
            day = closed[-24:]
            last = closed[-1].close
            out.append(
                Ticker24h(
                    market=name,
                    last=last,
                    open=day[0].open,
                    high=max(c.high for c in day),
                    low=min(c.low for c in day),
                    volume=sum(c.volume for c in day),
                    volume_quote=sum(c.volume * c.close for c in day),
                    bid=last * Decimal("0.9995"),
                    ask=last * Decimal("1.0005"),
                )
            )
        return out

    async def markets(self) -> list[MarketInfo]:
        return [
            MarketInfo(
                market=name,
                status="trading",
                base=name.split("-")[0],
                quote="EUR",
                min_order_base=Decimal("0.001"),
                min_order_quote=Decimal("5"),
                tick_size=Decimal("0.01"),
                quantity_decimals=4,
            )
            for name in self.market_names
        ]
