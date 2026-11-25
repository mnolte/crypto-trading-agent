"""The inspector's call status, reasons and attention level (item 043).

One fixed registry explains every reason in the spec's words. ``classify``
runs once when a call is stored; ``attention`` looks only at earlier rows,
so a stored level never changes when newer calls arrive. The migration's
backfill and the four writers all use this module — the screen never
invents explanations of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import LlmCallRow

if TYPE_CHECKING:
    from app.models import Settings

Attention = Literal["none", "watch", "act"]
OutcomeStatus = Literal["success", "warning", "error"]


@dataclass(frozen=True)
class ReasonText:
    code: str
    label: str
    meaning: str
    likely_cause: str
    what_to_do: str
    links: tuple[tuple[str, str], ...] = ()


LINK_DECISION = "decision"
LINK_KEYS = "settings_keys"
LINK_LOGS = "logs"
LINK_INSIGHTS = "insights"
LINK_MODEL = "settings_model"
LINK_UNIVERSE = "universe"
LINK_CALLS = "calls"

REASONS: dict[str, ReasonText] = {
    "changed_by_code": ReasonText(
        code="changed_by_code",
        label="Changed by code",
        meaning=("The model's answer was valid, but code changed the action, usually BUY → SKIP"),
        likely_cause=(
            "A risk gate or post-check did its job: costs, reward to risk, levels, "
            "the regime rule, a pause"
        ),
        what_to_do=(
            "Usually nothing. Open the decision for the gate's numbers. If one gate keeps "
            "blocking good setups, check Insights and test another threshold in a shadow "
            "before changing it"
        ),
        links=(("Open the decision", LINK_DECISION), ("Insights", LINK_INSIGHTS)),
    ),
    "claim_mismatch": ReasonText(
        code="claim_mismatch",
        label="Claim doesn't match the input",
        meaning=(
            "The answer names a signal the brief doesn't support. The decision stands: "
            "code checks the levels and the gates itself, not the model's words"
        ),
        likely_cause="The model described something it couldn't see in its input",
        what_to_do=(
            "Once: nothing. If you judge the call, mark it Wrong with a note, so it lands "
            "in the labelled export. Often under one prompt: test a prompt change in a "
            "shadow first"
        ),
        links=(("Open the decision", LINK_DECISION),),
    ),
    "check_warning": ReasonText(
        code="check_warning",
        label="Output check warned",
        meaning="An output check other than Signal claims warned, and the action was kept",
        likely_cause=(
            "The answer is inside the rules but near an edge the check watches (the "
            "check's own text says which)"
        ),
        what_to_do=(
            "Read the check's line in the answer. Once: nothing. Often for one check: "
            "look at that rule on Insights before changing a threshold"
        ),
        links=(("Open the decision", LINK_DECISION), ("Insights", LINK_INSIGHTS)),
    ),
    "input_warning": ReasonText(
        code="input_warning",
        label="Input incomplete",
        meaning=(
            "The model answered with part of its input missing: a market-context field "
            "was missing or stale and sent as null"
        ),
        likely_cause="A collector source failed or answered late at that cycle",
        what_to_do=(
            "Once: nothing. Repeating for one source: that source is failing; Settings → "
            "API keys and data shows it and Logs has the error"
        ),
        links=(("API keys and data", LINK_KEYS), ("Logs for this call", LINK_LOGS)),
    ),
    "retried": ReasonText(
        code="retried",
        label="Retried",
        meaning="The first attempt failed and the one retry answered",
        likely_cause="A short failure at the provider or on the network",
        what_to_do="Nothing. Many in a day: look at the timeouts and errors of the same period",
        links=(("Calls with this reason", LINK_CALLS),),
    ),
    "slow": ReasonText(
        code="slow",
        label="Slow",
        meaning="The answer took longer than 15 s but arrived",
        likely_cause="The provider was busy, or the answer was long",
        what_to_do=(
            "Calls near the 30 s timeout risk failing. If they repeat, pick a faster model"
        ),
        links=(("Model and reviews", LINK_MODEL),),
    ),
    "number_check_failed": ReasonText(
        code="number_check_failed",
        label="Number check failed",
        meaning=(
            "A briefing, review or Ask answer contained a number that isn't in its facts. "
            'The plain template (or "no answer") was shown instead'
        ),
        likely_cause="The model rounded differently, computed a new number or invented one",
        what_to_do=(
            "Once: nothing. Often: try another model for that text. The facts and the text "
            "are both in this call"
        ),
        links=(("Model and reviews", LINK_MODEL),),
    ),
    "timeout": ReasonText(
        code="timeout",
        label="Timeout",
        meaning=(
            "No answer within timeouts.openai_s (30 s), and the retry timed out too. The "
            "Rules engine decided the pair for this cycle; nothing was traded on a guess"
        ),
        likely_cause="The provider was overloaded or the network failed",
        what_to_do=(
            "Once: nothing. Several cycles in a row: check the provider's status page; "
            "Logs shows whether the network or the provider fails"
        ),
        links=(("Logs for this call", LINK_LOGS),),
    ),
    "api_error": ReasonText(
        code="api_error",
        label="API error",
        meaning=(
            "The provider answered with an HTTP error, or the connection failed, also on the retry"
        ),
        likely_cause=(
            "401 or 403: the key is wrong, expired or lacks access. 404: the model name "
            "is unknown. 429: rate limit or spending limit. 500\u2013599: the provider's own "
            "failure. No status: network or DNS"
        ),
        what_to_do=(
            "401/403: replace the key in Settings → API keys and data. 404: pick a listed "
            "model. 429: raise the account's limits. 5xx: wait; check the status page. "
            "No status: check the computer's network"
        ),
        links=(("API keys and data", LINK_KEYS), ("Logs for this call", LINK_LOGS)),
    ),
    "invalid_output": ReasonText(
        code="invalid_output",
        label="Invalid output",
        meaning=(
            "The answer couldn't be used: not JSON or not the schema, also on the retry. "
            "Counted as HOLD"
        ),
        likely_cause="The model ignored the format, or its answer was cut off",
        what_to_do=(
            "Once: nothing. Often for one model: it doesn't follow the schema; pick another"
        ),
        links=(("Model and reviews", LINK_MODEL),),
    ),
    "input_blocked": ReasonText(
        code="input_blocked",
        label="Input blocked",
        meaning=(
            "An input check failed, so the brief was not sent and the cycle held this pair. "
            "The request that would have been sent is stored"
        ),
        likely_cause=(
            "Missing or out-of-range fields, stale candles, too little history, or another "
            "pair's prices in the brief"
        ),
        what_to_do=(
            "Open the failed input check. Stale or missing candles: Universe → Data sync "
            "shows what loaded. A cross-pair leak is a bug: report it with the call link"
        ),
        links=(("Universe → Data sync", LINK_UNIVERSE), ("Logs for this call", LINK_LOGS)),
    ),
}


def api_error_cause(http_status: int | None) -> tuple[str, str]:
    """The status-specific cause and what-to-do for ``api_error``."""
    if http_status in (401, 403):
        return (
            "The key is wrong, expired or lacks access",
            "Replace the key in Settings → API keys and data",
        )
    if http_status == 404:
        return ("The model name is unknown", "Pick a listed model in Settings → Model and reviews")
    if http_status == 429:
        return ("Rate limit or spending limit reached", "Raise the OpenAI account's limits")
    if http_status is not None and 500 <= http_status <= 599:
        return ("The provider's own failure", "Wait; check the provider's status page")
    return ("Network or DNS failure", "Check the computer's network; Logs names the failure")


def _loads_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def parse_reasons(raw: str | None) -> list[dict[str, str | None]]:
    """The stored reason JSON as dictionaries, tolerating older list-of-code rows."""
    out: list[dict[str, str | None]] = []
    for item in _loads_list(raw):
        if isinstance(item, str):
            out.append({"code": item, "detail": None})
        elif isinstance(item, dict) and isinstance(item.get("code"), str):
            detail = item.get("detail")
            out.append({"code": item["code"], "detail": str(detail) if detail else None})
    return out


def reason_codes(row: LlmCallRow) -> list[str]:
    return [str(r["code"]) for r in parse_reasons(row.reasons)]


def parsed_action(row: LlmCallRow) -> str | None:
    if not row.parsed:
        return None
    try:
        value = json.loads(row.parsed)
    except (TypeError, json.JSONDecodeError):
        return None
    action = value.get("action") if isinstance(value, dict) else None
    return action if action in ("BUY", "SELL", "HOLD", "SKIP") else None


def _checks(raw: str | None) -> list[dict[str, Any]]:
    return [c for c in _loads_list(raw) if isinstance(c, dict)]


def _attempts(row: LlmCallRow) -> list[dict[str, Any]]:
    return _checks(row.attempts)


def answering_attempt(row: LlmCallRow) -> dict[str, Any] | None:
    attempts = _attempts(row)
    return attempts[-1] if attempts else None


def http_status(row: LlmCallRow) -> int | None:
    attempt = answering_attempt(row)
    status = attempt.get("http_status") if attempt else None
    return int(status) if isinstance(status, int) else None


def _first_failed(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((c for c in checks if c.get("status") == "fail"), None)


def _warning_detail(check: dict[str, Any]) -> str:
    name = str(check.get("name") or "check")
    detail = str(check.get("detail") or "")
    return f"{name}: {detail}" if detail else name


def _input_warning_detail(check: dict[str, Any]) -> str:
    return _warning_detail(check)


def classify(
    row: LlmCallRow,
    final_action: str | None,
    slow_s: float,
    *,
    change_detail: str | None = None,
) -> tuple[OutcomeStatus, list[dict[str, str | None]]]:
    """Return ``(status, reasons)`` for one call, exactly as stored.

    ``final_action`` is the decision the engine recorded. ``change_detail``
    is the gate's skip reason or the failed post-check when the caller knows
    it; the migration derives it the same way.
    """
    if row.status in ("timeout", "api_error", "invalid_output", "input_blocked"):
        detail: str | None = None
        if row.status == "api_error":
            status = http_status(row)
            detail = f"HTTP {status}" if status is not None else "no HTTP status"
        elif row.status == "input_blocked":
            failed = _first_failed(_checks(row.input_checks))
            detail = _warning_detail(failed) if failed else None
        elif row.status == "invalid_output":
            failed = _first_failed(_checks(row.output_checks))
            detail = str(failed.get("name")) if failed else None
        return "error", [{"code": row.status, "detail": detail}]

    reasons: list[dict[str, str | None]] = []
    model_action = parsed_action(row)
    if model_action is not None and final_action is not None and model_action != final_action:
        detail = change_detail
        if detail is None:
            failed = _first_failed(_checks(row.output_checks))
            detail = str(failed.get("name")) if failed else None
        reasons.append({"code": "changed_by_code", "detail": detail})

    output_checks = _checks(row.output_checks)
    for check in output_checks:
        if check.get("status") != "warn":
            continue
        name = str(check.get("name") or "")
        if name == "Rules baseline":
            continue  # information, never a warning reason
        if name == "Signal claims":
            reasons.append(
                {"code": "claim_mismatch", "detail": str(check.get("detail") or "") or None}
            )
        else:
            reasons.append({"code": "check_warning", "detail": name})

    for check in _checks(row.input_checks):
        if check.get("status") == "warn":
            reasons.append({"code": "input_warning", "detail": _input_warning_detail(check)})

    attempts = _attempts(row)
    if len(attempts) > 1 and attempts[-1].get("outcome") == "sent":
        first_error = attempts[0].get("error")
        reasons.append(
            {
                "code": "retried",
                "detail": str(first_error) if first_error else "first attempt failed",
            }
        )

    answer = answering_attempt(row)
    latency = answer.get("latency_ms") if answer else row.latency_ms
    if isinstance(latency, int | float) and latency > slow_s * 1000:
        reasons.append({"code": "slow", "detail": f"{latency / 1000:.1f} s"})

    if row.feature in ("briefing", "review", "ask"):
        number_check = next(
            (
                c
                for c in output_checks
                if c.get("status") == "fail" and str(c.get("name", "")).startswith("Numbers match")
            ),
            None,
        )
        if number_check is not None:
            reasons.append(
                {
                    "code": "number_check_failed",
                    "detail": str(number_check.get("detail") or "") or None,
                }
            )

    return ("warning", reasons) if reasons else ("success", [])


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _has(row: LlmCallRow, code: str) -> bool:
    return code in reason_codes(row)


def _reason_detail(row: LlmCallRow, code: str) -> str | None:
    return next(
        (r.get("detail") for r in parse_reasons(row.reasons) if r.get("code") == code), None
    )


def _failure(row: LlmCallRow) -> bool:
    if _has(row, "timeout"):
        return True
    status = http_status(row)
    return _has(row, "api_error") and status is not None and status >= 500


def _provider_failure(row: LlmCallRow) -> bool:
    return _has(row, "timeout") or _has(row, "api_error")


def _decision_feature(feature: str) -> bool:
    return feature == "decision" or feature.startswith("shadow:")


def _cycle_streak(rows: list[LlmCallRow], current: LlmCallRow, matches: Any) -> int:
    """Consecutive earlier cycle numbers, ending at ``current.cycle``, with a match."""
    if current.cycle is None:
        return 0
    cycles = {
        row.cycle
        for row in rows
        if row.cycle is not None and row.cycle < (current.cycle or 0) and matches(row)
    }
    streak = 1
    cycle = current.cycle - 1
    while cycle in cycles:
        streak += 1
        cycle -= 1
    return streak


def _attention_match(
    session: Session,
    row: LlmCallRow,
    reasons: list[dict[str, str | None]],
    cfg: Any,
) -> tuple[Attention, str | None]:
    """The first matching attention rule and a short stored-data explanation."""
    codes = {str(r["code"]) for r in reasons}
    if not codes:
        return "none", None
    session.flush()  # rows written earlier in this transaction count as earlier rows
    at = _naive(row.at)
    earlier = list(
        session.scalars(
            select(LlmCallRow)
            .where(LlmCallRow.account_id == row.account_id, LlmCallRow.at <= at)
            .order_by(LlmCallRow.at.asc(), LlmCallRow.id.asc())
        ).all()
    )
    earlier = [item for item in earlier if item.id != row.id]
    peers = [item for item in earlier if item.feature == row.feature]
    status = http_status(row)

    # ---- needs you ------------------------------------------------------
    if "api_error" in codes and status in (401, 403, 404):
        return "act", f"HTTP {status} means the key or model access has to be fixed"
    if "api_error" in codes and status == 429:
        cutoff = at - timedelta(hours=24)
        count = 1 + sum(
            1
            for item in earlier
            if _naive(item.at) >= cutoff and _has(item, "api_error") and http_status(item) == 429
        )
        if count >= cfg.act_429_per_day:
            return "act", f"{count} HTTP 429 failures in 24 hours"
    if row.cycle is not None and _failure(row):
        streak = _cycle_streak(peers, row, _failure)
        if streak >= cfg.act_in_a_row_cycles:
            return "act", f"timeout or provider failure in {streak} cycles in a row"
    if row.cycle is not None and "input_blocked" in codes:
        streak = _cycle_streak(
            peers,
            row,
            lambda item: item.pair == row.pair and _has(item, "input_blocked"),
        )
        if streak >= cfg.act_in_a_row_cycles:
            return "act", f"input blocked for {row.pair} in {streak} cycles in a row"
    if row.cycle is not None and "input_warning" in codes:
        detail = next((r.get("detail") for r in reasons if r.get("code") == "input_warning"), None)
        streak = _cycle_streak(
            peers,
            row,
            lambda item: (
                _has(item, "input_warning") and _reason_detail(item, "input_warning") == detail
            ),
        )
        if streak >= cfg.act_in_a_row_cycles:
            return "act", f"the same input warning in {streak} cycles in a row"
    if "invalid_output" in codes:
        recent = list(
            session.scalars(
                select(LlmCallRow)
                .where(LlmCallRow.model == row.model, LlmCallRow.at < at)
                .order_by(LlmCallRow.at.desc(), LlmCallRow.id.desc())
                .limit(cfg.act_invalid_window - 1)
            ).all()
        )
        bad = 1 + sum(1 for item in recent if _has(item, "invalid_output"))
        if bad >= cfg.act_invalid_count:
            return (
                "act",
                f"{bad} unusable answers in {row.model}'s last {cfg.act_invalid_window} calls",
            )
    if "number_check_failed" in codes:
        recent = list(
            session.scalars(
                select(LlmCallRow)
                .where(LlmCallRow.feature == row.feature, LlmCallRow.at < at)
                .order_by(LlmCallRow.at.desc(), LlmCallRow.id.desc())
                .limit(cfg.act_number_check_window - 1)
            ).all()
        )
        bad = 1 + sum(1 for item in recent if _has(item, "number_check_failed"))
        if bad >= cfg.act_number_check_count:
            return (
                "act",
                (
                    f"{bad} of the last {cfg.act_number_check_window} "
                    f"{row.feature} texts failed the number check"
                ),
            )

    # ---- keep an eye on it ----------------------------------------------
    if row.cycle is not None and _provider_failure(row):
        near = next(
            (
                item
                for item in reversed(peers)
                if item.cycle is not None
                and item.cycle < row.cycle
                and row.cycle - item.cycle <= cfg.watch_within_cycles
                and _provider_failure(item)
            ),
            None,
        )
        if near is not None:
            when = f"cycle {near.cycle}"
            return (
                "watch",
                (
                    f"a second timeout or provider error within "
                    f"{cfg.watch_within_cycles} cycles ({when})"
                ),
            )
    if "slow" in codes:
        cutoff = at - timedelta(hours=24)
        count = 1 + sum(1 for item in earlier if _naive(item.at) >= cutoff and _has(item, "slow"))
        if count >= cfg.watch_slow_per_day:
            return "watch", f"{count} slow calls in 24 hours"
    claim_codes = {"claim_mismatch", "check_warning"}
    current_claim = next((r for r in reasons if r.get("code") in claim_codes), None)
    if current_claim is not None and _decision_feature(row.feature):
        recent = list(
            session.scalars(
                select(LlmCallRow)
                .where(LlmCallRow.feature == row.feature, LlmCallRow.at < at)
                .order_by(LlmCallRow.at.desc(), LlmCallRow.id.desc())
                .limit(cfg.watch_claim_window - 1)
            ).all()
        )
        code = str(current_claim["code"])
        detail = current_claim.get("detail") if code == "check_warning" else None

        def same_warning(item: LlmCallRow) -> bool:
            if code == "claim_mismatch":
                return _has(item, "claim_mismatch")
            return _has(item, "check_warning") and _reason_detail(item, "check_warning") == detail

        count = 1 + sum(1 for item in recent if same_warning(item))
        share = count / cfg.watch_claim_window * 100
        if share >= cfg.watch_claim_share_pct:
            return (
                "watch",
                (
                    f"{count} of the last {cfg.watch_claim_window} decision calls "
                    f"({share:.0f}%) have this warning"
                ),
            )
    return "none", None


def attention(
    session: Session,
    row: LlmCallRow,
    reasons: list[dict[str, str | None]],
    cfg: Any,
) -> Attention:
    """The stored attention level: ``none``, ``watch`` or ``act``."""
    return _attention_match(session, row, reasons, cfg)[0]


def attention_why(
    session: Session,
    row: LlmCallRow,
    reasons: list[dict[str, str | None]],
    cfg: Any,
) -> str | None:
    """The short stored-data explanation of the attention rule that fired."""
    return _attention_match(session, row, reasons, cfg)[1]


def stamp(
    session: Session,
    row: LlmCallRow,
    *,
    final_action: str | None,
    settings: Settings,
    change_detail: str | None = None,
) -> None:
    """Write status, reasons and attention on a call row in its transaction."""
    outcome, found = classify(
        row,
        final_action,
        float(settings.inspector.slow_call_seconds),
        change_detail=change_detail,
    )
    row.outcome_status = outcome
    row.reasons = json.dumps(found, separators=(",", ":"), ensure_ascii=False)
    row.attention = attention(session, row, found, settings.inspector.attention)


def why_here(reason: dict[str, str | None] | str, row: LlmCallRow) -> str:
    """One sentence from the row's own data for one reason."""
    if isinstance(reason, str):
        code, detail = reason, None
    else:
        code, detail = str(reason["code"]), reason.get("detail")
    attempts = _attempts(row)
    if code in ("timeout", "api_error"):
        parts: list[str] = []
        status = http_status(row)
        if status is not None:
            parts.append(f"HTTP {status}")
        errors = [str(a.get("error")) for a in attempts if a.get("error")]
        if errors:
            parts.append(errors[-1])
        times = [a.get("latency_ms") for a in attempts if isinstance(a.get("latency_ms"), int)]
        if times:
            parts.append(" + ".join(f"{int(t) / 1000:.1f} s" for t in times))
        return " · ".join(parts) or (
            str(detail) if detail else "The stored attempts record the failure"
        )
    if code == "slow":
        attempt = answering_attempt(row) or {}
        latency = attempt.get("latency_ms") or row.latency_ms
        return f"The answering attempt took {int(latency or 0) / 1000:.1f} s"
    if code == "retried" and attempts:
        first = attempts[0]
        return (
            f"The first attempt failed after {int(first.get('latency_ms') or 0) / 1000:.1f} s"
            + (f": {first.get('error')}" if first.get("error") else "")
            + "; the retry answered"
        )
    if code == "changed_by_code":
        model_action = parsed_action(row) or "the model's action"
        return f"{model_action} did not survive code's checks" + (f" — {detail}" if detail else "")
    if detail:
        return str(detail)
    check = next(
        (
            c
            for c in [*_checks(row.input_checks), *_checks(row.output_checks)]
            if c.get("status") in ("warn", "fail")
        ),
        None,
    )
    if check is not None:
        return _warning_detail(check)
    return "The stored checks and attempts record this reason"


