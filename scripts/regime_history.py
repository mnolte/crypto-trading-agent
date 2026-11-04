#!/usr/bin/env python3
"""
regime_history.py — how long does each coin spend in each regime, and are its
sideways ranges wide enough to trade after costs?

Part 1, regimes. Labels every closed 4h bar with the Trading Agent's regime rules
(docs/SPEC.md, "Indicator definitions -> Regime") and reports per market the share of
time in each regime, how long runs last, down stretches per year and the current regime.
  up        4h ADX >= 25, 4h EMA 20 > EMA 50, daily close > daily EMA 200
  down      4h ADX >= 25, 4h EMA 20 < EMA 50, daily close < daily EMA 200
  sideways  everything else; a new label needs 2 closed 4h bars in a row

Part 2, sideways ranges (skip with --no-ranges). Uses 1h candles too:
  - width of every sideways stretch (highest high to lowest low)
  - every sideways hour where price is within 1 x ATR(1h) above the nearest support,
    as the agent's sideways rule requires: the room up to the nearest resistance, and
    whether a BUY would pass the agent's gates. The stop must sit just below a real
    support (support - 0.1 x ATR) inside the allowed zone (at least the fee-gate minimum
    below price, at most 5 x ATR(1h)), and the nearest resistance must give 2.0R.
    Checked for market orders (costs 0.6%, minimum stop 2.0%) and limit entries
    (costs 0.45%, minimum stop 1.5%).
  - a simple illustrative backtest of those setups: one position per pair, stop first
    when a candle touches both levels, stop to breakeven + fees at +1R, 5-day time stop,
    6h cooldown, fees and slippage included. No model, no other gates. Limit entries are
    assumed to always fill, so the limit result is an upper bound.
  Supports and resistances are swing lows and highs (3 bars each side) from the last
  200 1h bars and 120 4h bars, as in the agent, without merging nearby levels.

Part 3, up-regime trend trades (skip with --no-trend). The Rules BUY from the spec
("Rules engine"), evaluated on every closed 1h bar, no model:
  - regime up, C1 (4h EMA 20 > EMA 50 and daily close > EMA 200) and at least 4 of
    C2-C6 (1h EMA 20 > EMA 50, close > VWAP, RSI 45-70, OBV trend > +0.1, ADX >= 25),
    and price at most 1.5 x ATR(1h) above the 1h EMA 20
  - stop: lowest low of the last 6 1h bars - 0.2 x ATR, widened to the fee-gate minimum;
    no trade when that is more than 5 x ATR away
  - target "rules": nearest resistance within 6 x ATR, otherwise entry + 2.5R;
    target "2.5R": always entry + 2.5R (an alternative, not in the spec)
  - the 2.0R gate, then exits: stop, target, breakeven + fees at +1R, Rules SELL
    (1h close below EMA 50 with OBV trend below 0, or the 4h EMA 20 crossing below
    EMA 50), 5-day time stop; 6h cooldown after a stop-out
  Same costs as part 2. Limit entries are assumed to always fill (an upper bound).

Part 4, research lab (only with --lab). Splits the period into a training part and a
holdout test part (--split, default 2025-01-01) and reports both:
  - trend variants of the Rules BUY: the spec as is; a 2.5R target without the Rules
    SELL; a trailing stop from +2R without a target; and the same signals on 4h bars
    (2.5R target, or trailing with a 20-day time stop). Market costs, no model.
  - exposure filters: hold the coin while a filter is true, else EUR (regime up; C1;
    daily close above EMA 200), against buy and hold, with 0.3% per switch: return,
    max drawdown and time in the market.
  Tune ideas on the train columns only; a variant counts only if the test columns agree.

Part 5, portfolio test (only with --portfolio). All coins together, at 4h closes: hold up
to --slots coins (default 3) whose filter is true, each bought with equity / slots and
sold when the filter turns false; new candidates ranked by their 7-day return; 0.3% per
side. Filters: regime up, C1, daily close above EMA 200, and regime up with BTC on the
looser EMA 200 filter. Benchmarks: BTC buy and hold, static BTC + cash (20/30/50% BTC,
rebalanced about monthly), BTC only with each filter, and equal-weight buy and hold. Train,
test and full windows, plus a sensitivity grid (ADX 20/25/30 x 1-3 confirmation bars).

Part 6, strategy families (only with --families). Daily closes, all coins, 0.3% per side,
against BTC buy and hold and static BTC + cash, with train/test windows and a sensitivity grid:
  A  breakout trend following: buy a close above the highest high of the previous 55 days,
     exit on a trailing stop 3 x ATR(20) below the highest close since entry
  B  relative-strength rotation: every 7 days hold the top 3 coins by 60-day return, only
     if that return is positive, otherwise EUR
  C  dip buying in an uptrend: above the daily EMA 200, buy when RSI(2) < 10, sell when
     the close is above its 5-day SMA or after 10 days

Only reads public Bitvavo candles. No API key, no account, no orders, and it does
not touch the agent or its database. Standard library only. Candles are cached in
$TA_DATA_DIR/candle_cache (data/candle_cache by default) so a second run only
fetches what is new.

Usage:
  python3 regime_history.py                      # 9 default markets, 4 years
  python3 regime_history.py --years 6 BTC-EUR ETH-EUR
  python3 regime_history.py --no-ranges --no-trend   # part 1 only, much faster
  python3 regime_history.py --csv runs.csv --trades-csv trades.csv
  python3 regime_history.py --lab --no-ranges   # regimes, trend trades and the lab
  python3 regime_history.py --portfolio --no-ranges --no-trend   # regimes + portfolio test
  python3 regime_history.py --families --no-ranges --no-trend    # regimes + strategy families
"""

import argparse
import bisect
import csv
import json
import os
import statistics
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

API = "https://api.bitvavo.com/v2"
DEFAULT_MARKETS = [
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "ADA-EUR",
    "LINK-EUR",
    "AVAX-EUR",
    "DOGE-EUR",
    "LTC-EUR",
]
ADX_TREND = 25.0
CONFIRM_BARS = 2
H1_MS = 3600 * 1000
H4_MS = 4 * H1_MS
D1_MS = 24 * H1_MS
STEP = {"1h": H1_MS, "4h": H4_MS, "1d": D1_MS}
# Same data dir the backend uses (config.py: $TA_DATA_DIR, default ./data)
CACHE_DEFAULT = os.path.join(os.environ.get("TA_DATA_DIR", "data"), "candle_cache")

# Agent settings used by part 2 (Settings defaults in the spec)
MIN_RR = 2.0
MIN_STOP_ATR = 0.5
MAX_STOP_ATR = 5.0
STOP_BUFFER_ATR = 0.1
NEAR_SUPPORT_ATR = 1.0
TIME_STOP_H = 5 * 24
COOLDOWN_H = 6
SWING_K = 3
LOOKBACK_1H = 200
LOOKBACK_4H = 120
# per-leg costs in %: entry, exit at target, exit at stop / time stop / breakeven
# target rules: the agent's sideways rule (nearest resistance) and an alternative
# (the top of the range: highest 4h swing high of the last 120 4h bars above price)
TARGETS = ("nearest", "range_top")
# part 3 (Rules engine defaults in the spec)
RULES_MIN_CONDITIONS = 4
RULES_MAX_EXTENSION_ATR = 1.5
RULES_STOP_LOOKBACK = 6
RULES_STOP_BUFFER_ATR = 0.2
RULES_TARGET_MAX_ATR = 6.0
RULES_FALLBACK_R = 2.5
RSI_LOW, RSI_HIGH = 45.0, 70.0
OBV_BULL = 0.1
TREND_TARGETS = ("rules", "2.5R")
COSTS: dict[str, dict[str, float]] = {
    "market": {"entry": 0.30, "target": 0.30, "stop": 0.30, "min_stop_pct": 2.0},
    "limit": {"entry": 0.15, "target": 0.15, "stop": 0.30, "min_stop_pct": 1.5},
}

Bar = tuple[int, float, float, float, float]  # open_ts_ms, high, low, close, volume
Label = tuple[int, str | None]  # close_ts_ms, regime label
Run = tuple[str, int, int, int]  # label, start_close_ts, bars, first h4 index
Variant = tuple[str, str]  # (order mode, target rule)
Trade = tuple[int, int, str, float]  # entry_ts, exit_ts, reason, net_R


# ---------------------------------------------------------------- data


