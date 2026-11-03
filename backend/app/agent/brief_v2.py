"""Brief version 2: ``trade_math`` and the richer ``previous_decision``.

Version 2 tells the model what code did with its last answer and does the
BUY arithmetic for it: the allowed stop zone, real support/resistance
levels to pick from, the R/R of every workable stop+target pair and
whether a BUY is possible at all. Everything is computed here — the model
only chooses.

``upgrade_brief_to_v2`` rebuilds those fields for a stored v1 brief (a
replay on an old call): the decisions table supplies ``previous_decision``
and candles closed by the brief's own ``now`` supply the level search, so
an upgrade never sees the future.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.market.base import Candle, MarketInfo, drop_forming
from app.agent.signals import (
    LEVEL_BARS_1H,
    LEVEL_BARS_4H,
    LEVEL_MERGE_ATR,
    SWING_SIDE_BARS,
    merge_levels,
    prev_day_high_low,
    swing_points,
)
from app.db import LlmCallRow
from app.models import Regime

BRIEF_VERSION = 2

OUTCOMES = ("executed", "skipped", "held", "vetoed", "expired", "unfilled", "approved", "none")

# The level search reads this many closed bars per timeframe — deeper than
# the 3 supports/resistances the structure block lists.
STOP_CANDIDATES = 5
TARGET_CANDIDATES = 5
MAX_PAIRS = 10
REPEAT_LOOKBACK = 25


def brief_version_of(brief: dict[str, Any]) -> int:
    """The brief version a stored brief JSON was built for."""
    return BRIEF_VERSION if "trade_math" in brief else 1


# ----------------------------------------------------------- previous_decision


@dataclass
class PrevDecision:
    """The pair's previous decision and what code did with it.

    ``buy_basis`` carries the skipped BUY's ``stop_basis``/``target_basis``
    from its stored model output (v2 calls only) so the repeat-BUY check
    can compare without touching the brief. ``failed_gate_names`` mirrors
    the gates stored on the decision row.
    """

    action: str
    invalidation: str | None
    at: datetime | None = None
    outcome: str = "none"
    failed_gates: list[dict[str, Any]] = field(default_factory=list)
    repeats: int = 1
    buy_basis: tuple[str | None, str | None] = (None, None)
    failed_gate_names: list[str] = field(default_factory=list)
    levels: tuple[float | None, float | None, float | None] | None = None

    def to_brief(self, version: int) -> dict[str, Any]:
        """The brief block: v1 keeps only action + invalidation."""
        out: dict[str, Any] = {"action": self.action, "invalidation": self.invalidation}
        if version >= BRIEF_VERSION:
            out.update(
                {
                    "at": self.at.isoformat() if self.at else None,
                    "outcome": self.outcome,
                    "failed_gates": self.failed_gates,
                    "repeats": self.repeats,
                }
            )
        return out


def _outcome(action: str, status: str) -> str:
    """The decision's outcome as the brief reports it."""
    if action == "SKIP":
        return "skipped"
    if status in ("executed", "approved"):
        return "executed"  # an approved pending filled
    if status in ("vetoed", "expired", "unfilled"):
        return status
    if action == "HOLD":
        return "held"
    return "none"  # still pending, or a limit order with no outcome yet


