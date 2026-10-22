#!/usr/bin/env python3
"""
regime_history.py — how long does each coin spend in an up, sideways or down regime?

Labels every closed 4h bar with the Trading Agent's regime rules (docs/SPEC.md,
"Indicator definitions -> Regime") and reports, per market:
  - share of time in each regime
  - how long regime runs last (median, mean, 90th percentile, longest)
  - how many separate down stretches there were per year
  - the current regime and its age

Rules (same as the agent):
  up        4h ADX >= 25, 4h EMA 20 > EMA 50, daily close > daily EMA 200
  down      4h ADX >= 25, 4h EMA 20 < EMA 50, daily close < daily EMA 200
  sideways  everything else
  A new label only takes effect after 2 closed 4h bars in a row agree.

Only reads public Bitvavo candles. No API key, no account, no orders, and it does
not touch the agent or its database. Standard library only.

Usage:
  python3 regime_history.py                      # 9 default markets, 4 years
  python3 regime_history.py --years 6 BTC-EUR ETH-EUR
  python3 regime_history.py --csv runs.csv       # also write every run to a CSV
"""
import argparse
import csv
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.bitvavo.com/v2"
DEFAULT_MARKETS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR",
                   "LINK-EUR", "AVAX-EUR", "DOGE-EUR", "LTC-EUR"]
ADX_TREND = 25.0
CONFIRM_BARS = 2
H4_MS = 4 * 3600 * 1000
D1_MS = 24 * 3600 * 1000


# ---------------------------------------------------------------- data

