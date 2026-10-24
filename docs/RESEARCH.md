# Strategy research, October 2026

Repository copy of the Claude Doc "Strategy research, October 2026" (23 October 2026).
The script is `tools/regime_history.py`. This describes a backtest, not investment advice.

## Summary

After costs, on 9 Bitvavo EUR markets over the last 4 years, no active approach tested beat
a fixed mix of BTC and EUR at the same drawdown. That covers trading the agent's Rules BUY on
1h or 4h candles (several exit variants), trading sideways ranges, and allocating to coins by
regime. The regime allocator does protect against altcoin crashes (similar return to an
equal-weight basket at a third of the drawdown), but a static BTC + cash mix with the same
drawdown did as well, with almost no trades.

The Trading Agent stays a paper-only research platform. New strategy features are paused.
An idea must beat the static BTC mix at the same drawdown, in the training and the test
period, with stable sensitivity, in the research script before it goes into the spec.

## Method

- **Data:** public Bitvavo candles (1h, 4h, 1d) for BTC, ETH, SOL, XRP, ADA, LINK, AVAX, DOGE
  and LTC against EUR, 23 October 2022 to 23 October 2026.
- **Rules:** the agent's own definitions from the spec: regime (4h ADX >= 25, 4h EMA 20 vs 50,
  daily close vs EMA 200, 2-bar confirmation), Rules BUY conditions C1-C6, stop and target
  rules, the 2.0R and fee gates, breakeven at +1R, 5-day time stop, 6-hour cooldown.
- **Costs:** market orders at 0.25% fee + 0.05% slippage per side (0.6% per round trip);
  limit entries at a 0.15% maker fee, assumed always filled (an upper bound). Every switch in
  the allocator tests costs 0.3%.
- **Holdout:** training up to 1 January 2025, test from then on. A result counts only if it
  holds in both.
- **Not modelled:** the language model (Rules logic only), interest on EUR, taxes, order-book
  depth, delisted coins. The coin list is today's survivors, which flatters every result.

## Results

### Regimes

Every coin spends 66-69% of the time sideways, 9-20% in an up regime and 13-25% in a down
regime. Up stretches last a median of 2-4 days (9-11 days at the 90th percentile).

### Sideways ranges

From a support in a sideways regime, the nearest resistance is a median 0.2-0.5% away, while
the fee gate needs a target at least 4% away. The sideways rule almost never trades and its
few trades lost. Aiming for the top of the range traded 80-200 times a year per coin and lost
on every coin (-0.13R to -0.38R per trade).

### Trend trades (Rules BUY, no model)

| Variant, all coins pooled | Train trades | Train avg | Test trades | Test avg |
| --- | --- | --- | --- | --- |
| 1h, as in the spec | 179 | -0.15R | 80 | -0.02R |
| 1h, 2.5R target, no Rules SELL | 851 | -0.26R | 432 | -0.20R |
| 1h, trailing stop from 2R | 818 | -0.22R | 413 | -0.19R |
| 4h, 2.5R target | 554 | -0.13R | 282 | -0.21R |
| 4h, trailing stop from 2R | 480 | -0.13R | 261 | -0.23R |

### Allocators against benchmarks (full period, after costs)

| Strategy | Return | CAGR | Max DD | CAGR/DD | Test return / max DD |
| --- | --- | --- | --- | --- | --- |
| BTC buy & hold | +273% | 39% | 52% | 0.75 | -20% / 52% |
| Static 50% BTC + cash, monthly | +116% | 21% | 29% | 0.73 | -7% / 29% |
| Static 30% BTC + cash, monthly | +63% | 13% | 18% | 0.71 | -3% / 18% |
| Static 20% BTC + cash, monthly | +40% | 9% | 12% | 0.70 | -2% / 13% |
| Equal-weight buy & hold, 9 coins | +76% | 15% | 73% | 0.21 | -52% / 74% |
| Hold while regime up, all coins | +78% | 16% | 22% | 0.72 | -5% / 22% |
| Regime up, BTC on daily > EMA 200, all coins | +102% | 19% | 22% | 0.86 | -3% / 22% |
| BTC only, hold while daily > EMA 200 | +147% | 25% | 38% | 0.67 | +2% / 29% |

The regime allocator matches a static mix of about 35-40% BTC at the same drawdown, in the
test period too. The BTC-on-EMA-200 variant scored best, but it was designed after seeing BTC
fail and its advantage comes from the training period only.

### Sensitivity

The regime allocator's test result swings from -2% to -37% across ADX 20/25/30 and 1-3
confirmation bars; the agent's default sits in the best spot. Every setting still beat the
equal-weight basket in the test period (-52%).

## Conclusions

1. **Active trading has no edge here** (firm): every Rules BUY variant loses after costs, on
   1h and 4h, in both periods, over thousands of trades.
2. **Range trading has no edge here** (firm).
3. **Timing adds nothing over sizing** (strong indication, one market cycle): the allocator's
   crash protection is matched by holding less BTC.

**Rule for future ideas:** into the spec only when, in the research script, an idea beats the
static BTC + cash mix at the same maximum drawdown in training and test, and the sensitivity
grid agrees.

## Status

| Item | Status |
| --- | --- |
| Paper agent (v0.1) | Keeps running, unchanged |
| v0.2 collector | Keeps running; test its signals in the script in 3-6 months |
| Fix PR | Finish |
| PR 12, limit entries | Finish only if nearly done |
| PR 13, prompt v2 | Resumed 24 October 2026 (decision quality, no strategy change) |
| PR 10, 11 | Paused |
| v0.3 | Not started |

## How to reproduce

```bash
python3 tools/regime_history.py                                     # regimes, ranges, trend trades
python3 tools/regime_history.py --lab --no-ranges --no-trend        # trend variants, train/test
python3 tools/regime_history.py --portfolio --no-ranges --no-trend  # allocators vs benchmarks
```

Standard library only; candles are cached in `$TA_DATA_DIR/candle_cache`. A later run covers a
different 4-year window and shows somewhat different numbers.
