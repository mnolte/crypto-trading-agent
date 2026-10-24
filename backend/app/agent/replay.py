"""Shared replay plumbing: resending a stored call under another prompt.

When the target prompt wants a newer brief version than the stored brief
carries, the brief is upgraded first — the v2 fields are recomputed from
the stored brief, the decisions table and the candles closed by the
brief's own ``now``, so a replay never sees the future. The request keeps
the stored shape with instructions, parameters, model and the output
schema swapped to the target prompt's version.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.agent.brief import BriefMeta, brief_json
from app.agent.brief_v2 import (
    brief_version_of,
    previous_decision_v2,
    upgrade_brief_to_v2,
)
from app.agent.deciders.openai_decider import check_output, parse_model_output
from app.agent.market.base import MarketInfo
from app.agent.openai_client import CallResult
from app.agent.prompts import LoadedPrompt
from app.db import LlmCallRow, utcnow
from app.models import CheckOut, Regime, Settings


def meta_from_brief(brief: dict[str, Any]) -> BriefMeta:
    """Rebuild the check meta a stored brief implies — replays don't have
    live candles, so every value comes from the stored JSON."""
    tf = brief.get("tf", {})
    atr_pct = None
    h1 = tf.get("1h", {})
    if isinstance(h1.get("atr_pct"), dict):
        atr_pct = h1["atr_pct"].get("value")
    price = float(brief.get("price") or 0)
    atr_1h = (atr_pct * price / 100) if atr_pct and price else None
    data_age = brief.get("data_age_s", {})
    limits = brief.get("limits", {}).get("max_age_s", {})
    regime_data = brief.get("regime", {})
    neutral = Regime(trend="sideways", volatility="normal", age_4h=0)
    return BriefMeta(
        pair=brief.get("pair", ""),
        now=datetime.fromisoformat(brief["now"]) if brief.get("now") else utcnow(),
        price=price,
        atr_1h=atr_1h,
        holding=brief.get("position") is not None,
        regime=Regime.model_validate(regime_data["pair"]) if regime_data.get("pair") else neutral,
        btc_regime=Regime.model_validate(regime_data["btc"]) if regime_data.get("btc") else None,
        bars={},
        data_age_s={k: float(v) for k, v in data_age.items()},
        stale_tfs=[tf_ for tf_, age in data_age.items() if age > limits.get(tf_, 0)],
        support=[lvl["price"] for lvl in brief.get("structure", {}).get("support", [])],
        resistance=[lvl["price"] for lvl in brief.get("structure", {}).get("resistance", [])],
    )


def replay_request(
    session: Session,
    orig: LlmCallRow,
    prompt: LoadedPrompt,
    model: str | None,
    *,
    settings: Settings,
    market_info: MarketInfo | None = None,
) -> tuple[dict[str, Any], dict[str, Any], BriefMeta, bool]:
    """The request for a replay: the stored body with the target prompt's
    instructions, parameters and schema — and the brief upgraded to the
    prompt's brief version. Returns (payload, brief, meta, upgraded)."""
    orig_request = json.loads(orig.request_json or "{}")
    input_data = orig_request.get("input")
    brief: dict[str, Any] = json.loads(input_data) if isinstance(input_data, str) else {}
    upgraded = False
    if brief and prompt.brief_version > brief_version_of(brief):
        brief = upgrade_brief_to_v2(
            session,
            brief,
            account_id=orig.account_id,
            settings=settings,
            market_info=market_info,
        )
        upgraded = True

    payload = {
        **orig_request,
        "model": model or orig.model,
        "instructions": prompt.body,
        "temperature": prompt.temperature,
        "max_output_tokens": prompt.max_output_tokens,
    }
    if brief:
        payload["input"] = brief_json(brief)
    fmt = (payload.get("text") or {}).get("format") or {}
    if fmt.get("type") == "json_schema" and fmt.get("name") == "decision":
        from app.agent.deciders.openai_decider import decision_schema

        payload["text"]["format"]["schema"] = decision_schema(prompt.brief_version)

    meta = meta_from_brief(brief)
    if market_info is not None and market_info.tick_size:
        meta.tick = float(market_info.tick_size)
    meta.prev = previous_decision_v2(
        session, orig.account_id, meta.pair, before=meta.now, tick=meta.tick
    )
    return payload, brief, meta, upgraded


def evaluate_replay(
    result: CallResult,
    brief: dict[str, Any],
    meta: BriefMeta,
    settings: Settings,
    brief_version: int,
) -> tuple[list[CheckOut], str]:
    """The output checks a replay gets: schema parse plus post-checks. The
    Rules baseline is not re-evaluated (its inputs aren't in the stored
    request)."""
    out, parse_error = parse_model_output(result.output_text, brief_version)
    verdict = check_output(
        out, parse_error, brief, meta, "?", settings, brief_version=brief_version
    )
    for i, check in enumerate(verdict.checks):
        if check.name == "Rules baseline":
            verdict.checks[i] = CheckOut(
                name="Rules baseline",
                status="pass",
                detail="not evaluated for a replay",
            )
    status = result.status if out is not None or result.status != "ok" else "invalid_output"
    return verdict.checks, status


def replay_row(
    orig: LlmCallRow,
    prompt: LoadedPrompt,
    model: str,
    result: CallResult,
    payload: dict[str, Any],
    out_checks: list[CheckOut],
    status: str,
    *,
    now: Any,
) -> LlmCallRow:
    """The llm_calls row a replay produces — feature "replay", linked to
    the original call."""
    import uuid

    return LlmCallRow(
        id=uuid.uuid4().hex[:16],
        at=now,
        cycle=None,
        account_id=orig.account_id,
        pair=orig.pair,
        decision_id=None,
        feature="replay",
        replay_of=orig.id,
        model=model,
        prompt_version=prompt.version,
        status=status,
        attempts=json.dumps(
            [
                {
                    "at": a.at.isoformat(),
                    "outcome": a.outcome,
                    "error": a.error,
                    "http_status": a.http_status,
                    "latency_ms": a.latency_ms,
                }
                for a in result.attempts
            ]
        ),
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=None,
        cost_eur=None,
        request_id=result.request_id,
        request_json=(
            result.request_bytes.decode("utf-8", errors="replace")
            if result.request_bytes
            else json.dumps(payload)
        ),
        response_raw=(
            result.response_bytes.decode("utf-8", errors="replace")
            if result.response_bytes is not None
            else None
        ),
        parsed=result.output_text,
        input_checks=orig.input_checks,
        output_checks=json.dumps([c.model_dump() for c in out_checks]),
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )


def upgraded_marker(orig_brief: dict[str, Any], replay_brief: dict[str, Any]) -> str | None:
    """ "v1 → v2" when the replay's brief is a newer version than the
    original call's."""
    have, want = brief_version_of(orig_brief), brief_version_of(replay_brief)
    return f"v{have} → v{want}" if want > have else None