def _fetch(market: str, interval: str, since_ms: int, now_ms: int) -> dict[int, list[float]]:
    step = STEP[interval]
    out: dict[int, list[float]] = {}
    end = now_ms
    while True:
        url = f"{API}/{market}/candles?interval={interval}&limit=1440&end={end}"
        req = urllib.request.Request(url, headers={"User-Agent": "regime-history/1.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict):  # error object
            raise RuntimeError(f"{market} {interval}: {rows}")
        if not rows:
            break
        for ts, _o, h, low, c, v in rows:
            ts = int(ts)
            if ts + step <= now_ms:  # closed bars only (ts is the open time)
                out[ts] = [float(h), float(low), float(c), float(v)]
        oldest = min(int(r[0]) for r in rows)
        if oldest <= since_ms or len(rows) < 1440:
            break
        end = oldest - 1
        time.sleep(0.25)  # stay far below the rate limit
    return out


def fetch_candles(
    market: str, interval: str, since_ms: int, cache_dir: str | None = None
) -> list[Bar]:
    """Closed candles from since_ms until now, oldest first: (open_ts, high, low, close)."""
    now_ms = int(time.time() * 1000)
    cached: dict[int, list[float]] = {}
    path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{market}_{interval}.json")
        if os.path.exists(path):
            with open(path) as f:
                cached = {int(k): v for k, v in json.load(f).items()}
            if any(len(v) < 4 for v in cached.values()):  # cache from before volume was kept
                cached = {}
    if cached and min(cached) <= since_ms:
        fresh = _fetch(market, interval, max(cached), now_ms)  # only what is new
    else:
        fresh = _fetch(market, interval, since_ms, now_ms)
    cached.update(fresh)
    if path:
        with open(path, "w") as f:
            json.dump({str(k): v for k, v in cached.items()}, f)
    return [
        (t, cached[t][0], cached[t][1], cached[t][2], cached[t][3])
        for t in sorted(cached)
        if t >= since_ms
    ]


# ---------------------------------------------------------------- indicators


def ema(values: list[float], n: int) -> list[float | None]:
    """EMA seeded with the SMA of the first n values; None until n + 50 bars (spec)."""
    res: list[float | None] = [None] * len(values)
    if len(values) < n:
        return res
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    raw: list[float | None] = [None] * len(values)
    raw[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        raw[i] = e
    for i in range(len(values)):
        if i >= n + 50 - 1:
            res[i] = raw[i]
    return res


def atr(
    highs: list[float], lows: list[float], closes: list[float], n: int = 14
) -> list[float | None]:
    """Wilder's ATR. None until n bars."""
    size = len(closes)
    res: list[float | None] = [None] * size
    if size <= n:
        return res
    tr = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, size)
    ]
    a = sum(tr[1 : n + 1]) / n
    res[n] = a
    for i in range(n + 1, size):
        a = (a * (n - 1) + tr[i]) / n
        res[i] = a
    return res


def adx(
    highs: list[float], lows: list[float], closes: list[float], n: int = 14
) -> list[float | None]:
    """Wilder's ADX. None until enough bars."""
    size = len(closes)
    res: list[float | None] = [None] * size
    if size < 2 * n + 1:
        return res
    tr, pdm, mdm = [0.0] * size, [0.0] * size, [0.0] * size
    for i in range(1, size):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    s_tr = sum(tr[1 : n + 1])
    s_p = sum(pdm[1 : n + 1])
    s_m = sum(mdm[1 : n + 1])
    dx: list[float | None] = [None] * size

    def _dx(st: float, sp: float, sm: float) -> float:
        if st == 0:
            return 0.0
        pdi, mdi = 100 * sp / st, 100 * sm / st
        return 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)

    dx[n] = _dx(s_tr, s_p, s_m)
    for i in range(n + 1, size):
        s_tr = s_tr - s_tr / n + tr[i]
        s_p = s_p - s_p / n + pdm[i]
        s_m = s_m - s_m / n + mdm[i]
        dx[i] = _dx(s_tr, s_p, s_m)
    a = sum(x for x in dx[n : 2 * n] if x is not None) / n
    res[2 * n - 1] = a
    for i in range(2 * n, size):
        dx_i = dx[i]
        assert dx_i is not None  # filled by the smoothing loop above
        a = (a * (n - 1) + dx_i) / n
        res[i] = a
    return res


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI. None until n bars."""
    size = len(closes)
    res: list[float | None] = [None] * size
    if size <= n:
        return res
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, size)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, size)]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def _r(g: float, lo: float) -> float:
        return 100.0 if lo == 0 else 100 - 100 / (1 + g / lo)

    res[n] = _r(ag, al)
    for i in range(n + 1, size):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        res[i] = _r(ag, al)
    return res


def vwap(bars: list[Bar], n: int = 24) -> list[float | None]:
    """Rolling n-bar VWAP of the typical price (spec: 24 bars)."""
    res: list[float | None] = [None] * len(bars)
    pv = vol = 0.0
    for i, b in enumerate(bars):
        tp = (b[1] + b[2] + b[3]) / 3
        pv += tp * b[4]
        vol += b[4]
        if i >= n:
            o = bars[i - n]
            pv -= (o[1] + o[2] + o[3]) / 3 * o[4]
            vol -= o[4]
        if i >= n - 1 and vol > 0:
            res[i] = pv / vol
    return res


def obv_trend(bars: list[Bar], n: int = 20) -> list[float | None]:
    """(OBV now - OBV n bars ago) / volume of those n bars; range -1 to 1 (spec)."""
    size = len(bars)
    obv = [0.0] * size
    for i in range(1, size):
        c, pc, v = bars[i][3], bars[i - 1][3], bars[i][4]
        obv[i] = obv[i - 1] + (v if c > pc else -v if c < pc else 0.0)
    res: list[float | None] = [None] * size
    for i in range(n, size):
        vol = sum(b[4] for b in bars[i - n + 1 : i + 1])
        res[i] = (obv[i] - obv[i - n]) / vol if vol > 0 else 0.0
    return res


def swings(
    highs: list[float], lows: list[float], k: int = SWING_K
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Swing highs and lows: (index, price) where the bar is the extreme of k bars each side."""
    sh: list[tuple[int, float]] = []
    sl: list[tuple[int, float]] = []
    for j in range(k, len(highs) - k):
        if highs[j] == max(highs[j - k : j + k + 1]):
            sh.append((j, highs[j]))
        if lows[j] == min(lows[j - k : j + k + 1]):
            sl.append((j, lows[j]))
    return sh, sl


# ---------------------------------------------------------------- regime


def label_bars(
    h4: list[Bar], d1: list[Bar], adx_trend: float = ADX_TREND, confirm: int = CONFIRM_BARS
) -> list[Label]:
    """Confirmed regime label per closed 4h bar: list of (close_ts_ms, label or None)."""
    c4 = [b[3] for b in h4]
    e20, e50 = ema(c4, 20), ema(c4, 50)
    a14 = adx([b[1] for b in h4], [b[2] for b in h4], c4)
    cd = [b[3] for b in d1]
    e200 = ema(cd, 200)
    d_close_ts = [b[0] + D1_MS for b in d1]

    out: list[Label] = []
    j = -1
    current: str | None = None
    pending: str | None = None
    streak = 0
    for i, bar in enumerate(h4):
        close_ts = bar[0] + H4_MS
        while j + 1 < len(d1) and d_close_ts[j + 1] <= close_ts:  # latest daily bar closed by now
            j += 1
        e20v, e50v, adxv = e20[i], e50[i], a14[i]
        d_ref = e200[j] if j >= 0 else None
        if e20v is None or e50v is None or adxv is None or d_ref is None:
            out.append((close_ts, None))
            continue
        trending = adxv >= adx_trend
        d_up, d_dn = cd[j] > d_ref, cd[j] < d_ref
        if trending and e20v > e50v and d_up:
            raw = "up"
        elif trending and e20v < e50v and d_dn:
            raw = "down"
        else:
            raw = "sideways"
        if current is None:
            current = raw
        elif raw != current:
            streak = streak + 1 if raw == pending else 1
            pending = raw
            if streak >= confirm:
                current, pending, streak = raw, None, 0
        else:
            pending, streak = None, 0
        out.append((close_ts, current))
    return out


def runs_of(labels: list[Label]) -> list[Run]:
    """(label, start_close_ts, bars, first_index) for each run of equal labels."""
    runs: list[Run] = []
    start: int | None = None
    cur: str | None = None
    first: int | None = None
    n = 0
    for idx, (ts, lab) in enumerate(labels):
        if lab is None:
            continue
        if lab != cur:
            if cur is not None and start is not None and first is not None:
                runs.append((cur, start, n, first))
            cur, start, n, first = lab, ts, 0, idx
        n += 1
    if cur is not None and start is not None and first is not None:
        runs.append((cur, start, n, first))
    return runs


# ---------------------------------------------------------------- sideways ranges


def range_widths(h4: list[Bar], runs: list[Run], since_ms: int) -> list[float]:
    """High-to-low width (%) of every sideways run that starts in the window."""
    widths: list[float] = []
    for lab, start, n, first in runs:
        if lab != "sideways" or start < since_ms:
            continue
        seg = h4[first : first + n]
        lo = min(b[2] for b in seg)
        hi = max(b[1] for b in seg)
        widths.append(100 * (hi - lo) / lo)
    return widths


def analyse_ranges(
    h1: list[Bar], h4: list[Bar], labels4: list[Label], since_ms: int
) -> tuple[dict[str, Any], dict[Variant, dict[int, tuple[float, float]]]]:
    """Near-support samples, feasibility per cost mode, and setups for the backtest."""
    hi1, lo1, c1 = [b[1] for b in h1], [b[2] for b in h1], [b[3] for b in h1]
    atr1 = atr(hi1, lo1, c1)
    sh1, sl1 = swings(hi1, lo1)
    sh4, sl4 = swings([b[1] for b in h4], [b[2] for b in h4])
    close4 = [b[0] + H4_MS for b in h4]
    sh1_idx, sl1_idx = [j for j, _ in sh1], [j for j, _ in sl1]
    sh4_idx, sl4_idx = [j for j, _ in sh4], [j for j, _ in sl4]

    def levels(
        piv: list[tuple[int, float]], piv_idx: list[int], lo_i: int, hi_i: int
    ) -> list[float]:
        a, b = bisect.bisect_left(piv_idx, lo_i), bisect.bisect_right(piv_idx, hi_i)
        return [p for _, p in piv[a:b]]

    variants: list[Variant] = [(m, t) for t in TARGETS for m in COSTS]
    stats: dict[str, Any] = {
        "sideways_h": 0,
        "near_h": 0,
        "no_structure_h": 0,
        "upside": [],
        "upside_top": [],
        "feasible": dict.fromkeys(variants, 0),
        "no_stop": dict.fromkeys(variants, 0),
        "low_rr": dict.fromkeys(variants, 0),
    }
    setups: dict[Variant, dict[int, tuple[float, float]]] = {
        v: {} for v in variants
    }  # 1h index -> (stop, target)
    k4 = -1
    for i, bar in enumerate(h1):
        ct = bar[0] + H1_MS
        while k4 + 1 < len(close4) and close4[k4 + 1] <= ct:  # latest closed 4h bar
            k4 += 1
        av = atr1[i]
        if ct < since_ms or k4 < 0 or labels4[k4][1] != "sideways" or av is None:
            continue
        stats["sideways_h"] += 1
        price, a = c1[i], av
        # confirmed swings only: pivot j is known once bar j + SWING_K has closed
        k4c = k4 - SWING_K
        lows = levels(sl1, sl1_idx, i - LOOKBACK_1H, i - SWING_K) + levels(
            sl4, sl4_idx, k4 - LOOKBACK_4H, k4c
        )
        highs = levels(sh1, sh1_idx, i - LOOKBACK_1H, i - SWING_K) + levels(
            sh4, sh4_idx, k4 - LOOKBACK_4H, k4c
        )
        supports = [p for p in lows if p < price]
        resists = [p for p in highs if p > price]
        if not supports or not resists:
            stats["no_structure_h"] += 1
            continue
        s1, r1 = max(supports), min(resists)
        tops = [p for p in levels(sh4, sh4_idx, k4 - LOOKBACK_4H, k4c) if p > price]
        r_top = max(tops) if tops else max(resists)
        if price - s1 > NEAR_SUPPORT_ATR * a:
            continue
        stats["near_h"] += 1
        stats["upside"].append(100 * (r1 / price - 1))
        stats["upside_top"].append(100 * (r_top / price - 1))
        for mode, tgt in variants:
            c = COSTS[mode]
            target = r1 if tgt == "nearest" else r_top
            zone_hi = min(price * (1 - c["min_stop_pct"] / 100), price - MIN_STOP_ATR * a)
            zone_lo = price - MAX_STOP_ATR * a
            stops = [
                s - STOP_BUFFER_ATR * a
                for s in supports
                if zone_lo <= s - STOP_BUFFER_ATR * a <= zone_hi
            ]
            if not stops:
                stats["no_stop"][(mode, tgt)] += 1
                continue
            stop = max(stops)
            if (target - price) / (price - stop) < MIN_RR:
                stats["low_rr"][(mode, tgt)] += 1
                continue
            stats["feasible"][(mode, tgt)] += 1
            setups[(mode, tgt)][i] = (stop, target)
    return stats, setups


