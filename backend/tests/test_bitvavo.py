"""Bitvavo client: response parsing, sorting, rate-limit headers, 429
back-off, pagination and error mapping. Everything is respx-mocked — no
real network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.agent.market.base import MarketDataError, RateLimitError, UpstreamError
from app.agent.market.bitvavo import BASE_URL, BitvavoClient

MS = 1_700_000_000_000  # fixed epoch ms fixture


def candle_row(i: int, price: str = "100") -> list[object]:
    return [MS + i * 3_600_000, price, "101", "99", price, "12.5"]


@respx.mock
async def test_candles_parsed_sorted_decimal() -> None:
    # Returned out of order on purpose; the client sorts ascending.
    respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(
        return_value=httpx.Response(
            200, json=[candle_row(2, "102"), candle_row(0, "100"), candle_row(1, "101")]
        )
    )
    client = BitvavoClient()
    candles = await client.candles("BTC-EUR", "1h", 10)
    await client.aclose()
    assert [float(c.close) for c in candles] == [100.0, 101.0, 102.0]
    assert candles[0].open_time == datetime.fromtimestamp(MS / 1000, UTC)
    assert isinstance(candles[0].close, Decimal)
    assert candles[0].volume == Decimal("12.5")


@respx.mock
async def test_candles_paginate_past_page_cap() -> None:
    route = respx.get(f"{BASE_URL}/BTC-EUR/candles")

    def respond(request: httpx.Request) -> httpx.Response:
        if "end" in request.url.params:
            # Older page, returned before the newest page.
            return httpx.Response(200, json=[candle_row(i) for i in range(500)])
        return httpx.Response(200, json=[candle_row(500 + i) for i in range(1000)])

    route.mock(side_effect=respond)
    client = BitvavoClient()
    candles = await client.candles("BTC-EUR", "1h", 1500)
    await client.aclose()
    assert route.call_count == 2
    assert len(candles) == 1500
    times = [c.open_time for c in candles]
    assert times == sorted(times)


@respx.mock
async def test_candles_short_page_stops_paging() -> None:
    route = respx.get(f"{BASE_URL}/BTC-EUR/candles")
    route.mock(return_value=httpx.Response(200, json=[candle_row(i) for i in range(5)]))
    client = BitvavoClient()
    candles = await client.candles("BTC-EUR", "1h", 8)
    await client.aclose()
    assert route.call_count == 1  # fewer rows than asked: no more history
    assert len(candles) == 5


@respx.mock
async def test_429_waits_for_reset_then_retries() -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    reset_at = MS + 30_000
    route = respx.get(f"{BASE_URL}/BTC-EUR/candles")
    route.mock(
        side_effect=[
            httpx.Response(
                429,
                json={"errorCode": 105, "error": "rate limit"},
                headers={"bitvavo-ratelimit-resetat": str(reset_at)},
            ),
            httpx.Response(200, json=[candle_row(0)]),
        ]
    )
    client = BitvavoClient(sleep=fake_sleep)
    candles = await client.candles("BTC-EUR", "1h", 5)
    await client.aclose()
    assert len(candles) == 1
    assert len(sleeps) == 1
    assert client.rate_limit_reset_at == datetime.fromtimestamp(reset_at / 1000, UTC)


@respx.mock
async def test_429_exhaustion_raises_rate_limit() -> None:
    respx.get(f"{BASE_URL}/BTC-EUR/candles").mock(
        return_value=httpx.Response(429, json={"errorCode": 105})
    )
    client = BitvavoClient(sleep=lambda s: _noop())
    with pytest.raises(RateLimitError):
        await client.candles("BTC-EUR", "1h", 5)
    await client.aclose()


async def _noop() -> None:
    return None


@respx.mock
async def test_500_raises_upstream() -> None:
    respx.get(f"{BASE_URL}/ticker/price").mock(
        return_value=httpx.Response(500, json={"errorCode": 300, "error": "broken"})
    )
    client = BitvavoClient()
    with pytest.raises(UpstreamError):
        await client.ticker_price("BTC-EUR")
    await client.aclose()


@respx.mock
async def test_network_error_raises_market_data_error() -> None:
    respx.get(f"{BASE_URL}/ticker/price").mock(side_effect=httpx.ConnectError("no route"))
    client = BitvavoClient()
    with pytest.raises(MarketDataError):
        await client.ticker_price("BTC-EUR")
    await client.aclose()


@respx.mock
async def test_order_book_and_spread() -> None:
    respx.get(f"{BASE_URL}/BTC-EUR/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "BTC-EUR",
                "bids": [["99.0", "1.0"], ["98.0", "2.0"]],
                "asks": [["101.0", "1.5"], ["102.0", "2.5"]],
            },
        )
    )
    client = BitvavoClient()
    book = await client.order_book("BTC-EUR", depth=2)
    await client.aclose()
    assert book.best_bid == Decimal("99.0")
    assert book.best_ask == Decimal("101.0")
    assert book.spread_pct == pytest.approx(Decimal("2.0"))


@respx.mock
async def test_ticker_24h_and_markets() -> None:
    respx.get(f"{BASE_URL}/ticker/24h").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "BTC-EUR",
                "last": "100",
                "volume": "10",
                "volumeQuote": "1000",
                "bid": "99",
                "ask": "101",
            },
        )
    )
    respx.get(f"{BASE_URL}/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "market": "BTC-EUR",
                    "status": "trading",
                    "base": "BTC",
                    "quote": "EUR",
                    "minOrderInBaseAsset": "0.0001",
                    "minOrderInQuoteAsset": "5",
                    "tickSize": "0.01",
                    "quantityDecimals": "4",
                }
            ],
        )
    )
    client = BitvavoClient()
    tickers = await client.ticker_24h("BTC-EUR")
    markets = await client.markets()
    await client.aclose()
    assert tickers[0].volume_quote == Decimal("1000")
    assert tickers[0].spread_pct == pytest.approx(Decimal("2.0"))
    m = markets[0]
    assert m.market == "BTC-EUR" and m.status == "trading"
    assert m.min_order_quote == Decimal("5") and m.tick_size == Decimal("0.01")


@respx.mock
async def test_rate_limit_headers_tracked() -> None:
    respx.get(f"{BASE_URL}/time").mock(
        return_value=httpx.Response(
            200,
            json={"time": MS},
            headers={
                "bitvavo-ratelimit-remaining": "987",
                "bitvavo-ratelimit-limit": "1000",
                "bitvavo-ratelimit-resetat": str(MS + 60_000),
            },
        )
    )
    client = BitvavoClient()
    t = await client.time()
    await client.aclose()
    assert t == datetime.fromtimestamp(MS / 1000, UTC)
    assert client.rate_limit_remaining == 987
    assert client.rate_limit_limit == 1000
