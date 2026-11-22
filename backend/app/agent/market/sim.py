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

import copy
import hashlib
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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

# Display names for the simulated markets (GET /assets equivalent).
_SIM_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "XRP": "XRP",
    "ADA": "Cardano",
    "LINK": "Chainlink",
    "AVAX": "Avalanche",
    "DOGE": "Dogecoin",
    "LTC": "Litecoin",
    "USDC": "USD Coin",
}


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
            # model_construct: values are already typed; skipping validation
            # matters when a walk generates ~16k bars at once.
            Candle.model_construct(
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

    def clone(self) -> _MarketWalk:
        """A copy that continues the same walk: same bars, same rng stream.

        Candle objects are immutable, so the history list is shared; the rng
        state and the frontier are copied, making the clone indistinguishable
        from a walk generated from scratch.
        """
        walk = _MarketWalk.__new__(_MarketWalk)
        walk.rng = np.random.default_rng(0)
        walk.rng.bit_generator.state = copy.deepcopy(self.rng.bit_generator.state)
        walk.vol = self.vol
        walk.base_vol = self.base_vol
        walk.candles = list(self.candles)
        walk.next_open = self.next_open
        walk.next_close = self.next_close
        return walk


# Per-process template walks keyed by (seed, market). Generating the hourly
# history from GENESIS is the slow part of a SimMarket, so the first walk to
# reach a given frontier becomes the template: later SimMarket instances clone
# it instead of replaying thousands of bars. Cloning preserves the rng stream,
# so prices stay identical to generating each instance from scratch.
_WALK_TEMPLATES: dict[tuple[int, str], _MarketWalk] = {}


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
            out[-1] = Candle.model_construct(
                open_time=start,
                open=last.open,
                high=max(last.high, c.high),
                low=min(last.low, c.low),
                close=c.close,
                volume=last.volume + c.volume,
            )
        else:
            out.append(
                Candle.model_construct(
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
        self._walks: dict[str, _MarketWalk] = {}

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
            key = (self.seed, market)
            template = _WALK_TEMPLATES.get(key)
            if template is None:
                template = _MarketWalk(self.seed, market)
                _WALK_TEMPLATES[key] = template
            self._walks[market] = template.clone()
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
        needed = end or self.now
        if start is not None:
            needed = max(needed, start)
        walk = self._walk(market)
        walk.ensure(needed)
        key = (self.seed, market)
        template = _WALK_TEMPLATES[key]
        if walk.next_open > template.next_open:
            # The clone outran the template: adopt it so later SimMarket
            # instances start from the longer history.
            _WALK_TEMPLATES[key] = walk.clone()
        bars: list[Candle] = walk.candles
        if interval != "1h":
            # The template can hold bars beyond `needed`; a fresh walk would
            # stop there, so aggregate only what it would have generated to
            # keep the trailing (forming) bucket identical. Only the newest
            # `limit` buckets survive the trailing cut, so hours that cannot
            # contribute to them are skipped (+1 bucket for a partial first
            # bucket at the cut boundary).
            per_bucket = 4 if interval == "4h" else 24
            hourly = [c for c in bars if c.open_time <= needed]
            bars = _aggregate(hourly[-(limit + 1) * per_bucket :], interval)
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

    async def ticker_price(
        self, market: str, *, max_429_wait_s: float | None = None, **_kwargs: Any
    ) -> Decimal:
        closed = drop_forming(await self.candles(market, "1h", 2), "1h", self.now)
        if not closed:
            return Decimal("0")
        return closed[-1].close

    async def ticker_prices(self) -> dict[str, Decimal]:
        return {name: await self.ticker_price(name) for name in self.market_names}

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

    async def assets(self) -> dict[str, str]:
        return {
            name.split("-")[0]: _SIM_NAMES.get(name.split("-")[0], name.split("-")[0])
            for name in self.market_names
        }

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
