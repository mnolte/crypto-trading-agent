"""The OpenAI decision layer.

``decide`` runs the Rules pre-filter first: no call is made while the
filter sees nothing and no position is open (the decision row keeps
``prefilter=False`` so the inspector can show the skip). Rules also decide
when no API key is set, the daily budget is spent, the monthly cost cap is
reached, or the model call fails.

A call that is made is recorded in ``llm_calls`` before the result is
known: the exact request bytes, every attempt, the raw response, token
counts, the cost estimate and the post-check results. Every failure ends
as HOLD — never a trade — with the named check in the decision's error;
only an R/R below the minimum travels on to the risk gates, where it
becomes SKIP.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent import budget
from app.agent.brief import (
    MAX_ENTRY_DISTANCE_PCT,
    TF_FIELDS,
    TF_FIELDS_1D_EXTRA,
    BriefInputs,
    BriefMeta,
    brief_json,
    build_brief,
    input_checks,
    pair_history,
)
from app.agent.brief_v2 import BRIEF_VERSION, previous_decision_v2
from app.agent.broker.paper import PaperBroker
from app.agent.deciders.base import DecideContext, Draft, RiskView
from app.agent.deciders.rules import RulesDecider
from app.agent.market.base import MarketClient, MarketDataError, MarketInfo, drop_forming
from app.agent.openai_client import CallResult, OpenAIResponsesClient
from app.agent.prompts import LoadedPrompt, PromptNotFoundError, load_prompt
from app.agent.signals import aligned_correlation
from app.config import get_secret
from app.db import AMSTERDAM, LlmCallRow, utcnow
from app.models import (
    CheckOut,
    Settings,
    Signal,
    SignalRead,
    Tf,
)

CORR_BARS = 720
CONTEXT_SPECS: dict[str, tuple[str, Tf, int]] = {
    "btc_1h": ("BTC-EUR", "1h", 800),
    "btc_4h": ("BTC-EUR", "4h", 300),
    "btc_1d": ("BTC-EUR", "1d", 300),
    "usdc_1h": ("USDC-EUR", "1h", 48),
}

# Signal names the model cites, normalised (lowercase, a-z and 0-9 only),
# mapped to the brief's canonical keys. A cited name is matched against the
# brief's own keys first ("vwap_dist" -> "vwapdist"); this table covers the
# names that don't normalise to a key, like "VWAP Distance".
SIGNAL_ALIASES: dict[str, str] = {
    "rsi": "rsi",
    "rsi14": "rsi",
    "ema20vs50": "ema20_vs_50",
    "ema2050": "ema20_vs_50",
    "emacross": "ema20_vs_50",
    "ema20ema50": "ema20_vs_50",
    "emas": "ema20_vs_50",
    "vwapdist": "vwap_dist",
    "vwapdistance": "vwap_dist",
    "vwap": "vwap_dist",
    "vsvwap": "vwap_dist",
    "pricevsvwap": "vwap_dist",
    "obvtrend": "obv_trend",
    "obv": "obv_trend",
    "adx": "adx",
    "adx14": "adx",
    "atrpct": "atr_pct",
    "atr": "atr_pct",
    "atrpercent": "atr_pct",
    "atrrank": "atr_rank",
    "atrpercentile": "atr_rank",
    "volxavg": "vol_x_avg",
    "volume": "vol_x_avg",
    "volumexavg": "vol_x_avg",
    "volumevsaverage": "vol_x_avg",
    "relvolume": "vol_x_avg",
    "ema200dist": "ema200_dist",
    "ema200": "ema200_dist",
    "vsema200": "ema200_dist",
    "pricevsema200": "ema200_dist",
    # market_context keys (timeframe-free)
    "btctrend": "btc_trend_4h",
    "btc4htrend": "btc_trend_4h",
    "btc1dtrend": "btc_trend_1d",
    "btcdailytrend": "btc_trend_1d",
    "ethbtc": "eth_btc_trend",
}

# Keys that live per timeframe; ema200_dist only exists on 1d.
_TF_SIGNAL_KEYS = set(TF_FIELDS) | set(TF_FIELDS_1D_EXTRA)


def _norm_signal_name(name: str) -> str:
    """Lowercase, keep a-z and 0-9: 'VWAP Distance' -> 'vwapdistance'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def signal_claims_check(
    signals: Sequence[ModelSignal | ModelSignalV2], brief: dict[str, Any]
) -> CheckOut:
    """Compare each cited signal with the brief's read on its own tf.

    A cited name is normalised, then matched against the brief's canonical
    keys and the alias table. A match on a timeframe the brief doesn't
    carry (e.g. ema200_dist on 1h) warns "no such value"; a name that
    matches nothing warns "not in brief". A matching name with a
    contradicting read is still a warn — aliases never turn a
    contradiction into a pass.
    """
    tf_blocks = brief.get("tf", {})
    ctx_block = brief.get("market_context", {})
    canonical: dict[str, str] = {}
    for block in tf_blocks.values():
        if isinstance(block, dict):
            for key in block:
                canonical.setdefault(_norm_signal_name(key), key)
    for key in ctx_block:
        canonical.setdefault(_norm_signal_name(key), key)

    problems: list[str] = []
    for sig in signals:
        norm = _norm_signal_name(sig.name)
        field = canonical.get(norm) or SIGNAL_ALIASES.get(norm)
        label = f"{sig.name} · {sig.tf}"
        if field is None:
            problems.append(f"{label}: not in brief")
            continue
        block = tf_blocks.get(sig.tf)
        item = block.get(field) if isinstance(block, dict) else None
        if not isinstance(item, dict) or item.get("read") is None:
            item = ctx_block.get(field)
        brief_read = item.get("read") if isinstance(item, dict) else None
        if brief_read is None:
            if field in _TF_SIGNAL_KEYS:
                problems.append(f"{label}: no such value on {sig.tf}")
            else:
                problems.append(f"{label}: not in brief")
        elif brief_read != sig.read:
            problems.append(f"{label}: model says {sig.read}, brief says {brief_read}")
    detail = "; ".join(dict.fromkeys(problems))
    return CheckOut(
        name="Signal claims",
        status="warn" if detail else "pass",
        detail=detail or "every claim matches the brief",
    )


class ModelSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tf: Tf
    read: SignalRead


class _DecisionBase(BaseModel):
    """The fields every decision output shares; each brief version pins its
    own ``signals`` element type on top."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0, le=1)
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    reasoning: str = Field(default="", max_length=600)


class ModelDecision(_DecisionBase):
    """The model's output, schema-checked. Levels are nullable; the
    post-checks decide whether each may be empty."""

    signals: list[ModelSignal] = Field(default_factory=list)
    invalidation: str | None = None
    invalidation_price: float | None = None


# Signal names v2 allows — exactly the brief's per-timeframe indicator
# keys (ema200_dist exists on 1d only).
SignalName = Literal[
    "rsi",
    "ema20_vs_50",
    "vwap_dist",
    "obv_trend",
    "adx",
    "atr_pct",
    "atr_rank",
    "vol_x_avg",
    "ema200_dist",
]

StopBasis = Literal["S1", "S2", "S3", "S4", "S5"]
TargetBasis = Literal["R1", "R2", "R3", "R4", "R5"]


class ModelSignalV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SignalName
    tf: Tf
    read: SignalRead


class ModelDecisionV2(_DecisionBase):
    """The v2 output: v1 plus the basis ids of the chosen stop and target
    candidates, and signal names constrained to the brief's keys."""

    signals: list[ModelSignalV2] = Field(default_factory=list)
    invalidation: str | None = None
    invalidation_price: float | None = None
    stop_basis: StopBasis | None = None
    target_basis: TargetBasis | None = None


AnyDecision = ModelDecision | ModelDecisionV2


def decision_schema(brief_version: int = 1) -> dict[str, Any]:
    """The structured-output schema sent with every call: the Pydantic
    schema, normalised for OpenAI strict mode (every property required,
    nullables via anyOf)."""
    model = ModelDecisionV2 if brief_version >= BRIEF_VERSION else ModelDecision
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema["properties"].keys())
    for item in schema.get("$defs", {}).values():
        item["additionalProperties"] = False
        item["required"] = list(item.get("properties", {}).keys())
    return schema


def parse_model_output(
    text: str | None, brief_version: int = 1
) -> tuple[AnyDecision | None, str | None]:
    """Parse and schema-check the model's text. Returns the decision or the
    reason it failed."""
    if not text:
        return None, "empty output"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    model = ModelDecisionV2 if brief_version >= BRIEF_VERSION else ModelDecision
    try:
        return model.model_validate(data), None
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return None, f"schema violation: {loc}: {first['msg']}"


