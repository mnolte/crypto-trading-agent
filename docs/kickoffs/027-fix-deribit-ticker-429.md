# 027 · fix · Deribit ticker 429 rate limit

Kickoff for Devin. Start the session with: "Build docs/kickoffs/027-fix-deribit-ticker-429.md."

```text
027 (Deribit ticker 429 rate limit). Goal: the hourly market-context
collect finishes the Deribit skew fetch without 429 warnings. The skew
values, the instruments selected and the stored rows don't change: this
item only fixes how the ~216 per-instrument /public/ticker requests are
paced and retried. Build on the latest main.

Names (docs/work-items.md): branch fix/027-deribit-ticker-429, PR title
"[027] fix: Deribit ticker 429 rate limit", commits start with [027].
This item's kickoff PR added docs/kickoffs/027-fix-deribit-ticker-429.md
and docs/changes/027-fix-deribit-ticker-429.md; the build PR moves the
kickoff unchanged to docs/kickoffs/archive/ with git mv. Put `Closes #47`
in the PR description (the issue is mnolte/trading-agent#47).

Read:
1. docs/changes/027-fix-deribit-ticker-429.md and issue #47 (root cause
   and proposed fix)
2. backend/app/context/collector.py — _get (the 2-attempt retry),
   _try, _collect_deribit (the per-instrument ticker gather under
   asyncio.Semaphore(4)) and calculate_skew (what fields it needs:
   greeks.delta, mark_iv, underlying_price)
3. docs/SPEC.md → v0.2 → Market context collector (the skew_25d row:
   25-delta put minus 25-delta call on the expiry closest to 30 days,
   expiries under 7 days ignored, hourly) and Settings, secrets and
   persistence (where a new threshold default would live)
4. backend/app/models.py — the Settings models, for where a new
   Deribit rate value belongs; backend/app/sources.py for the source
   descriptor's rate_limit text
5. AGENTS.md → hard rules (failures end as HOLD/SKIP-style degradation,
   no real network calls in tests, thresholds from config or Settings)

What exists today (main):
- _collect_deribit picks the expiry closest to 30 days (>= 7 days out)
  per symbol and calls /public/ticker once per selected option
  instrument: ~120 BTC + ~96 ETH = ~216 requests per hourly collect(),
  run by context_loop at minute 5 of every hour.
- asyncio.Semaphore(4) allows 4 in flight; _get retries once after any
  status in RETRY_STATUSES (429 included), sleeping 1 s on 429 or
  Retry-After capped at 30 s. A 429 burst therefore exhausts the single
  retry inside the same limited window and every instrument logs
  "market context request failed deribit /public/ticker: 429".
- /public/get_book_summary_by_currency is NOT a replacement: it has
  mark_iv and underlying_price but no greeks, and calculate_skew needs
  greeks.delta. Keep the per-instrument calls.
- The collector's clock and sleep are injectable; tests mock httpx with
  respx. Look at backend/tests for the existing collector tests and
  follow their fixtures.

Build, in this order (one commit per step):

1. Rate limiting. Add a Settings-backed value for the Deribit public
   request rate (requests per second or minimum interval; pick the
   shape that fits the existing Settings models, default conservative
   enough that the hourly ~216-request burst stays under Deribit's
   unauthenticated limit — Deribit documents per-method limits; the
   observed 429s say the effective ceiling is low). Apply it to every
   Deribit call in the collector (DVOL pages and ticker calls), shared
   across the gather so concurrency can't bypass it. OKX stays as is.
   The limiter must be fair under asyncio.gather and must not busy-wait:
   use the injected sleep so tests stay deterministic.
2. 429 retry budget. Give 429 (and 5xx, unchanged) a real retry budget:
   several attempts (suggest 4-5 total) with exponential backoff and
   jitter, honoring Retry-After when present (still capped). Network
   errors keep the existing single retry. A request that still fails
   after the budget ends as today: _try logs once and the point is
   skipped; the source error is recorded once per collect, not per
   instrument (check whether errors[0] already gives that; if the
   warning-per-instrument is itself too noisy, log the summary once and
   keep per-request detail at debug level — but only if it doesn't hide
   real failures).
3. Tests (no network; respx, fake clock/sleep):
   - pass: a ticker request answered 429 then 200 produces the skew
     point; the retry waits the backoff/Retry-After the fake sleep
     recorded;
   - pass: N instruments complete and the recorded request times respect
     the configured minimum interval;
   - fail: persistent 429s exhaust the budget, the source error is
     recorded, no exception escapes collect(), and the skew point is
     absent (null path);
   - the existing skew/dvol collector tests still pass unchanged.
4. Run only what you changed: ruff on the touched files, the collector
   test file(s) with -k, eslint/vitest only if frontend files change
   (they shouldn't). Push early as a draft PR; CI is the proof.

Do not:
- change which instruments are selected, the skew maths, the stored
  metrics or the hourly cadence;
- add a new external dependency for rate limiting or retry;
- hit the real Deribit API in tests;
- edit docs/SPEC.md, design/ or backend/prompts/ (ask instead);
- delete data/, a database or a backup outside a test's own folder.

In the PR description: the chosen rate default and why; the retry
budget; "Issues closed" listing each point of #47 with file:line or the
test that proves it; deviations from the spec and design differences
(none expected).
```
