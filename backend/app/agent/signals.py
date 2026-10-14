"""Indicator maths, reads, levels, structure and the regime.

Everything here follows docs/SPEC.md "Indicator definitions" exactly and runs
on closed candles only, in UTC. The brief, the Indicators screen, the Rules
engine and the signal-claims check all use these values, so they can never
disagree. Floats only inside this module — money stays Decimal elsewhere.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel

from app.agent.market.base import Candle
from app.models import MarketSettings, Regime, Signal, SignalRead, Tf

EMA_WARMUP_BARS = 50  # an EMA is valid only after n + 50 bars
ATR_PCT_LOOKBACK: dict[Tf, int] = {"1h": 720, "4h": 180, "1d": 30}  # 30 days per tf
VWAP_WINDOW = 24
OBV_TREND_BARS = 20
VOLUME_AVG_BARS = 20
SWING_SIDE_BARS = 3
LEVEL_BARS_1H = 200
LEVEL_BARS_4H = 120
LEVEL_MERGE_ATR = 0.3
CORR_BARS = 720  # 30 days of 1h bars

Floats = NDArray[np.float64]


# ------------------------------------------------------------------ raw maths


def ema(values: Floats, n: int) -> Floats:
    """EMA seeded with the SMA of the first n values; NaN before index n-1."""
    out = np.full(len(values), np.nan)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    out[n - 1] = float(np.mean(values[:n]))
    for i in range(n, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def ema_valid_from(n: int) -> int:
    """First bar index at which an EMA(n) is valid (after n + 50 bars)."""
    return n + EMA_WARMUP_BARS - 1


def rsi(closes: Floats, n: int = 14) -> Floats:
    """Wilder's RSI on closes; NaN before index n."""
    out = np.full(len(closes), np.nan)
    if len(closes) <= n:
        return out
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:n]))
    avg_loss = float(np.mean(losses[:n]))
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        g, lo = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + lo) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(high: Floats, low: Floats, close: Floats) -> Floats:
    """TR[0] = high-low; afterwards the full Wilder true range."""
    tr = np.empty(len(close))
    tr[0] = high[0] - low[0]
    prev_close = close[:-1]
    tr[1:] = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    return tr


def _wilder_sum_seed(values: Floats, n: int, start: int = 1) -> Floats:
    """Wilder smoothing seeded with the SUM of the first n values (from
    ``start``). Used for TR and directional movement, where Wilder seeds with
    a sum rather than an average."""
    out = np.full(len(values), np.nan)
    if len(values) <= start + n - 1:
        return out
    out[start + n - 1] = float(np.sum(values[start : start + n]))
    for i in range(start + n, len(values)):
        out[i] = out[i - 1] - out[i - 1] / n + values[i]
    return out


def atr(high: Floats, low: Floats, close: Floats, n: int = 14) -> Floats:
    """Wilder's ATR. First value at index n is the mean of TR[1..n]."""
    tr = true_range(high, low, close)
    sm = _wilder_sum_seed(tr, n, start=1)
    return sm / n


def adx(high: Floats, low: Floats, close: Floats, n: int = 14) -> tuple[Floats, Floats, Floats]:
    """Wilder's ADX from +DI and -DI. Returns (adx, +di, -di).

    +DM/-DM and TR are Wilder-smoothed (sum-seeded); DX is the normalised DI
    gap; ADX is the mean-seeded Wilder average of the first n DX values, so
    the first ADX lands at index 2n-1.
    """
    nan = np.full(len(close), np.nan)
    if len(close) < 2 * n:
        return nan, nan.copy(), nan.copy()
    up_move = np.diff(high)
    down_move = -np.diff(low)
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    # Align to price bars: index i covers move from i-1 to i; bar 0 has none.
    plus_dm = np.concatenate([[0.0], plus_dm])
    minus_dm = np.concatenate([[0.0], minus_dm])
    tr = true_range(high, low, close)

    sm_tr = _wilder_sum_seed(tr, n, start=1)
    sm_p = _wilder_sum_seed(plus_dm, n, start=1)
    sm_m = _wilder_sum_seed(minus_dm, n, start=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = 100.0 * sm_p / sm_tr
        minus_di = 100.0 * sm_m / sm_tr
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)

    out = np.full(len(close), np.nan)
    first_dx = n  # first index where dx is defined
    first_adx = first_dx + n - 1
    if first_adx < len(close):
        window = dx[first_dx : first_adx + 1]
        if not np.isnan(window).any():
            out[first_adx] = float(np.mean(window))
            for i in range(first_adx + 1, len(close)):
                out[i] = (out[i - 1] * (n - 1) + dx[i]) / n
    plus_di = np.where(np.isnan(plus_di), np.nan, plus_di)
    minus_di = np.where(np.isnan(minus_di), np.nan, minus_di)
    return out, plus_di, minus_di


