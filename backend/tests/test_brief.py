"""The market brief: one pair's data only, all reads identical to the
engine's own signal list, and the input checks that gate the call."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent.brief import (
    BriefInputs,
    _price,
    brief_json,
    build_brief,
    input_checks,
    previous_decision,
)
from app.agent.broker.paper import HeldPosition
from app.agent.deciders.openai_decider import ModelDecision, check_output
from app.agent.market.base import Candle, MarketInfo
from app.agent.signals import Levels, MarketContext, PairSignals, TfValues
from app.db import DecisionRow, make_engine, make_session_factory
from app.models import Regime, Settings

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _candles(n: int, step: timedelta, end: datetime, price: float = 100.0) -> list[Candle]:
    """Fresh closed candles ending inside ``end``'s current bar."""
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


def _tf() -> TfValues:
    return TfValues(
        close=100.0,
        ema20=101.0,
        ema50=99.0,
        ema200=95.0,
        rsi14=58.0,
        atr14=0.5,
        atr_pct=0.5,
        atr_percentile=45.0,
        adx14=28.0,
        plus_di14=30.0,
        minus_di14=12.0,
        vwap=99.5,
        price_vs_vwap_pct=0.5,
        price_vs_ema200_pct=5.0,
        obv_trend=0.4,
        volume_ratio=1.1,
    )


def _signals(pair: str = "SOL-EUR") -> PairSignals:
    return PairSignals(
        pair=pair,
        at=NOW,
        tf={"1h": _tf(), "4h": _tf(), "1d": _tf()},
        signals=[],
        levels=Levels(
            support=[98.0, 95.0],
            resistance=[103.0, 107.0],
            prev_day_high=102.0,
            prev_day_low=97.0,
        ),
        structure=[],
        regime=Regime(trend="up", volatility="normal", age_4h=12),
        context=MarketContext(
            rs_btc_24h_pct=1.2,
            rs_btc_7d_pct=3.4,
            corr_btc_30d=0.65,
            eur_usd_1h_pct=0.01,
            eur_usd_24h_pct=-0.2,
        ),
    )


@pytest.fixture
def session(tmp_path: Path, migrated_db: Path) -> Iterator[Session]:
    db = tmp_path / "agent.db"
    shutil.copyfile(migrated_db, db)
    factory = make_session_factory(make_engine(db))
    s = factory()
    yield s
    s.close()


