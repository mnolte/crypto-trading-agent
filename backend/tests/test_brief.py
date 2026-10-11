"""The market brief: one pair's data only, all reads identical to the
engine's own signal list, and the input checks that gate the call."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent.brief import (
    BriefInputs,
    brief_json,
    build_brief,
    input_checks,
    previous_decision,
)
from app.agent.market.base import Candle
from app.agent.signals import Levels, MarketContext, PairSignals, TfValues
from app.db import DecisionRow, make_engine, make_session_factory, run_migrations
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
def session(tmp_path: Path) -> Iterator[Session]:
    db = tmp_path / "agent.db"
    run_migrations(db)
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
    assert previous_decision(session, 1, "SOL-EUR") == ("SELL", "stop hit")


def test_previous_decision_none_without_history(session: Session) -> None:
    assert previous_decision(session, 1, "SOL-EUR") is None


def test_bad_price_fails_ranges(session: Session) -> None:
    brief, meta = build_brief(_inputs(session, price=Decimal("0")))
    checks = {c.name: c for c in input_checks(brief, meta)}
    assert checks["Ranges valid"].status == "fail"
