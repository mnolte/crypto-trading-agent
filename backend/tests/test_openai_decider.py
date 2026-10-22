"""The OpenAI decider: parse/schema, every post-check at its boundary, and
the decide() orchestration — pre-filter, budgets, brief, call, logging and
the Rules fallback. The model never reaches the risk gates on a failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.brief import TF_FIELDS, BriefMeta
from app.agent.broker.paper import PaperBroker
from app.agent.deciders.base import DecideContext, Draft, RiskView
from app.agent.deciders.openai_decider import (
    SIGNAL_ALIASES,
    ModelDecision,
    ModelSignal,
    OpenAIDecider,
    check_output,
    decision_schema,
    parse_model_output,
    signal_claims_check,
)
from app.agent.market.base import Candle, MarketClient, MarketDataError, OrderBook
from app.agent.openai_client import CallResult
from app.db import ActivityRow, LlmCallRow, make_engine, make_session_factory, run_migrations
from app.models import Regime, Settings
from tests.test_brief import _signals

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


# ----------------------------------------------------------------- fixtures --


def _meta(**kw: Any) -> BriefMeta:
    defaults: dict[str, Any] = {
        "pair": "SOL-EUR",
        "now": NOW,
        "price": 100.0,
        "atr_1h": 0.5,
        "holding": False,
        "regime": Regime(trend="up", volatility="normal", age_4h=5),
        "btc_regime": None,
        "bars": {"1h": 150, "4h": 150, "1d": 300},
        "data_age_s": {"1h": 10.0, "4h": 10.0, "1d": 10.0},
        "stale_tfs": [],
        "support": [98.0],
        "resistance": [105.0],
    }
    defaults.update(kw)
    return BriefMeta(**defaults)


def _brief(**kw: Any) -> dict[str, Any]:
    """The brief JSON the signal-claims check reads."""
    brief: dict[str, Any] = {
        "pair": "SOL-EUR",
        "price": 100.0,
        "tf": {
            "1h": {"rsi": {"value": 58.0, "read": "bullish"}},
            "4h": {"rsi": {"value": 58.0, "read": "bullish"}},
            "1d": {"rsi": {"value": 58.0, "read": "bullish"}},
        },
        "market_context": {
            "btc_trend_4h": {"value": 1.0, "read": "bullish"},
        },
        "data_age_s": {},
        "limits": {"max_age_s": {}},
    }
    brief.update(kw)
    return brief


def _check(
    out: ModelDecision | None,
    *,
    parse_error: str | None = None,
    brief: dict[str, Any] | None = None,
    meta: BriefMeta | None = None,
    rules_action: str = "BUY",
    settings: Settings | None = None,
) -> Any:
    return check_output(
        out,
        parse_error,
        brief or _brief(),
        meta or _meta(),
        rules_action,
        settings or Settings(),
    )


def _buy(**kw: Any) -> ModelDecision:
    """A valid BUY against the default meta (price 100, ATR 0.5)."""
    defaults: dict[str, Any] = {
        "action": "BUY",
        "confidence": 0.7,
        "entry": 100.2,
        "stop": 99.2,
        "target": 102.5,
        "reasoning": "trend up",
        "signals": [{"name": "rsi", "tf": "1h", "read": "bullish"}],
        "invalidation": "below support",
        "invalidation_price": 99.7,
    }
    defaults.update(kw)
    return ModelDecision.model_validate(defaults)


def _named(checks: Any, name: str) -> Any:
    return next(c for c in checks if c.name == name)


# ------------------------------------------------------------------- parse --


def test_parse_empty_output() -> None:
    out, err = parse_model_output(None)
    assert out is None and err == "empty output"


def test_parse_invalid_json() -> None:
    out, err = parse_model_output("not json")
    assert out is None and err is not None and "invalid JSON" in err


def test_parse_schema_violation() -> None:
    out, err = parse_model_output('{"action":"MOON","confidence":0.5}')
    assert out is None and err is not None and "schema violation" in err


def test_parse_valid_minimal() -> None:
    out, err = parse_model_output(json.dumps({"action": "HOLD", "confidence": 0.3}))
    assert err is None and out is not None and out.action == "HOLD"


def test_decision_schema_is_strict() -> None:
    schema = decision_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


# ------------------------------------------------------------- post-checks --


def test_unparseable_output_holds() -> None:
    verdict = _check(None, parse_error="invalid JSON: x")
    assert verdict.action == "HOLD"
    assert _named(verdict.checks, "Schema valid").status == "fail"
    assert verdict.failed is not None


def test_buy_while_holding_fails() -> None:
    verdict = _check(_buy(), meta=_meta(holding=True))
    assert _named(verdict.checks, "Action allowed").status == "fail"
    assert verdict.action == "HOLD"


def test_sell_without_position_fails() -> None:
    out = ModelDecision(action="SELL", confidence=0.6)
    verdict = _check(out, meta=_meta(holding=False))
    assert _named(verdict.checks, "Action allowed").status == "fail"
    assert verdict.action == "HOLD"


def test_sell_with_position_passes() -> None:
    out = ModelDecision(action="SELL", confidence=0.6)
    verdict = _check(out, meta=_meta(holding=True), rules_action="SELL")
    assert _named(verdict.checks, "Action allowed").status == "pass"
    assert verdict.action == "SELL"


def test_buy_missing_levels_fails() -> None:
    verdict = _check(_buy(stop=None))
    assert _named(verdict.checks, "Levels sane").status == "fail"
    assert verdict.action == "HOLD"


def test_buy_levels_out_of_order_fails() -> None:
    verdict = _check(_buy(stop=100.5))  # stop above entry
    assert _named(verdict.checks, "Levels sane").status == "fail"
    assert verdict.action == "HOLD"


def test_entry_distance_boundary() -> None:
    # 0.5% is allowed; anything beyond fails.
    verdict = _check(_buy(entry=100.5))
    assert _named(verdict.checks, "Levels sane").status != "fail"
    verdict = _check(_buy(entry=100.51))
    assert _named(verdict.checks, "Levels sane").status == "fail"
    assert verdict.action == "HOLD"


def test_stop_atr_band_boundaries() -> None:
    settings = Settings()
    lo = float(settings.risk.stop_min_atr)  # 0.5
    hi = float(settings.risk.stop_max_atr)  # 5
    # stop exactly lo*ATR below entry passes; just inside the band fails.
    verdict = _check(_buy(entry=100.0, stop=100.0 - lo * 0.5), settings=settings)
    assert _named(verdict.checks, "Levels sane").status != "fail"
    verdict = _check(_buy(entry=100.0, stop=100.0 - (lo - 0.1) * 0.5), settings=settings)
    assert _named(verdict.checks, "Levels sane").status == "fail"
    verdict = _check(_buy(entry=100.0, stop=100.0 - hi * 0.5), settings=settings)
    assert _named(verdict.checks, "Levels sane").status != "fail"
    verdict = _check(_buy(entry=100.0, stop=100.0 - (hi + 0.1) * 0.5), settings=settings)
    assert _named(verdict.checks, "Levels sane").status == "fail"


def test_rr_below_minimum_warns_but_travels_on() -> None:
    # R/R below the min is a warn: the risk gate turns it into SKIP.
    settings = Settings()
    verdict = _check(_buy(entry=100.0, stop=99.0, target=100.5), settings=settings)
    check = _named(verdict.checks, "R/R recomputed")
    assert check.status == "warn"
    assert verdict.action == "BUY"


def test_invalidation_inside_stop_entry_kept() -> None:
    verdict = _check(_buy(invalidation_price=99.7))
    assert verdict.invalidation_price == Decimal("99.7")


def test_invalidation_outside_band_dropped_with_warn() -> None:
    verdict = _check(_buy(invalidation_price=98.0))  # below the stop
    assert verdict.invalidation_price is None
    assert _named(verdict.checks, "Levels sane").status == "warn"


def test_levels_on_hold_are_warned_and_ignored() -> None:
    out = ModelDecision(action="HOLD", confidence=0.5, entry=100.0)
    verdict = _check(out, rules_action="HOLD")
    assert _named(verdict.checks, "Levels sane").status == "warn"
    assert verdict.action == "HOLD"


def test_signal_claim_mismatch_warns() -> None:
    out = _buy(signals=[{"name": "rsi", "tf": "1h", "read": "bearish"}])
    verdict = _check(out)
    check = _named(verdict.checks, "Signal claims")
    assert check.status == "warn"
    assert "rsi" in check.detail


def test_unknown_signal_claim_warns() -> None:
    out = _buy(signals=[{"name": "macd", "tf": "1h", "read": "bullish"}])
    verdict = _check(out)
    assert _named(verdict.checks, "Signal claims").status == "warn"


def test_matching_signal_claims_pass() -> None:
    verdict = _check(_buy())
    assert _named(verdict.checks, "Signal claims").status == "pass"


# ---------------------------------------------------- signal claims -----

FIXTURES = Path(__file__).parent / "fixtures"

# The market_context keys the brief can carry (all tf-free).
_CTX_KEYS = (
    "btc_trend_4h",
    "btc_trend_1d",
    "btc_chg_24h_pct",
    "btc_atr_rank_1h",
    "eth_btc_trend",
    "eur_usd_1h_pct",
    "eur_usd_24h_pct",
    "rs_btc_24h_pct",
    "rs_btc_7d_pct",
    "corr_btc_30d",
)


def _full_brief(read: str = "bullish") -> dict[str, Any]:
    """A brief carrying every signal field on every tf plus all context
    keys, all read `read`."""
    tf = {name: {"value": 1.0, "read": read} for name in TF_FIELDS}
    return _brief(
        tf={
            "1h": dict(tf),
            "4h": dict(tf),
            "1d": {**tf, "ema200_dist": {"value": 1.0, "read": read}},
        },
        market_context={name: {"value": 1.0, "read": read} for name in _CTX_KEYS},
    )


def _sig(name: str, tf: str, read: str = "bullish") -> ModelSignal:
    return ModelSignal.model_validate({"name": name, "tf": tf, "read": read})


def test_cycle410_fixture_signal_claims_pass() -> None:
    """Stored BTC-EUR call req_102120132edb44c48b5328dd35f94b1d (cycle 410,
    21 Oct 2026): the model cited "VWAP Distance", "EMA 20 vs 50" and "OBV
    Trend" — names the old check reported as "not in brief" twice. Every
    cited read matches the brief, so the check passes."""
    brief = json.loads((FIXTURES / "brief_cycle410.json").read_text())
    out, err = parse_model_output((FIXTURES / "output_cycle410.json").read_text())
    assert err is None and out is not None
    check = signal_claims_check(out.signals, brief)
    assert check.status == "pass", check.detail


@pytest.mark.parametrize(
    "cited", ["VWAP Distance", "VWAP", "price vs VWAP", "vwap_dist", "VWAP-Dist."]
)
def test_vwap_name_variants_resolve(cited: str) -> None:
    check = signal_claims_check([_sig(cited, "1h")], _full_brief())
    assert check.status == "pass", check.detail


@pytest.mark.parametrize(("alias", "key"), sorted(SIGNAL_ALIASES.items()))
def test_every_alias_resolves_to_a_brief_key(alias: str, key: str) -> None:
    """Each alias maps to a canonical key, and a claim citing the alias
    resolves to it — never 'not in brief'."""
    canonical = set(TF_FIELDS) | {"ema200_dist"} | set(_CTX_KEYS)
    assert key in canonical
    tf = "1d" if key == "ema200_dist" else "1h"
    check = signal_claims_check([_sig(alias, tf)], _full_brief())
    assert check.status == "pass", f"{alias} -> {key}: {check.detail}"


def test_unknown_signal_warns_not_in_brief() -> None:
    check = signal_claims_check([_sig("macd", "1h")], _full_brief())
    assert check.status == "warn"
    assert check.detail == "macd · 1h: not in brief"


@pytest.mark.parametrize("key", sorted(TF_FIELDS))
def test_contradiction_on_1h_flagged(key: str) -> None:
    check = signal_claims_check([_sig(key, "1h", "bearish")], _full_brief())
    assert check.status == "warn"
    assert f"{key} · 1h: model says bearish, brief says bullish" == check.detail


def test_aliased_contradiction_still_flagged() -> None:
    """'VWAP Distance' resolves to vwap_dist; a contradicting read is a
    warn, never a pass."""
    check = signal_claims_check([_sig("VWAP Distance", "1h", "bearish")], _full_brief())
    assert check.status == "warn"
    assert "model says bearish, brief says bullish" in check.detail


def test_ema200_on_1h_warns_no_such_value() -> None:
    check = signal_claims_check([_sig("ema200_dist", "1h")], _full_brief())
    assert check.status == "warn"
    assert check.detail == "ema200_dist · 1h: no such value on 1h"


def test_missing_tf_warns_no_such_value() -> None:
    brief = _full_brief()
    del brief["tf"]["4h"]
    check = signal_claims_check([_sig("rsi", "4h")], brief)
    assert check.status == "warn"
    assert check.detail == "rsi · 4h: no such value on 4h"


def test_context_keys_match_regardless_of_tf() -> None:
    check = signal_claims_check([_sig("BTC trend", "4h")], _full_brief())
    assert check.status == "pass", check.detail


def test_duplicate_claims_merge_to_one_message() -> None:
    check = signal_claims_check(
        [_sig("macd", "1h"), _sig("macd", "1h"), _sig("RSI", "1h", "bearish")],
        _full_brief(),
    )
    assert check.status == "warn"
    assert check.detail == (
        "macd · 1h: not in brief; RSI · 1h: model says bearish, brief says bullish"
    )


def test_stale_data_allows_only_hold() -> None:
    meta = _meta(stale_tfs=["1h"])
    verdict = _check(ModelDecision(action="HOLD", confidence=0.2), meta=meta, rules_action="HOLD")
    assert _named(verdict.checks, "Stale-data rule").status == "pass"
    verdict = _check(_buy(), meta=meta)
    assert _named(verdict.checks, "Stale-data rule").status == "fail"
    assert verdict.action == "HOLD"


def test_buy_in_down_regime_fails_for_alt() -> None:
    meta = _meta(regime=Regime(trend="down", volatility="normal", age_4h=3))
    verdict = _check(_buy(), meta=meta)
    assert _named(verdict.checks, "Regime rule").status == "fail"
    assert verdict.action == "HOLD"


def test_btc_buy_in_down_regime_caps_confidence() -> None:
    meta = _meta(
        pair="BTC-EUR",
        regime=Regime(trend="down", volatility="normal", age_4h=3),
    )
    verdict = _check(_buy(confidence=0.9), meta=meta)
    assert _named(verdict.checks, "Regime rule").status == "warn"
    assert verdict.confidence == pytest.approx(0.6)
    assert verdict.action == "BUY"


def test_sideways_requires_support_below_entry() -> None:
    meta = _meta(
        regime=Regime(trend="sideways", volatility="normal", age_4h=3),
        support=[],
    )
    verdict = _check(_buy(), meta=meta)
    assert _named(verdict.checks, "Regime rule").status == "fail"


def test_sideways_entry_not_far_above_support() -> None:
    meta = _meta(
        regime=Regime(trend="sideways", volatility="normal", age_4h=3),
        support=[99.0],  # 1.0 below entry = 2 x ATR > 1 x ATR
    )
    verdict = _check(_buy(entry=100.0, stop=99.0), meta=meta)
    assert _named(verdict.checks, "Regime rule").status == "fail"


def test_sideways_target_not_above_resistance() -> None:
    meta = _meta(
        regime=Regime(trend="sideways", volatility="normal", age_4h=3),
        resistance=[101.0],
    )
    verdict = _check(_buy(target=102.5), meta=meta)
    assert _named(verdict.checks, "Regime rule").status == "fail"


def test_high_volatility_caps_confidence() -> None:
    meta = _meta(regime=Regime(trend="up", volatility="high", age_4h=3))
    verdict = _check(_buy(confidence=0.9), meta=meta)
    assert verdict.confidence == pytest.approx(0.7)
    assert _named(verdict.checks, "Regime rule").status == "warn"


def test_rules_baseline_disagreement_is_a_warn() -> None:
    verdict = _check(_buy(), rules_action="HOLD")
    check = _named(verdict.checks, "Rules baseline")
    assert check.status == "warn"
    assert verdict.action == "BUY"


def test_valid_buy_passes_all_checks() -> None:
    verdict = _check(_buy())
    assert verdict.action == "BUY"
    assert verdict.failed is None
    assert all(c.status != "fail" for c in verdict.checks)


# ------------------------------------------------------------- decide() ----


def _candles(n: int, step: timedelta, end: datetime, price: float = 100.0) -> list[Candle]:
    out: list[Candle] = []
    p = price
    for i in range(n):
        p *= 1.0005
        out.append(
            Candle(
                open_time=end - (n - i) * step,
                open=f"{p / 1.0005:.6f}",
                high=f"{p * 1.002:.6f}",
                low=f"{p * 0.998:.6f}",
                close=f"{p:.6f}",
                volume="10",
            )
        )
    return out


class _FakeRules:
    name = "rules"

    def __init__(self, draft: Draft) -> None:
        self.draft = draft
        self.calls = 0

    async def decide(self, ctx: DecideContext) -> Draft:
        self.calls += 1
        return self.draft


class _FakeMarket:
    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        return OrderBook(market=market, bids=[], asks=[])

    async def ticker_24h(self, market: str | None = None) -> list[Any]:
        return []


class _FailingMarket:
    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        raise MarketDataError("down")

    async def ticker_24h(self, market: str | None = None) -> list[Any]:
        raise MarketDataError("down")


class _StubAccount:
    day_start_equity = "10000"


class _StubBroker:
    """The broker view the decider needs: id, positions, account metrics."""

    def __init__(self) -> None:
        self.account_id = 1
        self.positions: dict[str, Any] = {}
        self.account = _StubAccount()

    def equity(self) -> Decimal:
        return Decimal("10000")

    def today_pnl(self, now: datetime) -> Decimal:
        return Decimal("0")

    def exposure(self) -> Decimal:
        return Decimal("0")

    def drawdown_pct(self) -> Decimal:
        return Decimal("0")


class _FakeClient:
    """A scripted OpenAIResponsesClient: returns a fixed CallResult and
    records the payload it was given."""

    result: CallResult | None = None
    last_payload: dict[str, Any] | None = None
    calls = 0

    def __init__(self, result: CallResult) -> None:
        _FakeClient.result = result

    async def call(self, payload: dict[str, Any]) -> CallResult:
        type(self).calls += 1
        type(self).last_payload = payload
        result = type(self).result
        assert result is not None
        return result

    async def aclose(self) -> None:
        pass


def _ok_result(text: str) -> CallResult:
    return CallResult(
        status="ok",
        request_bytes=b'{"model":"gpt-4o-mini"}',
        response_bytes=b'{"id":"resp"}',
        output_text=text,
        input_tokens=100,
        output_tokens=50,
        request_id="req-1",
        latency_ms=12,
    )


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db = tmp_path / "agent.db"
    run_migrations(db)
    yield make_session_factory(make_engine(db))


def _ctx(**kw: Any) -> DecideContext:
    defaults: dict[str, Any] = {
        "pair": "SOL-EUR",
        "now": NOW,
        "price": Decimal("100.0"),
        "signals": _signals(),
        "holding": False,
        "position_stop": None,
        "position_target": None,
        "position_entry": None,
        "candles_1h": _candles(150, HOUR, NOW),
        "candles_4h": _candles(150, 4 * HOUR, NOW),
        "candles_1d": _candles(300, timedelta(days=1), NOW),
        "settings": Settings(),
        "cycle": 3,
        "context_candles": {
            "btc_1h": _candles(60, HOUR, NOW, price=60000.0),
            "btc_4h": _candles(150, 4 * HOUR, NOW, price=60000.0),
            "btc_1d": _candles(300, timedelta(days=1), NOW, price=60000.0),
            "usdc_1h": _candles(48, HOUR, NOW, price=0.9),
            "eth_4h": _candles(150, 4 * HOUR, NOW, price=3000.0),
        },
    }
    defaults.update(kw)
    return DecideContext(**defaults)


def _rules_draft(**kw: Any) -> Draft:
    defaults: dict[str, Any] = {
        "action": "HOLD",
        "confidence": 0.0,
        "reasoning": "nothing",
        "signals": [],
        "prefilter": True,
    }
    defaults.update(kw)
    return Draft(**defaults)


def _decider(
    sessions: sessionmaker[Session],
    *,
    rules_draft: Draft | None = None,
    key: str | None = "sk",
    client_result: CallResult | None = None,
    market: Any = None,
) -> tuple[OpenAIDecider, _FakeRules]:
    rules = _FakeRules(rules_draft or _rules_draft())
    factory: Any = (
        (lambda key: _FakeClient(client_result))
        if client_result is not None
        else (lambda key: _FakeClient(_ok_result('{"action":"HOLD","confidence":0.5}')))
    )
    decider = OpenAIDecider(
        session_factory=sessions,
        market_client=cast("MarketClient", market or _FakeMarket()),
        broker=cast("Callable[[], PaperBroker]", _StubBroker),
        risk_view=lambda s: RiskView(
            throttle_on=False, correlated_slots_left=1, shock_brake_on=False
        ),
        key=lambda: key,
        client_factory=factory,
        rules=rules,  # type: ignore[arg-type]
    )
    return decider, rules


def _calls(sessions: sessionmaker[Session]) -> list[LlmCallRow]:
    session = sessions()
    try:
        return list(session.scalars(select(LlmCallRow)).all())
    finally:
        session.close()


async def test_prefilter_skip_makes_no_call(sessions: sessionmaker[Session]) -> None:
    decider, rules = _decider(sessions, rules_draft=_rules_draft(prefilter=False))
    draft = await decider.decide(_ctx())
    assert draft is rules.draft
    assert _calls(sessions) == []


async def test_prefilter_off_calls_anyway(sessions: sessionmaker[Session]) -> None:
    decider, _ = _decider(sessions, rules_draft=_rules_draft(prefilter=False))
    ctx = _ctx(settings=Settings(model={"rules_prefilter": False}))
    draft = await decider.decide(ctx)
    assert draft.decider == "openai"
    assert len(_calls(sessions)) == 1


async def test_holding_calls_despite_prefilter(sessions: sessionmaker[Session]) -> None:
    decider, _ = _decider(sessions, rules_draft=_rules_draft(prefilter=False))
    draft = await decider.decide(_ctx(holding=True))
    assert draft.decider == "openai"
    assert len(_calls(sessions)) == 1


async def test_no_key_returns_rules_without_a_call(
    sessions: sessionmaker[Session],
) -> None:
    decider, rules = _decider(sessions, key=None)
    draft = await decider.decide(_ctx())
    assert draft is rules.draft
    assert _calls(sessions) == []


async def test_daily_budget_used_falls_back_to_rules(
    sessions: sessionmaker[Session],
) -> None:
    session = sessions()
    day_start = datetime(2026, 10, 10, 22, 0, tzinfo=UTC)  # Amsterdam midnight
    repo.add_llm_call(
        session,
        LlmCallRow(
            id="used-1",
            at=day_start + timedelta(hours=1),
            cycle=None,
            account_id=1,
            pair="SOL-EUR",
            decision_id=None,
            feature="decision",
            replay_of=None,
            model="gpt-4o-mini",
            prompt_version="decision.v1",
            status="ok",
            attempts="[]",
            latency_ms=None,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            cost_eur=None,
            request_id=None,
            request_json=None,
            response_raw=None,
            parsed=None,
            input_checks="[]",
            output_checks="[]",
            verdict=None,
            verdict_note=None,
            verdict_at=None,
        ),
    )
    session.commit()
    session.close()
    decider, rules = _decider(sessions)
    ctx = _ctx(settings=Settings(model={"daily_call_budget": 1}))
    draft = await decider.decide(ctx)
    assert draft is rules.draft
    session = sessions()
    try:
        alerts = list(session.scalars(select(ActivityRow).where(ActivityRow.kind == "alert")).all())
        assert len(alerts) == 1
        assert "budget" in alerts[0].text.lower()
    finally:
        session.close()


async def test_monthly_cap_falls_back_to_rules(
    sessions: sessionmaker[Session],
) -> None:
    decider, rules = _decider(sessions)
    ctx = _ctx(settings=Settings(model={"monthly_cost_cap_eur": "0"}))
    draft = await decider.decide(ctx)
    assert draft is rules.draft
    assert _calls(sessions) == []


async def test_input_blocked_records_call_and_holds(
    sessions: sessionmaker[Session],
) -> None:
    decider, _ = _decider(sessions)
    ctx = _ctx(candles_1d=_candles(50, timedelta(days=1), NOW))  # History fails
    draft = await decider.decide(ctx)
    rows = _calls(sessions)
    assert len(rows) == 1
    assert rows[0].status == "input_blocked"
    assert draft.action == "HOLD"
    assert draft.prefilter is True
    assert draft.llm_call_id == rows[0].id
    assert draft.error and "History" in draft.error


async def test_api_error_logs_call_and_falls_back(
    sessions: sessionmaker[Session],
) -> None:
    decider, rules = _decider(
        sessions,
        client_result=CallResult(
            status="api_error",
            request_bytes=b'{"model":"m"}',
            error="HTTP 500: boom",
        ),
    )
    draft = await decider.decide(_ctx())
    rows = _calls(sessions)
    assert len(rows) == 1
    assert rows[0].status == "api_error"
    assert draft is rules.draft
    assert "fell back to Rules" in draft.reasoning
    assert draft.llm_call_id == rows[0].id


async def test_invalid_output_holds_and_marks_call(
    sessions: sessionmaker[Session],
) -> None:
    decider, _ = _decider(sessions, client_result=_ok_result("definitely not json"))
    draft = await decider.decide(_ctx())
    rows = _calls(sessions)
    assert len(rows) == 1
    assert rows[0].status == "invalid_output"
    assert draft.action == "HOLD"
    assert draft.error is not None and "Schema valid" in draft.error


async def test_valid_buy_becomes_a_buy_draft_with_audit(
    sessions: sessionmaker[Session],
) -> None:
    output = json.dumps(
        {
            "action": "BUY",
            "confidence": 0.7,
            "entry": 100.2,
            "stop": 99.2,
            "target": 102.5,
            "reasoning": "uptrend",
            "signals": [{"name": "rsi", "tf": "1h", "read": "bullish"}],
            "invalidation": "below support",
            "invalidation_price": 99.7,
        }
    )
    decider, _ = _decider(
        sessions,
        rules_draft=_rules_draft(action="BUY", prefilter=True),
        client_result=_ok_result(output),
    )
    session = sessions()
    repo.upsert_candles(
        session,
        "USDC-EUR",
        "1d",
        _candles(3, timedelta(days=1), NOW, price=0.9),
    )
    session.commit()
    session.close()
    draft = await decider.decide(_ctx())
    rows = _calls(sessions)
    assert len(rows) == 1
    row = rows[0]
    assert draft.action == "BUY"
    assert draft.entry == Decimal("100.2")
    assert draft.stop == Decimal("99.2")
    assert draft.target == Decimal("102.5")
    assert draft.invalidation_price == Decimal("99.7")
    assert draft.decider == "openai"
    assert draft.model == "gpt-4o-mini"
    assert draft.prompt_version == "decision.v1"
    assert draft.llm_call_id == row.id
    assert row.status == "ok"
    assert row.model == "gpt-4o-mini"
    assert row.input_tokens == 100 and row.output_tokens == 50
    assert row.cost_usd is not None and row.cost_eur is not None
    # The brief went out as JSON for exactly this pair.
    payload = _FakeClient.last_payload
    assert payload is not None
    sent = json.loads(payload["input"])
    assert sent["pair"] == "SOL-EUR"
    assert payload["temperature"] == 0


async def test_market_data_failure_leaves_liquidity_null_not_failed(
    sessions: sessionmaker[Session],
) -> None:
    decider, _ = _decider(sessions, market=_FailingMarket())
    draft = await decider.decide(_ctx())
    assert draft.decider == "openai"  # the call still happened
    payload = _FakeClient.last_payload
    assert payload is not None
    sent = json.loads(payload["input"])
    assert sent["liquidity"]["spread_pct"] is None
