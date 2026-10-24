"""Brief version 2: trade_math, previous_decision outcome/repeats, the v2
output schema and post-checks, brief upgrades for replays, and the prompt
comparison report."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.brief import brief_json, build_brief, previous_decision
from app.agent.brief_v2 import (
    BRIEF_VERSION,
    brief_version_of,
    trade_math_block,
    upgrade_brief_to_v2,
)
from app.agent.deciders.openai_decider import (
    ModelDecisionV2,
    check_output,
    decision_schema,
    parse_model_output,
)
from app.agent.market.base import Candle, MarketInfo
from app.agent.prompts import load_prompt
from app.db import LlmCallRow, make_engine, make_session_factory, run_migrations
from app.models import Regime, Settings
from app.tools.compare_prompts import run_report
from tests.test_brief import _inputs, _seed_decision
from tests.test_openai_decider import _meta

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    db = tmp_path / "agent.db"
    run_migrations(db)
    s = make_session_factory(make_engine(db))()
    yield s
    s.close()


def _candles_with_marks(
    n: int,
    step: timedelta,
    end: datetime,
    *,
    base: float = 100.0,
    lows: dict[int, float] | None = None,
    highs: dict[int, float] | None = None,
) -> list[Candle]:
    """``n`` closed candles; ``lows``/``highs`` map a bar index (from the
    end, 0 = last bar) to an explicit low/high so known swing levels form."""
    lows = lows or {}
    highs = highs or {}
    out: list[Candle] = []
    for i in range(n):
        k = n - 1 - i  # index from the end
        lo = lows.get(k, base * 0.995)
        hi = highs.get(k, base * 1.005)
        out.append(
            Candle(
                open_time=end - (n - i) * step,
                open=f"{base:.6f}",
                high=f"{max(hi, base):.6f}",
                low=f"{min(lo, base):.6f}",
                close=f"{base:.6f}",
                volume="10",
            )
        )
    return out


def _tf1h(reads: dict[str, str]) -> dict[str, Any]:
    return {k: {"value": 1.0, "read": r} for k, r in reads.items()}


def _tm(
    *,
    pair: str = "SOL-EUR",
    price: float = 100.0,
    atr: float = 0.5,
    regime: Regime | None = None,
    holding: bool = False,
    tf1h: dict[str, Any] | None = None,
    lows: dict[int, float] | None = None,
    highs: dict[int, float] | None = None,
    limits: dict[str, Any] | None = None,
    candles_1h: list[Candle] | None = None,
    candles_4h: list[Candle] | None = None,
    min_stop_pct: float = 2.0,
) -> dict[str, Any]:
    marks_l = lows or {5: 98.0, 10: 96.0, 15: 93.0}
    marks_h = highs or {3: 101.5, 8: 105.0}
    c1h = (
        candles_1h
        if candles_1h is not None
        else _candles_with_marks(200, HOUR, NOW, lows=marks_l, highs=marks_h)
    )
    c4h = candles_4h if candles_4h is not None else _candles_with_marks(120, 4 * HOUR, NOW)
    return trade_math_block(
        pair=pair,
        now=NOW,
        price=price,
        atr_1h=atr,
        tf1h=tf1h
        or _tf1h(
            {
                "rsi": "bullish",
                "ema20_vs_50": "bullish",
                "vwap_dist": "bullish",
                "obv_trend": "bullish",
                "adx": "trending",
            }
        ),
        candles_1h=c1h,
        candles_4h=c4h,
        regime=regime or Regime(trend="up", volatility="normal", age_4h=12),
        holding=holding,
        limits=limits
        or {
            "min_rr": 2.0,
            "min_stop_atr": 0.5,
            "max_stop_atr": 5.0,
            "min_stop_pct": min_stop_pct,
        },
        stop_buffer_atr=0.1,
        round_price=lambda v: round(v, 4),
    )


# ----------------------------------------------------------------- prompt ----


def test_prompt_header_declares_brief_version() -> None:
    assert load_prompt("decision.v1").brief_version == 1
    assert load_prompt("decision.v2").brief_version == 2


def test_brief_version_of_detects_trade_math() -> None:
    assert brief_version_of({"pair": "X"}) == 1
    assert brief_version_of({"trade_math": {}}) == 2


# --------------------------------------------------------------- v1 golden --


def test_v1_brief_is_byte_identical(session: Session) -> None:
    """A brief_version-1 build stays byte-identical to the pre-v2 output."""
    golden = (FIXTURES / "brief_v1_golden.json").read_text()
    brief, _ = build_brief(_inputs(session), 1)
    assert brief_json(brief) == golden


def test_v1_brief_with_previous_decision_is_byte_identical(session: Session) -> None:
    _seed_decision(session, "d1", 1, NOW - HOUR, "HOLD")
    session.commit()
    inp = _inputs(session, previous_decision=previous_decision(session, 1, "SOL-EUR"))
    golden = (FIXTURES / "brief_v1_prevdec_golden.json").read_text()
    brief, _ = build_brief(inp, 1)
    assert brief_json(brief) == golden


def test_v2_brief_adds_exactly_the_new_fields(session: Session) -> None:
    v1, _ = build_brief(_inputs(session), 1)
    v2, _ = build_brief(_inputs(session), BRIEF_VERSION)
    new_keys = set(v2) - set(v1)
    assert new_keys == {"trade_math"}
    # every v1 field except previous_decision is unchanged
    skip = {"previous_decision", "trade_math"}
    assert {k: v for k, v in v2.items() if k not in skip} == {
        k: v for k, v in v1.items() if k not in skip
    }


def test_v2_previous_decision_extends_v1_shape(session: Session) -> None:
    _seed_decision(session, "d1", 1, NOW - HOUR, "HOLD")
    session.commit()
    inp = _inputs(session, previous_decision=previous_decision(session, 1, "SOL-EUR"))
    brief, _ = build_brief(inp, BRIEF_VERSION)
    prev = brief["previous_decision"]
    assert prev["action"] == "HOLD"
    assert prev["invalidation"] == "stop hit"
    assert prev["outcome"] == "held"
    assert prev["repeats"] >= 1
    assert prev["at"].startswith("2026-10-11T11:00")
    assert "trade_math" in brief


# --------------------------------------------------------------- trade_math --


def test_stop_zone_bounds() -> None:
    tm = _tm()
    zone = tm["stop_zone"]
    # highest = min(100*(1-0.02), 100-0.5*0.5)=98.0; lowest = 100-5*0.5=97.5
    assert zone == {"highest": 98.0, "lowest": 97.5}


def test_support_just_outside_the_zone_is_excluded() -> None:
    # levels at 98.0 (inside) and 98.5 (stop 98.45 > zone.highest) and
    # 93.0 (stop 92.95 < zone.lowest)
    tm = _tm(lows={5: 98.0, 10: 98.5, 15: 93.0})
    levels = [c["level"] for c in tm["stop_candidates"]]
    assert levels == [98.0]


def test_stop_candidates_carry_source_and_dist() -> None:
    tm = _tm(lows={5: 98.0})
    cand = tm["stop_candidates"][0]
    assert cand["id"] == "S1"
    assert cand["price"] == round(98.0 - 0.05, 4)
    assert cand["dist_pct"] == pytest.approx(-2.05, abs=0.01)
    assert cand["source"] in ("1h swing", "4h swing", "previous day low")


def test_min_stop_pct_changes_the_zone() -> None:
    # market orders: 2.0% floor; limit orders: 1.5% floor
    tm20 = _tm(min_stop_pct=2.0)
    tm15 = _tm(min_stop_pct=1.5)
    assert tm20["stop_zone"]["highest"] == 98.0
    assert tm15["stop_zone"]["highest"] == 98.5
    # the 98.5 level now fits inside the zone
    cands = _tm(min_stop_pct=1.5, lows={5: 98.0, 10: 98.5})["stop_candidates"]
    levels = [c["level"] for c in cands]
    assert 98.5 in levels


def test_sideways_lists_only_the_nearest_resistance() -> None:
    tm = _tm(
        regime=Regime(trend="sideways", volatility="normal", age_4h=12),
        lows={5: 99.7},
        highs={3: 101.5, 8: 104.0},
        atr=0.5,
    )
    assert len(tm["target_candidates"]) == 1
    # the nearest of every source wins — here the flat 100.5 previous-day high
    assert tm["target_candidates"][0]["price"] == 100.5
    assert tm["target_candidates"][0]["source"] == "previous day high"


def test_up_regime_lists_up_to_five_targets() -> None:
    highs = {i: 100.5 + i for i in (3, 8, 13, 18, 23, 28)}
    tm = _tm(highs=highs)
    assert len(tm["target_candidates"]) <= 5
    assert [t["id"] for t in tm["target_candidates"]] == [
        f"R{i}" for i in range(1, len(tm["target_candidates"]) + 1)
    ]


def test_no_pair_means_infeasible_with_reason() -> None:
    tm = _tm(highs={3: 101.5, 8: 104.0})  # best rr = 4/2.05 < 2
    assert tm["buy_feasible"] is False
    assert "nearest resistance" in tm["infeasible_reason"]
    assert "100.5" in tm["infeasible_reason"]  # prev-day high is the nearest
    assert "+0.5%" in tm["infeasible_reason"]
    assert "2.0R" in tm["infeasible_reason"]


def test_down_regime_infeasible_for_alt_but_btc_allowed() -> None:
    down = Regime(trend="down", volatility="normal", age_4h=12)
    tm = _tm(pair="SOL-EUR", regime=down)
    assert tm["buy_feasible"] is False
    assert "down" in tm["infeasible_reason"]
    tm = _tm(pair="BTC-EUR", regime=down)
    assert tm["buy_feasible"] is True
    assert tm["infeasible_reason"] is None


def test_sideways_too_far_above_support_infeasible() -> None:
    # every candle of the previous UTC day bottoms at 97.0 — the nearest
    # support (prev-day low) sits 6 ATR below the price
    lows = dict.fromkeys(range(12, 36), 97.0)
    tm = _tm(
        regime=Regime(trend="sideways", volatility="normal", age_4h=12),
        lows=lows,
        atr=0.5,
    )
    assert tm["buy_feasible"] is False
    assert "ATR(1h) above the nearest support" in tm["infeasible_reason"]


def test_holding_keeps_trade_math_but_infeasible() -> None:
    tm = _tm(holding=True)
    assert tm["buy_feasible"] is False
    assert tm["infeasible_reason"] == "position open"
    assert tm["stop_zone"] is not None  # the maths is still sent


def test_pairs_sorted_best_rr_first_and_capped() -> None:
    tm = _tm()
    assert tm["buy_feasible"] is True
    assert tm["pairs"]
    rrs = [p["rr"] for p in tm["pairs"]]
    assert rrs == sorted(rrs, reverse=True)
    assert all(rr >= 2.0 for rr in rrs)
    assert len(tm["pairs"]) <= 10


@pytest.mark.parametrize(("age", "required"), [(5, 4), (6, 3), (0, 4), (12, 3)])
def test_conditions_required_follows_regime_age(age: int, required: int) -> None:
    tm = _tm(regime=Regime(trend="up", volatility="normal", age_4h=age))
    assert tm["conditions_1h_required"] == required
    assert tm["conditions_1h_met"] == 5


def test_conditions_1h_reads_the_brief_reads() -> None:
    tm = _tm(
        tf1h=_tf1h(
            {
                "rsi": "bearish",
                "ema20_vs_50": "bullish",
                "vwap_dist": "neutral",
                "obv_trend": "bullish",
                "adx": "ranging",
            }
        )
    )
    assert tm["conditions_1h"] == {
        "ema20_above_50": True,
        "above_vwap": False,
        "obv_bullish": True,
        "rsi_bullish": False,
        "adx_trending": False,
    }
    assert tm["conditions_1h_met"] == 2


# ------------------------------------------------------- previous_decision --


def _skip_with_gates(session: Session, did: str, at: datetime, *, plan: str | None = None) -> None:
    gates = json.dumps(
        [
            {"name": "Entry pause", "status": "pass", "value": "open"},
            {
                "name": "Min reward/risk",
                "status": "fail",
                "value": "R/R 1.50 < 2.00",
                "limit": "R/R >= 2.00",
            },
        ]
    )
    _seed_decision(session, did, 1, at, "SKIP")
    row = repo.get_decision_row(session, did)
    assert row is not None
    row.gates = gates
    row.plan = plan


def test_previous_decision_outcome_and_failed_gates(session: Session) -> None:
    _skip_with_gates(session, "sk1", NOW - HOUR, plan='{"entry":"100","stop":"99","target":"101"}')
    session.commit()
    prev = previous_decision(session, 1, "SOL-EUR")
    assert prev is not None
    assert prev.outcome == "skipped"
    assert prev.failed_gates == [
        {"name": "Min reward/risk", "value": "R/R 1.50 < 2.00", "limit": "R/R >= 2.00"}
    ]
    block = prev.to_brief(BRIEF_VERSION)
    assert block["outcome"] == "skipped"
    assert block["failed_gates"][0]["name"] == "Min reward/risk"
    assert block["repeats"] == 1


def test_previous_decision_outcomes(session: Session) -> None:
    cases = [
        ("SKIP", "none", "skipped"),
        ("HOLD", "none", "held"),
        ("BUY", "executed", "executed"),
        ("SELL", "executed", "executed"),
        ("BUY", "vetoed", "vetoed"),
        ("BUY", "expired", "expired"),
        ("BUY", "unfilled", "unfilled"),
        ("BUY", "none", "none"),
        ("BUY", "pending", "none"),
    ]
    for i, (action, status, _want) in enumerate(cases):
        _seed_decision(session, f"d{i}", 1, NOW - timedelta(minutes=10 * (len(cases) - i)), action)
        row = repo.get_decision_row(session, f"d{i}")
        assert row is not None
        row.status = status
    session.commit()
    prev = previous_decision(session, 1, "SOL-EUR")
    assert prev is not None and prev.outcome == "none"  # latest = pending BUY
    # earlier rows still map correctly through the streak logic
    from app.agent.brief_v2 import _outcome

    for action, status, want in cases:
        assert _outcome(action, status) == want


def test_repeats_counts_identical_skipped_buys(session: Session) -> None:
    same_plan = '{"entry":"100","stop":"99","target":"101"}'
    for i in range(3):
        _skip_with_gates(session, f"sk{i}", NOW - timedelta(minutes=15 * (3 - i)), plan=same_plan)
    # a different-level BUY breaks the streak
    _skip_with_gates(
        session, "older", NOW - 3 * HOUR, plan='{"entry":"100","stop":"97","target":"103"}'
    )
    session.commit()
    prev = previous_decision(session, 1, "SOL-EUR")
    assert prev is not None
    assert prev.repeats == 3


def test_repeats_stops_at_a_different_outcome(session: Session) -> None:
    same_plan = '{"entry":"100","stop":"99","target":"101"}'
    _skip_with_gates(session, "sk1", NOW - timedelta(minutes=15), plan=same_plan)
    _seed_decision(session, "h", 1, NOW - timedelta(minutes=30), "HOLD")
    _skip_with_gates(session, "sk2", NOW - timedelta(minutes=45), plan=same_plan)
    session.commit()
    prev = previous_decision(session, 1, "SOL-EUR")
    assert prev is not None and prev.repeats == 1


# ----------------------------------------------------------------- upgrade --


def _stored_v1_brief() -> dict[str, Any]:
    brief, _ = build_brief(_inputs(None), 1)  # type: ignore[arg-type]
    return brief


def test_upgrade_adds_v2_fields_and_keeps_v1_bytes(session: Session) -> None:
    brief = _stored_v1_brief()
    upgraded = upgrade_brief_to_v2(session, brief, account_id=1, settings=Settings())
    assert brief_version_of(upgraded) == 2
    for key, value in brief.items():
        if key == "previous_decision":
            continue
        assert upgraded[key] == value  # the v1 fields are untouched


def test_upgrade_never_sees_future_candles(session: Session) -> None:
    """A candle closed after the brief's ``now`` must not leak into the
    level search — here it would forge a support inside the zone."""
    brief = _stored_v1_brief()
    now = datetime.fromisoformat(brief["now"])
    # closed candles before now: one honest support at 96.0 (outside zone)
    c1h = _candles_with_marks(200, HOUR, now, lows={5: 96.0})
    repo.upsert_candles(session, "SOL-EUR", "1h", c1h)
    # a future-forming candle at a forged support that would sit in the zone
    repo.upsert_candles(
        session,
        "SOL-EUR",
        "1h",
        [
            Candle(
                open_time=now,
                open="100",
                high="100.2",
                low="98.0",
                close="100",
                volume="5",
            )
        ],
    )
    upgraded = upgrade_brief_to_v2(session, brief, account_id=1, settings=Settings())
    levels = [c["level"] for c in upgraded["trade_math"]["stop_candidates"]]
    assert 98.0 not in levels


def test_upgrade_uses_only_decisions_before_the_brief(session: Session) -> None:
    brief = _stored_v1_brief()
    now = datetime.fromisoformat(brief["now"])
    _seed_decision(session, "old", 1, now - HOUR, "HOLD")
    _seed_decision(session, "new", 1, now + HOUR, "SKIP")  # after the brief
    session.commit()
    upgraded = upgrade_brief_to_v2(session, brief, account_id=1, settings=Settings())
    prev = upgraded["previous_decision"]
    assert prev is not None and prev["action"] == "HOLD"


def test_upgrade_prev_day_low_is_a_stop_candidate(session: Session) -> None:
    """The previous day low joins the search even when no swing made it."""
    brief = _stored_v1_brief()
    now = datetime.fromisoformat(brief["now"])
    # 200 flat 1h candles with a five-bar plateau at 97.8 through the
    # previous UTC day (inside the zone); a plateau forms no strict swing
    # low, so the level only reaches the brief via the previous-day low
    c1h = _candles_with_marks(200, HOUR, now, lows=dict.fromkeys(range(31, 36), 97.8))
    repo.upsert_candles(session, "SOL-EUR", "1h", c1h)
    upgraded = upgrade_brief_to_v2(session, brief, account_id=1, settings=Settings())
    sources = {c["source"] for c in upgraded["trade_math"]["stop_candidates"]}
    assert "previous day low" in sources


def test_btc_fixture_1630_is_infeasible_naming_the_resistance(session: Session) -> None:
    """The stored BTC-EUR call of 21 Oct 16:30 UTC (price 75287): the
    nearest resistance at 75823 (+0.71%) can't reach 2R over the minimum
    stop, so the upgraded brief must say BUY is impossible."""
    brief = json.loads((FIXTURES / "brief_btc_20261021_1630.json").read_text())
    for tf in ("1h", "4h"):
        rows = json.loads((FIXTURES / f"candles_btc_1630_{tf}.json").read_text())
        candles = [
            Candle(
                open_time=datetime.fromtimestamp(r["ms"] / 1000, UTC),
                open=Decimal(r["o"]),
                high=Decimal(r["h"]),
                low=Decimal(r["l"]),
                close=Decimal(r["c"]),
                volume=Decimal(r["v"]),
            )
            for r in rows
        ]
        repo.upsert_candles(session, "BTC-EUR", tf, candles)
    btc = MarketInfo(
        market="BTC-EUR", status="trading", base="BTC", quote="EUR", tick_size=Decimal("1")
    )
    upgraded = upgrade_brief_to_v2(
        session, brief, account_id=1, settings=Settings(), market_info=btc
    )
    tm = upgraded["trade_math"]
    assert tm["buy_feasible"] is False
    assert "75823" in tm["infeasible_reason"]
    assert "+0.71%" in tm["infeasible_reason"]
    assert "2.0R" in tm["infeasible_reason"]


# ------------------------------------------------------------------ schema --


def test_v2_schema_is_strict_with_enum_signals() -> None:
    schema = decision_schema(BRIEF_VERSION)
    props = schema["properties"]
    assert "stop_basis" in props and "target_basis" in props
    assert set(schema["required"]) == set(props)
    sig_name = schema["$defs"]["ModelSignalV2"]["properties"]["name"]
    assert set(sig_name["enum"]) == {
        "rsi",
        "ema20_vs_50",
        "vwap_dist",
        "obv_trend",
        "adx",
        "atr_pct",
        "atr_rank",
        "vol_x_avg",
        "ema200_dist",
    }


def test_v1_schema_unchanged() -> None:
    assert "stop_basis" not in decision_schema(1)["properties"]


def test_parse_v2_roundtrips_and_rejects_bad_signal_name() -> None:
    text = json.dumps(
        {
            "action": "BUY",
            "confidence": 0.6,
            "entry": 100.0,
            "stop": 98.0,
            "target": 105.0,
            "stop_basis": "S1",
            "target_basis": "R2",
            "reasoning": "ok",
            "signals": [{"name": "rsi", "tf": "1h", "read": "bullish"}],
            "invalidation": "below 98",
            "invalidation_price": 98.5,
        }
    )
    out, err = parse_model_output(text, BRIEF_VERSION)
    assert err is None and isinstance(out, ModelDecisionV2)
    assert out.stop_basis == "S1" and out.target_basis == "R2"
    bad = json.loads(text)
    bad["signals"] = [{"name": "RSI 14", "tf": "1h", "read": "bullish"}]
    out, err = parse_model_output(json.dumps(bad), BRIEF_VERSION)
    assert out is None and err is not None and "schema violation" in err


# --------------------------------------------------------------- post-checks --


def _v2_brief(**tm_kw: Any) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "pair": "SOL-EUR",
        "price": 100.0,
        "tf": {"1h": {}, "4h": {}, "1d": {}},
        "trade_math": _tm(**tm_kw),
        "previous_decision": None,
        "data_age_s": {},
        "limits": {"max_age_s": {}},
        "structure": {"support": [], "resistance": []},
        "market_context": {},
        "regime": {"pair": {"trend": "up", "volatility": "normal", "age_4h": 12}},
    }
    return brief


def _buy_v2(**kw: Any) -> ModelDecisionV2:
    defaults: dict[str, Any] = {
        "action": "BUY",
        "confidence": 0.6,
        "entry": 100.0,
        "stop": 97.95,
        "target": 105.0,
        "stop_basis": "S1",
        "target_basis": "R1",
        "reasoning": "trend up",
        "signals": [{"name": "rsi", "tf": "1h", "read": "bullish"}],
        "invalidation": "below 98",
        "invalidation_price": 98.5,
    }
    defaults.update(kw)
    return ModelDecisionV2.model_validate(defaults)


def _named(checks: list[Any], name: str) -> Any:
    return next(c for c in checks if c.name == name)


def _check_v2(
    out: ModelDecisionV2 | None,
    *,
    brief: dict[str, Any] | None = None,
    meta: Any = None,
    rules: str = "BUY",
) -> Any:
    return check_output(
        out,
        None,
        brief or _v2_brief(),
        meta or _meta(),
        rules,
        Settings(),
        brief_version=BRIEF_VERSION,
    )


def test_buy_while_infeasible_fails() -> None:
    brief = _v2_brief(pair="SOL-EUR", regime=Regime(trend="down", volatility="normal", age_4h=9))
    verdict = _check_v2(_buy_v2(), brief=brief)
    assert _named(verdict.checks, "Trade math").status == "fail"
    assert verdict.action == "HOLD"


def test_buy_with_basis_not_a_listed_pair_fails() -> None:
    verdict = _check_v2(_buy_v2(stop_basis="S1", target_basis="R5"))
    check = _named(verdict.checks, "Trade math")
    assert check.status == "fail"
    assert "not a listed trade_math pair" in check.detail


def test_buy_with_matching_basis_and_levels_passes() -> None:
    brief = _v2_brief()
    pair = brief["trade_math"]["pairs"][0]
    s = next(c for c in brief["trade_math"]["stop_candidates"] if c["id"] == pair["stop_id"])
    t = next(c for c in brief["trade_math"]["target_candidates"] if c["id"] == pair["target_id"])
    out = _buy_v2(
        stop=s["price"],
        target=t["price"],
        stop_basis=pair["stop_id"],
        target_basis=pair["target_id"],
    )
    verdict = _check_v2(out, brief=brief)
    assert _named(verdict.checks, "Trade math").status == "pass"


def test_stop_off_by_more_than_one_tick_fails() -> None:
    brief = _v2_brief()
    pair = brief["trade_math"]["pairs"][0]
    s = next(c for c in brief["trade_math"]["stop_candidates"] if c["id"] == pair["stop_id"])
    t = next(c for c in brief["trade_math"]["target_candidates"] if c["id"] == pair["target_id"])
    meta = _meta(tick=0.5)
    off = _buy_v2(
        stop=s["price"] - 0.6,
        target=t["price"],
        stop_basis=pair["stop_id"],
        target_basis=pair["target_id"],
    )
    verdict = _check_v2(off, brief=brief, meta=meta)
    assert _named(verdict.checks, "Trade math").status == "fail"
    # within one tick passes
    near = _buy_v2(
        stop=s["price"] - 0.4,
        target=t["price"],
        stop_basis=pair["stop_id"],
        target_basis=pair["target_id"],
    )
    verdict = _check_v2(near, brief=brief, meta=meta)
    assert _named(verdict.checks, "Trade math").status == "pass"


def test_missing_invalidation_warns_on_v2_actions() -> None:
    for action, holding in (("BUY", False), ("SELL", True), ("HOLD", True)):
        out = ModelDecisionV2.model_validate({"action": action, "confidence": 0.5})
        verdict = _check_v2(out, meta=_meta(holding=holding))
        assert _named(verdict.checks, "Invalidation").status == "warn", action
    # a BUY missing invalidation is a warn, not the v1 Levels fail
    verdict = _check_v2(_buy_v2(invalidation=None, invalidation_price=None))
    assert _named(verdict.checks, "Invalidation").status == "warn"
    assert _named(verdict.checks, "Levels sane").status != "fail"


def test_basis_ids_on_hold_warn_and_are_ignored() -> None:
    out = ModelDecisionV2.model_validate(
        {"action": "HOLD", "confidence": 0.5, "stop_basis": "S1", "stop": 98.0}
    )
    verdict = _check_v2(out, rules="HOLD")
    check = _named(verdict.checks, "Levels sane")
    assert check.status == "warn"
    assert "stop_basis" in check.detail


def test_repeated_rejected_buy_warns() -> None:
    from app.agent.brief_v2 import PrevDecision

    brief = _v2_brief()
    brief["previous_decision"] = {
        "action": "SKIP",
        "invalidation": None,
        "at": "2026-10-11T11:00:00+00:00",
        "outcome": "skipped",
        "failed_gates": [{"name": "Min reward/risk", "value": "R/R 1.5", "limit": "R/R >= 2.00"}],
        "repeats": 2,
    }
    pair = brief["trade_math"]["pairs"][0]
    meta = _meta(
        prev=PrevDecision(
            action="SKIP",
            invalidation=None,
            outcome="skipped",
            failed_gates=brief["previous_decision"]["failed_gates"],
            buy_basis=(pair["stop_id"], pair["target_id"]),
            failed_gate_names=["Min reward/risk"],
        )
    )
    s = next(c for c in brief["trade_math"]["stop_candidates"] if c["id"] == pair["stop_id"])
    t = next(c for c in brief["trade_math"]["target_candidates"] if c["id"] == pair["target_id"])
    out = _buy_v2(
        stop=s["price"],
        target=t["price"],
        stop_basis=pair["stop_id"],
        target_basis=pair["target_id"],
    )
    verdict = _check_v2(out, brief=brief, meta=meta)
    assert _named(verdict.checks, "Repeated rejected BUY").status == "warn"
    # a different basis is not a repeat
    prev_ok = _meta(
        prev=PrevDecision(
            action="SKIP",
            invalidation=None,
            outcome="skipped",
            buy_basis=("S2", "R1"),
            failed_gate_names=["Min reward/risk"],
            failed_gates=[{"name": "Min reward/risk"}],
        )
    )
    verdict2 = _check_v2(out, brief=brief, meta=prev_ok)
    assert _named(verdict2.checks, "Repeated rejected BUY").status == "pass"


# ------------------------------------------------------------- compare tool --


def _stored_call(
    call_id: str, *, brief: dict[str, Any], parsed: dict[str, Any] | None = None
) -> LlmCallRow:
    request = {
        "model": "gpt-4o-mini",
        "instructions": "v1 prompt",
        "input": brief_json(brief),
        "temperature": 0,
        "max_output_tokens": 600,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "decision",
                "strict": True,
                "schema": decision_schema(1),
            }
        },
    }
    return LlmCallRow(
        id=call_id,
        at=NOW,
        cycle=1,
        account_id=1,
        pair="SOL-EUR",
        decision_id=None,
        feature="decision",
        replay_of=None,
        model="gpt-4o-mini",
        prompt_version="decision.v1",
        status="ok",
        attempts="[]",
        latency_ms=10,
        input_tokens=100,
        output_tokens=50,
        cost_usd="0.0001",
        cost_eur="0.0001",
        request_id="req",
        request_json=json.dumps(request),
        response_raw=b"{}".decode(),
        parsed=json.dumps(parsed or {"action": "HOLD", "confidence": 0.5}),
        input_checks="[]",
        output_checks=json.dumps(
            [
                {"name": "Signal claims", "status": "pass", "detail": ""},
                {"name": "Rules baseline", "status": "pass", "detail": "agrees (HOLD)"},
            ]
        ),
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.payloads: list[dict[str, Any]] = []

    async def call(self, payload: dict[str, Any]) -> Any:
        from app.agent.openai_client import CallResult

        self.payloads.append(payload)
        return CallResult(
            status="ok",
            request_bytes=json.dumps(payload).encode(),
            response_bytes=b"{}",
            output_text=self.text,
            input_tokens=110,
            output_tokens=60,
        )


async def test_compare_report_replays_and_resumes(session: Session) -> None:
    brief = _stored_v1_brief()
    session.add(_stored_call("c1", brief=brief))
    session.commit()
    client = _FakeClient(
        json.dumps(
            {
                "action": "HOLD",
                "confidence": 0.9,
                "entry": None,
                "stop": None,
                "target": None,
                "stop_basis": None,
                "target_basis": None,
                "reasoning": "infeasible per trade_math",
                "signals": [{"name": "rsi", "tf": "1h", "read": "bullish"}],
                "invalidation": "conditions improve",
                "invalidation_price": None,
            }
        )
    )
    report = await run_report(
        session,
        from_version="decision.v1",
        to_version="decision.v2",
        settings=Settings(),
        client=client,
    )
    assert "decision.v1 → decision.v2" in report
    assert "invalid outputs" in report
    assert "switch criteria" in report
    # the replay row was stored and upgraded
    replays = session.scalars(select(LlmCallRow).where(LlmCallRow.feature == "replay")).all()
    assert len(replays) == 1
    assert replays[0].request_json is not None
    sent = json.loads(replays[0].request_json)
    sent_brief = json.loads(sent["input"])
    assert "trade_math" in sent_brief
    assert sent["text"]["format"]["schema"]["properties"].get("stop_basis") is not None
    # resume: a second run replays nothing new
    report2 = await run_report(
        session,
        from_version="decision.v1",
        to_version="decision.v2",
        settings=Settings(),
        client=_FakeClient("{}"),
    )
    assert "replayed 0 of" in report2
    assert len(session.scalars(select(LlmCallRow).where(LlmCallRow.feature == "replay")).all()) == 1


async def test_report_criteria_print_pass_fail(session: Session) -> None:
    session.add(_stored_call("c1", brief=_stored_v1_brief()))
    session.commit()
    client = _FakeClient('{"action":"HOLD","confidence":0.9,"invalidation":"x"}')
    report = await run_report(
        session,
        from_version="decision.v1",
        to_version="decision.v2",
        settings=Settings(),
        client=client,
    )
    assert "no more invalid outputs than v1" in report
    assert "no repeated rejected BUYs" in report
    assert "no regime violations" in report
