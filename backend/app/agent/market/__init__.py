"""Market data: the Bitvavo client and the simulated market.

Both implement the same ``MarketClient`` protocol, so the engine, the
eligibility check and tests never care which one is active.
"""

from __future__ import annotations

from collections.abc import Callable

from app.agent.market.base import (
    Candle,
    MarketClient,
    MarketDataError,
    MarketInfo,
    OrderBook,
    RateLimitError,
    Ticker24h,
    UpstreamError,
)
from app.agent.market.bitvavo import BitvavoClient
from app.agent.market.sim import SimMarket
from app.models import Settings


def make_market_client(
    settings: Settings,
    on_event: Callable[[BaseException | None], None] | None = None,
) -> MarketClient:
    """Build the market client selected by the data_source setting."""
    if settings.data_source == "sim":
        return SimMarket()
    return BitvavoClient(on_event=on_event)


__all__ = [
    "Candle",
    "MarketClient",
    "MarketDataError",
    "MarketInfo",
    "OrderBook",
    "RateLimitError",
    "SimMarket",
    "Ticker24h",
    "UpstreamError",
    "make_market_client",
]