def backtest(
    h1: list[Bar],
    setups: dict[int, tuple[float, float]],
    mode: str,
    sell: list[bool] | None = None,
    cooldown_after_stop_only: bool = False,
) -> list[Trade]:
    """One position at a time. Returns list of trades (entry_ts, exit_ts, reason, net_R).

    sell: optional per-bar exit signal (Rules SELL), exits at that bar's close.
    """
    c = COSTS[mode]
    trades: list[Trade] = []
    n = len(h1)
    idx = sorted(setups)
    pos = 0
    while pos < len(idx):
        i = idx[pos]
        stop, target = setups[i]
        entry = h1[i][3]
        risk = entry - stop
        be_stop = entry * (1 + (c["entry"] + c["stop"]) / 100)
        cur_stop = stop
        moved = False
        exit_px: float | None = None
        reason: str | None = None
        j = i
        for j in range(i + 1, min(i + 1 + TIME_STOP_H, n)):
            hi, lo = h1[j][1], h1[j][2]
            if lo <= cur_stop:  # stop first when both are touched
                exit_px, reason = cur_stop, ("breakeven" if moved else "stop")
                break
            if hi >= target:
                exit_px, reason = target, "target"
                break
            if sell is not None and sell[j]:
                exit_px, reason = h1[j][3], "sell"
                break
            if not moved and hi >= entry + risk:  # from the next bar on
                cur_stop, moved = max(cur_stop, be_stop), True
        if exit_px is None:
            j = min(i + TIME_STOP_H, n - 1)
            exit_px, reason = h1[j][3], "time"
        exit_cost = c["target"] if reason == "target" else c["stop"]
        gross_pct = 100 * (exit_px / entry - 1)
        net_r = (gross_pct - c["entry"] - exit_cost) / (100 * risk / entry)
        assert reason is not None  # every exit path sets a reason
        trades.append((h1[i][0], h1[j][0], reason, net_r))
        only_after_stop = cooldown_after_stop_only and reason != "stop"
        resume = j + 1 if only_after_stop else j + COOLDOWN_H
        pos = bisect.bisect_left(idx, resume)
    return trades


# ---------------------------------------------------------------- up-regime trend trades


def analyse_trend(
    h1: list[Bar], h4: list[Bar], d1: list[Bar], labels4: list[Label], since_ms: int
) -> tuple[dict[str, Any], dict[Variant, dict[int, tuple[float, float]]], list[bool]]:
    """Rules BUY setups per (order mode, target rule), plus the per-bar Rules SELL signal."""
    hi1, lo1, c1 = [b[1] for b in h1], [b[2] for b in h1], [b[3] for b in h1]
    atr1 = atr(hi1, lo1, c1)
    e20, e50 = ema(c1, 20), ema(c1, 50)
    rsi1, adx1 = rsi(c1), adx(hi1, lo1, c1)
    vw, obv = vwap(h1), obv_trend(h1)
    sh1, _ = swings(hi1, lo1)
    sh4, _ = swings([b[1] for b in h4], [b[2] for b in h4])
    sh1_idx, sh4_idx = [j for j, _ in sh1], [j for j, _ in sh4]
    c4 = [b[3] for b in h4]
    e20_4, e50_4 = ema(c4, 20), ema(c4, 50)
    close4 = [b[0] + H4_MS for b in h4]
    cd = [b[3] for b in d1]
    e200_d = ema(cd, 200)
    close_d = [b[0] + D1_MS for b in d1]

    def highs_in(
        piv: list[tuple[int, float]], piv_idx: list[int], lo_i: int, hi_i: int
    ) -> list[float]:
        a, b = bisect.bisect_left(piv_idx, lo_i), bisect.bisect_right(piv_idx, hi_i)
        return [p for _, p in piv[a:b]]

    variants: list[Variant] = [(m, t) for t in TREND_TARGETS for m in COSTS]
    stats: dict[str, Any] = {
        "up_h": 0,
        "signal_h": 0,
        "feasible": dict.fromkeys(variants, 0),
        "too_wide": dict.fromkeys(variants, 0),
        "low_rr": dict.fromkeys(variants, 0),
    }
    setups: dict[Variant, dict[int, tuple[float, float]]] = {v: {} for v in variants}
    sell = [False] * len(h1)
    k4 = kd = -1
    for i, bar in enumerate(h1):
        ct = bar[0] + H1_MS
        prev_k4 = k4
        while k4 + 1 < len(close4) and close4[k4 + 1] <= ct:  # latest closed 4h bar
            k4 += 1
        while kd + 1 < len(close_d) and close_d[kd + 1] <= ct:  # latest closed daily bar
            kd += 1
        # Rules SELL signal (used only while holding)
        e50i, obvi = e50[i], obv[i]
        if e50i is not None and obvi is not None and c1[i] < e50i and obvi < 0:
            sell[i] = True
        if k4 != prev_k4 and k4 >= 1:
            a20, a50, b20, b50 = e20_4[k4], e50_4[k4], e20_4[k4 - 1], e50_4[k4 - 1]
            if None not in (a20, a50, b20, b50) and a20 < a50 and b20 >= b50:  # type: ignore[operator]
                sell[i] = True
        if ct < since_ms or k4 < 0 or kd < 0 or labels4[k4][1] != "up":
            continue
        stats["up_h"] += 1
        vals = (
            atr1[i],
            e20[i],
            e50[i],
            rsi1[i],
            adx1[i],
            vw[i],
            obv[i],
            e20_4[k4],
            e50_4[k4],
            e200_d[kd],
        )
        if any(v is None for v in vals):
            continue
        a, ema20, ema50, r, ad, vwp, ob, f20, f50, d200 = (float(v) for v in vals)  # type: ignore[arg-type]
        price = c1[i]
        if not (f20 > f50 and cd[kd] > d200):  # C1
            continue
        met = sum(
            (
                ema20 > ema50,  # C2
                price > vwp,  # C3
                RSI_LOW <= r <= RSI_HIGH,  # C4
                ob > OBV_BULL,  # C5
                ad >= ADX_TREND,  # C6
            )
        )
        if met < RULES_MIN_CONDITIONS or price - ema20 > RULES_MAX_EXTENSION_ATR * a:
            continue
        stats["signal_h"] += 1
        raw_stop = min(lo1[max(0, i - RULES_STOP_LOOKBACK + 1) : i + 1]) - RULES_STOP_BUFFER_ATR * a
        resists = [
            p
            for p in highs_in(sh1, sh1_idx, i - LOOKBACK_1H, i - SWING_K)
            + highs_in(sh4, sh4_idx, k4 - LOOKBACK_4H, k4 - SWING_K)
            if p > price
        ]
        for mode, tgt in variants:
            c = COSTS[mode]
            stop = min(raw_stop, price * (1 - c["min_stop_pct"] / 100), price - MIN_STOP_ATR * a)
            risk = price - stop
            if risk > MAX_STOP_ATR * a:
                stats["too_wide"][(mode, tgt)] += 1
                continue
            near = [p for p in resists if p - price <= RULES_TARGET_MAX_ATR * a]
            target = min(near) if tgt == "rules" and near else price + RULES_FALLBACK_R * risk
            if (target - price) / risk < MIN_RR:
                stats["low_rr"][(mode, tgt)] += 1
                continue
            stats["feasible"][(mode, tgt)] += 1
            setups[(mode, tgt)][i] = (stop, target)
    return stats, setups, sell


# ---------------------------------------------------------------- research lab (part 4)

LAB_COST = COSTS["market"]  # the lab uses market-order costs (the conservative case)
SWITCH_COST_PCT = 0.30  # per side for the exposure filters: fee 0.25% + slippage 0.05%
TRAIL_AFTER_R = 2.0  # spec: trailing starts once price reaches entry + 2R
TRAIL_ATR = 2.0  # spec: highest close since entry - 2 x ATR, never down

Setup = tuple[float, float | None]  # stop, target (None = no fixed target)
LabTrade = tuple[int, float]  # entry_ts, net_R