def rolling_vwap(
    high: Floats, low: Floats, close: Floats, volume: Floats, window: int = VWAP_WINDOW
) -> Floats:
    """Rolling VWAP: sum(typical*vol)/sum(vol) over the last ``window`` bars."""
    typical = (high + low + close) / 3.0
    pv = typical * volume
    out = np.full(len(close), np.nan)
    if len(close) < window:
        return out
    cum_pv = np.concatenate([[0.0], np.cumsum(pv)])
    cum_v = np.concatenate([[0.0], np.cumsum(volume)])
    num = cum_pv[window:] - cum_pv[:-window]
    den = cum_v[window:] - cum_v[:-window]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[window - 1 :] = np.where(den > 0, num / den, np.nan)
    return out


def obv(close: Floats, volume: Floats) -> Floats:
    """On-balance volume: cumulative signed volume."""
    direction = np.sign(np.diff(close, prepend=close[0]))
    return np.cumsum(direction * volume)


def obv_trend(close: Floats, volume: Floats, bars: int = OBV_TREND_BARS) -> float | None:
    """(OBV now - OBV ``bars`` ago) / total volume of those bars; range -1..1."""
    if len(close) <= bars:
        return None
    o = obv(close, volume)
    total = float(np.sum(volume[-bars:]))
    if total <= 0:
        return None
    return float(np.clip((o[-1] - o[-1 - bars]) / total, -1.0, 1.0))


def volume_ratio(volume: Floats, bars: int = VOLUME_AVG_BARS) -> float | None:
    """Last closed bar's volume / simple average of the last ``bars`` volumes."""
    if len(volume) < bars:
        return None
    avg = float(np.mean(volume[-bars:]))
    return float(volume[-1] / avg) if avg > 0 else None


def atr_percentile(atr_pct: Floats, lookback: int) -> float | None:
    """Percentile rank of the latest ATR% inside the last ``lookback`` values."""
    window = atr_pct[~np.isnan(atr_pct)][-lookback:]
    if len(window) < 2 or np.isnan(atr_pct[-1]):
        return None
    current = window[-1]
    return float(np.sum(window <= current) / len(window) * 100.0)


