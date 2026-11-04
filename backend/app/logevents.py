# ruff: noqa: E501
"""The event registry: every log event, in one place.

Every log call names an event from this registry. Each entry has the
code, its level, the component, what it means in one sentence and, for
WARNING and up, what to do. A log call with an unregistered event fails
a test (strict mode in ``logstore``); a WARNING-or-up event without
"what to do" fails the registry check at import.

Codes are stable; renaming one is noted in ``docs/CHANGES.md``.
``GET /api/logs/events`` serves this registry so the Logs screen can
show the meaning and what to do when a line is opened.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LogEvent:
    code: str
    level: int
    component: str
    meaning: str
    what_to_do: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "level": logging.getLevelName(self.level),
            "component": self.component,
            "meaning": self.meaning,
            "what_to_do": self.what_to_do,
        }


DBG = logging.DEBUG
INF = logging.INFO
WRN = logging.WARNING
ERR = logging.ERROR
CRT = logging.CRITICAL

_EVENTS = [
    # ---- app lifecycle ----------------------------------------------------
    LogEvent("app.start", INF, "app", "The app started: version, config and data folder."),
    LogEvent("app.stop", INF, "app", "The app stopped cleanly."),
    LogEvent("app.shutdown", INF, "app", "Shutdown requested; running work gets its grace period."),
    LogEvent(
        "app.shutdown_forced",
        WRN,
        "app",
        "Second Ctrl+C: skipping the graceful wait.",
        "No action needed; the session still commits or rolls back and checkpoints.",
    ),
    LogEvent(
        "app.interrupted_run",
        INF,
        "app",
        "A What-if run in progress at shutdown was cancelled and marked interrupted.",
    ),
    LogEvent(
        "app.unclean_shutdown",
        WRN,
        "app",
        "The previous run did not shut down cleanly; the database was checked and cycles marked interrupted.",
        "Usually the computer was stopped or killed. Nothing to do unless it repeats.",
    ),
    LogEvent(
        "config.invalid",
        CRT,
        "app",
        "A config or settings value failed validation at startup.",
        "Fix the named key and start again.",
    ),
    LogEvent(
        "secrets.unavailable",
        ERR,
        "app",
        "The OS keyring can't be read or written; keys count as not set.",
        "Check the keyring backend (login keyring unlocked, secret service running); the Needs you key item lists which keys are affected.",
    ),
    # ---- market data --------------------------------------------------------
    LogEvent(
        "candles.stale",
        WRN,
        "market.bitvavo",
        "The newest closed candle is missing or too old after the candle retries; the pair is held and marked stale.",
        "Usually a slow exchange publish or a network blip. If a pair stays stale, check the Bitvavo status page and your connection.",
    ),
    LogEvent(
        "candles.fetch_failed",
        WRN,
        "market.bitvavo",
        "A candles request failed after its retries; the pair uses stored candles or is held.",
        "Check the Bitvavo status and the http.call lines around it for the status code.",
    ),
    LogEvent(
        "stream.disconnected",
        WRN,
        "market.stream",
        "The price stream lost its connection; the watcher used REST prices meanwhile.",
        "Reconnects are automatic. If it repeats every few minutes, check the connection.",
    ),
    LogEvent(
        "stream.down",
        ERR,
        "market.stream",
        "The price stream stayed down for 5 minutes; the watcher is on REST prices only.",
        "Check the Bitvavo status page and the connection; exits still work on REST prices.",
    ),
    LogEvent(
        "book.unavailable",
        WRN,
        "market.bitvavo",
        "The order book for a limit entry could not be read, so no limit order was placed.",
        "A limit entry needs a readable book. If it repeats for one pair, check Bitvavo's book endpoint for it.",
    ),
    LogEvent(
        "clock.offset_failed",
        WRN,
        "app",
        "The exchange clock could not be read this cycle; the offset shown is the last known value.",
        "Information only. If it repeats, the /time endpoint may be failing — check candles.fetch_failed lines too.",
    ),
    LogEvent(
        "clock.offset_high",
        WRN,
        "app",
        "The local clock is more than 2 s off the exchange clock.",
        "Information only, cycles still run. Check the system clock if it grows.",
    ),
    LogEvent(
        "eligibility.failing",
        WRN,
        "market.universe",
        "An active market failed today's eligibility check.",
        "The market stays active but takes no new entries. Review it on the Markets screen.",
    ),
    LogEvent(
        "source.failing",
        ERR,
        "market.bitvavo",
        "A data source failed three times in a row; its error is on the Sources card.",
        "Check the source's status on the Sources card and its key in Settings; the app keeps trading on the last good data.",
    ),
    LogEvent(
        "eligibility.check_failed",
        ERR,
        "market.universe",
        "The daily eligibility check itself failed; yesterday's result stands.",
        "Check the job.failed line for the error; the check runs again tomorrow.",
    ),
    # ---- engine ---------------------------------------------------------------
    LogEvent("cycle.start", INF, "agent.scheduler", "A cycle started."),
    LogEvent(
        "cycle.end",
        INF,
        "agent.scheduler",
        "A cycle ended; the summary line carries duration, pairs, model calls, actions and errors.",
    ),
    LogEvent(
        "cycle.slow",
        WRN,
        "agent.scheduler",
        "A cycle took over 5 minutes.",
        "One slow cycle is fine. Repeating slow cycles usually mean a slow API — check the duration_ms column.",
    ),
    LogEvent(
        "cycle.pair_failed",
        ERR,
        "agent.scheduler",
        "An unhandled exception while deciding one pair; the pair was recorded as HOLD with the error id.",
        "Open the error id in Logs for the traceback. Other pairs were not affected.",
    ),
    LogEvent(
        "cycle.step_failed",
        ERR,
        "agent.scheduler",
        "An unhandled exception in a cycle step; the cycle was stored as an error.",
        "Open the error id in Logs for the traceback; the slot shows an error mark.",
    ),
    LogEvent(
        "scheduler.slot_skipped",
        WRN,
        "agent.scheduler",
        "A scheduled slot did not run — the computer was asleep, the app was stopped, or the agent was paused. Slots are never replayed.",
        "Information only; the slot is stored with its reason.",
    ),
    LogEvent(
        "watcher.pass",
        DBG,
        "agent.watcher",
        "One 30-second watcher pass: prices, stops, targets, kill level.",
    ),
    LogEvent(
        "watcher.unpriced",
        CRT,
        "agent.watcher",
        "An open position has had no usable price for 2 minutes; exits can't be checked on it.",
        "Check the price source and the position's pair; this is a Needs you item until the price is back.",
    ),
    LogEvent(
        "decision.final",
        INF,
        "agent.decider",
        "A pair's final action for the cycle, with the decider and reason.",
    ),
    LogEvent(
        "prefilter.pass",
        DBG,
        "agent.rules",
        "A pair passed the Rules pre-filter and reaches the model.",
    ),
    LogEvent(
        "prefilter.held", DBG, "agent.rules", "A pair failed the Rules pre-filter; no model call."
    ),
    LogEvent(
        "model.call_sent",
        DBG,
        "agent.decider",
        "A decision call went to the model; the llm_calls row id links the stored request.",
    ),
    LogEvent(
        "model.call_returned",
        DBG,
        "agent.decider",
        "A decision call returned; tokens, latency and cost are stored.",
    ),
    LogEvent(
        "model.call_failed",
        ERR,
        "agent.decider",
        "The decision call failed after its retry; Rules answered this cycle.",
        "Once in a while is normal. If it repeats, check status.openai.com and the latency column in the LLM inspector.",
    ),
    LogEvent(
        "model.output_invalid",
        WRN,
        "agent.decider",
        "The model's answer was not valid JSON or failed the schema; HOLD stands.",
        "Check the raw response in the LLM inspector; a repeating failure may need a prompt version bump.",
    ),
    LogEvent(
        "postcheck.failed",
        WRN,
        "agent.decider",
        "The model's answer failed a post-check (levels, action, regime); HOLD stands.",
        "Check which post-check fired in the decision record.",
    ),
    LogEvent(
        "model.cost_cap",
        WRN,
        "agent.decider",
        "The monthly cost cap is reached; decisions fall back to Rules until the 1st.",
        "Raise the cap in Settings or wait for next month.",
    ),
    # ---- broker ---------------------------------------------------------------
    LogEvent(
        "fill.entry", INF, "broker.paper", "A paper buy filled; price, slippage and fee are stored."
    ),
    LogEvent("fill.exit", INF, "broker.paper", "A paper sell filled with its reason and result."),
    LogEvent("order.placed", INF, "broker.paper", "A limit order was placed."),
    LogEvent("order.filled", INF, "broker.paper", "A limit order filled."),
    LogEvent("order.cancelled", INF, "broker.paper", "A limit order was cancelled unfilled."),
    LogEvent("stop.moved", INF, "broker.paper", "A stop moved (breakeven or trailing)."),
    LogEvent(
        "kill.fired",
        CRT,
        "agent.watcher",
        "The kill switch fired: drawdown passed the kill level; all positions were closed.",
        "Review the drawdown on Performance, then acknowledge the kill to resume.",
    ),
    # ---- storage ----------------------------------------------------------------
    LogEvent(
        "db.locked",
        ERR,
        "app",
        "The trading database stayed locked past its timeout; the unit of work was rolled back.",
        "If it repeats, something else is holding the database — check for a second process or a long backup.",
    ),
    LogEvent(
        "db.unwritable",
        CRT,
        "app",
        "The trading database can't be written (disk full, read-only or corrupt).",
        "Check disk space and file permissions right away — this is a Needs you item and trading state may not persist.",
    ),
    LogEvent(
        "db.checkpoint_failed",
        WRN,
        "app",
        "The WAL checkpoint at shutdown did not finish; the next start replays the log normally.",
        "Usually another process still had the database open. Nothing to do unless it repeats.",
    ),
    # ---- jobs ---------------------------------------------------------------------
    LogEvent("job.start", INF, "ops.jobs", "A scheduled job started."),
    LogEvent("job.end", INF, "ops.jobs", "A scheduled job ended."),
    LogEvent(
        "job.failed",
        ERR,
        "ops.jobs",
        "A scheduled job failed; the next run stays on schedule.",
        "Open the error id in Logs for the traceback; the job retries on its normal schedule.",
    ),
    # ---- background tasks ------------------------------------------------------------
    LogEvent(
        "task.crashed",
        ERR,
        "app",
        "A background task raised; it is restarted after a backoff.",
        "Check the traceback; if the task keeps crashing the next line is task.crash_loop.",
    ),
    LogEvent(
        "task.crash_loop",
        CRT,
        "app",
        "A background task crashed 5 times in 10 minutes; it keeps restarting but something is wrong.",
        "This is a Needs you degraded item — check the task.crashed lines for the cause.",
    ),
    # ---- notifications ------------------------------------------------------------------
    LogEvent("notify.sent", INF, "notify", "A notification was delivered on a channel."),
    LogEvent(
        "notify.email_failed",
        ERR,
        "notify",
        "The email channel failed; the browser notification still went out.",
        "Check the SMTP settings and send a test from Settings.",
    ),
    LogEvent(
        "notify.email_skipped",
        WRN,
        "notify",
        "An email was not sent because SMTP is not configured; the browser notification went out.",
        "Set up SMTP in Settings, or turn email off for this event.",
    ),
    # ---- api --------------------------------------------------------------------------------
    LogEvent("api.request", DBG, "api", "An API request completed."),
    LogEvent(
        "api.rejected", DBG, "api", "An API request was rejected (validation, not found, conflict)."
    ),
    LogEvent(
        "api.unhandled",
        ERR,
        "api",
        "An API request raised an unhandled exception; the client got a 500 with an error id.",
        "Open the error id in Logs for the traceback.",
    ),
    LogEvent(
        "frontend.error",
        ERR,
        "frontend",
        "The frontend reported an error or an unhandled rejection.",
        "Open the line for the route and component stack; the agent itself is not affected.",
    ),
    LogEvent("ws.client_lost", DBG, "api", "A WebSocket client stopped answering and was dropped."),
    # ---- logs store ------------------------------------------------------------------------------
    LogEvent(
        "logs.dropped",
        WRN,
        "ops.logs",
        "The log queue was full; DEBUG lines were dropped first, then INFO. WARNING and up are never dropped.",
        "Raise logging.queue_size or check what is flooding the log at DEBUG.",
    ),
    LogEvent(
        "logs.store_failed",
        ERR,
        "ops.logs",
        "logs.db can't be written; lines are going to the fallback file and stderr.",
        "Check disk space and file permissions on data/logs/; lines are imported once it is writable again.",
    ),
    LogEvent(
        "legacy.error",
        ERR,
        "app",
        "An error row imported once from the retired errors table.",
        "Review it like any other problem; the original row stays in errors_legacy.",
    ),
    # ---- misc ---------------------------------------------------------------------------------------
    LogEvent(
        "python.warning",
        WRN,
        "python.warning",
        "A Python warning was raised (deprecation, pandas/numpy).",
        "Warnings from app code fail tests; library warnings should be tracked to a fix or filtered deliberately.",
    ),
    LogEvent("retry.attempt", DBG, "app", "An external call is being retried."),
    LogEvent(
        "http.call",
        DBG,
        "market.bitvavo",
        "An external HTTP call: method, endpoint, status, duration, attempt.",
    ),
    LogEvent(
        "ask.tool_failed",
        ERR,
        "agent.decider",
        "An Ask tool raised; the question got an error answer instead.",
        "Open the error id in Logs for the traceback.",
    ),
    LogEvent("key.tested", INF, "app", "An API key test ran with its result."),
    LogEvent(
        "settings.saved",
        INF,
        "app",
        "Settings were saved; the changed keys and values are in the data field.",
    ),
    LogEvent("notify.test", INF, "notify", "A test notification was sent."),
]

EVENTS: dict[str, LogEvent] = {e.code: e for e in _EVENTS}

# Strict mode (tests): an unregistered event raises at the logging call
# site instead of passing silently.
STRICT = os.environ.get("TA_STRICT_EVENTS") == "1"


def set_strict(on: bool) -> None:
    global STRICT
    STRICT = on


def check(event: str | None) -> None:
    """Raise on an event the registry doesn't know (strict mode only)."""
    if STRICT and event is not None and event not in EVENTS:
        raise KeyError(f"unregistered log event {event!r}")


def check_registry() -> None:
    """Every WARNING-or-up event must say what to do. Runs at import and
    in tests."""
    for e in _EVENTS:
        if e.level >= logging.WARNING and not e.what_to_do:
            raise ValueError(f"event {e.code} has no what_to_do")


check_registry()