def rules_signals(
    bars: list[Bar],
    tf_ms: int,
    h4: list[Bar],
    d1: list[Bar],
    labels4: list[Label],
    since_ms: int,
    use_1h_swings: bool,
) -> tuple[dict[int, tuple[float, float, list[float]]], list[float | None], list[bool]]:
    """Rules BUY signals on any timeframe (1h or 4h), as in the spec's Rules engine.

    Returns {bar index: (raw_stop, atr, resistances above price)}, the ATR series and the
    per-bar Rules SELL signal (close below EMA 50 with OBV trend below 0, or the 4h EMA 20
    crossing below EMA 50).
    """
    hi, lo, cl = [b[1] for b in bars], [b[2] for b in bars], [b[3] for b in bars]
    at = atr(hi, lo, cl)
    e20, e50 = ema(cl, 20), ema(cl, 50)
    rs, ad = rsi(cl), adx(hi, lo, cl)
    vw, ob = vwap(bars), obv_trend(bars)
    piv_hi, _ = swings(hi, lo)
    piv_idx = [j for j, _ in piv_hi]
    c4 = [b[3] for b in h4]
    e20_4, e50_4 = ema(c4, 20), ema(c4, 50)
    close4 = [b[0] + H4_MS for b in h4]
    sh4, _ = swings([b[1] for b in h4], [b[2] for b in h4])
    sh4_idx = [j for j, _ in sh4]
    cd = [b[3] for b in d1]
    e200 = ema(cd, 200)
    close_d = [b[0] + D1_MS for b in d1]
    lookback = LOOKBACK_1H if tf_ms == H1_MS else LOOKBACK_4H

    def piv_between(piv: list[tuple[int, float]], idx: list[int], a: int, b: int) -> list[float]:
        x, y = bisect.bisect_left(idx, a), bisect.bisect_right(idx, b)
        return [p for _, p in piv[x:y]]

    signals: dict[int, tuple[float, float, list[float]]] = {}
    sell = [False] * len(bars)
    k4 = kd = -1
    for i, bar in enumerate(bars):
        ct = bar[0] + tf_ms
        prev_k4 = k4
        while k4 + 1 < len(close4) and close4[k4 + 1] <= ct:
            k4 += 1
        while kd + 1 < len(close_d) and close_d[kd + 1] <= ct:
            kd += 1
        e50i, obi = e50[i], ob[i]
        if e50i is not None and obi is not None and cl[i] < e50i and obi < 0:
            sell[i] = True
        if k4 != prev_k4 and k4 >= 1:
            a20, a50, b20, b50 = e20_4[k4], e50_4[k4], e20_4[k4 - 1], e50_4[k4 - 1]
            known = a20 is not None and a50 is not None and b20 is not None and b50 is not None
            if known and a20 < a50 and b20 >= b50:  # type: ignore[operator]
                sell[i] = True
        if ct < since_ms or k4 < 0 or kd < 0 or labels4[k4][1] != "up":
            continue
        vals = (at[i], e20[i], e50[i], rs[i], ad[i], vw[i], ob[i], e20_4[k4], e50_4[k4], e200[kd])
        if any(v is None for v in vals):
            continue
        a, m20, m50, r, x, vwp, o, f20, f50, d200 = (float(v) for v in vals)  # type: ignore[arg-type]
        price = cl[i]
        if not (f20 > f50 and cd[kd] > d200):  # C1
            continue
        met = sum((m20 > m50, price > vwp, RSI_LOW <= r <= RSI_HIGH, o > OBV_BULL, x >= ADX_TREND))
        if met < RULES_MIN_CONDITIONS or price - m20 > RULES_MAX_EXTENSION_ATR * a:
            continue
        raw_stop = min(lo[max(0, i - RULES_STOP_LOOKBACK + 1) : i + 1]) - RULES_STOP_BUFFER_ATR * a
        res = piv_between(piv_hi, piv_idx, i - lookback, i - SWING_K)
        if use_1h_swings:
            res += piv_between(sh4, sh4_idx, k4 - LOOKBACK_4H, k4 - SWING_K)
        signals[i] = (raw_stop, a, [p for p in res if p > price])
    return signals, at, sell


def lab_setups(
    bars: list[Bar], signals: dict[int, tuple[float, float, list[float]]], target_rule: str
) -> dict[int, Setup]:
    """Stops widened to the fee-gate minimum; target per rule ('rules', '2.5R' or 'none')."""
    out: dict[int, Setup] = {}
    for i, (raw_stop, a, res) in signals.items():
        price = bars[i][3]
        stop = min(raw_stop, price * (1 - LAB_COST["min_stop_pct"] / 100), price - MIN_STOP_ATR * a)
        risk = price - stop
        if risk > MAX_STOP_ATR * a:
            continue
        if target_rule == "none":
            out[i] = (stop, None)
            continue
        near = [p for p in res if p - price <= RULES_TARGET_MAX_ATR * a]
        rules_target = min(near) if target_rule == "rules" and near else None
        target = rules_target if rules_target is not None else price + RULES_FALLBACK_R * risk
        if (target - price) / risk >= MIN_RR:
            out[i] = (stop, target)
    return out


def simulate(
    bars: list[Bar],
    at: list[float | None],
    setups: dict[int, Setup],
    time_stop_bars: int,
    cooldown_bars: int,
    sell: list[bool] | None,
    trail: bool,
) -> list[LabTrade]:
    """One position at a time; stop first; breakeven + fees at +1R; optional trailing stop."""
    c = LAB_COST
    trades: list[LabTrade] = []
    idx = sorted(setups)
    pos, n = 0, len(bars)
    while pos < len(idx):
        i = idx[pos]
        stop, target = setups[i]
        entry = bars[i][3]
        risk = entry - stop
        be_stop = entry * (1 + (c["entry"] + c["stop"]) / 100)
        cur_stop, best_close = stop, entry
        trailing = False
        exit_px: float | None = None
        reason = "time"
        j = i
        for j in range(i + 1, min(i + 1 + time_stop_bars, n)):
            hi, lo, cl = bars[j][1], bars[j][2], bars[j][3]
            if lo <= cur_stop:
                exit_px, reason = cur_stop, ("stop" if cur_stop < entry else "protected")
                break
            if target is not None and hi >= target:
                exit_px, reason = target, "target"
                break
            if sell is not None and sell[j]:
                exit_px, reason = cl, "sell"
                break
            if hi >= entry + risk:
                cur_stop = max(cur_stop, be_stop)
            best_close = max(best_close, cl)
            aj = at[j]
            if trail and aj is not None and (trailing or hi >= entry + TRAIL_AFTER_R * risk):
                trailing = True
                cur_stop = max(cur_stop, best_close - TRAIL_ATR * aj)
        if exit_px is None:
            j = min(i + time_stop_bars, n - 1)
            exit_px = bars[j][3]
        exit_cost = c["target"] if reason == "target" else c["stop"]
        gross = 100 * (exit_px / entry - 1)
        trades.append((bars[i][0], (gross - c["entry"] - exit_cost) / (100 * risk / entry)))
        resume = j + (cooldown_bars if reason == "stop" else 1)
        pos = bisect.bisect_left(idx, resume)
    return trades


# (label, timeframe, target rule, Rules SELL on, trailing, time stop in days)
LAB_VARIANTS: list[tuple[str, str, str, bool, bool, int]] = [
    ("1h spec: rules target + SELL", "1h", "rules", True, False, 5),
    ("1h 2.5R target, no SELL", "1h", "2.5R", False, False, 5),
    ("1h trailing from 2R, no target", "1h", "none", False, True, 5),
    ("4h 2.5R target, no SELL", "4h", "2.5R", False, False, 5),
    ("4h trailing from 2R, no target", "4h", "none", False, True, 20),
]
EXPOSURE_FILTERS = ("buy & hold", "regime up", "C1 (4h EMA + daily EMA 200)", "daily > EMA 200")


def exposure(
    h4: list[Bar], d1: list[Bar], labels4: list[Label], start_ms: int, end_ms: int
) -> dict[str, tuple[float, float, float, int]]:
    """Hold the coin while a filter is true, else EUR. Switch at 4h closes, 0.3% per side.

    Returns per filter: (return %, max drawdown %, time in the market %, round trips).
    """
    c4 = [b[3] for b in h4]
    e20_4, e50_4 = ema(c4, 20), ema(c4, 50)
    cd = [b[3] for b in d1]
    e200 = ema(cd, 200)
    close_d = [b[0] + D1_MS for b in d1]
    out: dict[str, tuple[float, float, float, int]] = {}
    for name in EXPOSURE_FILTERS:
        eq, peak, max_dd, held, inside, trips, bars_n = 1.0, 1.0, 0.0, False, 0, 0, 0
        prev_close: float | None = None
        kd = -1
        for k, bar in enumerate(h4):
            ct = bar[0] + H4_MS
            while kd + 1 < len(close_d) and close_d[kd + 1] <= ct:
                kd += 1
            if ct < start_ms or ct > end_ms:
                continue
            if held and prev_close is not None:
                eq *= bar[3] / prev_close
            if name == "buy & hold":
                want = True
            elif name == "regime up":
                want = labels4[k][1] == "up"
            elif name.startswith("C1"):
                a, b, d = e20_4[k], e50_4[k], e200[kd] if kd >= 0 else None
                want = a is not None and b is not None and d is not None and a > b and cd[kd] > d
            else:
                d = e200[kd] if kd >= 0 else None
                want = d is not None and cd[kd] > d
            if want != held:
                eq *= 1 - SWITCH_COST_PCT / 100
                trips += 1 if held else 0
                held = want
            peak = max(peak, eq)
            max_dd = max(max_dd, 100 * (1 - eq / peak))
            inside += 1 if held else 0
            bars_n += 1
            prev_close = bar[3]
        out[name] = (100 * (eq - 1), max_dd, 100 * inside / bars_n if bars_n else 0.0, trips)
    return out


def run_lab(
    h1: list[Bar], h4: list[Bar], d1: list[Bar], labels4: list[Label], since_ms: int, split_ms: int
) -> tuple[dict[str, list[LabTrade]], dict[str, dict[str, tuple[float, float, float, int]]]]:
    sig1, at1, sell1 = rules_signals(h1, H1_MS, h4, d1, labels4, since_ms, use_1h_swings=True)
    sig4, at4, sell4 = rules_signals(h4, H4_MS, h4, d1, labels4, since_ms, use_1h_swings=False)
    trades: dict[str, list[LabTrade]] = {}
    for label, tf, tgt, use_sell, trail, days_ in LAB_VARIANTS:
        bars, sig, at, sell = (h1, sig1, at1, sell1) if tf == "1h" else (h4, sig4, at4, sell4)
        per_day = 24 if tf == "1h" else 6
        setups = lab_setups(bars, sig, tgt)
        trades[label] = simulate(
            bars,
            at,
            setups,
            days_ * per_day,
            COOLDOWN_H if tf == "1h" else 2,
            sell if use_sell else None,
            trail,
        )
    end_ms = h4[-1][0] + H4_MS if h4 else since_ms
    expo = {
        "train": exposure(h4, d1, labels4, since_ms, split_ms),
        "test": exposure(h4, d1, labels4, split_ms, end_ms),
    }
    return trades, expo


def split_stats(trades: list[LabTrade], split_ms: int) -> tuple[int, float, int, float]:
    tr = [r for t, r in trades if t < split_ms]
    te = [r for t, r in trades if t >= split_ms]
    return (
        len(tr),
        statistics.mean(tr) if tr else float("nan"),
        len(te),
        statistics.mean(te) if te else float("nan"),
    )


# ---------------------------------------------------------------- portfolio test (part 5)