def swing_points(
    high: Floats, low: Floats, side: int = SWING_SIDE_BARS
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(index, price) of bars that are the strict high/low of ``side`` bars
    on each side."""
    span = 2 * side + 1
    if len(high) < span:
        return [], []
    # Window j covers bars j..j+2*side; its centre is bar j+side. Vectorised:
    # a bar is a swing high when it strictly exceeds both flanks' maxima.
    wh = np.lib.stride_tricks.sliding_window_view(high, span)
    wl = np.lib.stride_tricks.sliding_window_view(low, span)
    hi_hit = wh[:, side] > np.maximum(wh[:, :side].max(axis=1), wh[:, side + 1 :].max(axis=1))
    lo_hit = wl[:, side] < np.minimum(wl[:, :side].min(axis=1), wl[:, side + 1 :].min(axis=1))
    highs = [(int(i), float(high[i])) for i in np.flatnonzero(hi_hit) + side]
    lows = [(int(i), float(low[i])) for i in np.flatnonzero(lo_hit) + side]
    return highs, lows


def merge_levels(levels: list[float], within: float) -> list[float]:
    """Merge swing levels closer than ``within`` into their mean."""
    merged: list[list[float]] = []
    for level in sorted(levels):
        if merged and level - merged[-1][-1] <= within:
            merged[-1].append(level)
        else:
            merged.append([level])
    return [float(np.mean(group)) for group in merged]


def log_returns_by_time(candles: list[Candle]) -> dict[datetime, float]:
    """open_time -> log return vs the previous bar in the same series."""
    out: dict[datetime, float] = {}
    closes = [float(c.close) for c in candles]
    for i in range(1, len(candles)):
        if closes[i - 1] > 0 and closes[i] > 0:
            out[candles[i].open_time] = float(np.log(closes[i] / closes[i - 1]))
    return out


def aligned_correlation(a: list[Candle], b: list[Candle], max_bars: int) -> float | None:
    """Pearson correlation of 1h log returns on bars present in both series."""
    ra, rb = log_returns_by_time(a), log_returns_by_time(b)
    times = sorted(set(ra) & set(rb))[-max_bars:]
    if len(times) < 30:
        return None
    x = np.array([ra[t] for t in times])
    y = np.array([rb[t] for t in times])
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def period_return(closes: Floats, bars: int) -> float | None:
    """Simple return over the last ``bars`` bars, in fraction."""
    if len(closes) <= bars or closes[-1 - bars] == 0:
        return None
    return float(closes[-1] / closes[-1 - bars] - 1.0)


# --------------------------------------------------------------------- output


class TfValues(BaseModel):
    """Every indicator value on one timeframe, latest closed bar.

    ``None`` means not enough history yet (EMA warm-up, short series); the
    callers treat it as "no read", never as a trade signal.
    """

    close: float | None
    ema20: float | None
    ema50: float | None
    ema200: float | None
    rsi14: float | None
    atr14: float | None
    atr_pct: float | None
    atr_percentile: float | None
    adx14: float | None
    plus_di14: float | None
    minus_di14: float | None
    vwap: float | None
    price_vs_vwap_pct: float | None
    price_vs_ema200_pct: float | None
    obv_trend: float | None
    volume_ratio: float | None


class Levels(BaseModel):
    support: list[float]  # nearest below price first
    resistance: list[float]  # nearest above price first
    prev_day_high: float | None
    prev_day_low: float | None


class MarketContext(BaseModel):
    rs_btc_24h_pct: float | None
    rs_btc_7d_pct: float | None
    corr_btc_30d: float | None
    eur_usd_1h_pct: float | None
    eur_usd_24h_pct: float | None


class PairSignals(BaseModel):
    """Everything a pair's latest closed data says, in one place."""

    pair: str
    at: datetime
    tf: dict[str, TfValues]
    signals: list[Signal]
    levels: Levels
    structure: list[str]
    regime: Regime
    context: MarketContext


# ----------------------------------------------------------------- per-tf values


def _arrays(candles: list[Candle]) -> tuple[Floats, Floats, Floats, Floats]:
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    close = np.array([float(c.close) for c in candles])
    volume = np.array([float(c.volume) for c in candles])
    return high, low, close, volume


def _last(series: Floats) -> float | None:
    return float(series[-1]) if len(series) and not np.isnan(series[-1]) else None


def compute_tf_values(candles: list[Candle], tf: Tf) -> TfValues:
    """All indicator values on one timeframe's latest closed bar."""
    none = TfValues(
        close=None,
        ema20=None,
        ema50=None,
        ema200=None,
        rsi14=None,
        atr14=None,
        atr_pct=None,
        atr_percentile=None,
        adx14=None,
        plus_di14=None,
        minus_di14=None,
        vwap=None,
        price_vs_vwap_pct=None,
        price_vs_ema200_pct=None,
        obv_trend=None,
        volume_ratio=None,
    )
    if not candles:
        return none
    high, low, close, volume = _arrays(candles)
    ema20, ema50, ema200 = ema(close, 20), ema(close, 50), ema(close, 200)
    atr14 = atr(high, low, close)
    adx14, plus_di, minus_di = adx(high, low, close)
    vwap = rolling_vwap(high, low, close, volume)
    atr_pct_arr = atr14 / close * 100.0
    last_close = float(close[-1])
    i = len(close) - 1

    def vs(ref: float | None) -> float | None:
        return (last_close / ref - 1.0) * 100.0 if ref else None

    return TfValues(
        close=last_close,
        ema20=_last(ema20) if i >= ema_valid_from(20) else None,
        ema50=_last(ema50) if i >= ema_valid_from(50) else None,
        ema200=_last(ema200) if i >= ema_valid_from(200) else None,
        rsi14=_last(rsi(close)),
        atr14=_last(atr14),
        atr_pct=float(atr_pct_arr[-1]) if not np.isnan(atr_pct_arr[-1]) else None,
        atr_percentile=atr_percentile(atr_pct_arr, ATR_PCT_LOOKBACK[tf]),
        adx14=_last(adx14),
        plus_di14=_last(plus_di),
        minus_di14=_last(minus_di),
        vwap=_last(vwap),
        price_vs_vwap_pct=vs(_last(vwap)),
        price_vs_ema200_pct=vs(_last(ema200)),
        obv_trend=obv_trend(close, volume),
        volume_ratio=volume_ratio(volume),
    )


# ----------------------------------------------------------------------- reads


def rsi_read(v: float | None) -> SignalRead | None:
    if v is None:
        return None
    return "bullish" if v > 55 else "bearish" if v < 45 else "neutral"


def price_rel_read(pct: float | None) -> SignalRead | None:
    """Read of a % distance from a reference (VWAP, EMA 200)."""
    if pct is None:
        return None
    return "bullish" if pct > 0.1 else "bearish" if pct < -0.1 else "neutral"


def obv_read(v: float | None) -> SignalRead | None:
    if v is None:
        return None
    return "bullish" if v > 0.1 else "bearish" if v < -0.1 else "neutral"


def ema_cross_read(ema20: float | None, ema50: float | None) -> SignalRead | None:
    if ema20 is None or ema50 is None:
        return None
    return "bullish" if ema20 > ema50 else "bearish"


def adx_read(v: float | None, adx_threshold: float) -> SignalRead | None:
    if v is None:
        return None
    return "trending" if v >= adx_threshold else "ranging"


def atr_rank_read(p: float | None) -> SignalRead | None:
    if p is None:
        return None
    return "low" if p < 30 else "high" if p > 80 else "normal"


def volume_read(v: float | None) -> SignalRead | None:
    if v is None:
        return None
    return "high" if v > 1.5 else "low" if v < 0.7 else "normal"


def _sig(name: str, tf: Tf, read: SignalRead) -> Signal:
    return Signal(name=name, tf=tf, read=read)


def reads_for_tf(values: TfValues, tf: Tf, adx_threshold: float) -> list[Signal]:
    """Bullish / bearish / neutral (and trending/low/high) labels per spec."""
    out: list[Signal] = []
    read = rsi_read(values.rsi14)
    if read is not None:
        out.append(_sig("RSI 14", tf, read))
    for name, pct in (
        ("Price vs VWAP", values.price_vs_vwap_pct),
        ("Price vs EMA 200", values.price_vs_ema200_pct),
    ):
        read = price_rel_read(pct)
        if read is not None:
            out.append(_sig(name, tf, read))
    read = obv_read(values.obv_trend)
    if read is not None:
        out.append(_sig("OBV trend", tf, read))
    read = ema_cross_read(values.ema20, values.ema50)
    if read is not None:
        out.append(_sig("EMA 20 vs 50", tf, read))
    read = adx_read(values.adx14, adx_threshold)
    if read is not None:
        out.append(_sig("ADX 14", tf, read))
    read = atr_rank_read(values.atr_percentile)
    if read is not None:
        out.append(_sig("ATR %", tf, read))
        out.append(_sig("ATR percentile", tf, read))
    read = volume_read(values.volume_ratio)
    if read is not None:
        out.append(_sig("Volume x avg", tf, read))
    return out


# ---------------------------------------------------------------------- levels


def compute_levels(
    candles_1h: list[Candle], candles_4h: list[Candle], atr_1h: float | None
) -> list[float]:
    """Merged swing levels from the last 200 1h and 120 4h bars."""
    if atr_1h is None or atr_1h <= 0:
        return []
    points: list[float] = []
    for candles, keep in ((candles_1h, LEVEL_BARS_1H), (candles_4h, LEVEL_BARS_4H)):
        tail = candles[-keep:]
        if len(tail) < 2 * SWING_SIDE_BARS + 1:
            continue
        high, low, _, _ = _arrays(tail)
        highs, lows = swing_points(high, low)
        points.extend(p for _, p in highs)
        points.extend(p for _, p in lows)
    return merge_levels(points, LEVEL_MERGE_ATR * atr_1h)


def nearest_levels(
    merged: list[float], price: float, count: int = 3
) -> tuple[list[float], list[float]]:
    """The ``count`` nearest levels below (support) and above (resistance)."""
    below = sorted((lv for lv in merged if lv < price), key=lambda lv: price - lv)[:count]
    above = sorted((lv for lv in merged if lv > price), key=lambda lv: lv - price)[:count]
    return below, above


def prev_day_high_low(candles_1h: list[Candle], now: datetime) -> tuple[float | None, float | None]:
    """High and low of the previous UTC day, from 1h candles."""
    today = now.astimezone(UTC).date()
    yesterday = date.fromordinal(today.toordinal() - 1)
    bars = [c for c in candles_1h if c.open_time.astimezone(UTC).date() == yesterday]
    if not bars:
        return None, None
    return float(max(c.high for c in bars)), float(min(c.low for c in bars))


# ------------------------------------------------------------------ structure


def _swing_run(points: list[tuple[int, float]], increasing: bool) -> int:
    """Trailing run of swing points each higher (or lower) than the previous."""
    run = 0
    for i in range(len(points) - 1, 0, -1):
        higher = points[i][1] > points[i - 1][1]
        if higher == increasing:
            run += 1
        else:
            break
    return run + 1 if run else 0


def structure_summary(candles_1h: list[Candle], min_range_bars: int) -> list[str]:
    """Template phrases: EMA-20 streak, swing sequences, range-bound."""
    out: list[str] = []
    if len(candles_1h) < SWING_SIDE_BARS * 2 + 2:
        return out
    high, low, close, _ = _arrays(candles_1h)
    ema20 = ema(close, 20)

    streak = 0
    side = 0
    for i in range(len(close) - 1, -1, -1):
        if np.isnan(ema20[i]) or close[i] == ema20[i]:
            break
        s = 1 if close[i] > ema20[i] else -1
        if side == 0:
            side = s
        if s != side:
            break
        streak += 1
    if streak >= 2:
        out.append(f"{streak} consecutive closes {'above' if side > 0 else 'below'} EMA 20")

    highs, lows = swing_points(high, low)
    run_h = _swing_run(highs, increasing=True)
    run_l = _swing_run(lows, increasing=False)
    if run_h >= 2 and run_h >= run_l:
        out.append(f"{run_h} consecutive higher swing highs")
    elif run_l >= 2:
        out.append(f"{run_l} consecutive lower swing lows")

    atr14 = atr(high, low, close)
    current_atr = _last(atr14)
    if current_atr and current_atr > 0:
        limit = 1.5 * current_atr
        n = 0
        for n_try in range(2, min(len(close), LEVEL_BARS_1H) + 1):
            window = high[-n_try:]
            win_low = low[-n_try:]
            if float(np.max(window)) - float(np.min(win_low)) <= limit:
                n = n_try
            else:
                break
        if n >= min_range_bars:
            out.append(f"range-bound for {n} bars")
    return out


# ---------------------------------------------------------------------- regime


def _raw_trend(
    adx_v: float,
    ema20_v: float,
    ema50_v: float,
    daily_close: float,
    daily_ema200: float,
    threshold: float,
) -> Literal["up", "down", "sideways"]:
    if adx_v >= threshold and ema20_v > ema50_v and daily_close > daily_ema200:
        return "up"
    if adx_v >= threshold and ema20_v < ema50_v and daily_close < daily_ema200:
        return "down"
    return "sideways"


def confirm_trend_labels(raw: list[str]) -> list[str]:
    """Apply the 2-bar confirmation to a per-bar raw trend series.

    A new label only takes effect after two consecutive raw bars agree; a
    single dissenting bar is absorbed into the previous label.
    """
    if not raw:
        return []
    confirmed = [raw[0]]
    for i in range(1, len(raw)):
        if raw[i] == confirmed[-1] or raw[i] != raw[i - 1]:
            confirmed.append(confirmed[-1])
        else:
            confirmed.append(raw[i])
    return confirmed


def trend_labels_4h(
    candles_4h: list[Candle], candles_1d: list[Candle], adx_threshold: float
) -> list[str]:
    """The confirmed trend label of every closed 4h bar.

    The raw trend is evaluated per closed 4h bar (4h ADX, 4h EMA cross, and
    the most recent closed daily bar's close vs daily EMA 200); the 2-bar
    confirmation is applied to the whole series. Used both for the current
    regime and for the Indicators regime band.
    """
    if not candles_4h:
        return []

    high, low, close, _ = _arrays(candles_4h)
    ema20, ema50 = ema(close, 20), ema(close, 50)
    adx14, _, _ = adx(high, low, close)
    n = len(close)

    d_close = np.array([float(c.close) for c in candles_1d]) if candles_1d else None
    d_ema200 = ema(d_close, 200) if d_close is not None else None
    d_close_time = [c.open_time.timestamp() + 86400 for c in candles_1d]

    raw: list[str] = []
    for i in range(n):
        if i < ema_valid_from(50) or np.isnan(adx14[i]) or d_close is None or d_ema200 is None:
            raw.append("sideways")
            continue
        close_time = candles_4h[i].open_time.timestamp() + 14400
        d = -1
        for j in range(len(d_close_time) - 1, -1, -1):
            if d_close_time[j] <= close_time:
                d = j
                break
        if d < 0 or d < ema_valid_from(200) or np.isnan(d_ema200[d]):
            raw.append("sideways")
            continue
        raw.append(_raw_trend(adx14[i], ema20[i], ema50[i], d_close[d], d_ema200[d], adx_threshold))
    return confirm_trend_labels(raw)


def compute_regime(
    candles_4h: list[Candle],
    candles_1d: list[Candle],
    atr_percentile_1h: float | None,
    adx_threshold: float,
) -> Regime:
    """Trend x volatility x age, with the 2-bar confirmation on trend changes.

    A new label only takes effect after two consecutive 4h bars agree, and
    age counts the closed 4h bars since the confirmed label last changed.
    """
    if atr_percentile_1h is None:
        vol: Literal["low", "normal", "high"] = "normal"
    elif atr_percentile_1h < 30:
        vol = "low"
    elif atr_percentile_1h > 80:
        vol = "high"
    else:
        vol = "normal"

    confirmed = trend_labels_4h(candles_4h, candles_1d, adx_threshold)
    if not confirmed:
        return Regime(trend="sideways", volatility=vol, age_4h=0)

    n = len(confirmed)
    current = confirmed[-1]
    # Age = closed 4h bars since the confirmed label last changed, i.e. the
    # index distance from the latest bar back to the start of the current run.
    run_start = n - 1
    while run_start > 0 and confirmed[run_start - 1] == current:
        run_start -= 1
    age = (n - 1) - run_start

    trend = cast(Literal["up", "down", "sideways"], current)
    return Regime(trend=trend, volatility=vol, age_4h=age)


# --------------------------------------------------------------------- context


def compute_context(
    candles_1h: list[Candle],
    btc_1h: list[Candle] | None,
    usdc_1h: list[Candle] | None,
) -> MarketContext:
    """Relative strength and correlation vs BTC, EUR/USD move via USDC-EUR."""
    close = np.array([float(c.close) for c in candles_1h])
    rs_24h = rs_7d = corr = None
    if btc_1h and candles_1h:
        btc_close = np.array([float(c.close) for c in btc_1h])
        p24, b24 = period_return(close, 24), period_return(btc_close, 24)
        p7d, b7d = period_return(close, 168), period_return(btc_close, 168)
        if p24 is not None and b24 is not None:
            rs_24h = (p24 - b24) * 100.0
        if p7d is not None and b7d is not None:
            rs_7d = (p7d - b7d) * 100.0
        corr = aligned_correlation(candles_1h, btc_1h, CORR_BARS)
    eu_1h = eu_24h = None
    if usdc_1h:
        usdc_close = np.array([float(c.close) for c in usdc_1h])
        r1, r24 = period_return(usdc_close, 1), period_return(usdc_close, 24)
        eu_1h = -r1 * 100.0 if r1 is not None else None
        eu_24h = -r24 * 100.0 if r24 is not None else None
    return MarketContext(
        rs_btc_24h_pct=rs_24h,
        rs_btc_7d_pct=rs_7d,
        corr_btc_30d=corr,
        eur_usd_1h_pct=eu_1h,
        eur_usd_24h_pct=eu_24h,
    )


# ----------------------------------------------------------------------- entry


def compute_pair_signals(
    pair: str,
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    candles_1d: list[Candle],
    *,
    btc_1h: list[Candle] | None = None,
    usdc_1h: list[Candle] | None = None,
    settings: MarketSettings | None = None,
    now: datetime | None = None,
) -> PairSignals:
    """Compute every indicator, read, level, the structure summary, context
    and the regime for one pair. All inputs must be closed candles only."""
    settings = settings or MarketSettings()
    now = now or datetime.now(UTC)
    adx_threshold = float(settings.adx_threshold)

    tf_values = {
        "1h": compute_tf_values(candles_1h, "1h"),
        "4h": compute_tf_values(candles_4h, "4h"),
        "1d": compute_tf_values(candles_1d, "1d"),
    }
    signals = [
        s
        for tf, values in tf_values.items()
        for s in reads_for_tf(values, tf, adx_threshold)  # type: ignore[arg-type]
    ]

    merged = compute_levels(candles_1h, candles_4h, tf_values["1h"].atr14)
    last_close = tf_values["1h"].close or 0.0
    support, resistance = nearest_levels(merged, last_close)
    pdh, pdl = prev_day_high_low(candles_1h, now)

    regime = compute_regime(
        candles_4h,
        candles_1d,
        tf_values["1h"].atr_percentile,
        adx_threshold,
    )

    return PairSignals(
        pair=pair,
        at=now,
        tf=tf_values,
        signals=signals,
        levels=Levels(
            support=support,
            resistance=resistance,
            prev_day_high=pdh,
            prev_day_low=pdl,
        ),
        structure=structure_summary(candles_1h, settings.structure_min_range_bars),
        regime=regime,
        context=compute_context(candles_1h, btc_1h, usdc_1h),
    )


def decision_signals(claims: list[Signal], reads: PairSignals | None) -> list[Signal]:
    """The signal list stored on a decision: the decider's own chips (C1..C6
    or the model's claims) first, then the computed per-tf reads, so the
    drawer's signal stack fills all three timeframe columns."""
    out = list(claims)
    if reads is not None:
        seen = {(s.name, s.tf) for s in out}
        out += [s for s in reads.signals if (s.name, s.tf) not in seen]
    return out
