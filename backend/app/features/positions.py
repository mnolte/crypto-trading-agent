"""Positions & orders (016): the shared position facts and the endpoint data.

``position_facts`` is the one place the per-position maths lives — stop
distance (% and ATR), target distance, P&L in R, the result if the stop
hits, R/R at entry, the time stop and the stop-near flag — so the
snapshot and the Positions screen can never disagree. It also fills the
Dashboard bar extras on the ``Position`` model in place, exactly as the
snapshot always has.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.agent.broker import HeldPosition
from app.models import Position

HUNDRED = Decimal("100")


@dataclass
class PositionFacts:
    """Everything the Positions screen shows for one open position."""

    stop_dist_pct: Decimal | None
    stop_dist_atr: Decimal | None
    target_dist_pct: Decimal | None
    target_dist_atr: Decimal | None
    pnl_r: Decimal | None
    stop_above_entry: bool
    stop_near: bool
    stop_hit_eur: Decimal  # qty x (stop - entry), before fees
    stop_hit_r: Decimal | None
    stop_hit_pct_equity: Decimal | None
    rr_at_entry: Decimal | None
    time_stop_at: datetime | None


def position_facts(
    model: Position,
    held: HeldPosition,
    atr: Decimal | None,
    *,
    equity: Decimal,
    stop_near_atr: Decimal,
    time_stop_days: Decimal,
) -> PositionFacts:
    """Fill ``model``'s dashboard extras and return the full fact set."""
    stop_above_entry = held.stop >= held.entry
    model.stop_above_entry = stop_above_entry
    facts = PositionFacts(
        stop_dist_pct=None,
        stop_dist_atr=None,
        target_dist_pct=None,
        target_dist_atr=None,
        pnl_r=None,
        stop_above_entry=stop_above_entry,
        stop_near=False,
        stop_hit_eur=held.qty * (held.stop - held.entry),
        stop_hit_r=((held.stop - held.entry) / held.r_unit if held.r_unit > 0 else None),
        stop_hit_pct_equity=(
            held.qty * (held.stop - held.entry) / equity * HUNDRED if equity > 0 else None
        ),
        rr_at_entry=((held.target - held.entry) / held.r_unit if held.r_unit > 0 else None),
        time_stop_at=held.opened_at + timedelta(days=float(time_stop_days)),
    )
    if held.last <= 0:
        return facts
    dist = held.last - held.stop
    model.stop_dist_pct = dist / held.last * HUNDRED
    facts.stop_dist_pct = model.stop_dist_pct
    if atr is not None and atr > 0:
        model.stop_dist_atr = dist / atr
        model.stop_near = model.stop_dist_atr < stop_near_atr
        facts.stop_dist_atr = model.stop_dist_atr
        facts.stop_near = model.stop_near
        facts.target_dist_pct = (held.target - held.last) / held.last * HUNDRED
        facts.target_dist_atr = (held.target - held.last) / atr
    if held.qty > 0 and held.r_unit > 0:
        model.pnl_r = held.unrealised / (held.qty * held.r_unit)
        facts.pnl_r = model.pnl_r
    return facts
