"""The Decider protocol every decision layer implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

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
    prev_4h_ema20: float | None = None
    prev_4h_ema50: float | None = None
    btc_regime: Regime | None = None
    settings: Settings | None = None


@dataclass
class Draft:
    """A decider's proposal before the risk gates.

    For BUY the decider proposes entry/stop/target; the risk layer sizes it.
    ``prefilter`` records whether the Rules pre-filter would have called the
    model — stored so the inspector can explain skipped cycles later.
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


class Decider(Protocol):
    """One decision layer. Returns a Draft; never sizes money."""

    name: str

    def decide(self, ctx: DecideContext) -> Draft: ...
