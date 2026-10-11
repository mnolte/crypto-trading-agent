"""The Decider protocol every decision layer implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.agent.broker.paper import HeldPosition
from app.agent.market.base import Candle
from app.agent.signals import PairSignals
from app.models import Regime, Settings, Signal


@dataclass
class DecideContext:
    """One pair's decision inputs. Nothing from another pair's prices,
    sizes or P&L is ever in here."""

    pair: str
    now: datetime
    price: Decimal
    signals: PairSignals
    holding: bool
    position_stop: Decimal | None
    position_target: Decimal | None
    position_entry: Decimal | None
    candles_1h: list[Candle] = field(default_factory=list)
    candles_4h: list[Candle] = field(default_factory=list)
    candles_1d: list[Candle] = field(default_factory=list)
    prev_4h_ema20: float | None = None
    prev_4h_ema50: float | None = None
    btc_regime: Regime | None = None
    settings: Settings | None = None
    cycle: int = 0
    position: HeldPosition | None = None
    # Market-wide context the brief may use: btc_1h/btc_4h/btc_1d/usdc_1h.
    context_candles: dict[str, list[Candle]] = field(default_factory=dict)
    # True while a per-coin or global entry pause is in force. Flat pairs
    # don't reach the model then (no BUY could execute anyway); an open
    # position still gets its call so SELL keeps working.
    entries_paused: bool = False


@dataclass
class Draft:
    """A decider's proposal before the risk gates.

    For BUY the decider proposes entry/stop/target; the risk layer sizes it.
    ``prefilter`` records whether the Rules pre-filter would have called the
    model — stored so the inspector can explain skipped cycles later.

    The OpenAI decider fills the audit fields: ``decider`` says which layer
    actually answered ("openai", or "rules" on a fallback), ``llm_call_id``
    links the stored call and ``raw_brief``/``raw_response`` hold the exact
    request and response for the decision drawer. ``error`` carries the
    check that produced a HOLD.
    """

    action: str  # "BUY" | "SELL" | "HOLD"
    confidence: float
    reasoning: str
    signals: list[Signal]
    invalidation: str | None = None
    invalidation_price: Decimal | None = None
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    prefilter: bool = False
    decider: str = "rules"
    model: str | None = None
    prompt_version: str | None = None
    llm_call_id: str | None = None
    raw_brief: str | None = None
    raw_response: str | None = None
    error: str | None = None


@dataclass
class RiskView:
    """The engine's risk state at brief time, for the portfolio block."""

    throttle_on: bool
    correlated_slots_left: int
    shock_brake_on: bool


class Decider(Protocol):
    """One decision layer. Returns a Draft; never sizes money."""

    name: str

    async def decide(self, ctx: DecideContext) -> Draft: ...
