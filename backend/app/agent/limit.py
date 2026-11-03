"""Limit (post-only) order helpers: placement, fill rules, fee-gate costs.

A limit order in this paper model only fills when the market trades
*through* the limit by at least one price tick — touching the limit is not
a fill. The fill price is always the limit price itself.
"""

from __future__ import annotations

from decimal import Decimal

from app.models import PaperSettings


def post_only_limit(entry: Decimal, best_bid: Decimal | None, best_ask: Decimal | None) -> Decimal:
    """A buy limit at or above the best ask would cross: move to the bid."""
    if best_ask is not None and best_bid is not None and entry >= best_ask:
        return best_bid
    return entry


def buy_fills(last: Decimal, limit: Decimal, tick: Decimal | None) -> bool:
    """A buy limit fills when the last price is one full tick below it."""
    if tick is not None and tick > 0:
        return last <= limit - tick
    return last < limit


def sell_fills(last: Decimal, limit: Decimal, tick: Decimal | None) -> bool:
    """A sell limit fills when the last price is one full tick above it."""
    if tick is not None and tick > 0:
        return last >= limit + tick
    return last > limit


def entry_round_trip_cost_pct(paper: PaperSettings) -> Decimal:
    """Fee-gate cost per side of the planned round trip, in percent.

    Market entries pay taker + slippage on entry and on the stop exit;
    limit entries pay maker on entry but still stop out at market
    (taker + slippage).
    """
    if paper.entry_orders == "limit":
        return paper.maker_fee_pct + paper.taker_fee_pct + paper.slippage_pct
    return paper.taker_fee_pct * 2 + paper.slippage_pct * 2


def entry_fee_pct(paper: PaperSettings) -> Decimal:
    """The fee the planned entry order pays (maker for limit entries)."""
    return paper.maker_fee_pct if paper.entry_orders == "limit" else paper.taker_fee_pct