RANK_BARS = 42  # 7 days of 4h bars: rank new candidates by their 7-day return
SENS_ADX = (20.0, 25.0, 30.0)
SENS_CONFIRM = (1, 2, 3)

Filter = str  # "up", "c1" or "ema200"
PfResult = tuple[float, float, float, int]  # return %, max drawdown %, time invested %, entries


def coin_filters(
    h4: list[Bar], d1: list[Bar], adx_trend: float = ADX_TREND, confirm: int = CONFIRM_BARS
) -> dict[Filter, dict[int, bool]]:
    """Per filter: {4h close ts: in the market?} for one coin."""
    labels = label_bars(h4, d1, adx_trend, confirm)
    c4 = [b[3] for b in h4]
    e20, e50 = ema(c4, 20), ema(c4, 50)
    cd = [b[3] for b in d1]
    e200 = ema(cd, 200)
    close_d = [b[0] + D1_MS for b in d1]
    out: dict[Filter, dict[int, bool]] = {"up": {}, "c1": {}, "ema200": {}}
    kd = -1
    for k, bar in enumerate(h4):
        ct = bar[0] + H4_MS
        while kd + 1 < len(close_d) and close_d[kd + 1] <= ct:
            kd += 1
        d = e200[kd] if kd >= 0 else None
        above = d is not None and cd[kd] > d
        a, b = e20[k], e50[k]
        out["up"][ct] = labels[k][1] == "up"
        out["c1"][ct] = above and a is not None and b is not None and a > b
        out["ema200"][ct] = above
    return out


def run_portfolio(
    closes: dict[str, dict[int, float]],
    timeline: list[int],
    want: dict[str, dict[int, bool]] | None,
    slots: int,
    start_ms: int,
    end_ms: int,
    hold_only: str | None = None,
) -> PfResult:
    """Simulate at 4h closes between start and end, starting flat with equity 1.

    want=None with hold_only=coin: buy and hold that coin. want=None and hold_only=None:
    equal-weight buy and hold of every coin. Otherwise hold up to `slots` coins whose filter
    is true, each bought with equity / slots, sold when its filter turns false.
    """
    cost = SWITCH_COST_PCT / 100
    cash = 1.0
    qty: dict[str, float] = {}
    last: dict[str, float] = {}
    peak, max_dd, invested, steps, entries = 1.0, 0.0, 0.0, 0, 0
    hist: dict[str, list[float]] = {c: [] for c in closes}
    started = False
    for ts in timeline:
        for c in closes:
            px = closes[c].get(ts)
            if px is not None:
                last[c] = px
                hist[c].append(px)
        if ts < start_ms or ts > end_ms:
            continue
        coins = [c for c in closes if c in last]
        if not started:
            started = True
            if want is None:
                buy = [hold_only] if hold_only else coins
                buy = [c for c in buy if c in last]
                for c in buy:
                    alloc = 1.0 / len(buy)
                    qty[c] = alloc * (1 - cost) / last[c]
                    entries += 1
                cash = 0.0
        if want is not None:
            for c in list(qty):  # exits first
                if not want[c].get(ts, False):
                    cash += qty.pop(c) * last[c] * (1 - cost)
            equity = cash + sum(q * last[c] for c, q in qty.items())
            free = slots - len(qty)
            if free > 0:
                cands = [c for c in coins if c not in qty and want[c].get(ts, False)]

                def mom(c: str) -> float:
                    h = hist[c]
                    return h[-1] / h[-1 - RANK_BARS] - 1 if len(h) > RANK_BARS else 0.0

                for c in sorted(cands, key=mom, reverse=True)[:free]:
                    alloc = min(equity / slots, cash)
                    if alloc <= 1e-9:
                        break
                    qty[c] = alloc * (1 - cost) / last[c]
                    cash -= alloc
                    entries += 1
        equity = cash + sum(q * last[c] for c, q in qty.items())
        peak = max(peak, equity)
        max_dd = max(max_dd, 100 * (1 - equity / peak))
        invested += (equity - cash) / equity if equity > 0 else 0.0
        steps += 1
    equity = cash + sum(q * last[c] * (1 - cost) for c, q in qty.items())  # value after selling
    return (100 * (equity - 1), max_dd, 100 * invested / steps if steps else 0.0, entries)


STATIC_BTC_WEIGHTS = (0.2, 0.3, 0.5)
REBALANCE_BARS = 180  # about 30 days of 4h bars


def run_static(
    btc: dict[int, float],
    timeline: list[int],
    weight: float,
    start_ms: int,
    end_ms: int,
    rebalance_bars: int = REBALANCE_BARS,
) -> PfResult:
    """Hold `weight` of equity in BTC and the rest in EUR, rebalanced about monthly."""
    cost = SWITCH_COST_PCT / 100
    qty, cash, px = 0.0, 1.0, None
    peak, max_dd, invested, steps, trades = 1.0, 0.0, 0.0, 0, 0
    since_rebalance = rebalance_bars
    for ts in timeline:
        p = btc.get(ts)
        if p is not None:
            px = p
        if ts < start_ms or ts > end_ms or px is None:
            continue
        equity = cash + qty * px
        if since_rebalance >= rebalance_bars:
            diff = weight * equity - qty * px  # EUR to buy (+) or sell (-)
            if abs(diff) > 1e-9:
                if diff > 0:
                    qty += diff * (1 - cost) / px
                    cash -= diff
                else:
                    qty += diff / px
                    cash += -diff * (1 - cost)
                trades += 1
            since_rebalance = 0
        since_rebalance += 1
        equity = cash + qty * px
        peak = max(peak, equity)
        max_dd = max(max_dd, 100 * (1 - equity / peak))
        invested += qty * px / equity if equity > 0 else 0.0
        steps += 1
    final = cash + qty * (px or 0.0) * (1 - cost)
    return (100 * (final - 1), max_dd, 100 * invested / steps if steps else 0.0, trades)


def cagr(ret_pct: float, years: float) -> float:
    return (
        100 * ((1 + ret_pct / 100) ** (1 / years) - 1) if years > 0 and ret_pct > -100 else -100.0
    )


def portfolio_report(
    data: dict[str, tuple[list[Bar], list[Bar]]], since_ms: int, split_ms: int, slots: int
) -> None:
    closes = {m: {b[0] + H4_MS: b[3] for b in h4} for m, (h4, _) in data.items()}
    timeline = sorted({ts for c in closes.values() for ts in c})
    end_ms = timeline[-1]
    windows = {
        "train": (since_ms, split_ms),
        "test": (split_ms, end_ms),
        "full": (since_ms, end_ms),
    }
    years = {k: (b - a) / (365.25 * D1_MS) for k, (a, b) in windows.items()}
    base = {m: coin_filters(h4, d1) for m, (h4, d1) in data.items()}
    btc = next((m for m in data if m.startswith("BTC-")), None)

    def want_for(f: Filter, btc_filter: Filter | None = None) -> dict[str, dict[int, bool]]:
        return {m: base[m][btc_filter if (btc_filter and m == btc) else f] for m in data}

    Runner = Callable[[int, int], PfResult]

    def filt(
        want: dict[str, dict[int, bool]] | None,
        n: int,
        hold: str | None = None,
        universe: dict[str, dict[int, float]] | None = None,
    ) -> Runner:
        cl = universe if universe is not None else closes

        def run(a: int, b: int) -> PfResult:
            return run_portfolio(cl, timeline, want, n, a, b, hold)

        return run

    def static(series: dict[int, float], weight: float) -> Runner:
        def run(a: int, b: int) -> PfResult:
            return run_static(series, timeline, weight, a, b)

        return run

    strategies: list[tuple[str, Runner]] = []
    if btc:
        strategies.append(("BTC buy & hold", filt(None, 1, btc)))
        for w in STATIC_BTC_WEIGHTS:
            strategies.append(
                (f"static {w:.0%} BTC + cash, monthly rebalance", static(closes[btc], w))
            )
        btc_only = {btc: closes[btc]}
        for f, name in (("up", "regime up"), ("c1", "C1"), ("ema200", "daily > EMA 200")):
            want_btc = {btc: base[btc][f]}
            strategies.append((f"BTC only, hold while {name}", filt(want_btc, 1, None, btc_only)))
    strategies.append(("equal-weight buy & hold, all coins", filt(None, len(data))))
    for f, name in (("up", "regime up"), ("c1", "C1"), ("ema200", "daily > EMA 200")):
        strategies.append((f"hold while {name}, max {slots}", filt(want_for(f), slots)))
        strategies.append((f"hold while {name}, all coins", filt(want_for(f), len(data))))
    if btc:
        mixed = want_for("up", "ema200")
        strategies.append((f"regime up, BTC on daily > EMA 200, max {slots}", filt(mixed, slots)))
        strategies.append(("regime up, BTC on daily > EMA 200, all coins", filt(mixed, len(data))))

    print(
        f"\nPortfolio test ({len(data)} coins, 4h closes, 0.3% per side, equal slots, no model; "
        f"train {iso(since_ms)} to {iso(split_ms)}, test from {iso(split_ms)})"
    )
    print(
        f"  {'strategy':46} {'train':>8} {'max DD':>6}   {'test':>8} {'max DD':>6}   "
        f"{'full':>8} {'CAGR':>7} {'max DD':>6} {'CAGR/DD':>7} {'in mkt':>6} {'buys':>5}"
    )
    for name, runner in strategies:
        res = {k: runner(a, b) for k, (a, b) in windows.items()}
        tr, te, fu = res["train"], res["test"], res["full"]
        cg = cagr(fu[0], years["full"])
        ratio = cg / fu[1] if fu[1] > 0 else float("nan")
        print(
            f"  {name:46} {tr[0]:+7.0f}% {tr[1]:5.0f}%   {te[0]:+7.0f}% {te[1]:5.0f}%   "
            f"{fu[0]:+7.0f}% {cg:+6.0f}% {fu[1]:5.0f}% {ratio:7.2f} "
            f"{fu[2]:5.0f}% {fu[3]:5}"
        )
    print(
        "  CAGR/DD: yearly growth per unit of the worst drawdown; compare strategies at similar "
        "risk, not only on return."
    )

    print(
        f"\n  Sensitivity: hold while regime up, max {slots} (test window: return / max DD; "
        f"full window: CAGR / max DD)"
    )
    print(
        f"  {'ADX':>5} {'confirm':>8}   {'test return':>11} {'max DD':>6}   "
        f"{'full CAGR':>9} {'max DD':>6}"
    )
    for adx_t in SENS_ADX:
        for conf in SENS_CONFIRM:
            want = {m: coin_filters(h4, d1, adx_t, conf)["up"] for m, (h4, d1) in data.items()}
            te = run_portfolio(closes, timeline, want, slots, split_ms, end_ms)
            fu = run_portfolio(closes, timeline, want, slots, since_ms, end_ms)
            mark = "  <- agent default" if (adx_t, conf) == (ADX_TREND, CONFIRM_BARS) else ""
            print(
                f"  {adx_t:5.0f} {conf:8}   {te[0]:+10.0f}% {te[1]:5.0f}%   "
                f"{cagr(fu[0], years['full']):+8.0f}% {fu[1]:5.0f}%{mark}"
            )
    print(
        "  A result only counts if it beats the benchmarks in the test window and the sensitivity "
        "rows agree. Survivorship: these coins are today's survivors, which flatters every row."
    )


