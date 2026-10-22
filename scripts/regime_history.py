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

Only reads public Bitvavo candles. No API key, no account, no orders, and it does
not touch the agent or its database. Standard library only. Candles are cached in
$TA_DATA_DIR/candle_cache (data/candle_cache by default) so a second run only
fetches what is new.

Usage:
  python3 regime_history.py                      # 9 default markets, 4 years
  python3 regime_history.py --years 6 BTC-EUR ETH-EUR
  python3 regime_history.py --no-ranges --no-trend   # part 1 only, much faster
  python3 regime_history.py --csv runs.csv --trades-csv trades.csv
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


def label_bars(h4: list[Bar], d1: list[Bar]) -> list[Label]:
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
        trending = adxv >= ADX_TREND
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
            if streak >= CONFIRM_BARS:
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


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("markets", nargs="*", default=DEFAULT_MARKETS)
    ap.add_argument("--years", type=float, default=4.0, help="years to analyse (default 4)")
    ap.add_argument("--no-ranges", action="store_true", help="skip part 2 (sideways ranges)")
    ap.add_argument("--no-trend", action="store_true", help="skip part 3 (up-regime trend trades)")
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
    print(
        f"Regime history, last {args.years:g} years. Rules: ADX >= {ADX_TREND:g}, "
        f"{CONFIRM_BARS}-bar confirmation. Durations in days (1 bar = 4h)."
    )
    for m in args.markets:
        try:
            d1 = fetch_candles(m, "1d", warm_d1, args.cache or None)
            h4 = fetch_candles(m, "4h", warm_h4, args.cache or None)
            need_h1 = not (args.no_ranges and args.no_trend)
            h1 = fetch_candles(m, "1h", warm_h1, args.cache or None) if need_h1 else []
        except Exception as e:
            print(f"\n{m}: could not fetch candles ({e})", file=sys.stderr)
            continue
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