def _inputs(session: Session, **kw: object) -> BriefInputs:
    """A complete brief input on fresh candles for all timeframes."""
    defaults: dict[str, object] = {
        "pair": "SOL-EUR",
        "now": NOW,
        "price": Decimal("100.0"),
        "signals": _signals(),
        "holding": False,
        "settings": Settings(),
        "session": session,
        "source": "sim",
        "candles": {
            "1h": _candles(150, HOUR, NOW),
            "4h": _candles(150, 4 * HOUR, NOW),
            "1d": _candles(300, timedelta(days=1), NOW),
        },
        "context_candles": {
            "btc_1h": _candles(60, HOUR, NOW, price=60000.0),
            "btc_4h": _candles(150, 4 * HOUR, NOW, price=60000.0),
            "btc_1d": _candles(300, timedelta(days=1), NOW, price=60000.0),
            "usdc_1h": _candles(48, HOUR, NOW, price=0.9),
            "eth_4h": _candles(150, 4 * HOUR, NOW, price=3000.0),
        },
        "btc_regime": Regime(trend="up", volatility="normal", age_4h=8),
        "liquidity": {"spread_pct": 0.05, "depth_eur": 25000.0, "vol24h_vs_30d": 1.1},
        "account": {"exposure_pct": 3.0, "today_pnl_pct": -0.4, "drawdown_pct": 1.2},
        "correlated_slots_left": 1,
    }
    defaults.update(kw)
    return BriefInputs(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- structure --


def test_brief_carries_one_pair_and_json_roundtrips(session: Session) -> None:
    brief, meta = build_brief(_inputs(session))
    text = brief_json(brief)
    parsed = json.loads(text)  # no NaN/Infinity anywhere
    assert parsed["pair"] == "SOL-EUR"
    assert parsed["price"] == 100.0
    assert set(parsed["tf"]) == {"1h", "4h", "1d"}
    assert "ema200_dist" in parsed["tf"]["1d"]
    assert "ema200_dist" not in parsed["tf"]["1h"]
    assert meta.stale_tfs == []
    assert meta.holding is False


def test_brief_never_quotes_another_pairs_price(session: Session) -> None:
    brief, _ = build_brief(_inputs(session))
    text = brief_json(brief)
    # BTC context carries reads and % moves, never BTC's price level.
    ctx = json.loads(text)["market_context"]
    for item in ctx.values():
        assert set(item) <= {"value", "read", "source", "as_of"}
        assert item["value"] is None or abs(item["value"]) < 1000


def test_btc_brief_has_single_regime_block(session: Session) -> None:
    brief, _ = build_brief(_inputs(session, pair="BTC-EUR", signals=_signals("BTC-EUR")))
    assert "btc" not in brief["regime"]
    brief, _ = build_brief(_inputs(session))
    assert brief["regime"]["btc"]["trend"] == "up"


def test_missing_context_leaves_nulls_not_guesses(session: Session) -> None:
    brief, _ = build_brief(_inputs(session, context_candles={}))
    ctx = brief["market_context"]
    assert ctx["btc_trend_4h"]["value"] is None
    assert ctx["btc_trend_4h"]["read"] == "neutral"
    assert ctx["eth_btc_trend"]["value"] is None


# ------------------------------------------------------------ input checks --


def test_input_checks_all_pass_on_a_complete_brief(session: Session) -> None:
    brief, meta = build_brief(_inputs(session))
    checks = {c.name: c for c in input_checks(brief, meta)}
    for name in ("Fields complete", "Ranges valid", "Fresh", "History", "No cross-pair leak"):
        assert checks[name].status == "pass", checks[name].detail
    assert checks["Market context"].status == "pass"


def test_missing_indicator_fails_fields_complete(session: Session) -> None:
    signals = _signals()
    signals.tf["1h"].rsi14 = None
    brief, meta = build_brief(_inputs(session, signals=signals))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Fields complete"].status == "fail"
    assert "rsi (1h)" in checks["Fields complete"].detail


def test_stale_timeframe_fails_fresh(session: Session) -> None:
    candles = {
        "1h": _candles(150, HOUR, NOW - timedelta(hours=3)),
        "4h": _candles(150, 4 * HOUR, NOW),
        "1d": _candles(300, timedelta(days=1), NOW),
    }
    brief, meta = build_brief(_inputs(session, candles=candles))
    assert "1h" in meta.stale_tfs
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Fresh"].status == "fail"


def test_too_few_bars_fails_history(session: Session) -> None:
    candles = {
        "1h": _candles(150, HOUR, NOW),
        "4h": _candles(150, 4 * HOUR, NOW),
        "1d": _candles(100, timedelta(days=1), NOW),  # needs 250
    }
    brief, meta = build_brief(_inputs(session, candles=candles))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["History"].status == "fail"
    assert "1d: 100/250" in checks["History"].detail


def test_far_away_level_fails_cross_pair_leak(session: Session) -> None:
    signals = _signals()
    signals.levels.support = [200.0]  # 100% above the 100.0 price
    brief, meta = build_brief(_inputs(session, signals=signals))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["No cross-pair leak"].status == "fail"


def test_missing_context_warns_but_never_blocks(session: Session) -> None:
    brief, meta = build_brief(_inputs(session, context_candles={}))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Market context"].status == "warn"
    fails = [c for c in checks.values() if c.status == "fail"]
    assert all(c.name != "Market context" for c in fails)


def test_zero_price_does_not_crash_the_leak_check(session: Session) -> None:
    brief, meta = build_brief(_inputs(session, price=Decimal("0")))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Ranges valid"].status == "fail"  # the bad price is flagged
    assert checks["No cross-pair leak"].status == "pass"


# ----------------------------------------------------------------- repo glue --


def _seed_decision(
    session: Session,
    decision_id: str,
    account_id: int,
    at: datetime,
    action: str,
) -> None:
    session.add(
        DecisionRow(
            id=decision_id,
            cycle=1,
            at=at,
            account_id=account_id,
            pair="SOL-EUR",
            decider="openai",
            model=None,
            prompt_version=None,
            action=action,
            confidence=0.0,
            reasoning="seeded",
            invalidation="stop hit",
            signals="[]",
            gates="[]",
            status="none",
            regime='{"trend":"up","volatility":"normal","age_4h":1}',
            btc_regime='{"trend":"up","volatility":"normal","age_4h":1}',
            risk_pct_used="1",
            settings_version=1,
        )
    )


def test_previous_decision_only_sees_this_account(session: Session) -> None:
    # More than 20 newer rows from another account must not hide this
    # account's own latest decision.
    for i in range(25):
        _seed_decision(session, f"other-{i}", 2, NOW - timedelta(minutes=i), "BUY")
    _seed_decision(session, "own", 1, NOW - timedelta(hours=1), "SELL")
    session.commit()
    prev = previous_decision(session, 1, "SOL-EUR")
    assert prev is not None
    assert (prev.action, prev.invalidation) == ("SELL", "stop hit")


def test_previous_decision_none_without_history(session: Session) -> None:
    assert previous_decision(session, 1, "SOL-EUR") is None


def test_bad_price_fails_ranges(session: Session) -> None:
    brief, meta = build_brief(_inputs(session, price=Decimal("0")))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Ranges valid"].status == "fail"


def test_min_stop_pct_matches_the_fee_gate(session: Session) -> None:
    """``limits.min_stop_pct`` is the smallest stop distance that passes
    the "Costs vs risk" gate: round-trip costs under max_cost_pct_of_risk
    of the stop, so the floor is cost_pct * 100 / max_cost_pct_of_risk."""
    brief, _ = build_brief(_inputs(session))
    # Defaults: 0.25% fee + 0.05% slippage per side, costs under 30% of risk.
    assert brief["limits"]["min_stop_pct"] == pytest.approx(2.0)
    settings = Settings(risk={"max_cost_pct_of_risk": "50"})
    brief, _ = build_brief(_inputs(session, settings=settings))
    assert brief["limits"]["min_stop_pct"] == pytest.approx(1.2)


# ------------------------------------------------------------ price grid --


def _market(market: str, tick: str) -> MarketInfo:
    return MarketInfo(
        market=market,
        status="trading",
        base=market.split("-")[0],
        quote="EUR",
        tick_size=Decimal(tick),
    )


def test_price_rounds_to_the_markets_tick() -> None:
    btc = _market("BTC-EUR", "1.00")
    assert _price(61240.49, btc) == 61240.0
    assert _price(61240.5, btc) == 61241.0  # half up
    eth = _market("ETH-EUR", "0.10")
    assert _price(3011.44, eth) == 3011.4
    assert _price(3011.45, eth) == 3011.5
    pump = _market("PUMP-EUR", "0.0000001")
    assert _price(0.00383245, pump) == 0.0038325
    pepe = _market("PEPE-EUR", "0.0000000001")
    assert _price(0.0000039902, pepe) == 0.0000039902
    assert _price(0.00000399025, pepe) == 0.0000039903


def test_price_falls_back_to_five_significant_digits() -> None:
    assert _price(61240.567, None) == 61241.0
    assert _price(0.000003991, None) == 0.000003991
    assert _price(0.0, None) == 0.0
    assert _price(None, None) is None
    assert _price(float("nan"), None) is None


def test_btc_brief_quotes_whole_euro_grid_prices(session: Session) -> None:
    signals = _signals("BTC-EUR")
    signals.levels.support = [61240.49, 61100.51]
    signals.levels.resistance = [61500.26]
    brief, meta = build_brief(
        _inputs(
            session,
            pair="BTC-EUR",
            signals=signals,
            price=Decimal("61240.50"),
            market_info=_market("BTC-EUR", "1.00"),
        )
    )
    assert brief["price"] == 61241.0
    assert [lv["price"] for lv in brief["structure"]["support"]] == [61240.0, 61101.0]
    assert brief["structure"]["resistance"][0]["price"] == 61500.0
    # Meta carries the same grid values the model saw.
    assert meta.price == 61241.0
    assert meta.support == [61240.0, 61101.0]
    assert meta.resistance == [61500.0]


def test_eth_brief_quotes_tenth_euro_grid_prices(session: Session) -> None:
    brief, _ = build_brief(
        _inputs(
            session,
            pair="ETH-EUR",
            price=Decimal("3011.44"),
            market_info=_market("ETH-EUR", "0.10"),
        )
    )
    assert brief["price"] == 3011.4


def test_low_priced_pair_keeps_its_levels_and_passes_input_checks(
    session: Session,
) -> None:
    """PEPE-EUR priced at 4e-6 used to fail Ranges valid and No
    cross-pair leak: a fixed 4-decimal round flattened every level to 0."""
    pepe = _market("PEPE-EUR", "0.0000000001")
    signals = _signals("PEPE-EUR")
    signals.levels = Levels(
        support=[0.00000385, 0.00000380],
        resistance=[0.00000410, 0.00000420],
        prev_day_high=0.00000405,
        prev_day_low=0.00000378,
    )
    brief, meta = build_brief(
        _inputs(
            session,
            pair="PEPE-EUR",
            signals=signals,
            price=Decimal("0.000003991"),
            market_info=pepe,
        )
    )
    assert brief["price"] == 0.000003991
    assert brief["structure"]["support"][0]["price"] == 0.00000385
    assert brief["structure"]["prev_day_low"]["price"] == 0.00000378
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Ranges valid"].status == "pass", checks["Ranges valid"].detail
    assert checks["No cross-pair leak"].status == "pass", checks["No cross-pair leak"].detail


def test_low_priced_pair_never_uses_scientific_notation(session: Session) -> None:
    brief, _ = build_brief(
        _inputs(
            session,
            pair="PEPE-EUR",
            price=Decimal("0.000003991"),
            market_info=_market("PEPE-EUR", "0.0000000001"),
        )
    )
    text = brief_json(brief)
    assert '"price":0.000003991' in text
    assert not re.search(r"\d[eE][-+]?\d", text)
    assert json.loads(text)["price"] == 0.000003991


def test_position_prices_round_to_the_tick(session: Session) -> None:
    pos = HeldPosition(
        pair="BTC-EUR",
        qty=Decimal("0.1"),
        entry=Decimal("61197.55"),
        stop=Decimal("61000.40"),
        target=Decimal("61500.66"),
        opened_at=NOW - 10 * HOUR,
        decision_id="d1",
        last=Decimal("61200"),
    )
    brief, _ = build_brief(
        _inputs(
            session,
            pair="BTC-EUR",
            price=Decimal("61200.0"),
            holding=True,
            position=pos,
            market_info=_market("BTC-EUR", "1.00"),
        )
    )
    assert brief["position"]["entry"] == 61198.0
    assert brief["position"]["stop"] == 61000.0
    assert brief["position"]["target"] == 61501.0


def test_model_level_copied_from_a_rounded_level_passes_the_post_checks(
    session: Session,
) -> None:
    """Sideways regime: the raw resistance 61197.9 is quoted to the model
    as 61198 on BTC's 1-euro grid. A target equal to the quoted level
    must pass 'target above the nearest resistance' — the check compares
    against the same grid, not the raw float."""
    signals = _signals()
    signals.regime = Regime(trend="sideways", volatility="normal", age_4h=12)
    signals.levels.support = [61193.2]
    signals.levels.resistance = [61197.9]
    signals.tf["1h"].atr14 = 3.0
    brief, meta = build_brief(
        _inputs(
            session,
            pair="BTC-EUR",
            signals=signals,
            price=Decimal("61197.95"),
            market_info=_market("BTC-EUR", "1.00"),
        )
    )
    target = brief["structure"]["resistance"][0]["price"]
    assert target == 61198.0
    out = ModelDecision(
        action="BUY",
        confidence=0.5,
        entry=61195.0,
        stop=61193.0,
        target=target,
        invalidation_price=61194.0,
        reasoning="sideways bounce off support",
    )
    verdict = check_output(out, None, brief, meta, "HOLD", Settings())
    checks = {c.name: c for c in verdict.checks}
    assert "target above the nearest resistance" not in checks["Regime rule"].detail
    assert checks["Regime rule"].status != "fail"