# ---------------------------------------------------------------- strategy families (part 6)


def sma(values: list[float], n: int) -> list[float | None]:
    """Simple moving average; None until n values."""
    res: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            res[i] = s / n
    return res


class CoinDaily:
    """Daily series and indicators for one coin, indexed by daily close time."""

    def __init__(self, d1: list[Bar]) -> None:
        self.ts = [b[0] + D1_MS for b in d1]
        self.idx = {t: i for i, t in enumerate(self.ts)}
        self.high = [b[1] for b in d1]
        self.low = [b[2] for b in d1]
        self.close = [b[3] for b in d1]
        self.atr20 = atr(self.high, self.low, self.close, 20)
        self.ema200 = ema(self.close, 200)
        self.rsi2 = rsi(self.close, 2)
        self.sma: dict[int, list[float | None]] = {n: sma(self.close, n) for n in (5, 10)}

    def ret(self, i: int, days: int) -> float | None:
        return self.close[i] / self.close[i - days] - 1 if i >= days else None


class Position:
    def __init__(self, qty: float, entry_i: int, entry_px: float) -> None:
        self.qty = qty
        self.entry_i = entry_i
        self.best = entry_px


class Family:
    """A daily strategy: which held coins to sell and which coins to buy (best first)."""

    slots = 3

    def exits(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        raise NotImplementedError

    def entries(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        raise NotImplementedError


class Breakout(Family):
    """Buy a close above the highest high of the previous n days; trailing stop k x ATR(20)."""

    def __init__(self, n: int, k: float, slots: int) -> None:
        self.n, self.k, self.slots = n, k, slots

    def exits(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        out = []
        for c, p in held.items():
            s = coins[c]
            i = s.idx.get(ts)
            a = s.atr20[i] if i is not None else None
            if i is None or a is None:
                continue
            p.best = max(p.best, s.close[i])
            if s.close[i] < p.best - self.k * a:
                out.append(c)
        return out

    def entries(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        cands: list[tuple[float, str]] = []
        for c, s in coins.items():
            i = s.idx.get(ts)
            if c in held or i is None or i < self.n + 1 or s.atr20[i] is None:
                continue
            if s.close[i] > max(s.high[i - self.n : i]):
                r = s.ret(i, 30)
                cands.append((r if r is not None else 0.0, c))
        return [c for _, c in sorted(cands, reverse=True)]


class Rotation(Family):
    """Every 7 days hold the top k coins by their n-day return, if that return is positive."""

    def __init__(self, n: int, k: int, start_ms: int) -> None:
        self.n, self.slots, self.start_ms = n, k, start_ms

    def _rebalance_day(self, ts: int) -> bool:
        return ((ts - self.start_ms) // D1_MS) % 7 == 0

    def _target(self, coins: dict[str, CoinDaily], ts: int) -> list[str]:
        scored: list[tuple[float, str]] = []
        for c, s in coins.items():
            i = s.idx.get(ts)
            r = s.ret(i, self.n) if i is not None else None
            if r is not None and r > 0:
                scored.append((r, c))
        return [c for _, c in sorted(scored, reverse=True)[: self.slots]]

    def exits(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        if not self._rebalance_day(ts):
            return []
        target = self._target(coins, ts)
        return [c for c in held if c not in target]

    def entries(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        if not self._rebalance_day(ts):
            return []
        return [c for c in self._target(coins, ts) if c not in held]


class DipBuy(Family):
    """Above the daily EMA 200, buy when RSI(2) < threshold.

    Sell when the close is above the n-day SMA, or after 10 days.
    """

    def __init__(self, threshold: float, exit_sma: int, slots: int) -> None:
        self.threshold, self.exit_sma, self.slots = threshold, exit_sma, slots

    def exits(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        out = []
        for c, p in held.items():
            s = coins[c]
            i = s.idx.get(ts)
            if i is None:
                continue
            m = s.sma[self.exit_sma][i]
            if (m is not None and s.close[i] > m) or i - p.entry_i >= 10:
                out.append(c)
        return out

    def entries(self, coins: dict[str, CoinDaily], ts: int, held: dict[str, Position]) -> list[str]:
        cands: list[tuple[float, str]] = []
        for c, s in coins.items():
            i = s.idx.get(ts)
            if c in held or i is None:
                continue
            e, r = s.ema200[i], s.rsi2[i]
            if e is not None and r is not None and s.close[i] > e and r < self.threshold:
                cands.append((r, c))
        return [c for _, c in sorted(cands)]


def make_breakout(n: int, k: float, slots: int) -> Callable[[int], Family]:
    def make(start_ms: int) -> Family:
        return Breakout(n, k, slots)

    return make


def make_rotation(n: int, k: int) -> Callable[[int], Family]:
    def make(start_ms: int) -> Family:
        return Rotation(n, k, start_ms)

    return make


def make_dip(threshold: float, exit_sma: int, slots: int) -> Callable[[int], Family]:
    def make(start_ms: int) -> Family:
        return DipBuy(threshold, exit_sma, slots)

    return make


def run_family(
    coins: dict[str, CoinDaily], timeline: list[int], fam: Family, start_ms: int, end_ms: int
) -> PfResult:
    """Daily closes, flat start with equity 1, equal slots, 0.3% per side."""
    cost = SWITCH_COST_PCT / 100
    cash = 1.0
    held: dict[str, Position] = {}
    last: dict[str, float] = {}
    peak, max_dd, invested, steps, entries = 1.0, 0.0, 0.0, 0, 0
    for ts in timeline:
        for c, s in coins.items():
            i = s.idx.get(ts)
            if i is not None:
                last[c] = s.close[i]
        if ts < start_ms or ts > end_ms:
            continue
        for c in fam.exits(coins, ts, held):
            cash += held.pop(c).qty * last[c] * (1 - cost)
        equity = cash + sum(p.qty * last[c] for c, p in held.items())
        for c in fam.entries(coins, ts, held):
            if len(held) >= fam.slots:
                break
            alloc = min(equity / fam.slots, cash)
            if alloc <= 1e-9:
                break
            i = coins[c].idx[ts]
            held[c] = Position(alloc * (1 - cost) / last[c], i, last[c])
            cash -= alloc
            entries += 1
        equity = cash + sum(p.qty * last[c] for c, p in held.items())
        peak = max(peak, equity)
        max_dd = max(max_dd, 100 * (1 - equity / peak))
        invested += (equity - cash) / equity if equity > 0 else 0.0
        steps += 1
    final = cash + sum(p.qty * last[c] * (1 - cost) for c, p in held.items())
    return (100 * (final - 1), max_dd, 100 * invested / steps if steps else 0.0, entries)


def families_report(
    data: dict[str, tuple[list[Bar], list[Bar]]], since_ms: int, split_ms: int, slots: int
) -> None:
    coins = {m: CoinDaily(d1) for m, (_, d1) in data.items()}
    timeline = sorted({t for s in coins.values() for t in s.ts if t >= since_ms})
    if not timeline:
        return
    end_ms = timeline[-1]
    windows = {
        "train": (since_ms, split_ms),
        "test": (split_ms, end_ms),
        "full": (since_ms, end_ms),
    }
    years = (end_ms - since_ms) / (365.25 * D1_MS)
    n_all = len(coins)
    btc = next((m for m in coins if m.startswith("BTC-")), None)
    btc_closes = {t: coins[btc].close[i] for t, i in coins[btc].idx.items()} if btc else {}

    def fam_runner(make: Callable[[int], Family]) -> Callable[[int, int], PfResult]:
        def run(a: int, b: int) -> PfResult:
            return run_family(coins, timeline, make(a), a, b)

        return run

    def static_runner(w: float) -> Callable[[int, int], PfResult]:
        def run(a: int, b: int) -> PfResult:
            return run_static(btc_closes, timeline, w, a, b, rebalance_bars=30)

        return run

    rows: list[tuple[str, Callable[[int, int], PfResult]]] = []
    if btc:
        rows.append(("BTC buy & hold", static_runner(1.0)))
        for w in (0.3, 0.5):
            rows.append((f"static {w:.0%} BTC + cash, monthly rebalance", static_runner(w)))
    for n_slots in sorted({min(slots, n_all), n_all}):
        tag = f"max {n_slots}" if n_slots != n_all else "all coins"
        rows.append(
            (
                f"A breakout 55d, trail 3 ATR, {tag}",
                fam_runner(make_breakout(55, 3.0, n_slots)),
            )
        )
        rows.append(
            (
                f"C dip buy RSI2 < 10, exit > SMA5, {tag}",
                fam_runner(make_dip(10.0, 5, n_slots)),
            )
        )
    rows.append(("B rotation, top 3 by 60d return", fam_runner(make_rotation(60, 3))))

    print(
        f"\nStrategy families (daily closes, {n_all} coins, 0.3% per side, equal slots, no model; "
        f"train {iso(since_ms)} to {iso(split_ms)}, test from {iso(split_ms)})"
    )
    print(
        f"  {'strategy':46} {'train':>8} {'max DD':>6}   {'test':>8} {'max DD':>6}   "
        f"{'full':>8} {'CAGR':>7} {'max DD':>6} {'CAGR/DD':>7} {'in mkt':>6} {'buys':>5}"
    )
    for name, runner in rows:
        res = {k: runner(a, b) for k, (a, b) in windows.items()}
        tr, te, fu = res["train"], res["test"], res["full"]
        cg = cagr(fu[0], years)
        ratio = cg / fu[1] if fu[1] > 0 else float("nan")
        print(
            f"  {name:46} {tr[0]:+7.0f}% {tr[1]:5.0f}%   {te[0]:+7.0f}% {te[1]:5.0f}%   "
            f"{fu[0]:+7.0f}% {cg:+6.0f}% {fu[1]:5.0f}% {ratio:7.2f} {fu[2]:5.0f}% {fu[3]:5}"
        )

    grids: list[tuple[str, list[tuple[str, Callable[[int], Family]]]]] = [
        (
            f"A breakout, max {slots}: lookback x trailing ATR",
            [
                (f"{n}d, {k:g} ATR", make_breakout(n, k, slots))
                for n in (20, 55, 100)
                for k in (2.0, 3.0, 4.0)
            ],
        ),
        (
            "B rotation: return window x coins held",
            [(f"{n}d, top {k}", make_rotation(n, k)) for n in (30, 60, 90) for k in (1, 2, 3)],
        ),
        (
            f"C dip buy, max {slots}: RSI2 threshold x exit SMA",
            [
                (f"RSI2 < {t:g}, SMA{e}", make_dip(t, e, slots))
                for t in (5.0, 10.0, 20.0)
                for e in (5, 10)
            ],
        ),
    ]
    for title, cells in grids:
        print(f"\n  Sensitivity, {title}")
        print(
            f"  {'setting':20} {'test return':>11} {'max DD':>6}   "
            f"{'full CAGR':>9} {'max DD':>6} {'CAGR/DD':>7}"
        )
        for label, make in cells:
            te = run_family(coins, timeline, make(split_ms), split_ms, end_ms)
            fu = run_family(coins, timeline, make(since_ms), since_ms, end_ms)
            cg = cagr(fu[0], years)
            ratio = cg / fu[1] if fu[1] > 0 else float("nan")
            print(
                f"  {label:20} {te[0]:+10.0f}% {te[1]:5.0f}%   "
                f"{cg:+8.0f}% {fu[1]:5.0f}% {ratio:7.2f}"
            )
    print(
        "\n  Benchmark: the static BTC + cash rows. A family only counts if it beats the static "
        "mix "
        "with a similar max drawdown in train AND test, and its sensitivity grid agrees. Testing "
        "three families with many settings makes a lucky winner likely: be strict."
    )


# ---------------------------------------------------------------- report


def days(bars: int) -> float:
    return bars * 4 / 24


def fmt_d(x: float) -> str:
    return f"{x:5.1f}d" if x < 100 else f"{x:5.0f}d"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float("nan")


def report_regimes(
    market: str, labels: list[Label], runs: list[Run], since_ms: int
) -> float | None:
    labelled = [(t, lab) for t, lab in labels if lab is not None and t >= since_ms]
    if not labelled:
        print(f"\n{market}: not enough history")
        return None
    total = len(labelled)
    span_years = (labelled[-1][0] - labelled[0][0]) / (365.25 * D1_MS)
    print(
        f"\n{market}   {iso(labelled[0][0])} to {iso(labelled[-1][0])}   "
        f"({span_years:.1f} years, {total} 4h bars)"
    )
    print(
        f"  {'regime':9} {'time':>6} {'runs':>5} {'median':>7} {'mean':>7} "
        f"{'p90':>7} {'longest':>8}  longest from"
    )
    for reg in ("up", "sideways", "down"):
        share = 100 * sum(1 for _, lab in labelled if lab == reg) / total
        rr = [r for r in runs if r[0] == reg and r[1] >= since_ms]
        if not rr:
            print(f"  {reg:9} {share:5.1f}% {0:>5}")
            continue
        lens = sorted(days(r[2]) for r in rr)
        longest = max(rr, key=lambda r: r[2])
        print(
            f"  {reg:9} {share:5.1f}% {len(rr):>5} {fmt_d(statistics.median(lens)):>7} "
            f"{fmt_d(statistics.mean(lens)):>7} {fmt_d(pct(lens, 0.9)):>7} "
            f"{fmt_d(days(longest[2])):>8}  {iso(longest[1])}"
        )
    downs = [r for r in runs if r[0] == "down" and r[1] >= since_ms]
    buy_ok = 100 * sum(1 for _, lab in labelled if lab != "down") / total
    note = (
        "BTC may also buy in a down regime (confidence cap 0.6)"
        if market.startswith("BTC-")
        else "BUY allowed outside down regimes (sideways only near support)"
    )
    print(
        f"  down stretches per year: {len(downs) / span_years:.1f}   "
        f"time a BUY is possible: {buy_ok:.0f}%  ({note})"
    )
    last = runs[-1]
    print(f"  now: {last[0]} for {last[2]} bars ({days(last[2]):.1f} days)")
    return span_years


def report_ranges(
    widths: list[float],
    stats: dict[str, Any],
    results: dict[Variant, list[Trade]],
    span_years: float,
) -> None:
    print("  sideways ranges")
    if widths:
        print(
            f"    stretch width (high to low): median {statistics.median(widths):.1f}%   "
            f"p75 {pct(widths, 0.75):.1f}%   "
            f"at least 4.5%: {100 * sum(w >= 4.5 for w in widths) / len(widths):.0f}% "
            f"of stretches   "
            f"at least 3.5%: {100 * sum(w >= 3.5 for w in widths) / len(widths):.0f}%"
        )
    sh, near = stats["sideways_h"], stats["near_h"]
    if not sh:
        print("    no sideways hours in the window")
        return
    print(f"    near support (within 1 x ATR(1h)): {100 * near / sh:.1f}% of {sh} sideways hours")
    if near:
        up, top = stats["upside"], stats["upside_top"]
        print(
            f"    room to nearest resistance from there: median {statistics.median(up):.2f}%   "
            f"p75 {pct(up, 0.75):.2f}%   p90 {pct(up, 0.9):.2f}%"
        )
        print(
            f"    room to the range top from there:      median {statistics.median(top):.2f}%   "
            f"p75 {pct(top, 0.75):.2f}%   p90 {pct(top, 0.9):.2f}%"
        )
        for v in stats["feasible"]:
            f, ns, lr = stats["feasible"][v], stats["no_stop"][v], stats["low_rr"][v]
            print(
                f"    BUY passes the gates ({v[0]:6}, target {v[1]:9}): "
                f"{100 * f / near:5.1f}% of near-support hours   "
                f"(no support in stop zone {100 * ns / near:.0f}%, "
                f"below 2.0R {100 * lr / near:.0f}%)"
            )
    print(
        "  illustrative range backtest (no model, no other gates; limit assumes every entry fills)"
    )
    for (mode, tgt), trades in results.items():
        label = f"{mode:6} -> {tgt:9}"
        if not trades:
            print(f"    {label}: no trades")
            continue
        rs = [t[3] for t in trades]
        wins = sum(r > 0.05 for r in rs)  # breakeven exits (about 0R) are not wins
        by = {
            k: sum(1 for t in trades if t[2] == k) for k in ("target", "stop", "breakeven", "time")
        }
        print(
            f"    {label}: {len(trades):3} trades ({len(trades) / span_years:4.1f}/yr)   "
            f"win {100 * wins / len(rs):3.0f}%   "
            f"avg {statistics.mean(rs):+.2f}R   total {sum(rs):+.1f}R   "
            f"exits: target {by['target']}, stop {by['stop']}, "
            f"breakeven {by['breakeven']}, time {by['time']}"
        )


def report_trend(
    stats: dict[str, Any], results: dict[Variant, list[Trade]], span_years: float
) -> None:
    print("  up-regime trend trades (Rules BUY from the spec, no model)")
    up, sig = stats["up_h"], stats["signal_h"]
    if not up:
        print("    no up-regime hours in the window")
        return
    print(
        f"    BUY signal (C1 + 4 of C2-C6, not extended): {100 * sig / up:.1f}% of {up} "
        f"up-regime hours"
    )
    if sig:
        for v in stats["feasible"]:
            f, tw, lr = stats["feasible"][v], stats["too_wide"][v], stats["low_rr"][v]
            print(
                f"    passes the gates ({v[0]:6}, target {v[1]:5}): "
                f"{100 * f / sig:5.1f}% of signal hours   "
                f"(stop over 5 x ATR {100 * tw / sig:.0f}%, below 2.0R {100 * lr / sig:.0f}%)"
            )
    for (mode, tgt), trades in results.items():
        label = f"{mode:6} -> {tgt:5}"
        if not trades:
            print(f"    {label}: no trades")
            continue
        rs = [t[3] for t in trades]
        wins = sum(r > 0.05 for r in rs)
        by = {
            k: sum(1 for t in trades if t[2] == k)
            for k in ("target", "stop", "breakeven", "sell", "time")
        }
        print(
            f"    {label}: {len(trades):3} trades ({len(trades) / span_years:4.1f}/yr)   "
            f"win {100 * wins / len(rs):3.0f}%   "
            f"avg {statistics.mean(rs):+.2f}R   total {sum(rs):+.1f}R   "
            f"exits: target {by['target']}, stop {by['stop']}, breakeven {by['breakeven']}, "
            f"sell {by['sell']}, time {by['time']}"
        )


def report_lab(
    trades: dict[str, list[LabTrade]],
    expo: dict[str, dict[str, tuple[float, float, float, int]]],
    since_ms: int,
    split_ms: int,
) -> None:
    print(
        f"  research lab (train {iso(since_ms)} to {iso(split_ms)}, test from {iso(split_ms)}; "
        f"market costs; no model)"
    )
    print(
        f"    {'trend variant':34} {'train trades':>12} {'avg':>7}   {'test trades':>11} {'avg':>7}"
    )
    for label, tr in trades.items():
        n1, a1, n2, a2 = split_stats(tr, split_ms)
        print(f"    {label:34} {n1:12} {a1:+6.2f}R   {n2:11} {a2:+6.2f}R")
    print(
        f"    {'exposure filter (4h, 0.3%/switch)':34} "
        f"{'train return':>12} {'max DD':>7} {'in mkt':>6}   "
        f"{'test return':>11} {'max DD':>7} {'in mkt':>6}"
    )
    for name in EXPOSURE_FILTERS:
        r1, d1_, i1, _ = expo["train"][name]
        r2, d2, i2, _ = expo["test"][name]
        print(
            f"    {name:34} {r1:+11.0f}% {d1_:6.0f}% {i1:5.0f}%   "
            f"{r2:+10.0f}% {d2:6.0f}% {i2:5.0f}%"
        )


def report_lab_summary(lab_rows: list[dict[str, Any]], split_ms: int) -> None:
    print("\nSummary of the research lab (all markets pooled; market costs; no model)")
    print(
        f"  {'trend variant':34} {'train trades':>12} {'avg':>7}   {'test trades':>11} {'avg':>7}"
    )
    for label, *_ in LAB_VARIANTS:
        pooled: list[LabTrade] = [t for r in lab_rows for t in r["trades"][label]]
        n1, a1, n2, a2 = split_stats(pooled, split_ms)
        print(f"  {label:34} {n1:12} {a1:+6.2f}R   {n2:11} {a2:+6.2f}R")
    print(
        f"  {'exposure filter, median of markets':34} "
        f"{'train return':>12} {'max DD':>7}   {'test return':>11} {'max DD':>7}"
    )
    for name in EXPOSURE_FILTERS:
        tr = [r["expo"]["train"][name] for r in lab_rows]
        te = [r["expo"]["test"][name] for r in lab_rows]
        print(
            f"  {name:34} {statistics.median(x[0] for x in tr):+11.0f}% "
            f"{statistics.median(x[1] for x in tr):6.0f}%   "
            f"{statistics.median(x[0] for x in te):+10.0f}% "
            f"{statistics.median(x[1] for x in te):6.0f}%"
        )
    print(
        "  Read the test columns: a variant only counts if it also holds up there. "
        "Tune on train only."
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("markets", nargs="*", default=DEFAULT_MARKETS)
    ap.add_argument("--years", type=float, default=4.0, help="years to analyse (default 4)")
    ap.add_argument("--no-ranges", action="store_true", help="skip part 2 (sideways ranges)")
    ap.add_argument("--no-trend", action="store_true", help="skip part 3 (up-regime trend trades)")
    ap.add_argument("--lab", action="store_true", help="also run part 4, the research lab")
    ap.add_argument(
        "--portfolio", action="store_true", help="also run part 5, the portfolio test (all coins)"
    )
    ap.add_argument(
        "--families", action="store_true", help="also run part 6, three strategy families"
    )
    ap.add_argument("--slots", type=int, default=3, help="portfolio: max coins held (default 3)")
    ap.add_argument(
        "--split",
        default="2025-01-01",
        help="lab holdout: train before this UTC date, test from it (default 2025-01-01)",
    )
    ap.add_argument("--csv", help="write every regime run to this CSV file")
    ap.add_argument("--trades-csv", help="write every backtest trade to this CSV file")
    ap.add_argument(
        "--cache",
        default=CACHE_DEFAULT,
        help=f"candle cache folder (default {CACHE_DEFAULT}; '' to disable)",
    )
    args = ap.parse_args()

    since_ms = int((time.time() - args.years * 365.25 * 86400) * 1000)
    warm_d1 = since_ms - 260 * D1_MS  # EMA 200 on 1d is valid after 250 bars
    warm_h4 = since_ms - 150 * H4_MS
    warm_h1 = since_ms - 300 * H1_MS

    run_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    summary: list[dict[str, Any]] = []
    trend_summary: list[dict[str, Any]] = []
    lab_rows: list[dict[str, Any]] = []
    pf_data: dict[str, tuple[list[Bar], list[Bar]]] = {}
    split_ms = int(datetime.strptime(args.split, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
    print(
        f"Regime history, last {args.years:g} years. Rules: ADX >= {ADX_TREND:g}, "
        f"{CONFIRM_BARS}-bar confirmation. Durations in days (1 bar = 4h)."
    )
    for m in args.markets:
        try:
            d1 = fetch_candles(m, "1d", warm_d1, args.cache or None)
            h4 = fetch_candles(m, "4h", warm_h4, args.cache or None)
            need_h1 = args.lab or not (args.no_ranges and args.no_trend)
            h1 = fetch_candles(m, "1h", warm_h1, args.cache or None) if need_h1 else []
        except Exception as e:  # noqa: BLE001 - boundary: one market's fetch must not kill the batch; reported to stderr
            print(f"\n{m}: could not fetch candles ({e})", file=sys.stderr)
            continue
        if args.portfolio or args.families:
            pf_data[m] = (h4, d1)
        labels = label_bars(h4, d1)
        runs = runs_of(labels)
        span = report_regimes(m, labels, runs, since_ms)
        for reg, start, n, _ in runs:
            if start >= since_ms:
                run_rows.append(
                    {
                        "market": m,
                        "regime": reg,
                        "start_utc": iso(start),
                        "bars_4h": n,
                        "days": round(days(n), 2),
                    }
                )
        if not span or not h1:
            continue
        if args.lab:
            lab_trades, lab_expo = run_lab(h1, h4, d1, labels, since_ms, split_ms)
            report_lab(lab_trades, lab_expo, since_ms, split_ms)
            lab_rows.append({"name": m, "trades": lab_trades, "expo": lab_expo})
        if not args.no_trend:
            t_stats, t_setups, sell = analyse_trend(h1, h4, d1, labels, since_ms)
            t_results = {
                v: backtest(h1, t_setups[v], v[0], sell=sell, cooldown_after_stop_only=True)
                for v in t_setups
            }
            report_trend(t_stats, t_results, span)
            t_row: dict[str, Any] = {"name": m, "span": span, "modes": {}}
            for (mode, tgt), trades in t_results.items():
                rs = [t[3] for t in trades]
                t_row["modes"][(mode, tgt)] = (
                    len(trades) / span,
                    statistics.mean(rs) if rs else float("nan"),
                    sum(rs),
                )
                for t in trades:
                    trade_rows.append(
                        {
                            "market": m,
                            "part": "trend",
                            "orders": mode,
                            "target": tgt,
                            "entry_utc": iso(t[0]),
                            "exit_utc": iso(t[1]),
                            "exit": t[2],
                            "net_r": round(t[3], 3),
                        }
                    )
            trend_summary.append(t_row)
        if args.no_ranges:
            continue
        widths = range_widths(h4, runs, since_ms)
        stats, setups = analyse_ranges(h1, h4, labels, since_ms)
        results = {v: backtest(h1, setups[v], v[0]) for v in setups}
        report_ranges(widths, stats, results, span)
        row: dict[str, Any] = {
            "name": m,
            "modes": {},
            "span": span,
            "near_pct": 100 * stats["near_h"] / stats["sideways_h"] if stats["sideways_h"] else 0,
            "median_room": statistics.median(stats["upside"]) if stats["upside"] else float("nan"),
            "median_top": statistics.median(stats["upside_top"])
            if stats["upside_top"]
            else float("nan"),
        }
        for (mode, tgt), trades in results.items():
            rs = [t[3] for t in trades]
            row["modes"][(mode, tgt)] = (
                len(trades) / span,
                statistics.mean(rs) if rs else float("nan"),
                sum(rs),
            )
            for t in trades:
                trade_rows.append(
                    {
                        "market": m,
                        "part": "range",
                        "orders": mode,
                        "target": tgt,
                        "entry_utc": iso(t[0]),
                        "exit_utc": iso(t[1]),
                        "exit": t[2],
                        "net_r": round(t[3], 3),
                    }
                )
        summary.append(row)

    if summary:
        print("\nSummary of sideways range trading (illustrative; trades per year, average net R)")
        short = {"market": "mkt", "limit": "lim", "nearest": "nearest", "range_top": "top"}
        hdr = "".join(f"  {short[m] + ' > ' + short[tg]:>15}" for tg in TARGETS for m in COSTS)
        print(f"  {'market':9} {'near':>6} {'room':>6} {'top':>6}{hdr}")
        tot: dict[Variant, list[float]] = {}
        for r in summary:
            cells = ""
            for tg in TARGETS:
                for m in COSTS:
                    n, avg, total = r["modes"][(m, tg)]
                    cells += f"  {n:5.1f} {avg:+5.2f}R   " if n else f"  {'-':>15}"
                    a = tot.setdefault((m, tg), [0.0, 0.0])
                    a[0] += n
                    a[1] += total
            print(
                f"  {r['name']:9} {r['near_pct']:5.1f}% {r['median_room']:5.2f}% "
                f"{r['median_top']:5.2f}%{cells}"
            )
        span_avg = statistics.mean(r["span"] for r in summary)
        print(
            "  all markets together, net R per year: "
            + ",  ".join(f"{m} -> {tg}: {v[1] / span_avg:+.1f}R" for (m, tg), v in tot.items())
        )
    if trend_summary:
        print("\nSummary of up-regime trend trades (Rules BUY; trades per year, average net R)")
        short = {"market": "mkt", "limit": "lim", "rules": "rules", "2.5R": "2.5R"}
        hdr = "".join(
            f"  {short[m] + ' > ' + short[tg]:>15}" for tg in TREND_TARGETS for m in COSTS
        )
        print(f"  {'market':9}{hdr}")
        ttot: dict[Variant, float] = {}
        for r in trend_summary:
            cells = ""
            for tg in TREND_TARGETS:
                for m in COSTS:
                    n, avg, total = r["modes"][(m, tg)]
                    cells += f"  {n:5.1f} {avg:+5.2f}R   " if n else f"  {'-':>15}"
                    ttot[(m, tg)] = ttot.get((m, tg), 0.0) + total
            print(f"  {r['name']:9}{cells}")
        span_t = statistics.mean(r["span"] for r in trend_summary)
        print(
            "  all markets together, net R per year: "
            + ",  ".join(f"{m} -> {tg}: {v / span_t:+.1f}R" for (m, tg), v in ttot.items())
        )
    if lab_rows:
        report_lab_summary(lab_rows, split_ms)
    if pf_data and args.portfolio:
        portfolio_report(pf_data, since_ms, split_ms, args.slots)
    if pf_data and args.families:
        families_report(pf_data, since_ms, split_ms, args.slots)
    if args.csv and run_rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(run_rows[0]))
            w.writeheader()
            w.writerows(run_rows)
        print(f"\nWrote {len(run_rows)} runs to {args.csv}")
    if args.trades_csv and trade_rows:
        with open(args.trades_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trade_rows[0]))
            w.writeheader()
            w.writerows(trade_rows)
        print(f"Wrote {len(trade_rows)} trades to {args.trades_csv}")
    print(
        "\nNote: a market listed on Bitvavo later than the start date is analysed from its listing."
    )


if __name__ == "__main__":
    main()
