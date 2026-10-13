"""The deterministic Rules decider.

It is used three ways: as the pre-filter that decides whether the model is
called, as the baseline every model decision is compared with, and as the
fallback decider when there is no API key, the budget is spent, or the model
fails. Every threshold lives in ``Settings.rules`` / ``Settings.risk`` /
``Settings.market`` — the same values the engine reads.

Conditions (on the latest closed 1h bar):
- C1  higher-timeframe trend up: 4h EMA 20 > EMA 50 and daily close > EMA 200
- C2  1h EMA 20 > EMA 50
- C3  1h close > VWAP
- C4  1h RSI between 45 and 70
- C5  1h OBV trend > +0.1
- C6  1h ADX >= the shared threshold
"""

from __future__ import annotations

from decimal import Decimal

from app.agent.deciders.base import DecideContext, Draft
from app.agent.signals import PairSignals, TfValues
from app.models import Settings, Signal, Tf

COND_NAMES = ["C1", "C2", "C3", "C4", "C5", "C6"]


def evaluate_conditions(sig: PairSignals, settings: Settings) -> dict[str, bool]:
    """C1..C6 on the latest closed bars. A missing value counts as failed —
    the Rules engine never decides on absent data."""
    h1, h4, d1 = sig.tf["1h"], sig.tf["4h"], sig.tf["1d"]
    adx_threshold = float(settings.market.adx_threshold)

    def gt(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and a > b

    rsi = h1.rsi14
    obv = h1.obv_trend
    return {
        "C1": gt(h4.ema20, h4.ema50) and gt(d1.close, d1.ema200),
        "C2": gt(h1.ema20, h1.ema50),
        "C3": gt(h1.close, h1.vwap),
        "C4": rsi is not None and 45 <= rsi <= 70,
        "C5": obv is not None and obv > 0.1,
        "C6": h1.adx14 is not None and h1.adx14 >= adx_threshold,
    }


def _cond_signals(conds: dict[str, bool], sig: PairSignals, settings: Settings) -> list[Signal]:
    """Represent the condition verdicts as signal chips for the decision,
    plus the same test evaluated on the other timeframes so the drawer's
    signal stack fills every column of a condition row."""
    out = [
        Signal(name=name, tf="1h", read="bullish" if conds[name] else "neutral")
        for name in COND_NAMES
    ]
    out += _cond_other_tf_signals(sig, float(settings.market.adx_threshold))
    return out


def _cond_other_tf_signals(sig: PairSignals, adx_threshold: float) -> list[Signal]:
    """C1..C6 on 4h and 1d. C1 has no single-timeframe form, so its cells are
    the two legs of the condition itself: the 4h EMA cross and the daily
    close vs EMA 200. A missing input emits nothing, so the cell shows a
    dash rather than a verdict."""

    def gt(a: float | None, b: float | None) -> bool | None:
        return None if a is None or b is None else a > b

    def band(v: float | None) -> bool | None:
        return None if v is None else 45 <= v <= 70

    def over(v: float | None, limit: float) -> bool | None:
        return None if v is None else v > limit

    def at_least(v: float | None, limit: float) -> bool | None:
        return None if v is None else v >= limit

    h4, d1 = sig.tf["4h"], sig.tf["1d"]
    tfs: list[tuple[Tf, TfValues]] = [("4h", h4), ("1d", d1)]
    out: list[Signal] = []

    def add(name: str, tf: Tf, passed: bool | None) -> None:
        if passed is not None:
            out.append(Signal(name=name, tf=tf, read="bullish" if passed else "neutral"))

    add("C1", "4h", gt(h4.ema20, h4.ema50))
    add("C1", "1d", gt(d1.close, d1.ema200))
    for tf, v in tfs:
        add("C2", tf, gt(v.ema20, v.ema50))
        add("C3", tf, gt(v.close, v.vwap))
        add("C4", tf, band(v.rsi14))
        add("C5", tf, over(v.obv_trend, 0.1))
        add("C6", tf, at_least(v.adx14, adx_threshold))
    return out


class RulesDecider:
    """The baseline and fallback decider. Deterministic, no model call."""

    name = "rules"

    async def decide(self, ctx: DecideContext) -> Draft:
        assert ctx.settings is not None
        settings = ctx.settings
        rules = settings.rules
        sig = ctx.signals
        conds = evaluate_conditions(sig, settings)
        met = sum(1 for v in conds.values() if v)
        inner = sum(1 for k, v in conds.items() if k != "C1" and v)
        prefilter = ctx.holding or (conds["C1"] and inner >= rules.prefilter_min_conds)
        confidence = met / 6.0
        held = ", ".join(k for k in COND_NAMES if conds[k])
        missed = ", ".join(k for k in COND_NAMES if not conds[k])
        base_reason = f"conditions met {met}/6 ({held}); failed: {missed or 'none'}"

        if ctx.holding:
            sell = self._check_sell(ctx, sig)
            if sell is not None:
                sell.signals = _cond_signals(conds, sig, settings)
                sell.prefilter = True
                return sell
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"Holding: no sell rule fired. {base_reason}.",
                signals=_cond_signals(conds, sig, settings),
                invalidation=None,
                prefilter=True,
            )

        if not prefilter:
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"Pre-filter: {base_reason}.",
                signals=_cond_signals(conds, sig, settings),
                prefilter=False,
            )

        buy = self._check_buy(ctx, sig, conds, inner, confidence, base_reason)
        buy.prefilter = True
        return buy

    # ------------------------------------------------------------------ sell

    def _check_sell(self, ctx: DecideContext, sig: PairSignals) -> Draft | None:
        h1 = sig.tf["1h"]
        broke_1h = (
            h1.close is not None
            and h1.ema50 is not None
            and h1.close < h1.ema50
            and h1.obv_trend is not None
            and h1.obv_trend < 0
        )
        cross_4h = (
            ctx.prev_4h_ema20 is not None
            and ctx.prev_4h_ema50 is not None
            and sig.tf["4h"].ema20 is not None
            and sig.tf["4h"].ema50 is not None
            and ctx.prev_4h_ema20 >= ctx.prev_4h_ema50
            and sig.tf["4h"].ema20 < sig.tf["4h"].ema50
        )
        if broke_1h:
            reason = "1h close below EMA 50 with OBV trend below 0"
        elif cross_4h:
            reason = "4h EMA 20 crossed below EMA 50"
        else:
            return None
        return Draft(
            action="SELL",
            confidence=0.6,
            reasoning=f"Sell rule: {reason}.",
            signals=[],
            invalidation="the 1h trend re-establishes above EMA 50",
        )

    # ------------------------------------------------------------------- buy

    def _check_buy(
        self,
        ctx: DecideContext,
        sig: PairSignals,
        conds: dict[str, bool],
        inner: int,
        confidence: float,
        base_reason: str,
    ) -> Draft:
        assert ctx.settings is not None
        settings = ctx.settings
        rules = settings.rules
        h1 = sig.tf["1h"]
        cond_signals = _cond_signals(conds, sig, settings)

        if sig.regime.trend != "up":
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"Regime {sig.regime.trend}: Rules never BUY outside "
                f"an up regime. {base_reason}.",
                signals=cond_signals,
                prefilter=True,
            )
        if inner < rules.buy_min_conds or not conds["C1"]:
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"BUY needs C1 and {rules.buy_min_conds} of C2..C6. {base_reason}.",
                signals=cond_signals,
                prefilter=True,
            )

        entry = ctx.price
        atr = Decimal(str(h1.atr14)) if h1.atr14 else None
        if atr is None or atr <= 0 or h1.ema20 is None:
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"ATR/EMA 20 unavailable. {base_reason}.",
                signals=cond_signals,
                prefilter=True,
            )

        # Not more than buy_max_atr_above_ema20 x ATR above the 1h EMA 20 —
        # don't chase an extended move.
        ema20 = Decimal(str(h1.ema20))
        if entry - ema20 > rules.buy_max_atr_above_ema20 * atr:
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"Price more than {rules.buy_max_atr_above_ema20} x "
                f"ATR above 1h EMA 20. {base_reason}.",
                signals=cond_signals,
                prefilter=True,
            )

        # Stop: lowest low of the last stop_lookback_bars 1h bars minus
        # stop_atr_pad x ATR, widened to the fee gate's minimum distance.
        lows = [c.low for c in ctx.candles_1h[-rules.stop_lookback_bars :]]
        if not lows:
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning="No candle lows for the stop. " + base_reason,
                signals=cond_signals,
                prefilter=True,
            )
        stop = min(lows) - rules.stop_atr_pad * atr
        paper = settings.paper
        risk = settings.risk
        # Mirror the fee gate on the expected fill price so a widened stop
        # still passes it.
        fill_entry = entry * (1 + paper.slippage_pct / 100)
        min_stop_dist = (
            (paper.fee_pct * 2 + paper.slippage_pct * 2) / risk.max_cost_pct_of_risk * fill_entry
        )
        if fill_entry - stop < min_stop_dist:
            stop = fill_entry - min_stop_dist
        if entry - stop > rules.stop_max_atr * atr:
            return Draft(
                action="HOLD",
                confidence=confidence,
                reasoning=f"Needed stop more than {rules.stop_max_atr} x ATR "
                f"away; Rules does not BUY. {base_reason}.",
                signals=cond_signals,
                prefilter=True,
            )

        # Target: nearest resistance above entry within target_max_atr x ATR,
        # else entry + target_rr R.
        resistance = next(
            (r for r in sig.levels.resistance if Decimal(str(r)) > entry),
            None,
        )
        r_dist = entry - stop
        within_cap = (
            resistance is not None
            and Decimal(str(resistance)) - entry <= rules.target_max_atr * atr
        )
        if within_cap:
            target = Decimal(str(resistance))
        else:
            target = entry + rules.target_rr * r_dist

        # Invalidation: the nearest support between stop and entry.
        inv_price = next(
            (Decimal(str(s)) for s in sig.levels.support if stop < Decimal(str(s)) < entry),
            None,
        )
        return Draft(
            action="BUY",
            confidence=confidence,
            reasoning=f"Rules BUY: regime up, {inner} of C2..C6 met. {base_reason}.",
            signals=cond_signals,
            invalidation="1h close below the nearest support",
            invalidation_price=inv_price,
            entry=entry,
            stop=stop,
            target=target,
            prefilter=True,
        )
