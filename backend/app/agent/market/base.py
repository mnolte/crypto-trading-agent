"""Shared market-data types and the client protocol.

``BitvavoClient`` and ``SimMarket`` both implement ``MarketClient``; the
engine, the eligibility check and tests never care which one is active.
Candle prices stay ``Decimal`` — they are parsed from exchange strings and
only become floats inside indicator maths.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.models import Tf

TF_SECONDS: dict[Tf, int] = {"1h": 3600, "4h": 14400, "1d": 86400}


class MarketDataError(Exception):
    """Any fetch failure. The engine treats it as "mark pair stale"."""


class RateLimitError(MarketDataError):
    """HTTP 429 after the back-off retries are spent."""


class UpstreamError(MarketDataError):
    """A non-429 HTTP error or an unparsable response."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Candle(BaseModel):
    """One OHLCV bar. ``open_time`` is the bar's open in UTC."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # base asset

    def close_time(self, tf: Tf) -> datetime:
        return self.open_time + timedelta(seconds=TF_SECONDS[tf])


class BookLevel(BaseModel):
    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    market: str
    bids: list[BookLevel]
    asks: list[BookLevel]

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread_pct(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None or self.best_bid == 0:
            return None
        mid = (self.best_bid + self.best_ask) / 2
        return (self.best_ask - self.best_bid) / mid * 100


class Ticker24h(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str
    last: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None  # base, 24h
    volume_quote: Decimal | None = None  # quote (EUR), 24h
    bid: Decimal | None = None
    ask: Decimal | None = None

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid == 0:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100


class MarketInfo(BaseModel):
    """Per-market trading rules from GET /markets.

    Sizing and paper fills respect these; extra exchange fields are ignored.
    """

    model_config = ConfigDict(extra="ignore")

    market: str
    status: str  # "trading", "halted", "auction", "delisting", ...
    base: str
    quote: str
    min_order_base: Decimal | None = None
    min_order_quote: Decimal | None = None
    tick_size: Decimal | None = None
    quantity_decimals: int | None = None


RateLimitHook = Callable[[int | None, datetime | None], None]
SourceHook = Callable[[str | None], None]


class MarketClient(Protocol):
    """The interface every market source implements."""

    async def candles(
        self,
        market: str,
        interval: Tf,
        limit: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Candles ascending by open_time, including any still-forming last bar."""
        ...

    async def order_book(self, market: str, depth: int = 50) -> OrderBook: ...

    async def ticker_price(
        self, market: str, *, max_429_wait_s: float | None = None
    ) -> Decimal: ...

    async def ticker_24h(self, market: str | None = None) -> list[Ticker24h]: ...

    async def ticker_prices(self) -> dict[str, Decimal]:
        """Last price for every market in one request — the Universe
        screen refreshes prices at most once a minute (item 026)."""
        ...

    async def markets(self) -> list[MarketInfo]: ...

    async def assets(self) -> dict[str, str]:
        """``{symbol: name}`` from the exchange's public asset list —
        the display names on the Universe screen. A missing name stays
        absent; the screen shows "name pending"."""
        ...

    async def aclose(self) -> None: ...


def drop_forming(candles: list[Candle], tf: Tf, now: datetime) -> list[Candle]:
    """Drop candles whose close time is still in the future.

    Signals are computed on closed candles only; the still-forming last bar
    must never reach the indicator maths.
    """
    now = _utc(now)
    return [c for c in candles if _utc(c.open_time) + timedelta(seconds=TF_SECONDS[tf]) <= now]


def is_closed(candle: Candle, tf: Tf, now: datetime) -> bool:
    return _utc(candle.open_time) + timedelta(seconds=TF_SECONDS[tf]) <= _utc(now)


def closed_through(candles: list[Candle], tf: Tf, now: datetime) -> list[Candle]:
    """Alias kept for readability at call sites."""
    return drop_forming(candles, tf, now)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
