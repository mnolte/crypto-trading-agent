"""Bitvavo public REST client (no API key; market data only).

- Base URL ``https://api.bitvavo.com/v2``. Read-only GET endpoints.
- The API is weight-limited per IP per minute; every response carries
  ``bitvavo-ratelimit-remaining`` / ``-resetat`` headers, which we track so
  the source registry can show usage and the engine stays far below the cap.
- On HTTP 429 we wait until the reset timestamp and retry, up to
  ``max_429_retries``; anything else is an ``UpstreamError``.
- Candle timestamps arrive as epoch milliseconds; prices and volumes as
  strings. Candles are sorted ascending by open time — order is not assumed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agent.market.base import (
    Candle,
    MarketDataError,
    MarketInfo,
    OrderBook,
    RateLimitError,
    Ticker24h,
    UpstreamError,
)
from app.errors import plain_error
from app.models import Tf

BASE_URL = "https://api.bitvavo.com/v2"
MAX_CANDLE_PAGE = 1000  # Bitvavo's own per-request cap


class _BookResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str
    bids: list[tuple[Decimal, Decimal]] = []
    asks: list[tuple[Decimal, Decimal]] = []


class _TickerPriceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str
    price: Decimal


class _MarketResponseItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str
    status: str
    base: str
    quote: str
    min_order_base: Decimal | None = None
    min_order_quote: Decimal | None = None
    tick_size: Decimal | None = None
    quantity_decimals: int | None = None

    @classmethod
    def model_validate_bitvavo(cls, raw: dict[str, Any]) -> _MarketResponseItem:
        return cls(
            market=raw["market"],
            status=raw.get("status", "unknown"),
            base=raw.get("base", ""),
            quote=raw.get("quote", ""),
            min_order_base=raw.get("minOrderInBaseAsset"),
            min_order_quote=raw.get("minOrderInQuoteAsset"),
            tick_size=raw.get("tickSize"),
            quantity_decimals=raw.get("quantityDecimals"),
        )


class _Ticker24hItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str
    last: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    volumeQuote: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None


class _TimeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: int  # epoch ms


class BitvavoClient:
    """Async httpx client for the public Bitvavo endpoints we use.

    ``on_event`` is called with ``None`` on every successful request and the
    exception on every failure, so the source registry can track health.
    """

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        max_429_retries: int = 2,
        on_event: Callable[[BaseException | None], None] | None = None,
        sleep: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._max_429_retries = max_429_retries
        self.on_event = on_event
        self._sleep = sleep
        self.rate_limit_remaining: int | None = None
        self.rate_limit_limit: int | None = None
        self.rate_limit_reset_at: datetime | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ http

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_429_wait_s: float | None = None,
    ) -> Any:
        attempts = 0
        while True:
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                self._event(exc)
                raise MarketDataError(f"bitvavo request failed: {plain_error(exc)}") from exc
            self._track_rate_limit(response)
            if response.status_code == 429:
                if attempts >= self._max_429_retries:
                    error = RateLimitError("bitvavo rate limit exceeded")
                    self._event(error)
                    raise error
                attempts += 1
                await self._sleep(self._seconds_until_reset(max_429_wait_s))
                continue
            if response.status_code != 200:
                upstream = UpstreamError(
                    f"bitvavo {response.status_code} for {path}: {_error_text(response)}",
                    status=response.status_code,
                )
                self._event(upstream)
                raise upstream
            self._event(None)
            return response.json()

    def _track_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("bitvavo-ratelimit-remaining")
        limit = response.headers.get("bitvavo-ratelimit-limit")
        reset_at = response.headers.get("bitvavo-ratelimit-resetat")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
        if limit is not None:
            self.rate_limit_limit = int(limit)
        if reset_at is not None:
            self.rate_limit_reset_at = datetime.fromtimestamp(int(reset_at) / 1000, UTC)

    def _seconds_until_reset(self, cap: float | None = None) -> float:
        if self.rate_limit_reset_at is None:
            return min(5.0, cap) if cap is not None else 5.0
        delay = (self.rate_limit_reset_at - datetime.now(UTC)).total_seconds()
        wait = max(0.5, delay + 0.25)
        return min(wait, cap) if cap is not None else wait

    def _event(self, error: BaseException | None) -> None:
        if self.on_event is not None:
            self.on_event(error)

    # ------------------------------------------------------------- endpoints

    async def candles(
        self,
        market: str,
        interval: Tf,
        limit: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Candles ascending by open_time. Pages with ``end`` when the
        requested limit exceeds the API's per-request cap."""
        collected: list[Candle] = []
        cursor = end
        while len(collected) < limit:
            page_size = min(MAX_CANDLE_PAGE, limit - len(collected))
            params: dict[str, Any] = {"interval": interval, "limit": page_size}
            if start is not None:
                params["start"] = int(start.timestamp() * 1000)
            if cursor is not None:
                params["end"] = int(cursor.timestamp() * 1000) - 1
            raw = await self._get(f"/{market}/candles", params)
            page = _parse_candles(raw)
            if not page:
                break
            if cursor is None:
                collected = page
            else:
                collected = page + collected
            # Next page ends just before the earliest bar we already have.
            cursor = collected[0].open_time
            if len(page) < page_size:
                break
        seen: set[datetime] = set()
        unique: list[Candle] = []
        for candle in collected:
            if candle.open_time not in seen:
                seen.add(candle.open_time)
                unique.append(candle)
        if start is not None:
            unique = [c for c in unique if c.open_time >= start]
        return sorted(unique, key=lambda c: c.open_time)

    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        raw = await self._get(f"/{market}/book", {"depth": depth})
        try:
            book = _BookResponse.model_validate(raw)
        except ValidationError as exc:
            raise UpstreamError(f"unparsable order book for {market}") from exc
        from app.agent.market.base import BookLevel

        return OrderBook(
            market=book.market,
            bids=[BookLevel(price=p, size=s) for p, s in book.bids],
            asks=[BookLevel(price=p, size=s) for p, s in book.asks],
        )

    async def ticker_price(self, market: str, *, max_429_wait_s: float | None = None) -> Decimal:
        raw = await self._get("/ticker/price", {"market": market}, max_429_wait_s=max_429_wait_s)
        try:
            return _TickerPriceResponse.model_validate(raw).price
        except ValidationError as exc:
            raise UpstreamError(f"unparsable ticker price for {market}") from exc

    async def ticker_24h(self, market: str | None = None) -> list[Ticker24h]:
        params = {"market": market} if market else None
        raw = await self._get("/ticker/24h", params)
        items = raw if isinstance(raw, list) else [raw]
        try:
            return [
                Ticker24h(
                    market=i.market,
                    last=i.last,
                    open=i.open,
                    high=i.high,
                    low=i.low,
                    volume=i.volume,
                    volume_quote=i.volumeQuote,
                    bid=i.bid,
                    ask=i.ask,
                )
                for i in (_Ticker24hItem.model_validate(r) for r in items)
            ]
        except ValidationError as exc:
            raise UpstreamError("unparsable ticker/24h response") from exc

    async def ticker_prices(self) -> dict[str, Decimal]:
        """GET /ticker/price with no market → ``{market: price}`` for
        every market in one call."""
        raw = await self._get("/ticker/price")
        items = raw if isinstance(raw, list) else [raw]
        out: dict[str, Decimal] = {}
        for item in items:
            try:
                out[str(item["market"])] = Decimal(str(item["price"]))
            except (KeyError, TypeError, ArithmeticError) as exc:
                raise UpstreamError("unparsable ticker/price response") from exc
        return out

    async def markets(self) -> list[MarketInfo]:
        raw = await self._get("/markets")
        try:
            return [
                MarketInfo(
                    market=i.market,
                    status=i.status,
                    base=i.base,
                    quote=i.quote,
                    min_order_base=i.min_order_base,
                    min_order_quote=i.min_order_quote,
                    tick_size=i.tick_size,
                    quantity_decimals=i.quantity_decimals,
                )
                for i in (_MarketResponseItem.model_validate_bitvavo(r) for r in raw)
            ]
        except (ValidationError, KeyError) as exc:
            raise UpstreamError("unparsable /markets response") from exc

    async def assets(self) -> dict[str, str]:
        """GET /assets → ``{symbol: name}`` for display names."""
        raw = await self._get("/assets")
        try:
            return {
                str(item["symbol"]): str(item["name"])
                for item in raw
                if isinstance(item, dict) and item.get("symbol") and item.get("name")
            }
        except (TypeError, KeyError) as exc:
            raise UpstreamError("unparsable /assets response") from exc

    async def time(self) -> datetime:
        raw = await self._get("/time")
        return datetime.fromtimestamp(_TimeResponse.model_validate(raw).time / 1000, UTC)


def _parse_candles(raw: Any) -> list[Candle]:
    """[ts_ms, "open", "high", "low", "close", "volume"] -> Candle, ascending."""
    try:
        candles = [
            Candle(
                open_time=datetime.fromtimestamp(int(row[0]) / 1000, UTC),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
            )
            for row in raw
        ]
    except (IndexError, TypeError, ValueError, ArithmeticError) as exc:
        raise UpstreamError("unparsable candles response") from exc
    return sorted(candles, key=lambda c: c.open_time)


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("error") or body.get("errorCode") or body)
    except ValueError:
        pass
    return response.text[:200]