def fold_key(row: LlmCallRow, final_action: str | None) -> str:
    """The opaque key consecutive list rows fold on."""
    kind = row.pair or row.feature
    codes = ",".join(reason_codes(row))
    return "|".join(
        [kind, final_action or parsed_action(row) or "", row.outcome_status or "success", codes]
    )


def result_sentence(row: LlmCallRow, final_action: str | None) -> str:
    """The one-sentence answer at the top of the details."""
    model_action = parsed_action(row)
    if row.outcome_status == "error":
        if row.outcome_status == "error" and row.status == "timeout":
            tries = len(_attempts(row)) or 2
            rules = (
                f" · Rules answered: {final_action or 'HOLD'}" if row.feature == "decision" else ""
            )
            return f"No answer · timed out {tries} times{rules}"
        if row.status == "api_error":
            return "No usable answer · the provider or network failed"
        if row.status == "invalid_output":
            return "No usable answer · invalid output counted as HOLD"
        return "No call sent · an input check blocked it"
    if model_action and final_action and model_action != final_action:
        return f"Valid answer · code changed {model_action} → {final_action}"
    return "Valid answer, used as the model chose"


def final_action_sentence(row: LlmCallRow, final_action: str | None) -> str:
    model_action = parsed_action(row)
    if model_action and final_action and model_action != final_action:
        detail = parse_reasons(row.reasons)
        gate = next((r.get("detail") for r in detail if r.get("code") == "changed_by_code"), None)
        return f"The model answered {model_action} → code made it {final_action}" + (
            f" ({gate})" if gate else ""
        )
    if model_action:
        return f"The model answered {model_action} → code kept it"
    return "No model action was recorded"