@dataclass
class OutputVerdict:
    checks: list[CheckOut]
    action: str  # the action the draft takes after checks
    confidence: float
    invalidation_price: Decimal | None
    failed: str | None  # "check: detail" of the first failed check


def check_output(
    out: AnyDecision | None,
    parse_error: str | None,
    brief: dict[str, Any],
    meta: BriefMeta,
    rules_action: str,
    settings: Settings,
    brief_version: int = 1,
) -> OutputVerdict:
    """The post-checks every model answer goes through. A fail resolves the
    decision to HOLD; warnings adjust (drop a bad invalidation price, cap
    confidence) and are recorded."""
    checks: list[CheckOut] = []
    v2 = brief_version >= BRIEF_VERSION
    tm = brief.get("trade_math") if isinstance(brief.get("trade_math"), dict) else None

    if out is None:
        checks.append(
            CheckOut(name="Schema valid", status="fail", detail=parse_error or "no output")
        )
        return OutputVerdict(checks, "HOLD", 0.0, None, f"{checks[-1].name}: {checks[-1].detail}")
    checks.append(CheckOut(name="Schema valid", status="pass", detail="valid decision JSON"))

    # ---- action allowed ------------------------------------------------
    action_fails: list[str] = []
    if out.action == "BUY" and meta.holding:
        action_fails.append("BUY while a position is open")
    if out.action == "SELL" and not meta.holding:
        action_fails.append("SELL with no open position")
    checks.append(
        CheckOut(
            name="Action allowed",
            status="fail" if action_fails else "pass",
            detail="; ".join(action_fails) or f"{out.action} fits the position state",
        )
    )

    # ---- levels sane ---------------------------------------------------
    level_fails: list[str] = []
    level_warns: list[str] = []
    kept_inv: Decimal | None = None

    def _finite(v: float | None) -> bool:
        return v is not None and math.isfinite(v)

    if out.action == "BUY":
        # v2 moves the missing-invalidation case to a warn ("Invalidation"),
        # so Levels sane only demands the three prices there.
        needed = [_finite(out.entry), _finite(out.stop), _finite(out.target)]
        if not v2:
            needed.append(_finite(out.invalidation_price))
        if not all(needed):
            level_fails.append(
                "BUY needs entry, stop and target" + ("" if v2 else " and an invalidation price")
            )
        else:
            assert out.entry is not None and out.stop is not None and out.target is not None
            if not (out.stop < out.entry < out.target):
                level_fails.append(
                    f"levels out of order: stop {out.stop}, entry {out.entry}, target {out.target}"
                )
            if meta.price > 0:
                dist_pct = abs(out.entry - meta.price) / meta.price * 100
                if dist_pct > MAX_ENTRY_DISTANCE_PCT:
                    level_fails.append(
                        f"entry {dist_pct:.2f}% from the current price "
                        f"(max {MAX_ENTRY_DISTANCE_PCT}%)"
                    )
            atr = meta.atr_1h
            if atr is not None and atr > 0:
                dist_atr = (out.entry - out.stop) / atr
                lo, hi = float(settings.risk.stop_min_atr), float(settings.risk.stop_max_atr)
                if dist_atr < lo or dist_atr > hi:
                    level_fails.append(
                        f"stop {dist_atr:.1f} x ATR(1h) from entry (allowed {lo}-{hi})"
                    )
            if out.invalidation_price is None:
                pass  # v2: missing invalidation warns under "Invalidation"
            elif out.stop < out.invalidation_price < out.entry:
                kept_inv = Decimal(str(out.invalidation_price))
            else:
                level_warns.append("invalidation price outside stop..entry; dropped")
    else:
        levels_set = (
            ("entry", out.entry),
            ("stop", out.stop),
            ("target", out.target),
            ("stop_basis", getattr(out, "stop_basis", None)),
            ("target_basis", getattr(out, "target_basis", None)),
        )
        for name, value in levels_set:
            if value is not None:
                level_warns.append(f"{name} set on {out.action}; ignored")
        if out.invalidation_price is not None:
            if meta.holding and out.invalidation_price < meta.price and out.invalidation_price > 0:
                kept_inv = Decimal(str(out.invalidation_price))
            else:
                level_warns.append("invalidation price not below the current price; dropped")
    checks.append(
        CheckOut(
            name="Levels sane",
            status="fail" if level_fails else ("warn" if level_warns else "pass"),
            detail="; ".join(level_fails + level_warns) or "levels consistent",
        )
    )

    # ---- trade math (v2): a BUY must be feasible and pinned to a listed pair
    if v2 and out.action == "BUY":
        tm_fails: list[str] = []
        if tm is None:
            checks.append(
                CheckOut(
                    name="Trade math",
                    status="warn",
                    detail="brief has no trade_math; BUY not verified",
                )
            )
        elif tm.get("buy_feasible") is not True:
            tm_fails.append(f"trade_math.buy_feasible is false: {tm.get('infeasible_reason')}")
        else:
            basis = (
                getattr(out, "stop_basis", None),
                getattr(out, "target_basis", None),
            )
            pairs = {(p.get("stop_id"), p.get("target_id")) for p in tm.get("pairs") or []}
            if basis not in pairs:
                tm_fails.append(f"({basis[0]}, {basis[1]}) is not a listed trade_math pair")
            else:
                cand = {
                    c.get("id"): c.get("price")
                    for c in (tm.get("stop_candidates") or []) + (tm.get("target_candidates") or [])
                }
                tick = meta.tick or 0.0
                for name, level, want in (
                    ("stop", out.stop, cand.get(basis[0])),
                    ("target", out.target, cand.get(basis[1])),
                ):
                    if level is None or want is None:
                        continue
                    if abs(level - want) > max(tick, abs(want) * 1e-9):
                        which = basis[0] if name == "stop" else basis[1]
                        tm_fails.append(
                            f"{name} {level} differs from {which} price {want} "
                            "by more than one tick"
                        )
        checks.append(
            CheckOut(
                name="Trade math",
                status="fail" if tm_fails else "pass",
                detail="; ".join(tm_fails) or "BUY inside trade_math",
            )
        )

    # ---- R/R recomputed ------------------------------------------------
    rr_fail_capable = (
        out.action == "BUY"
        and _finite(out.entry)
        and _finite(out.stop)
        and _finite(out.target)
        and out.entry is not None
        and out.stop is not None
        and out.target is not None
        and out.entry > out.stop
    )
    if rr_fail_capable:
        assert out.entry is not None and out.stop is not None and out.target is not None
        rr = (out.target - out.entry) / (out.entry - out.stop)
        min_rr = float(settings.risk.min_rr)
        below = rr < min_rr
        checks.append(
            CheckOut(
                name="R/R recomputed",
                status="warn" if below else "pass",
                detail=f"R/R {rr:.2f} (min {min_rr})"
                + ("; the risk gate will SKIP" if below else ""),
            )
        )
    else:
        checks.append(
            CheckOut(
                name="R/R recomputed",
                status="pass",
                detail="not applicable" if out.action != "BUY" else "levels incomplete",
            )
        )

    # ---- signal claims ---------------------------------------------------
    checks.append(signal_claims_check(out.signals, brief))

    # ---- stale-data rule ------------------------------------------------
    if meta.stale_tfs:
        stale_ok = out.action == "HOLD"
        checks.append(
            CheckOut(
                name="Stale-data rule",
                status="pass" if stale_ok else "fail",
                detail=f"data stale for {', '.join(meta.stale_tfs)}; "
                + ("model held" if stale_ok else f"model answered {out.action}"),
            )
        )
    elif re.search(r"\bstale\b", out.reasoning, re.IGNORECASE):
        # Freshness is an input check — a model that sees a brief already
        # passed it, so a stale claim in reasoning is always a misread.
        checks.append(
            CheckOut(
                name="Stale-data rule",
                status="warn",
                detail="reasoning cites stale data, but every timeframe is fresh",
            )
        )
    else:
        checks.append(
            CheckOut(name="Stale-data rule", status="pass", detail="all timeframes fresh")
        )

    # ---- regime rule -----------------------------------------------------
    regime_fails: list[str] = []
    regime_warns: list[str] = []
    confidence = out.confidence
    reg = meta.regime
    if out.action == "BUY":
        if reg.trend == "down":
            if meta.pair == "BTC-EUR":
                if out.confidence > 0.6:
                    confidence = min(confidence, 0.6)
                    regime_warns.append("confidence capped at 0.6 (BTC BUY in a down regime)")
            else:
                regime_fails.append("BUY in a down regime")
        elif reg.trend == "sideways":
            atr = meta.atr_1h
            if atr is None or atr <= 0 or out.entry is None:
                regime_warns.append("ATR(1h) or entry unknown; sideways rules not checked")
            else:
                supports_below = [s for s in meta.support if s < out.entry]
                if not supports_below:
                    regime_fails.append("no support below the entry in a sideways regime")
                elif out.entry - max(supports_below) > 1.0 * atr:
                    regime_fails.append("entry more than 1 x ATR(1h) above the nearest support")
                above = list(meta.resistance)
                if above and out.target is not None and out.target > min(above):
                    regime_fails.append("target above the nearest resistance")
        if reg.volatility == "high" and out.confidence > 0.7:
            confidence = min(confidence, 0.7)
            regime_warns.append("confidence capped at 0.7 (high volatility)")
    checks.append(
        CheckOut(
            name="Regime rule",
            status="fail" if regime_fails else ("warn" if regime_warns else "pass"),
            detail="; ".join(regime_fails + regime_warns) or f"regime {reg.trend} respected",
        )
    )

    # ---- invalidation required (v2): BUY, SELL and HOLD-while-holding
    # must name what would prove them wrong, with its price.
    if v2:
        inv_warns: list[str] = []
        needs_inv = out.action in ("BUY", "SELL") or (out.action == "HOLD" and meta.holding)
        if needs_inv:
            if not out.invalidation:
                inv_warns.append("no invalidation text")
            if out.invalidation_price is None:
                inv_warns.append("no invalidation_price")
        checks.append(
            CheckOut(
                name="Invalidation",
                status="warn" if inv_warns else "pass",
                detail="; ".join(inv_warns) or "invalidation present",
            )
        )

    # ---- repeated rejected BUY (v2): same basis as the previous skipped
    # BUY while its failed gates are unchanged — the gates skip it anyway.
    if v2 and out.action == "BUY":
        prev = meta.prev
        brief_prev = brief.get("previous_decision") or {}
        brief_gates = {g.get("name") for g in brief_prev.get("failed_gates") or []}
        repeated = (
            prev is not None
            and prev.outcome == "skipped"
            and prev.buy_basis[0] is not None
            and prev.buy_basis
            == (getattr(out, "stop_basis", None), getattr(out, "target_basis", None))
            and brief_gates
            and brief_gates == set(prev.failed_gate_names)
        )
        checks.append(
            CheckOut(
                name="Repeated rejected BUY",
                status="warn" if repeated else "pass",
                detail=(
                    "same basis as the skipped previous BUY; gates will reject it again"
                    if repeated
                    else "not a repeat of the previous skipped BUY"
                ),
            )
        )

    # ---- rules baseline -------------------------------------------------
    agrees = rules_action == out.action
    checks.append(
        CheckOut(
            name="Rules baseline",
            status="pass" if agrees else "warn",
            detail=(
                f"agrees ({out.action})"
                if agrees
                else f"model {out.action} vs Rules {rules_action}"
            ),
        )
    )

    failed = next((f"{c.name}: {c.detail}" for c in checks if c.status == "fail"), None)
    return OutputVerdict(
        checks=checks,
        action="HOLD" if failed else out.action,
        confidence=confidence,
        invalidation_price=None if failed else kept_inv,
        failed=failed,
    )


