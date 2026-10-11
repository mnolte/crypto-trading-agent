"""Rules engine: C1..C6 each at its boundary, the pre-filter, Rules BUY
levels per the spec formulas, Rules SELL, the regime gate and generated
reasoning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.agent.deciders.base import DecideContext
from app.agent.deciders.rules import RulesDecider, evaluate_conditions
from app.agent.market.base import Candle
from app.agent.signals import Levels, MarketContext, PairSignals, TfValues
from app.models import Regime, Settings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _tf(**kw: float | None) -> TfValues:
    base: dict[str, float | None] = {
        "close": None,
        "ema20": None,
        "ema50": None,
        "ema200": None,
        "rsi14": None,
        "atr14": None,
        "atr_pct": None,
        "atr_percentile": None,
        "adx14": None,
        "plus_di14": None,
        "minus_di14": None,
        "vwap": None,
        "price_vs_vwap_pct": None,
        "price_vs_ema200_pct": None,
        "obv_trend": None,
        "volume_ratio": None,
    }
    base.update(kw)
    return TfValues(**base)


def _signals(
    h1: TfValues,
    h4: TfValues | None = None,
    d1: TfValues | None = None,
    *,
    trend: str = "up",
    support: list[float] | None = None,
    resistance: list[float] | None = None,
) -> PairSignals:
    return PairSignals(
        pair="X-EUR",
        at=NOW,
        tf={
            "1h": h1,
            "4h": h4 or _tf(),
            "1d": d1 or _tf(),
        },
        signals=[],
        levels=Levels(
            support=support or [],
            resistance=resistance or [],
            prev_day_high=None,
            prev_day_low=None,
        ),
        structure=[],
        regime=Regime(trend=trend, volatility="normal", age_4h=10),
        context=MarketContext(
            rs_btc_24h_pct=None,
            rs_btc_7d_pct=None,
            corr_btc_30d=None,
            eur_usd_1h_pct=None,
            eur_usd_24h_pct=None,
        ),
    )


def _bullish_signals(**over: object) -> PairSignals:
    """A fully bullish signal set: all six conditions hold."""
    h1 = _tf(
        close=100.0,
        ema20=99.5,
        ema50=98.0,
        rsi14=60.0,
        atr14=1.0,
        adx14=30.0,
        vwap=99.0,
        obv_trend=0.5,
    )
    h4 = _tf(ema20=102.0, ema50=101.0)
    d1 = _tf(close=105.0, ema200=95.0)
    kw: dict[str, object] = {"trend": "up", "resistance": [120.0]}
    kw.update(over)
    return _signals(h1, h4, d1, **kw)  # type: ignore[arg-type]


def _ctx(
    sig: PairSignals,
    *,
    holding: bool = False,
    price: Decimal = Decimal("100"),
    candles_1h: list[Candle] | None = None,
    prev_4h: tuple[float | None, float | None] = (None, None),
) -> DecideContext:
    return DecideContext(
        pair="X-EUR",
        now=NOW,
        price=price,
        signals=sig,
        holding=holding,
        position_entry=None,
        position_stop=None,
        position_target=None,
        candles_1h=candles_1h or _uptrend_candles(60),
        prev_4h_ema20=prev_4h[0],
        prev_4h_ema50=prev_4h[1],
        settings=Settings(),
    )


def _uptrend_candles(n: int) -> list[Candle]:
    """Steady uptrend 1h candles ending just under ctx price (100)."""
    out: list[Candle] = []
    price = 96.0
    for i in range(n):
        price *= 1.0006
        out.append(
            Candle(
                open_time=NOW - (n - i) * HOUR,
                open=Decimal(f"{price / 1.0006:.6f}"),
                high=Decimal(f"{price * 1.002:.6f}"),
                low=Decimal(f"{price * 0.998:.6f}"),
                close=Decimal(f"{price:.6f}"),
                volume=Decimal("100"),
            )
        )
    return out


# ---------------------------------------------------------------- conditions


def test_conditions_each_boundary() -> None:
    s = Settings()
    sig = _bullish_signals()
    conds = evaluate_conditions(sig, s)
    assert all(conds.values())

    # C1 fails when the 4h cross flips.
    c = evaluate_conditions(_signals(sig.tf["1h"], _tf(ema20=100.0, ema50=101.0), sig.tf["1d"]), s)
    assert not c["C1"]
    # C4 boundaries: 45 and 70 pass, outside fails.
    h1 = _tf(
        close=100.0,
        ema20=99.5,
        ema50=98.0,
        rsi14=44.9,
        atr14=1.0,
        adx14=30.0,
        vwap=99.0,
        obv_trend=0.5,
    )
    c = evaluate_conditions(_signals(h1, sig.tf["4h"], sig.tf["1d"]), s)
    assert not c["C4"]
    h1.rsi14 = 45.0
    c = evaluate_conditions(_signals(h1, sig.tf["4h"], sig.tf["1d"]), s)
    assert c["C4"]
    h1.rsi14 = 70.0
    c = evaluate_conditions(_signals(h1, sig.tf["4h"], sig.tf["1d"]), s)
    assert c["C4"]
    h1.rsi14 = 70.1
    c = evaluate_conditions(_signals(h1, sig.tf["4h"], sig.tf["1d"]), s)
    assert not c["C4"]
    h1.rsi14 = 60.0
    # C6 respects the shared ADX threshold: below it fails, at it passes.
    adx_threshold = float(s.market.adx_threshold)
    h1.adx14 = adx_threshold - 0.1
    c = evaluate_conditions(_signals(h1, sig.tf["4h"], sig.tf["1d"]), s)
    assert not c["C6"]
    h1.adx14 = adx_threshold
    c = evaluate_conditions(_signals(h1, sig.tf["4h"], sig.tf["1d"]), s)
    assert c["C6"]
    # Missing values fail, never guess.
    c = evaluate_conditions(_signals(_tf()), s)
    assert not any(c.values())


async def test_prefilter() -> None:
    decider = RulesDecider()
    # C1 + only 2 of C2..C6 (C3, C4) → no model call.
    h1 = _tf(
        close=100.0,
        ema20=97.0,
        ema50=98.0,
        rsi14=60.0,
        atr14=1.0,
        adx14=20.0,
        vwap=99.0,
        obv_trend=0.0,
    )
    h4 = _tf(ema20=102.0, ema50=101.0)
    d1 = _tf(close=105.0, ema200=95.0)
    d = await decider.decide(_ctx(_signals(h1, h4, d1)))
    assert d.action == "HOLD" and not d.prefilter
    # Holding always passes the pre-filter.
    d = await decider.decide(_ctx(_signals(h1, h4, d1), holding=True))
    assert d.prefilter


async def test_rules_buy_levels() -> None:
    decider = RulesDecider()
    d = await decider.decide(_ctx(_bullish_signals(resistance=[])))
    assert d.action == "BUY"
    assert d.entry == Decimal("100")
    candles = _uptrend_candles(60)
    atr = Decimal("1.0")
    min_low = min(c.low for c in candles[-6:])
    expected_stop = min_low - Decimal("0.2") * atr
    # Widened to the 2% fee-gate floor measured on the expected fill 100.05.
    fill_entry = Decimal("100.05")
    fee_floor = fill_entry * Decimal("0.02")
    if fill_entry - expected_stop < fee_floor:
        expected_stop = fill_entry - fee_floor
    assert d.stop == expected_stop
    # No resistance within 6 x ATR → entry + 2.5R.
    risk = Decimal("100") - d.stop
    assert d.target == Decimal("100") + Decimal("2.5") * risk
    assert d.confidence == 1.0  # 6 of 6 conditions


async def test_rules_buy_uses_resistance_target() -> None:
    decider = RulesDecider()
    # Resistance 103 within 6 x ATR(1) → target 103.
    d = await decider.decide(_ctx(_bullish_signals(resistance=[103.0])))
    assert d.action == "BUY" and d.target == Decimal("103")


async def test_no_buy_in_down_or_sideways_regime() -> None:
    decider = RulesDecider()
    for trend in ("down", "sideways"):
        d = await decider.decide(_ctx(_bullish_signals(trend=trend)))
        assert d.action == "HOLD"
        assert trend in d.reasoning


async def test_no_buy_when_extended_above_ema20() -> None:
    decider = RulesDecider()
    # Price 2 x ATR above EMA 20 (> 1.5 allowed).
    d = await decider.decide(_ctx(_bullish_signals(), price=Decimal("101.5")))
    # entry-ema20 = 2.0 > 1.5 x ATR(1.0) -> hold.
    assert d.action == "HOLD"


async def test_rules_sell_on_broken_1h_trend() -> None:
    decider = RulesDecider()
    h1 = _tf(close=97.0, ema50=98.0, obv_trend=-0.2, atr14=1.0)
    sig = _signals(h1, trend="down")
    d = await decider.decide(_ctx(sig, holding=True))
    assert d.action == "SELL"
    assert "EMA 50" in d.reasoning


async def test_rules_sell_on_4h_cross() -> None:
    decider = RulesDecider()
    h1 = _tf(close=100.0, ema50=99.0, obv_trend=0.3, atr14=1.0)
    h4 = _tf(ema20=100.0, ema50=100.5)
    sig = _signals(h1, h4, trend="down")
    d = await decider.decide(_ctx(sig, holding=True, prev_4h=(101.0, 100.5)))
    assert d.action == "SELL"
    assert "crossed" in d.reasoning


async def test_holding_hold_lists_reasons() -> None:
    decider = RulesDecider()
    d = await decider.decide(_ctx(_bullish_signals(), holding=True))
    assert d.action == "HOLD"
    assert "conditions met" in d.reasoning
