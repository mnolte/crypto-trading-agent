"""Positions & orders (016): the shared position facts and the endpoint data.

``position_facts`` fills the Dashboard bar extras on a ``Position`` model
from a held position — the one place stop distance, ATR distance, P&L in R
and the stop-above-entry flag are computed, so the snapshot and the
positions screen can never disagree.
"""

from __future__ import annotations

from decimal import Decimal

from app.agent.broker import HeldPosition
from app.models import Position


def position_facts(
    model: Position,
    held: HeldPosition,
    atr: Decimal | None,
    stop_near_atr: float,
) -> Position:
    """Fill the distance/P&L extras on ``model`` from ``held`` in place."""
    model.stop_above_entry = held.stop >= held.entry
    if held.last <= 0:
        return model
    dist = held.last - held.stop
    model.stop_dist_pct = dist / held.last * 100
    if atr is not None and atr > 0:
        model.stop_dist_atr = dist / atr
        model.stop_near = model.stop_dist_atr < stop_near_atr
    if held.qty > 0 and held.r_unit > 0:
        model.pnl_r = held.unrealised / (held.qty * held.r_unit)
    return model