def _parsed_output(session: Session, decision_id: str) -> dict[str, Any] | None:
    """The model output stored on the call that produced the decision."""
    row = session.scalar(
        select(LlmCallRow)
        .where(LlmCallRow.decision_id == decision_id)
        .order_by(LlmCallRow.at.desc())
        .limit(1)
    )
    if row is None or not row.parsed:
        return None
    try:
        parsed = json.loads(row.parsed)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _decision_levels(
    session: Session, row: Any
) -> tuple[float | None, float | None, float | None] | None:
    """(entry, stop, target) the decision proposed — the sized plan when
    there is one, else the model's own levels from the stored call."""
    if row.plan:
        try:
            plan = json.loads(row.plan)
            return (
                float(plan["entry"]) if plan.get("entry") is not None else None,
                float(plan["stop"]) if plan.get("stop") is not None else None,
                float(plan["target"]) if plan.get("target") is not None else None,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    parsed = _parsed_output(session, row.id)
    if parsed is None:
        return None
    try:
        return (
            float(parsed["entry"]) if parsed.get("entry") is not None else None,
            float(parsed["stop"]) if parsed.get("stop") is not None else None,
            float(parsed["target"]) if parsed.get("target") is not None else None,
        )
    except (TypeError, ValueError):
        return None


def _levels_match(
    a: tuple[float | None, ...] | None,
    b: tuple[float | None, ...] | None,
    tick: float | None,
) -> bool:
    """Two level sets are "the same" when every level is within one tick —
    or both carry no levels at all. Without a known tick the fallback is a
    hair above float noise."""
    if a is None or b is None:
        return a is None and b is None
    tol = tick if tick and tick > 0 else 1e-9
    return all(
        (x is None and y is None) or (x is not None and y is not None and abs(x - y) <= tol)
        for x, y in zip(a, b, strict=True)
    )


def previous_decision_v2(
    session: Session,
    account_id: int,
    pair: str,
    *,
    before: datetime | None = None,
    tick: float | None = None,
) -> PrevDecision | None:
    """The pair's previous decision for a v2 brief.

    ``before`` caps the look-back at a past cycle's time (replay upgrades);
    ``tick`` is the market's price grid for the "near-identical levels"
    comparison behind ``repeats``.
    """
    rows = repo.list_decision_rows(
        session, account_id, pair=pair, before=before, limit=REPEAT_LOOKBACK
    )
    if not rows:
        return None
    latest = rows[0]
    outcome = _outcome(latest.action, latest.status)
    latest_levels = _decision_levels(session, latest)

    # repeats: how many decisions in a row (the latest included) ended the
    # same way on levels within one tick of the latest's.
    repeats = 1
    for row in rows[1:]:
        if _outcome(row.action, row.status) != outcome:
            break
        if not _levels_match(_decision_levels(session, row), latest_levels, tick):
            break
        repeats += 1

    failed_gates: list[dict[str, Any]] = []
    try:
        gates = json.loads(latest.gates or "[]")
    except json.JSONDecodeError:
        gates = []
    for gate in gates:
        if isinstance(gate, dict) and gate.get("status") == "fail":
            failed_gates.append(
                {
                    "name": gate.get("name"),
                    "value": gate.get("value"),
                    "limit": gate.get("limit"),
                }
            )
    if outcome != "skipped":
        failed_gates = []

    buy_basis: tuple[str | None, str | None] = (None, None)
    if latest.action in ("BUY", "SKIP"):
        parsed = _parsed_output(session, latest.id)
        if parsed is not None:
            buy_basis = (parsed.get("stop_basis"), parsed.get("target_basis"))

    return PrevDecision(
        action=latest.action,
        invalidation=latest.invalidation,
        at=repo.aware_utc(latest.at),
        outcome=outcome,
        failed_gates=failed_gates,
        repeats=repeats,
        buy_basis=buy_basis,
        failed_gate_names=[str(g.get("name")) for g in failed_gates],
        levels=latest_levels,
    )


# ----------------------------------------------------------------- trade_math


def _merged_levels(candles: list[Candle], keep: int, atr: float) -> list[float]:
    """Merged swing levels (highs and lows alike — a level below price is a
    support whatever its origin) from the last ``keep`` closed bars."""
    tail = candles[-keep:]
    if len(tail) < 2 * SWING_SIDE_BARS + 1 or atr <= 0:
        return []
    high = np.array([float(c.high) for c in tail])
    low = np.array([float(c.low) for c in tail])
    highs, lows = swing_points(high, low)
    return merge_levels([p for _, p in highs] + [p for _, p in lows], LEVEL_MERGE_ATR * atr)


def _dedupe(levels: list[tuple[float, str]], tol: float) -> list[tuple[float, str]]:
    """Drop later sources within ``tol`` of an earlier one — the first
    source in the search order keeps the level."""
    kept: list[tuple[float, str]] = []
    for level, src in levels:
        if all(abs(level - k[0]) > tol for k in kept):
            kept.append((level, src))
    return kept


def _pct(value: float) -> str:
    """A percent in reasons: one decimal when readable, more when small."""
    return f"{value:.1f}" if abs(value) >= 1 else f"{value:.2g}"


def trade_math_block(
    *,
    pair: str,
    now: datetime,
    price: float,
    atr_1h: float | None,
    tf1h: dict[str, Any],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    regime: Regime,
    holding: bool,
    limits: dict[str, Any],
    stop_buffer_atr: float,
    round_price: Any,
) -> dict[str, Any]:
    """The ``trade_math`` block: zone, candidates, pairs, feasibility and
    the 1h conditions. ``round_price`` is the brief's tick-rounding helper
    so every quoted level sits on the market's grid."""
    min_rr = float(limits.get("min_rr") or 0)
    min_stop_atr = float(limits.get("min_stop_atr") or 0)
    max_stop_atr = float(limits.get("max_stop_atr") or 0)
    min_stop_pct = limits.get("min_stop_pct")
    min_stop_pct = float(min_stop_pct) if min_stop_pct is not None else None

    reads = {k: (tf1h.get(k) or {}).get("read") for k in _COND_KEYS}
    conditions = {
        "ema20_above_50": reads["ema20_vs_50"] == "bullish",
        "above_vwap": reads["vwap_dist"] == "bullish",
        "obv_bullish": reads["obv_trend"] == "bullish",
        "rsi_bullish": reads["rsi"] == "bullish",
        "adx_trending": reads["adx"] == "trending",
    }
    met = sum(1 for v in conditions.values() if v)
    required = 4 if regime.age_4h < 6 else 3

    atr = atr_1h if atr_1h is not None and atr_1h > 0 else None
    sideways = regime.trend == "sideways"
    out: dict[str, Any] = {
        "stop_zone": None,
        "stop_candidates": [],
        "target_candidates": [],
        "pairs": [],
        "conditions_1h": conditions,
        "conditions_1h_met": met,
        "conditions_1h_required": required,
        "buy_feasible": False,
        "infeasible_reason": None,
    }

    reason: str | None = None
    if holding:
        reason = "position open"
    elif regime.trend == "down" and pair != "BTC-EUR":
        reason = "regime is down"
    elif atr is None or price <= 0 or min_stop_pct is None:
        reason = "ATR(1h) or limits unavailable"

    # ---- levels from the last 200 1h / 120 4h swings + the previous day.
    supports: list[tuple[float, str]] = []
    resistances: list[tuple[float, str]] = []
    if atr is not None:
        tol = LEVEL_MERGE_ATR * atr
        lows_1h = _merged_levels(candles_1h, LEVEL_BARS_1H, atr)
        lows_4h = _merged_levels(candles_4h, LEVEL_BARS_4H, atr)
        pdh, pdl = prev_day_high_low(candles_1h, now)
        supports = _dedupe(
            [(lv, "1h swing") for lv in lows_1h if lv < price]
            + [(lv, "4h swing") for lv in lows_4h if lv < price]
            + ([(pdl, "previous day low")] if pdl is not None and pdl < price else []),
            tol,
        )
        resistances = _dedupe(
            [(lv, "1h swing") for lv in lows_1h if lv > price]
            + [(lv, "4h swing") for lv in lows_4h if lv > price]
            + ([(pdh, "previous day high")] if pdh is not None and pdh > price else []),
            tol,
        )

    # ---- the stop zone and the candidates inside it.
    stops: list[dict[str, Any]] = []
    zone_lo = zone_hi = None
    if atr is not None and price > 0 and min_stop_pct is not None:
        zone_hi = min(price * (1 - min_stop_pct / 100), price - min_stop_atr * atr)
        zone_lo = price - max_stop_atr * atr
        buf = stop_buffer_atr * atr
        inside = [(lv, src) for lv, src in supports if zone_lo <= lv - buf <= zone_hi]
        inside.sort(key=lambda item: -item[0])  # nearest to price first
        for i, (level, src) in enumerate(inside[:STOP_CANDIDATES], start=1):
            stop_price = level - buf
            stops.append(
                {
                    "id": f"S{i}",
                    "level": round_price(level),
                    "price": round_price(stop_price),
                    "dist_pct": round((stop_price / price - 1.0) * 100, 2),
                    "source": src,
                    "_level": level,
                    "_price": stop_price,
                }
            )
        out["stop_zone"] = {"highest": round_price(zone_hi), "lowest": round_price(zone_lo)}

    # ---- resistances above price; sideways keeps only the nearest.
    targets: list[dict[str, Any]] = []
    if price > 0:
        above = sorted(resistances, key=lambda item: item[0])
        for i, (level, src) in enumerate(above[: 1 if sideways else TARGET_CANDIDATES], start=1):
            targets.append(
                {
                    "id": f"R{i}",
                    "price": round_price(level),
                    "dist_pct": round((level / price - 1.0) * 100, 2),
                    "source": src,
                    "_price": level,
                }
            )

    # ---- every (stop, target) pair reaching min_rr, best first.
    pairs: list[dict[str, Any]] = []
    if price > 0:
        price_d = Decimal(str(price))
        for s in stops:
            for t in targets:
                reward = Decimal(str(t["_price"])) - price_d
                risk = price_d - Decimal(str(s["_price"]))
                if risk <= 0:
                    continue
                rr = reward / risk
                if rr >= Decimal(str(min_rr)):
                    pairs.append(
                        {
                            "stop_id": s["id"],
                            "target_id": t["id"],
                            "rr": float(rr.quantize(Decimal("0.01"))),
                        }
                    )
        pairs.sort(key=lambda p: -p["rr"])

    # ---- feasibility, checked in spec order; first cause wins.
    if reason is None:
        if sideways and atr is not None:
            nearest = max((lv for lv, _ in supports), default=None)
            if nearest is None:
                reason = "no support below the price in a sideways regime"
            elif (price - nearest) / atr > 1.0:
                reason = (
                    f"price is {(price - nearest) / atr:.2f} x ATR(1h) above the nearest "
                    f"support {round_price(nearest)} (max 1.0 in a sideways regime)"
                )
        if reason is None and not stops and zone_lo is not None and zone_hi is not None:
            near = (1 - zone_hi / price) * 100
            far = (1 - zone_lo / price) * 100
            reason = f"no support inside the stop zone ({_pct(near)}% to {_pct(far)}% below price)"
        elif reason is None and not targets:
            reason = "no resistance above the price"
        elif reason is None and not pairs and zone_hi is not None:
            r1 = targets[0]
            near = (1 - zone_hi / price) * 100
            reason = (
                f"nearest resistance {r1['price']} (+{r1['dist_pct']}%) is below "
                f"{min_rr}R of the minimum stop ({_pct(near)}% below price)"
            )

    out["stop_candidates"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in stops]
    out["target_candidates"] = [
        {k: v for k, v in t.items() if not k.startswith("_")} for t in targets
    ]
    out["pairs"] = pairs[:MAX_PAIRS]
    out["buy_feasible"] = reason is None
    out["infeasible_reason"] = reason
    return out


# --------------------------------------------------------------- upgrades --

_COND_KEYS = ("ema20_vs_50", "vwap_dist", "obv_trend", "rsi", "adx")


def upgrade_brief_to_v2(
    session: Session,
    brief: dict[str, Any],
    *,
    account_id: int,
    settings: Any,
    market_info: MarketInfo | None = None,
    round_price: Any = None,
) -> dict[str, Any]:
    """A stored v1 brief upgraded for a v2 replay.

    The new fields come from the stored brief itself (price, limits,
    regime, reads), the decisions table before the brief's ``now`` and the
    stored candles closed by then — never from later data.
    """
    from app.agent.brief import _price  # local import: brief.py imports this module

    pair = str(brief.get("pair") or "")
    now = datetime.fromisoformat(brief["now"]) if isinstance(brief.get("now"), str) else None
    if now is None:
        return dict(brief)
    price = float(brief.get("price") or 0)
    tf = brief.get("tf") or {}
    tf1h = tf.get("1h") or {}
    atr_pct = (tf1h.get("atr_pct") or {}).get("value")
    atr_1h = (float(atr_pct) * price / 100) if atr_pct and price else None

    # Only candles already closed at the brief's time — no lookahead.
    candles_1h = drop_forming(
        repo.load_candles(session, pair, "1h", LEVEL_BARS_1H + 24, end=now), "1h", now
    )
    candles_4h = drop_forming(
        repo.load_candles(session, pair, "4h", LEVEL_BARS_4H, end=now), "4h", now
    )

    regime_data = (brief.get("regime") or {}).get("pair")
    regime = (
        Regime.model_validate(regime_data)
        if isinstance(regime_data, dict)
        else Regime(trend="sideways", volatility="normal", age_4h=0)
    )
    limits = brief.get("limits") or {}
    rp = (lambda v: _price(v, market_info)) if round_price is None else round_price
    upgraded = dict(brief)
    upgraded["trade_math"] = trade_math_block(
        pair=pair,
        now=now,
        price=price,
        atr_1h=atr_1h,
        tf1h=tf1h,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        regime=regime,
        holding=brief.get("position") is not None,
        limits=limits,
        stop_buffer_atr=float(settings.risk.stop_buffer_atr),
        round_price=rp,
    )
    tick = float(market_info.tick_size) if market_info and market_info.tick_size else None
    prev = previous_decision_v2(session, account_id, pair, before=now, tick=tick)
    upgraded["previous_decision"] = prev.to_brief(BRIEF_VERSION) if prev else None
    return upgraded
