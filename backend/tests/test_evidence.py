"""Evidence (PR 8): Insights slices and calibration, the What-if replay
semantics, shadow strategies (start equity, cost-cap pause, independent
books), the Ask panel's read-only tools and the weekly review.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.broker.paper import PaperBroker
from app.agent.deciders.base import Draft
from app.agent.deciders.rules import RulesDecider
from app.agent.market.base import MarketInfo, drop_forming
from app.agent.market.sim import SimMarket
from app.agent.openai_client import CallResult
from app.agent.signals import compute_tf_values
from app.db import (
    AccountRow,
    AskConversationRow,
    DecisionRow,
    EquitySnapshotRow,
    LlmCallRow,
    PaperOrderRow,
    PositionRow,
    ShadowRow,
    TradeRow,
    WhatifRow,
    make_engine,
    make_session_factory,
)
from app.features import ask as ask_mod
from app.features import insights, shadows, whatif
from app.features.briefings import maybe_weekly_review, weekly_scheduled
from app.models import PaperSettings, Settings, WhatIfSpecIn

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)  # Wednesday 12:00 Amsterdam
HOUR = timedelta(hours=1)


@pytest.fixture
def sessions(tmp_path: Path, migrated_db: Path) -> Iterator[sessionmaker[Session]]:
    db = tmp_path / "agent.db"
    shutil.copyfile(migrated_db, db)
    yield make_session_factory(make_engine(db))


def _account(sessions: sessionmaker[Session], cash: str = "10000") -> AccountRow:
    session = sessions()
    try:
        account = repo.current_account(session, Decimal(cash), "2026-10-11")
        session.commit()
        session.expunge(account)
        return account
    finally:
        session.close()


def _infos(*markets: str) -> dict[str, MarketInfo]:
    return {
        m: MarketInfo(
            market=m,
            status="trading",
            base=m.split("-")[0],
            quote="EUR",
            min_order_base=Decimal("0.0001"),
            min_order_quote=Decimal("5"),
            tick_size=Decimal("0.01"),
            quantity_decimals=4,
        )
        for m in markets
    }


def _seed_candles(
    session: Session, market: str, sim: SimMarket, through: datetime, limit: int = 900
) -> None:
    for tf in ("1h", "4h", "1d"):
        candles = asyncio.run(sim.candles(market, cast(Any, tf), limit, end=through))
        repo.upsert_candles(session, market, cast(Any, tf), candles)


def _decision(
    *,
    id: str,
    at: datetime,
    pair: str = "BTC-EUR",
    account_id: int = 1,
    action: str = "HOLD",
    decider: str = "openai",
    status: str = "none",
    confidence: float = 0.6,
    shadow_id: str | None = None,
    plan: str | None = None,
) -> DecisionRow:
    return DecisionRow(
        id=id,
        cycle=1,
        at=at,
        account_id=account_id,
        pair=pair,
        decider=decider,
        model="gpt-4o-mini" if decider == "openai" else None,
        prompt_version="decision.v1" if decider == "openai" else None,
        action=action,
        confidence=confidence,
        reasoning="test",
        invalidation=None,
        signals="[]",
        plan=plan,
        gates="[]",
        status=status,
        execute_at=at if status == "executed" else None,
        error=None,
        regime='{"trend":"up","volatility":"normal","age_4h":2}',
        btc_regime='{"trend":"up","volatility":"normal","age_4h":2}',
        invalidation_price=None,
        risk_pct_used="1",
        settings_version=1,
        shadow_id=shadow_id,
        stand_in=False,
        raw_brief=None,
        raw_response=None,
        prefilter=True,
    )


def _outcome(
    decision_id: str,
    *,
    pair: str = "BTC-EUR",
    account_id: int = 1,
    at: datetime = NOW,
    action: str = "BUY",
    label: str = "win",
    realised_r: str | None = "1.2",
) -> Any:
    from app.db import OutcomeRow

    return OutcomeRow(
        decision_id=decision_id,
        horizon=72,
        account_id=account_id,
        pair=pair,
        action=action,
        reference_at=at,
        reference_price="100",
        forward_return_pct="2.0",
        best_move_pct="3.0",
        worst_move_pct="-0.5",
        best_move_r="1.5",
        worst_move_r="-0.2",
        first_touch=None,
        invalidated=False,
        invalidation_followup_pct=None,
        label=label,
        realised_r=realised_r,
        computed_at=at,
        method_version="v1",
    )


# ------------------------------------------------------------------ insights


def test_insights_slices_by_pair(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        _account(sessions)
        session = sessions()
        for i in range(12):
            session.add(
                _decision(
                    id=f"b{i}",
                    at=NOW - timedelta(hours=i),
                    action="BUY",
                    status="executed",
                )
            )
            session.add(
                _outcome(
                    f"b{i}",
                    at=NOW - timedelta(hours=i),
                    label="win" if i % 2 else "loss",
                )
            )
        for i in range(11):
            session.add(
                _decision(
                    id=f"s{i}",
                    at=NOW - timedelta(hours=i),
                    pair="SOL-EUR",
                    action="BUY",
                    status="executed",
                )
            )
            session.add(
                _outcome(
                    f"s{i}",
                    pair="SOL-EUR",
                    at=NOW - timedelta(hours=i),
                    label="loss",
                    realised_r="-1.0",
                )
            )
        session.commit()

        out = insights.compute_insights(session, now=NOW, settings=Settings(), dimension="pair")
        by_key = {s.key: s for s in out.slices}
        assert set(by_key) == {"BTC-EUR", "SOL-EUR"}
        assert by_key["BTC-EUR"].decisions == 12
        assert by_key["BTC-EUR"].buys == 12
        assert by_key["BTC-EUR"].buy_win_rate_pct == pytest.approx(50.0)
        assert by_key["SOL-EUR"].buy_win_rate_pct == 0.0
        assert by_key["SOL-EUR"].avg_r == pytest.approx(-1.0)
    finally:
        session.close()


def test_insights_hides_slices_under_ten(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        _account(sessions)
        session = sessions()
        for i in range(9):
            session.add(
                _decision(id=f"x{i}", at=NOW - timedelta(hours=i), action="BUY", status="executed")
            )
            session.add(_outcome(f"x{i}", at=NOW - timedelta(hours=i)))
        session.commit()
        out = insights.compute_insights(session, now=NOW, settings=Settings(), dimension="pair")
        assert out.slices == []  # 9 decisions is under the slice bar
        assert out.total_decisions == 9
        # The hidden slice still counts toward "N more to go".
        assert out.largest_slice_label == "BTC-EUR"
        assert out.largest_slice_decisions == 9
        assert out.decisions_needed == 21
    finally:
        session.close()


def test_insights_decisions_needed_with_no_decisions(
    sessions: sessionmaker[Session],
) -> None:
    session = sessions()
    try:
        _account(sessions)
        session = sessions()
        out = insights.compute_insights(session, now=NOW, settings=Settings(), dimension="pair")
        assert out.total_decisions == 0
        assert out.largest_slice_label is None
        assert out.largest_slice_decisions == 0
        assert out.decisions_needed == 30
    finally:
        session.close()


def test_insights_atr_series_matches_frozen_read(
    sessions: sessionmaker[Session],
) -> None:
    """The per-pair ATR% series returns the same percentile the old
    per-hour candle recompute produced."""
    sim = SimMarket(seed=7, markets=["BTC-EUR"], now=NOW)
    session = sessions()
    try:
        _seed_candles(session, "BTC-EUR", sim, NOW)
        series = insights._AtrPctSeries.load(session, "BTC-EUR", NOW - 100 * HOUR, NOW - HOUR)
        for hour in (NOW - 50 * HOUR, NOW - 30 * HOUR, NOW - HOUR):
            candles = drop_forming(
                repo.load_candles(session, "BTC-EUR", "1h", 800, end=hour + HOUR),
                "1h",
                hour + HOUR,
            )
            expected = (
                compute_tf_values(candles, "1h").atr_percentile if len(candles) >= 16 else None
            )
            if expected is None:
                assert series.at(hour) is None
            else:
                assert series.at(hour) == pytest.approx(expected)
        # Before the first candle there is nothing to read.
        assert series.at(NOW - 2000 * HOUR) is None
    finally:
        session.close()


def test_insights_calibration_bands(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        _account(sessions)
        session = sessions()
        for i in range(10):
            session.add(
                _decision(
                    id=f"c{i}",
                    at=NOW - timedelta(hours=i),
                    action="BUY",
                    status="executed",
                    confidence=0.85,
                )
            )
            session.add(
                _outcome(
                    f"c{i}",
                    at=NOW - timedelta(hours=i),
                    label="win" if i < 7 else "loss",
                )
            )
        session.commit()
        out = insights.compute_insights(
            session, now=NOW, settings=Settings(), dimension="confidence"
        )
        top = next(b for b in out.calibration if b.label == "0.8+")
        assert top.buys == 10
        assert top.stated_pct == pytest.approx(85.0)
        assert top.actual_pct == pytest.approx(70.0)
        assert out.calibration_gap_pts is not None
    finally:
        session.close()


# ------------------------------------------------------------------- shadows


def _live_account(session: Session, cash: str = "10000") -> AccountRow:
    account = repo.current_account(session, Decimal(cash), "2026-10-11")
    session.flush()
    return account


def _runner(
    sessions: sessionmaker[Session],
    sim: SimMarket,
    settings: Settings,
    draft: Draft,
    monkeypatch: pytest.MonkeyPatch,
    price_fn: Any | None = None,
) -> shadows.ShadowRunner:
    """A runner whose shadows all answer with the scripted draft."""

    class _FakeDecider:
        def __init__(self, **_kw: Any) -> None:
            self.rules = RulesDecider()

        async def decide(self, _ctx: Any) -> Draft:
            return draft

    monkeypatch.setattr(shadows, "OpenAIDecider", _FakeDecider)
    return shadows.ShadowRunner(
        session_factory=sessions,
        market_client=sim,
        market_rules=_infos("BTC-EUR", "SOL-EUR", "USDC-EUR"),
        live_settings=lambda: settings,
        clock=lambda: sim.now,
        price_fn=price_fn or sim.ticker_price,
    )


def test_shadow_starts_flat_with_live_equity(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        account = _live_account(session, "10000")
        session.commit()
        row = shadows.create_shadow(
            session,
            live_account=account,
            live_equity=Decimal("10000"),
            name=None,
            model="gpt-4o-mini",
            prompt_version="decision.v1",
            overrides={},
            daily_call_limit=300,
            now=NOW,
        )
        session.commit()
        shadow_acc = session.get(AccountRow, row.account_id)
        assert shadow_acc is not None
        assert Decimal(shadow_acc.cash) == Decimal("10000")
        assert row.start_equity == "10000"
        assert row.name == "Shadow A"
        assert (
            session.scalar(
                select(func.count())
                .select_from(PositionRow)
                .where(PositionRow.account_id == row.account_id)
            )
            == 0
        )
    finally:
        session.close()


def test_shadow_sell_on_own_position_while_live_flat(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shadow owns its book: it can SELL a position even when the live
    account holds nothing on that pair."""
    sim = SimMarket(seed=7, markets=["BTC-EUR", "USDC-EUR"], now=NOW)
    session = sessions()
    try:
        account = _live_account(session)
        _seed_candles(session, "BTC-EUR", sim, NOW)
        _seed_candles(session, "USDC-EUR", sim, NOW)
        row = shadows.create_shadow(
            session,
            live_account=account,
            live_equity=Decimal("10000"),
            name=None,
            model="gpt-4o-mini",
            prompt_version="decision.v1",
            overrides={},
            daily_call_limit=300,
            now=NOW - HOUR,
        )
        # The shadow holds BTC-EUR; the live account is flat.
        shadow_account = session.get(AccountRow, row.account_id)
        assert shadow_account is not None
        broker = PaperBroker(shadow_account, PaperSettings())
        broker.buy(
            session,
            "BTC-EUR",
            Decimal("100"),
            Decimal("10"),
            Decimal("95"),
            Decimal("110"),
            "seed-dec",
            NOW - HOUR,
        )
        broker.persist(session, NOW - HOUR)
        session.commit()
    finally:
        session.close()

    draft = Draft(
        action="SELL",
        confidence=0.8,
        reasoning="exit",
        signals=[],
        decider="openai",
        model="gpt-4o-mini",
        prompt_version="decision.v1",
    )
    runner = _runner(sessions, sim, Settings(active_markets=["BTC-EUR"]), draft, monkeypatch)
    asyncio.run(runner.run_cycle(NOW, 1))

    session = sessions()
    try:
        sell = session.scalars(
            select(DecisionRow).where(DecisionRow.shadow_id == row.id, DecisionRow.action == "SELL")
        ).all()
        assert len(sell) == 1
        assert sell[0].status == "executed"
        # Position closed on the shadow's book only.
        assert session.get(PositionRow, ("BTC-EUR", row.account_id)) is None
        assert (
            session.scalar(
                select(func.count()).select_from(PositionRow).where(PositionRow.account_id == 1)
            )
            == 0
        )
    finally:
        session.close()