def _checks_json(checks: list[CheckOut]) -> str:
    return json.dumps([c.model_dump() for c in checks])


def _attempts_json(result: CallResult) -> str:
    return json.dumps(
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
    )


class OpenAIDecider:
    """The model-backed decider: pre-filter, budgets, brief, call, checks."""

    name = "openai"

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        market_client: MarketClient,
        broker: Callable[[], PaperBroker],
        risk_view: Callable[[Session], RiskView],
        clock: Callable[[], datetime] = utcnow,
        key: Callable[[], str | None] | None = None,
        client_factory: Callable[[str], OpenAIResponsesClient] = OpenAIResponsesClient,
        prices: budget.ModelPrices | None = None,
        rules: RulesDecider | None = None,
        notify: Callable[[str, str, str], Awaitable[None]] | None = None,
        feature: str = "decision",
        market_rules: dict[str, MarketInfo] | None = None,
    ) -> None:
        self.feature = feature
        self._sessions = session_factory
        self.client = market_client
        self._market_rules = market_rules if market_rules is not None else {}
        self._broker = broker
        self._risk_view = risk_view
        self.clock = clock
        self._key = key or (lambda: get_secret("openai"))
        self._client_factory = client_factory
        self._prices = prices or budget.ModelPrices()
        self.rules = rules or RulesDecider()
        self._notify = notify
        self._alerted: set[str] = set()
        self._model_failures = 0

    async def decide(self, ctx: DecideContext) -> Draft:
        settings = ctx.settings or Settings()
        rules_draft = await self.rules.decide(ctx)
        want_call = ctx.holding or not settings.model.rules_prefilter or rules_draft.prefilter
        api_key = self._key()
        # A paused pair can't enter; while flat there's nothing to decide,
        # so the call is skipped and Rules answers. Holding pairs still
        # reach the model — SELL decisions keep working during a pause.
        if not want_call or not api_key or (ctx.entries_paused and not ctx.holding):
            return rules_draft

        session = self._sessions()
        try:
            broker = self._broker()
            now = ctx.now
            account_id = broker.account_id

            used = budget.calls_today(session, account_id, now)
            if used >= settings.model.daily_call_budget:
                await self._alert_once(
                    session,
                    now,
                    "budget",
                    f"Model call budget for today used "
                    f"({used}/{settings.model.daily_call_budget}); the Rules "
                    "baseline decides until midnight.",
                )
                return rules_draft
            spent = budget.month_spend_all(session, now)
            cap = budget.cap_state(spent, settings.model.monthly_cost_cap_eur)
            if cap == "cap":
                await self._alert_once(
                    session,
                    now,
                    "cap",
                    f"Model cost cap reached: EUR {spent} of "
                    f"{settings.model.monthly_cost_cap_eur} this month. Decisions "
                    "use the Rules baseline until the 1st.",
                )
                return rules_draft
            if cap == "warn":
                await self._alert_once(
                    session,
                    now,
                    "cap80",
                    f"Model spend passed 80% of the monthly cap (EUR {spent} of "
                    f"{settings.model.monthly_cost_cap_eur}); replays pause.",
                )

            try:
                prompt = load_prompt(settings.model.prompt_version)
            except PromptNotFoundError:
                repo.add_activity(
                    session,
                    "error",
                    f"prompt {settings.model.prompt_version} not found; Rules "
                    f"decided for {ctx.pair}",
                    pair=ctx.pair,
                    at=now,
                )
                session.commit()
                return rules_draft

            model = settings.model.model_id
            brief, meta, payload = await self._prepare(session, ctx, settings, broker, prompt)
            in_checks = input_checks(brief, meta)
            row = LlmCallRow(
                id=uuid.uuid4().hex[:16],
                at=now,
                cycle=ctx.cycle,
                account_id=account_id,
                pair=ctx.pair,
                decision_id=None,
                feature=self.feature,
                replay_of=None,
                model=model,
                prompt_version=prompt.version,
                status="ok",
                attempts="[]",
                latency_ms=None,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                cost_eur=None,
                request_id=None,
                request_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                response_raw=None,
                parsed=None,
                input_checks=_checks_json(in_checks),
                output_checks="[]",
                verdict=None,
                verdict_note=None,
                verdict_at=None,
            )
            input_fails = [c for c in in_checks if c.status == "fail"]
            if input_fails:
                row.status = "input_blocked"
                repo.add_llm_call(session, row)
                session.commit()
                first = input_fails[0]
                return Draft(
                    action="HOLD",
                    confidence=0.0,
                    reasoning=f"Model call blocked — {first.name}: {first.detail}",
                    signals=[],
                    decider="openai",
                    model=model,
                    prompt_version=prompt.version,
                    llm_call_id=row.id,
                    raw_brief=row.request_json,
                    error=f"{first.name}: {first.detail}",
                    prefilter=True,
                )

            client = self._client_factory(api_key)
            try:
                result = await client.call(payload)
            finally:
                await client.aclose()

            if result.request_bytes:
                row.request_json = result.request_bytes.decode("utf-8", errors="replace")
            if result.response_bytes is not None:
                row.response_raw = result.response_bytes.decode("utf-8", errors="replace")
            row.attempts = _attempts_json(result)
            row.latency_ms = result.latency_ms
            row.input_tokens = result.input_tokens
            row.output_tokens = result.output_tokens
            row.request_id = result.request_id
            row.status = result.status
            row.parsed = result.output_text
            usd, eur = budget.cost_estimates(
                self._prices,
                model,
                result.input_tokens,
                result.output_tokens,
                budget.usdc_eur_rate(session, now),
            )
            row.cost_usd = str(usd) if usd is not None else None
            row.cost_eur = str(eur) if eur is not None else None

            if result.status != "ok":
                await self._track_model_result(result.status, result.error or result.status)
                repo.add_llm_call(session, row)
                repo.add_activity(
                    session,
                    "error",
                    f"model call failed for {ctx.pair}: {result.error or result.status}",
                    pair=ctx.pair,
                    at=now,
                )
                session.commit()
                rules_draft.reasoning = (
                    f"OpenAI call failed ({result.error or result.status}); fell back "
                    f"to Rules. {rules_draft.reasoning}"
                )
                rules_draft.llm_call_id = row.id
                rules_draft.raw_brief = row.request_json
                rules_draft.raw_response = row.response_raw
                return rules_draft

            out, parse_error = parse_model_output(result.output_text, prompt.brief_version)
            if out is None:
                row.status = "invalid_output"
            await self._track_model_result(row.status, parse_error or row.status)
            verdict = check_output(
                out,
                parse_error,
                brief,
                meta,
                rules_draft.action,
                settings,
                brief_version=prompt.brief_version,
            )
            row.output_checks = _checks_json(verdict.checks)
            repo.add_llm_call(session, row)
            session.commit()
            draft = self._draft(out, verdict, model, prompt.version)
            draft.llm_call_id = row.id
            draft.raw_brief = row.request_json
            draft.raw_response = row.response_raw
            return draft
        finally:
            session.close()

    # ------------------------------------------------------------- helpers --

    def _draft(
        self,
        out: AnyDecision | None,
        verdict: OutputVerdict,
        model: str,
        prompt_version: str,
    ) -> Draft:
        signals = [Signal(name=s.name, tf=s.tf, read=s.read) for s in out.signals] if out else []
        base = Draft(
            action=verdict.action,
            confidence=verdict.confidence if out else 0.0,
            reasoning=out.reasoning if out else "",
            signals=signals,
            invalidation=out.invalidation if out else None,
            decider="openai",
            model=model,
            prompt_version=prompt_version,
            error=verdict.failed,
            prefilter=True,
        )
        if out is not None and verdict.action == "BUY":
            base.invalidation_price = verdict.invalidation_price
            base.entry = Decimal(str(out.entry)) if out.entry is not None else None
            base.stop = Decimal(str(out.stop)) if out.stop is not None else None
            base.target = Decimal(str(out.target)) if out.target is not None else None
        elif verdict.action != "BUY":
            base.invalidation_price = verdict.invalidation_price
        return base

    def _payload(
        self, settings: Settings, prompt: LoadedPrompt, brief: dict[str, Any]
    ) -> dict[str, Any]:
        """The Responses API request: model, system prompt, the one-pair
        brief and the strict output schema, with the prompt version's own
        parameters."""
        return {
            "model": settings.model.model_id,
            "instructions": prompt.body,
            "input": brief_json(brief),
            "temperature": prompt.temperature,
            "max_output_tokens": prompt.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "decision",
                    "strict": True,
                    "schema": decision_schema(prompt.brief_version),
                }
            },
        }

    async def _prepare(
        self,
        session: Session,
        ctx: DecideContext,
        settings: Settings,
        broker: PaperBroker,
        prompt: LoadedPrompt,
    ) -> tuple[dict[str, Any], BriefMeta, dict[str, Any]]:
        """Gather the brief inputs: stored candles for context the engine
        didn't pass, liquidity, this pair's history and the account view."""
        now = ctx.now
        candles = {"1h": ctx.candles_1h, "4h": ctx.candles_4h, "1d": ctx.candles_1d}
        context = dict(ctx.context_candles)
        for key, (market, tf, limit) in CONTEXT_SPECS.items():
            if not context.get(key):
                context[key] = drop_forming(repo.load_candles(session, market, tf, limit), tf, now)
        context["eth_4h"] = (
            ctx.candles_4h
            if ctx.pair == "ETH-EUR"
            else drop_forming(repo.load_candles(session, "ETH-EUR", "4h", 300), "4h", now)
        )
        liquidity = await self._liquidity(session, ctx.pair)
        trades, hours_since = pair_history(session, broker.account_id, ctx.pair, now)
        tick = self._tick_size(ctx.pair)
        prev = previous_decision_v2(session, broker.account_id, ctx.pair, tick=tick)

        equity = broker.equity()
        day_start = Decimal(broker.account.day_start_equity)
        pnl = broker.today_pnl(now)
        account = {
            "exposure_pct": float(broker.exposure() / equity * 100) if equity > 0 else 0.0,
            "today_pnl_pct": float(pnl / day_start * 100) if day_start > 0 else 0.0,
            "drawdown_pct": float(broker.drawdown_pct()),
        }
        rv = self._risk_view(session)
        held: list[tuple[str, float | None, float]] = []
        for other_pair, other_pos in broker.positions.items():
            if other_pair == ctx.pair:
                continue
            other_1h = drop_forming(
                repo.load_candles(session, other_pair, "1h", CORR_BARS), "1h", now
            )
            corr = aligned_correlation(ctx.candles_1h, other_1h, CORR_BARS)
            held.append((other_pair, corr, (now - other_pos.opened_at).total_seconds() / 3600))
        inp = BriefInputs(
            pair=ctx.pair,
            now=now,
            price=ctx.price,
            signals=ctx.signals,
            holding=ctx.holding,
            settings=settings,
            session=session,
            source=settings.data_source,
            market_info=self._market_rules.get(ctx.pair),
            candles=candles,
            context_candles=context,
            btc_regime=ctx.btc_regime,
            position=ctx.position,
            liquidity=liquidity,
            pair_trades=trades,
            hours_since_exit=hours_since,
            previous_decision=prev,
            account=account,
            held_portfolio=held,
            correlated_slots_left=rv.correlated_slots_left,
            risk_throttle=rv.throttle_on,
            shock_brake=rv.shock_brake_on,
        )
        brief, meta = build_brief(inp, prompt.brief_version)
        return brief, meta, self._payload(settings, prompt, brief)

    def _tick_size(self, pair: str) -> float | None:
        info = self._market_rules.get(pair)
        return float(info.tick_size) if info is not None and info.tick_size else None

    async def _liquidity(self, session: Session, pair: str) -> dict[str, float | None]:
        """Order-book spread and ±0.5% depth plus today's volume vs the
        30-day average. Failures leave the fields null, never guessed."""
        spread: float | None = None
        depth: float | None = None
        try:
            book = await self.client.order_book(pair)
            if book.best_bid is not None and book.best_ask is not None:
                mid = (book.best_bid + book.best_ask) / 2
                if book.spread_pct is not None:
                    spread = float(book.spread_pct)
                lo, hi = mid * Decimal("0.995"), mid * Decimal("1.005")
                depth = float(
                    sum(lvl.price * lvl.size for lvl in book.bids if lvl.price >= lo)
                    + sum(lvl.price * lvl.size for lvl in book.asks if lvl.price <= hi)
                )
        except MarketDataError:
            pass
        vol24: Decimal | None = None
        try:
            tickers = await self.client.ticker_24h(pair)
            if tickers:
                vol24 = tickers[0].volume_quote
        except MarketDataError:
            pass
        row = repo.get_market_row(session, pair)
        avg = Decimal(str(row.volume_30d_eur)) if row and row.volume_30d_eur else None
        ratio = float(vol24 / avg) if vol24 is not None and avg else None
        return {"spread_pct": spread, "depth_eur": depth, "vol24h_vs_30d": ratio}

    async def _alert_once(self, session: Session, now: datetime, kind: str, text: str) -> None:
        """Budget alerts fire once per period: daily budget once a day,
        monthly-cap alerts once a month (Amsterdam clock)."""
        ams = now.astimezone(AMSTERDAM)
        stamp = ams.strftime("%Y-%m-%d") if kind == "budget" else ams.strftime("%Y-%m")
        key = f"{kind}:{stamp}"
        if key in self._alerted:
            return
        self._alerted.add(key)
        repo.add_activity(session, "alert", text, at=now)
        session.commit()
        if self._notify is not None:
            await self._notify("model_failing", "Model limit reached", text)

    async def _track_model_result(self, status: str, detail: str) -> None:
        if status == "ok":
            self._model_failures = 0
            return
        self._model_failures += 1
        if self._model_failures == 3 and self._notify is not None:
            await self._notify(
                "model_failing",
                "Model failing",
                f"3 model calls failed in a row; latest: {detail}",
            )