def fetch_candles(market, interval, since_ms):
    """All closed candles from since_ms until now, oldest first. Pages backwards."""
    step = H4_MS if interval == "4h" else D1_MS
    now_ms = int(time.time() * 1000)
    out = {}
    end = now_ms
    while True:
        url = f"{API}/{market}/candles?interval={interval}&limit=1440&end={end}"
        req = urllib.request.Request(url, headers={"User-Agent": "regime-history/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read())
        if isinstance(rows, dict):  # error object
            raise RuntimeError(f"{market} {interval}: {rows}")
        if not rows:
            break
        for ts, o, h, l, c, v in rows:
            ts = int(ts)
            if ts + step <= now_ms:          # closed bars only (ts is the open time)
                out[ts] = (float(h), float(l), float(c))
        oldest = min(int(r[0]) for r in rows)
        if oldest <= since_ms or len(rows) < 1440:
            break
        end = oldest - 1
        time.sleep(0.25)                     # stay far below the rate limit
    ts_sorted = sorted(t for t in out if t >= since_ms - 400 * step)  # warm-up kept
    return [(t, *out[t]) for t in ts_sorted]


# ---------------------------------------------------------------- indicators

def ema(values, n):
    """EMA seeded with the SMA of the first n values; None until n + 50 bars (spec)."""
    res = [None] * len(values)
    if len(values) < n:
        return res
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    raw = [None] * len(values)
    raw[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        raw[i] = e
    for i in range(len(values)):
        if i >= n + 50 - 1:
            res[i] = raw[i]
    return res


def adx(highs, lows, closes, n=14):
    """Wilder's ADX. None until enough bars."""
    size = len(closes)
    res = [None] * size
    if size < 2 * n + 1:
        return res
    tr, pdm, mdm = [0.0] * size, [0.0] * size, [0.0] * size
    for i in range(1, size):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    s_tr = sum(tr[1:n + 1]); s_p = sum(pdm[1:n + 1]); s_m = sum(mdm[1:n + 1])
    dx = [None] * size

    def _dx(st, sp, sm):
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
    a = sum(dx[n:2 * n]) / n
    res[2 * n - 1] = a
    for i in range(2 * n, size):
        a = (a * (n - 1) + dx[i]) / n
        res[i] = a
    return res


# ---------------------------------------------------------------- regime

def label_bars(h4, d1):
    """Confirmed regime label per closed 4h bar: list of (close_ts_ms, label or None)."""
    c4 = [b[3] for b in h4]
    e20, e50 = ema(c4, 20), ema(c4, 50)
    a14 = adx([b[1] for b in h4], [b[2] for b in h4], c4)
    cd = [b[3] for b in d1]
    e200 = ema(cd, 200)
    d_close_ts = [b[0] + D1_MS for b in d1]

    out, j, current, pending, streak = [], -1, None, None, 0
    for i, bar in enumerate(h4):
        close_ts = bar[0] + H4_MS
        while j + 1 < len(d1) and d_close_ts[j + 1] <= close_ts:   # latest daily bar closed by now
            j += 1
        if None in (e20[i], e50[i], a14[i]) or j < 0 or e200[j] is None:
            out.append((close_ts, None))
            continue
        trending = a14[i] >= ADX_TREND
        d_up, d_dn = cd[j] > e200[j], cd[j] < e200[j]
        if trending and e20[i] > e50[i] and d_up:
            raw = "up"
        elif trending and e20[i] < e50[i] and d_dn:
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


def runs_of(labels):
    runs, start, cur, n = [], None, None, 0
    for ts, lab in labels:
        if lab is None:
            continue
        if lab != cur:
            if cur is not None:
                runs.append((cur, start, n))
            cur, start, n = lab, ts, 0
        n += 1
    if cur is not None:
        runs.append((cur, start, n))
    return runs


# ---------------------------------------------------------------- report

def days(bars):
    return bars * 4 / 24


def fmt_d(x):
    return f"{x:5.1f}d" if x < 100 else f"{x:5.0f}d"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def report(market, labels, runs, since_ms):
    labelled = [(t, l) for t, l in labels if l is not None and t >= since_ms]
    if not labelled:
        print(f"\n{market}: not enough history")
        return
    total = len(labelled)
    span_years = (labelled[-1][0] - labelled[0][0]) / (365.25 * D1_MS)
    print(f"\n{market}   {iso(labelled[0][0])} to {iso(labelled[-1][0])}   ({span_years:.1f} years, {total} 4h bars)")
    print(f"  {'regime':9} {'time':>6} {'runs':>5} {'median':>7} {'mean':>7} {'p90':>7} {'longest':>8}  longest from")
    for reg in ("up", "sideways", "down"):
        share = 100 * sum(1 for _, l in labelled if l == reg) / total
        rr = [r for r in runs if r[0] == reg and r[1] >= since_ms]
        if not rr:
            print(f"  {reg:9} {share:5.1f}% {0:>5}")
            continue
        lens = sorted(days(r[2]) for r in rr)
        p90 = lens[min(len(lens) - 1, int(0.9 * len(lens)))]
        longest = max(rr, key=lambda r: r[2])
        print(f"  {reg:9} {share:5.1f}% {len(rr):>5} {fmt_d(statistics.median(lens)):>7} "
              f"{fmt_d(statistics.mean(lens)):>7} {fmt_d(p90):>7} {fmt_d(days(longest[2])):>8}  {iso(longest[1])}")
    downs = [r for r in runs if r[0] == "down" and r[1] >= since_ms]
    buy_ok = 100 * sum(1 for _, l in labelled if l != "down") / total
    note = "BTC may also buy in a down regime (confidence cap 0.6)" if market.startswith("BTC-") else \
           "BUY allowed outside down regimes (sideways only near support)"
    print(f"  down stretches per year: {len(downs) / span_years:.1f}   time a BUY is possible: {buy_ok:.0f}%  ({note})")
    last = runs[-1]
    print(f"  now: {last[0]} for {last[2]} bars ({days(last[2]):.1f} days)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("markets", nargs="*", default=DEFAULT_MARKETS)
    ap.add_argument("--years", type=float, default=4.0, help="years to analyse (default 4)")
    ap.add_argument("--csv", help="write every regime run to this CSV file")
    args = ap.parse_args()

    since_ms = int((time.time() - args.years * 365.25 * 86400) * 1000)
    # Extra history so EMA 200 on 1d (250 bars incl. warm-up) is valid at the start.
    warm_d1 = since_ms - 260 * D1_MS
    warm_h4 = since_ms - 120 * H4_MS

    rows = []
    print(f"Regime history, last {args.years:g} years. Rules: ADX >= {ADX_TREND:g}, "
          f"{CONFIRM_BARS}-bar confirmation. Durations in days (1 bar = 4h).")
    for m in args.markets:
        try:
            d1 = [b for b in fetch_candles(m, "1d", warm_d1) if b[0] >= warm_d1]
            h4 = [b for b in fetch_candles(m, "4h", warm_h4) if b[0] >= warm_h4]
        except Exception as e:
            print(f"\n{m}: could not fetch candles ({e})", file=sys.stderr)
            continue
        labels = label_bars(h4, d1)
        runs = runs_of(labels)
        report(m, labels, runs, since_ms)
        for reg, start, n in runs:
            if start >= since_ms:
                rows.append({"market": m, "regime": reg, "start_utc": iso(start),
                             "bars_4h": n, "days": round(days(n), 2)})
    if args.csv and rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} runs to {args.csv}")
    print("\nNote: a market listed on Bitvavo later than the start date is analysed from its listing.")


if __name__ == "__main__":
    main()