def test_shadow_pauses_at_80_pct_but_watcher_still_fires(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 80% of the monthly cap the shadow stops deciding (no model calls,
    no Rules stand-in), but the 30-second watcher still exits positions."""
    sim = SimMarket(seed=7, markets=["BTC-EUR", "USDC-EUR"], now=NOW)
    session = sessions()
    try:
        account = _live_account(session)
        _seed_candles(session, "BTC-EUR", sim, NOW)
        _seed_candles(session, "USDC-EUR", sim, NOW)
        row = shadows.create_shadow(
            session,
            live_account=account,
            live_equity=Decimal("10000"),
            name=None,
            model="gpt-4o-mini",
            prompt_version="decision.v1",
            overrides={},
            daily_call_limit=300,
            now=NOW - 2 * HOUR,
        )
        shadow_account = session.get(AccountRow, row.account_id)
        assert shadow_account is not None
        broker = PaperBroker(shadow_account, PaperSettings())
        broker.buy(
            session,
            "BTC-EUR",
            Decimal("100"),
            Decimal("10"),
            Decimal("95"),
            Decimal("2000"),
            "seed-dec",
            NOW - 2 * HOUR,
        )
        broker.persist(session, NOW - 2 * HOUR)
        # Spend ≥ 80% of the €10 cap on the live account.
        session.add(
            LlmCallRow(
                id="call-cost",
                at=NOW - HOUR,
                cycle=1,
                account_id=1,
                pair="BTC-EUR",
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
                cost_usd="8.50",
                cost_eur="8.50",
                request_id="r1",
                request_json="{}",
                response_raw="{}",
                parsed=None,
                input_checks="[]",
                output_checks="[]",
                verdict=None,
                verdict_note=None,
                verdict_at=None,
            )
        )
        session.commit()
    finally:
        session.close()

    async def _low_price(_pair: str) -> Decimal:
        return Decimal("1")  # far below the stored stop — the stop fires

    runner = _runner(
        sessions,
        sim,
        Settings(active_markets=["BTC-EUR"]),
        Draft(action="BUY", confidence=0.9, reasoning="b", signals=[], decider="openai"),
        monkeypatch,
        price_fn=_low_price,
    )
    asyncio.run(runner.run_cycle(NOW, 1))

    session = sessions()
    try:
        assert (
            session.scalar(
                select(func.count()).select_from(DecisionRow).where(DecisionRow.shadow_id == row.id)
            )
            == 0
        )  # no decisions while paused — no Rules stand-in either
        assert (
            session.scalar(
                select(func.count())
                .select_from(LlmCallRow)
                .where(LlmCallRow.account_id == row.account_id)
            )
            == 0
        )  # and no model calls
        shadow_row = session.get(ShadowRow, row.id)
        assert shadow_row is not None
        pauses = json.loads(shadow_row.pauses)
        assert pauses[-1]["end"] is None  # pause window open
    finally:
        session.close()

    # The watcher still manages the open position: a dip to the stop fills.
    asyncio.run(runner.tick(NOW))
    session = sessions()
    try:
        assert session.get(PositionRow, ("BTC-EUR", row.account_id)) is None
        exit_trade = session.scalars(
            select(TradeRow).where(TradeRow.account_id == row.account_id)
        ).all()
        assert len(exit_trade) == 1
        assert exit_trade[0].exit_reason == "stop"
    finally:
        session.close()


# ------------------------------------------------------------------ what-if --


def _whatif_row(session: Session, start: datetime, end: datetime, overrides: dict[str, Any]) -> Any:
    from app.db import WhatifRow

    row = WhatifRow(
        id="wi-1",
        created_at=NOW,
        updated_at=NOW,
        name=None,
        spec=WhatIfSpecIn.model_validate(
            {"from": start.isoformat(), "to": end.isoformat(), "overrides": overrides}
        ).model_dump_json(),
        status="queued",
        progress=0.0,
        result=None,
        saved=False,
        error=None,
    )
    session.add(row)
    session.flush()
    return row


def _trade_row(
    *,
    id: int | None,
    decision_id: str,
    pair: str = "BTC-EUR",
    account_id: int = 1,
    opened_at: datetime,
    closed_at: datetime | None,
    entry: str = "100",
    exit_: str | None = "104",
) -> TradeRow:
    return TradeRow(
        id=id,
        account_id=account_id,
        pair=pair,
        qty="10",
        entry=entry,
        exit=exit_ or "104",
        opened_at=opened_at,
        closed_at=closed_at or opened_at,
        fee_eur="1",
        realised_eur="39",
        realised_r="1.3",
        initial_stop="97",
        r_unit="3",
        exit_reason="target",
        decision_id=decision_id,
    )


def test_whatif_vetoed_decision_never_fills(sessions: sessionmaker[Session]) -> None:
    sim = SimMarket(seed=7, markets=["BTC-EUR", "USDC-EUR"], now=NOW)
    start = NOW - 6 * HOUR
    session = sessions()
    try:
        _live_account(session)
        _seed_candles(session, "BTC-EUR", sim, NOW)
        _seed_candles(session, "USDC-EUR", sim, NOW)
        session.add(_decision(id="v1", at=NOW - 4 * HOUR, action="BUY", status="vetoed"))
        session.commit()
        row = _whatif_row(session, start, NOW, {})
        asyncio.run(whatif.run_whatif(session, row, _infos("BTC-EUR")))
        session.commit()
    finally:
        session.close()

    session = sessions()
    try:
        row = session.get(WhatifRow, "wi-1")
        assert row is not None
        assert row.status == "done", row.error
        result = json.loads(row.result)
        assert result["added"] == []
        assert result["removed"] == []
        assert result["trades"] == 0
        # Nothing leaked into the real tables.
        assert session.scalar(select(func.count()).select_from(TradeRow)) == 0
    finally:
        session.close()


def test_whatif_unchanged_settings_reproduce_reality(
    sessions: sessionmaker[Session],
) -> None:
    sim = SimMarket(seed=7, markets=["BTC-EUR", "USDC-EUR"], now=NOW)
    start = NOW - 6 * HOUR
    opened = NOW - 4 * HOUR
    closed = NOW - HOUR
    session = sessions()
    try:
        _live_account(session)
        _seed_candles(session, "BTC-EUR", sim, NOW)
        _seed_candles(session, "USDC-EUR", sim, NOW)
        session.add(
            EquitySnapshotRow(account_id=1, cycle=0, at=start, equity="10000", cash="10000")
        )
        plan = json.dumps(
            {
                "entry": "100",
                "fill_entry": "100.03",
                "stop": "95",
                "target": "115",
                "qty": "10",
                "notional": "1000.30",
                "rr": "2.97",
            }
        )
        session.add(_decision(id="buy-1", at=opened, action="BUY", status="executed", plan=plan))
        session.add(_trade_row(id=None, decision_id="buy-1", opened_at=opened, closed_at=closed))
        session.commit()
        row = _whatif_row(session, start, NOW, {})
        asyncio.run(whatif.run_whatif(session, row, _infos("BTC-EUR")))
        session.commit()
    finally:
        session.close()

    session = sessions()
    try:
        row = session.get(WhatifRow, "wi-1")
        assert row is not None
        assert row.status == "done", row.error
        result = json.loads(row.result)
        assert result["removed"] == []  # the real fill was kept
        assert result["trades"] == 1
        assert len(result["equity"]) >= 2
        # Real tables are untouched.
        assert session.scalar(select(func.count()).select_from(TradeRow)) == 1
    finally:
        session.close()


def test_whatif_rejects_unsafe_override(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        with pytest.raises(whatif.WhatIfSpecError):
            whatif.apply_overrides(Settings(), {"execution_mode": "auto"})
        with pytest.raises(whatif.WhatIfSpecError):
            whatif.apply_overrides(Settings(), {"model.openai_api_key": "x"})
    finally:
        session.close()


# ---------------------------------------------------------------------- ask --


def test_ask_unknown_tool_errors(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        data, cites = ask_mod.run_tool(session, "set_settings", {}, 1, None)
        assert "error" in data
        assert cites == []
    finally:
        session.close()


def test_ask_daily_limit_refuses(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        account = _live_account(session)
        settings = Settings()
        settings.learning.ask_daily_limit = 2
        conv = AskConversationRow(
            id="c1",
            created_at=NOW,
            updated_at=NOW,
            title="t",
            messages=json.dumps(
                [
                    {"role": "user", "text": "q1", "at": NOW.isoformat()},
                    {"role": "user", "text": "q2", "at": NOW.isoformat()},
                ]
            ),
        )
        session.add(conv)
        session.commit()
        with pytest.raises(ask_mod.AskRefusal):
            asyncio.run(
                ask_mod.answer_question(
                    session,
                    now=NOW,
                    account_id=account.id,
                    broker=None,
                    settings=settings,
                    question="again?",
                    conversation=None,
                    context=None,
                    key_fn=lambda: None,
                )
            )
    finally:
        session.close()


def test_ask_no_key_persists_conversation(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        account = _live_account(session)
        session.commit()
        session = sessions()
        msg, conv, call_ids = asyncio.run(
            ask_mod.answer_question(
                session,
                now=NOW,
                account_id=account.id,
                broker=None,
                settings=Settings(),
                question="Why no trade?",
                conversation=None,
                context=None,
                key_fn=lambda: None,
            )
        )
        session.commit()
        assert msg.source == "template"
        assert call_ids == []
        stored = session.get(AskConversationRow, conv.id)
        assert stored is not None
        messages = json.loads(stored.messages)
        assert messages[0]["role"] == "user"
        assert messages[-1]["role"] == "assistant"
    finally:
        session.close()


def test_ask_answer_with_tool_call(sessions: sessionmaker[Session]) -> None:
    """A scripted client that first calls get_decisions, then answers with a
    number straight from the tool output passes the number check."""
    session = sessions()
    try:
        account = _live_account(session)
        session.add(_decision(id="d1", at=NOW - HOUR, action="HOLD", status="none"))
        session.commit()

        call_log: list[dict[str, Any]] = []

        class _Client:
            def __init__(self, _key: str) -> None:
                self.round = 0

            async def call(self, payload: dict[str, Any]) -> CallResult:
                call_log.append(payload)
                self.round += 1
                if self.round == 1:
                    body = {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "get_decisions",
                                "arguments": "{}",
                                "call_id": "fc-1",
                            }
                        ]
                    }
                    return CallResult(
                        status="ok",
                        request_bytes=json.dumps(payload).encode(),
                        response_bytes=json.dumps(body).encode(),
                        output_text=None,
                    )
                return CallResult(
                    status="ok",
                    request_bytes=json.dumps(payload).encode(),
                    response_bytes=b'{"ok":true}',
                    output_text="The last decision was a HOLD on BTC-EUR.",
                )

            async def aclose(self) -> None:
                pass

        session = sessions()
        msg, _conv, call_ids = asyncio.run(
            ask_mod.answer_question(
                session,
                now=NOW,
                account_id=account.id,
                broker=None,
                settings=Settings(),
                question="What was the last decision?",
                conversation=None,
                context=None,
                key_fn=lambda: "sk-test",
                client_factory=lambda k: cast(Any, _Client(k)),
            )
        )
        assert msg.source == "model"
        assert "HOLD" in msg.text
        assert len(call_ids) == 2  # tool round + answer round
        assert msg.citations and msg.citations[0].kind == "decision"
        # The stateless Responses API needs the function_call item itself
        # echoed before its output, or the follow-up call 400s.
        kinds = [i.get("type") for i in call_log[1]["input"]]
        assert "function_call" in kinds
        assert "function_call_output" in kinds
    finally:
        session.close()


# ------------------------------------------------------------ weekly review --


def test_weekly_scheduled(sessions: sessionmaker[Session]) -> None:
    # Monday 08:00 Amsterdam is 06:00 UTC in October (CEST).
    sched = weekly_scheduled(NOW, "mon_08")
    assert sched is not None
    assert sched.weekday() == 0 and sched.hour == 6
    assert weekly_scheduled(NOW, "off") is None


def test_weekly_review_fallback_and_persist(sessions: sessionmaker[Session]) -> None:
    # Monday 06:30 UTC = 08:30 Amsterdam — within the missed-run grace.
    mon = datetime(2026, 10, 12, 6, 30, tzinfo=UTC)
    session = sessions()
    try:
        account = _live_account(session)
        broker = PaperBroker(account, PaperSettings())
        session.commit()
        session = sessions()
        sched = weekly_scheduled(mon, "mon_08")
        assert sched is not None and sched <= mon
        row = asyncio.run(
            maybe_weekly_review(
                session,
                now=mon,
                account=account,
                broker=broker,
                settings=Settings(),
                dispatcher_notify=None,
            )
        )
        assert row is not None
        assert row.type == "weekly"
        assert row.source == "template"  # no OpenAI key in tests
        session.commit()
        # Second call this week does nothing.
        row2 = asyncio.run(
            maybe_weekly_review(
                session,
                now=mon + timedelta(minutes=30),
                account=account,
                broker=broker,
                settings=Settings(),
                dispatcher_notify=None,
            )
        )
        assert row2 is None
    finally:
        session.close()


def test_shadow_b_limit_entries_leave_live_untouched(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shadow B = decision.v1 + limit entries: the override rests the
    shadow's entry as a post-only order while live stays on market and
    the live book is never touched."""
    sim = SimMarket(seed=7, markets=["BTC-EUR", "USDC-EUR"], now=NOW)
    live_settings = Settings(active_markets=["BTC-EUR"])
    session = sessions()
    try:
        account = _live_account(session)
        _seed_candles(session, "BTC-EUR", sim, NOW)
        _seed_candles(session, "USDC-EUR", sim, NOW)
        row = shadows.create_shadow(
            session,
            live_account=account,
            live_equity=Decimal("10000"),
            name=None,
            model="gpt-4o-mini",
            prompt_version="decision.v1",
            overrides={"paper.entry_orders": "limit"},
            daily_call_limit=300,
            now=NOW - HOUR,
        )
        session.commit()
    finally:
        session.close()

    last = asyncio.run(sim.ticker_price("BTC-EUR"))
    draft = Draft(
        action="BUY",
        confidence=0.8,
        reasoning="b",
        signals=[],
        decider="openai",
        model="gpt-4o-mini",
        prompt_version="decision.v1",
        entry=last,
        stop=last * Decimal("0.97"),
        target=last * Decimal("1.10"),
    )
    runner = _runner(sessions, sim, live_settings, draft, monkeypatch)
    asyncio.run(runner.run_cycle(NOW, 1))

    session = sessions()
    try:
        order = session.scalar(
            select(PaperOrderRow).where(PaperOrderRow.account_id == row.account_id)
        )
        assert order is not None
        assert order.leg == "entry" and order.status == "open"
        assert Decimal(order.limit_price) <= last  # post-only: never above the ask
        # The shadow's book waits; live holds nothing and placed nothing.
        assert session.get(PositionRow, ("BTC-EUR", row.account_id)) is None
        assert (
            session.scalar(
                select(func.count()).select_from(PaperOrderRow).where(PaperOrderRow.account_id == 1)
            )
            == 0
        )
        out = shadows.shadow_out(session, row, 1, NOW)
        assert out.entry_orders == "limit"
        assert out.orders_placed == 1 and out.orders_filled == 0
    finally:
        session.close()
    # The override never leaks into the live settings.
    assert live_settings.paper.entry_orders == "market"
