"""Indicator definitions: reference values on a fixed candle fixture, read
boundaries, levels, structure and the regime.

The reference values come from independent pandas/numpy implementations in
this file (different code path than app.agent.signals), plus hand-checked
edge cases — matching docs/SPEC.md "Testing and acceptance criteria".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.agent.market.base import Candle, drop_forming
from app.agent.signals import (
    TfValues,
    _raw_trend,
    adx,
    atr,
    atr_percentile,
    compute_pair_signals,
    compute_regime,
    confirm_trend_labels,
    ema,
    merge_levels,
    obv,
    obv_trend,
    reads_for_tf,
    rolling_vwap,
    rsi,
    structure_summary,
    swing_points,
    volume_ratio,
)
from app.models import MarketSettings

HOUR = timedelta(hours=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_candles(
    closes: np.ndarray, *, start: datetime = T0, step: timedelta = HOUR
) -> list[Candle]:
    """Flat candles from a close series: open = previous close."""
    out: list[Candle] = []
    prev = float(closes[0]) * 0.999
    for i, c in enumerate(closes):
        close = float(c)
        high = max(prev, close) * 1.001
        low = min(prev, close) * 0.999
        out.append(
            Candle(
                open_time=start + i * step,
                open=f"{prev:.10g}",
                high=f"{high:.10g}",
                low=f"{low:.10g}",
                close=f"{close:.10g}",
                volume="10",
            )
        )
        prev = close
    return out


@pytest.fixture
def fixture_closes() -> np.ndarray:
    rng = np.random.default_rng(42)
    return 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, 200)))


# ------------------------------------------------------------- reference impls
# Deliberately different formulations (pandas ewm / rolling) than the
# module under test.


def ref_ema(values: np.ndarray, n: int) -> np.ndarray:
    seed = float(np.mean(values[:n]))
    tail = pd.Series(values[n:])
    smoothed = pd.concat([pd.Series([seed]), tail]).ewm(span=n, adjust=False).mean().to_numpy()
    out = np.full(len(values), np.nan)
    out[n - 1] = seed
    out[n:] = np.asarray(smoothed[1:], dtype=np.float64)
    return out


def ref_rsi(closes: np.ndarray, n: int = 14) -> np.ndarray:
    deltas = np.diff(closes)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = float(np.mean(gains[:n]))
    avg_loss = float(np.mean(losses[:n]))

    def wilder(seed: float, xs: np.ndarray) -> np.ndarray:
        result = (
            pd.concat([pd.Series([seed]), pd.Series(xs)])
            .ewm(alpha=1 / n, adjust=False)
            .mean()
            .to_numpy()
        )
        return np.asarray(result, dtype=np.float64)

    rs_gains = wilder(avg_gain, gains[n:])
    rs_losses = wilder(avg_loss, losses[n:])
    out = np.full(len(closes), np.nan)
    rs = np.where(rs_losses == 0, np.inf, rs_gains / rs_losses)
    out[n:] = np.where(rs == np.inf, 100.0, 100.0 - 100.0 / (1.0 + rs))
    return out


def ref_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    tr = np.empty(len(close))
    tr[0] = high[0] - low[0]
    pc = close[:-1]
    tr[1:] = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - pc), abs(low[1:] - pc)))
    seed = float(np.mean(tr[1 : n + 1]))
    smoothed = (
        pd.concat([pd.Series([seed]), pd.Series(tr[n + 1 :])])
        .ewm(alpha=1 / n, adjust=False)
        .mean()
        .to_numpy()
    )
    out = np.full(len(close), np.nan)
    out[n:] = smoothed
    return out


def ref_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    tr = np.empty(len(close))
    tr[0] = 0.0
    pc = close[:-1]
    tr[1:] = np.maximum(high[1:] - low[1:], np.maximum(abs(high[1:] - pc), abs(low[1:] - pc)))
    up = np.diff(high)
    dn = -np.diff(low)
    pdm = np.concatenate([[0.0], np.where((up > dn) & (up > 0), up, 0.0)])
    mdm = np.concatenate([[0.0], np.where((dn > up) & (dn > 0), dn, 0.0)])

    def wilder_sum(xs: np.ndarray) -> np.ndarray:
        out = np.full(len(xs), np.nan)
        out[n] = float(np.sum(xs[1 : n + 1]))
        for i in range(n + 1, len(xs)):
            out[i] = out[i - 1] - out[i - 1] / n + xs[i]
        return out

    sm_tr, sm_p, sm_m = wilder_sum(tr), wilder_sum(pdm), wilder_sum(mdm)
    pdi, mdi = 100 * sm_p / sm_tr, 100 * sm_m / sm_tr
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    out = np.full(len(close), np.nan)
    first = 2 * n - 1
    out[first] = float(np.mean(dx[n : first + 1]))
    for i in range(first + 1, len(close)):
        out[i] = (out[i - 1] * (n - 1) + dx[i]) / n
    return out


# ---------------------------------------------------------------- core values


def test_ema_matches_reference(fixture_closes: np.ndarray) -> None:
    for n in (20, 50, 200):
        got = ema(fixture_closes, n)
        want = ref_ema(fixture_closes, n)
        np.testing.assert_allclose(got[~np.isnan(want)], want[~np.isnan(want)], rtol=1e-9)
        assert np.isnan(got[: n - 1]).all()


def test_ema_warmup_constant() -> None:
    got = ema(np.full(300, 42.0), 200)
    assert got[-1] == pytest.approx(42.0)


def test_rsi_matches_reference(fixture_closes: np.ndarray) -> None:
    got = rsi(fixture_closes)
    want = ref_rsi(fixture_closes)
    mask = ~np.isnan(want)
    np.testing.assert_allclose(got[mask], want[mask], atol=1e-8)


def test_rsi_edge_cases() -> None:
    assert rsi(np.arange(1.0, 40.0))[-1] == pytest.approx(100.0)  # only gains
    assert rsi(np.arange(40.0, 1.0, -1.0))[-1] == pytest.approx(0.0)  # only losses


def test_atr_matches_reference(fixture_closes: np.ndarray) -> None:
    candles = make_candles(fixture_closes)
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    close = np.array([float(c.close) for c in candles])
    got = atr(high, low, close)
    want = ref_atr(high, low, close)
    mask = ~np.isnan(want)
    np.testing.assert_allclose(got[mask], want[mask], rtol=1e-9)


def test_adx_matches_reference(fixture_closes: np.ndarray) -> None:
    candles = make_candles(fixture_closes)
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    close = np.array([float(c.close) for c in candles])
    got, _, _ = adx(high, low, close)
    want = ref_adx(high, low, close)
    mask = ~np.isnan(want)
    np.testing.assert_allclose(got[mask], want[mask], rtol=1e-9)


def test_adx_trending_market_is_high() -> None:
    strong = 100.0 * np.exp(np.cumsum(np.full(120, 0.01)))
    candles = make_candles(strong)
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    close = np.array([float(c.close) for c in candles])
    got, pdi, mdi = adx(high, low, close)
    assert got[-1] > 25
    assert pdi[-1] > mdi[-1]


def test_vwap_matches_reference(fixture_closes: np.ndarray) -> None:
    candles = make_candles(fixture_closes)
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    close = np.array([float(c.close) for c in candles])
    volume = np.array([float(c.volume) for c in candles])
    got = rolling_vwap(high, low, close, volume)
    typical = pd.Series((high + low + close) / 3)
    want = (
        (typical * pd.Series(volume)).rolling(24).sum() / pd.Series(volume).rolling(24).sum()
    ).to_numpy()
    mask = ~np.isnan(want)
    np.testing.assert_allclose(got[mask], want[mask], rtol=1e-9)


def test_obv_and_trend() -> None:
    close = np.array([10.0, 11.0, 10.5, 10.5, 12.0])
    volume = np.array([5.0, 5.0, 5.0, 5.0, 5.0])
    np.testing.assert_allclose(obv(close, volume), [0.0, 5.0, 0.0, 0.0, 5.0])
    rising = np.arange(1.0, 30.0)
    vol = np.full(29, 10.0)
    assert obv_trend(rising, vol) == pytest.approx(1.0)  # every bar up
    assert obv_trend(rising[::-1], vol) == pytest.approx(-1.0)  # every bar down


def test_volume_ratio() -> None:
    volume = np.concatenate([np.full(19, 10.0), [30.0]])
    assert volume_ratio(volume) == pytest.approx(30.0 / np.mean(volume))


def test_atr_percentile_bounds() -> None:
    series = np.arange(1.0, 101.0)  # last is max
    assert atr_percentile(series, 50) == pytest.approx(100.0)
    assert atr_percentile(series[::-1], 50) == pytest.approx(2.0)


def test_swing_points_and_merge() -> None:
    high = np.array([1.0, 2.0, 3.0, 9.0, 3.0, 2.0, 1.0, 5.0, 4.0, 3.0, 2.0])
    low = 10.0 - high
    highs, lows = swing_points(high, low, side=3)
    assert (3, 9.0) in highs
    assert (3, 1.0) in lows
    assert merge_levels([100.0, 100.2, 105.0], 0.3) == [pytest.approx(100.1), 105.0]


def test_drop_forming() -> None:
    candles = make_candles(np.full(5, 100.0))
    now = T0 + timedelta(hours=4, minutes=30)  # inside the last bar
    closed = drop_forming(candles, "1h", now)
    assert [c.open_time for c in closed] == [c.open_time for c in candles[:-1]]
    all_closed = drop_forming(candles, "1h", T0 + timedelta(hours=5, minutes=1))
    assert len(all_closed) == 5


# ---------------------------------------------------------------------- reads


def _values(**kw: float | None) -> TfValues:
    base: dict[str, Any] = dict.fromkeys(TfValues.model_fields)
    base.update(kw)
    return TfValues(**base)


def test_reads_boundaries() -> None:
    reads = reads_for_tf(
        _values(
            rsi14=55.0,
            price_vs_vwap_pct=0.11,
            price_vs_ema200_pct=-0.11,
            obv_trend=0.1,
            adx14=25.0,
            atr_percentile=80.0,
            volume_ratio=1.5,
            ema20=10.0,
            ema50=9.0,
        ),
        "1h",
        25.0,
    )
    by_name = {s.name: s.read for s in reads}
    assert by_name["RSI 14"] == "neutral"  # above 55 is bullish, exactly 55 is not
    assert by_name["Price vs VWAP"] == "bullish"
    assert by_name["Price vs EMA 200"] == "bearish"
    assert by_name["OBV trend"] == "neutral"  # beyond ±0.1 required
    assert by_name["EMA 20 vs 50"] == "bullish"
    assert by_name["ADX 14"] == "trending"  # at the threshold = trending
    assert by_name["ATR %"] == "normal"  # above the 80th is high
    assert by_name["Volume x avg"] == "normal"  # above 1.5 is high


def test_reads_other_side() -> None:
    reads = reads_for_tf(
        _values(
            rsi14=44.0,
            obv_trend=-0.2,
            adx14=30.0,
            atr_percentile=20.0,
            volume_ratio=0.6,
            ema20=9.0,
            ema50=10.0,
        ),
        "4h",
        25.0,
    )
    by_name = {s.name: s.read for s in reads}
    assert by_name["RSI 14"] == "bearish"
    assert by_name["OBV trend"] == "bearish"
    assert by_name["ADX 14"] == "trending"
    assert by_name["ATR %"] == "low"
    assert by_name["Volume x avg"] == "low"
    assert by_name["EMA 20 vs 50"] == "bearish"


# ---------------------------------------------------------------------- regime


def test_raw_trend_adx_boundary() -> None:
    # White-box on purpose: a real candle series cannot produce an ADX of
    # exactly the threshold, so the internal seam is called directly.
    # ADX at the threshold counts as trending; just below it the regime is
    # sideways even when the EMAs and daily agree.
    threshold = 25.0
    assert _raw_trend(threshold, 11.0, 10.0, 105.0, 100.0, threshold) == "up"
    assert _raw_trend(threshold, 9.0, 10.0, 95.0, 100.0, threshold) == "down"
    assert _raw_trend(threshold - 0.1, 11.0, 10.0, 105.0, 100.0, threshold) == "sideways"
    assert _raw_trend(threshold - 0.1, 9.0, 10.0, 95.0, 100.0, threshold) == "sideways"


def test_confirm_labels_two_bar_rule() -> None:
    assert confirm_trend_labels(["up", "sideways", "up"]) == ["up", "up", "up"]
    assert confirm_trend_labels(["up", "down", "down"]) == ["up", "up", "down"]
    assert confirm_trend_labels(["up", "down", "down", "up"]) == ["up", "up", "down", "down"]
    # A single dissenting bar never flips the label.
    assert confirm_trend_labels(["up", "up", "down", "up"]) == ["up", "up", "up", "up"]


def _trending_candles(
    n: int, start_price: float, up: bool, start: datetime, step: timedelta
) -> list[Candle]:
    drift = 0.008 if up else -0.008
    closes = start_price * np.exp(np.cumsum(np.full(n, drift)))
    return make_candles(closes, start=start, step=step)


def test_regime_up_and_age() -> None:
    settings = MarketSettings()
    four_h_start = T0
    daily_start = T0 - timedelta(days=400)
    candles_1d = _trending_candles(300, 50.0, True, daily_start, timedelta(days=1))
    candles_4h = _trending_candles(300, 60.0, True, four_h_start, timedelta(hours=4))
    regime = compute_regime(candles_4h, candles_1d, 55.0, float(settings.adx_threshold))
    assert regime.trend == "up"
    assert regime.volatility == "normal"  # 55th percentile
    # raw "up" starts once the 4h EMA 50 is valid (bar 99) and confirms one
    # bar later, so the label has held for ~199 of the 300 bars.
    assert regime.age_4h >= 150


def test_regime_sideways_when_timeframes_disagree() -> None:
    settings = MarketSettings()
    candles_1d = _trending_candles(300, 50.0, False, T0 - timedelta(days=400), timedelta(days=1))
    candles_4h = _trending_candles(300, 60.0, True, T0, timedelta(hours=4))
    regime = compute_regime(candles_4h, candles_1d, 50.0, float(settings.adx_threshold))
    assert regime.trend == "sideways"


def test_regime_volatility_bands() -> None:
    settings = MarketSettings()
    candles_4h = _trending_candles(50, 60.0, True, T0, timedelta(hours=4))
    assert compute_regime(candles_4h, [], 20.0, float(settings.adx_threshold)).volatility == "low"
    assert compute_regime(candles_4h, [], 85.0, float(settings.adx_threshold)).volatility == "high"
    assert (
        compute_regime(candles_4h, [], 50.0, float(settings.adx_threshold)).volatility == "normal"
    )


def test_regime_trend_change_needs_two_bars() -> None:
    settings = MarketSettings()
    # Long uptrend, then a single weak bar, then a real downtrend.
    candles_1d = _trending_candles(300, 50.0, True, T0 - timedelta(days=400), timedelta(days=1))
    up = _trending_candles(150, 60.0, True, T0, timedelta(hours=4))
    closes = np.array([float(c.close) for c in up])
    # One flat-ish 4h bar, then a sharp drop long enough to flip EMAs is not
    # needed — a single-bar fakeout must not relabel.
    fake = make_candles(
        np.array([closes[-1] * 0.995]),
        start=up[-1].open_time + timedelta(hours=4),
        step=timedelta(hours=4),
    )
    series = up + fake
    regime = compute_regime(series, candles_1d, 50.0, float(settings.adx_threshold))
    assert regime.trend == "up"


def test_structure_templates() -> None:
    # Long flat range, then a slow drift up so several closes sit above EMA20.
    closes = np.concatenate([np.full(80, 100.0), np.linspace(100.0, 103.0, 30)])
    candles = make_candles(closes)
    phrases = structure_summary(candles, min_range_bars=12)
    assert any("closes above EMA 20" in p for p in phrases)


def test_pair_signals_smoke(fixture_closes: np.ndarray) -> None:
    candles_1h = make_candles(fixture_closes[:150])
    candles_4h = make_candles(fixture_closes[:150], step=timedelta(hours=4))
    candles_1d = _trending_candles(300, 50.0, True, T0 - timedelta(days=400), timedelta(days=1))
    out = compute_pair_signals(
        "TEST-EUR",
        candles_1h,
        candles_4h,
        candles_1d,
        btc_1h=candles_1h,
        usdc_1h=candles_1h,
        now=T0 + timedelta(hours=200),
    )
    assert out.tf["1h"].close == pytest.approx(float(candles_1h[-1].close))
    assert out.tf["1h"].rsi14 is not None
    assert out.regime.trend in ("up", "down", "sideways")
    assert out.context.corr_btc_30d == pytest.approx(1.0)  # same series
    assert out.context.eur_usd_24h_pct is not None
    names = {(s.name, s.tf) for s in out.signals}
    assert ("RSI 14", "1h") in names and ("ADX 14", "4h") in names
