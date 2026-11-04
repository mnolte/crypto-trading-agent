"""The cycles card: slot states, skipped-slot recording, stop moves and
the grouped decisions of the last 24 hours."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import repo
from app.agent.engine import slot_floor
from app.api.deps import AppState
from app.config import get_env
from app.db import DecisionRow, EquitySnapshotRow, MarketRow, SkippedSlotRow, TradeRow
from app.features.cycles import build_cycles_recent
from app.models import CyclesRecent
from tests.test_engine import NOW, EngineEnv

H15 = timedelta(minutes=15)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Path) -> Iterator[Path]:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(migrated_db, data / "agent.db")
    monkeypatch.setenv("TA_DB_PATH", str(data / "agent.db"))
    monkeypatch.setenv("TA_DATA_DIR", str(data))
    get_env.cache_clear()
    yield data
    get_env.cache_clear()


def _state(client: TestClient) -> AppState:
    app: object = client.app
    return cast(AppState, app.state.app_state)  # type: ignore[attr-defined]


def _decision_row(**kw: object) -> DecisionRow:
    defaults: dict[str, object] = {
        "id": "d1",
        "cycle": 1,
        "at": NOW.replace(tzinfo=None),
        "account_id": 1,
        "pair": "SOL-EUR",
        "decider": "openai",
        "model": "gpt-4o-mini",
        "prompt_version": "decision.v1",
        "action": "HOLD",
        "confidence": 0.5,
        "reasoning": "test",
        "invalidation": None,
        "signals": "[]",
        "plan": None,
        "gates": "[]",
        "status": "none",
        "execute_at": None,
        "error": None,
        "regime": "{}",
        "btc_regime": "{}",
        "invalidation_price": None,
        "risk_pct_used": "1",
        "settings_version": 1,
        "shadow_id": None,
        "stand_in": False,
        "raw_brief": None,
        "raw_response": None,
        "prefilter": True,
    }
    defaults.update(kw)
    return DecisionRow(**defaults)


def _trade_row(**kw: object) -> TradeRow:
    defaults: dict[str, object] = {
        "account_id": 1,
        "pair": "SOL-EUR",
        "qty": "10",
        "entry": "100",
        "exit": "105",
        "opened_at": NOW - timedelta(hours=1),
        "closed_at": NOW - timedelta(minutes=30),
        "fee_eur": "0.6",
        "realised_eur": "49",
        "realised_r": "1.2",
        "exit_reason": "target",
        "decision_id": "d1",
    }
    defaults.update(kw)
    return TradeRow(**defaults)


def _snapshot(at: datetime, cycle: int = 1) -> EquitySnapshotRow:
    return EquitySnapshotRow(
        account_id=1,
        at=at.replace(tzinfo=None),
        cycle=cycle,
        equity="10000",
        cash="10000",
    )


def _build(state: AppState, now: datetime, hours: int = 24) -> CyclesRecent:
    session = state.session_factory()
    try:
        return build_cycles_recent(state, session, now, hours)
    finally:
        session.close()


# ------------------------------------------------------------------ slots ==


def test_grid_has_96_past_slots_across_the_dst_change(client: TestClient) -> None:
    """2026-10-25 03:00 CEST → 02:00 CET: the UTC grid never notices —
    the window still yields exactly 96 past quarter-hour slots."""
    state = _state(client)
    now = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)  # 12:00 CEST, post-change day
    out = _build(state, now)
    past = [s for s in out.slots if s.at <= now]
    future = [s for s in out.slots if s.at > now]
    assert len(past) == 96 and len(future) == 12
    from itertools import pairwise

    ats = [s.at for s in out.slots]
    assert all(b - a == H15 for a, b in pairwise(ats))
    # Jobs land on their UTC/Amsterdam instants.
    jobs = {s.at: s.jobs for s in out.slots if s.jobs}
    assert jobs[datetime(2026, 10, 25, 1, 0, tzinfo=UTC)] == ["eligibility"]
    assert jobs[datetime(2026, 10, 25, 3, 0, tzinfo=UTC)] == ["backup"]
    # 08:00 Amsterdam = 07:00 UTC after the clock change.
    assert "briefing" in jobs[datetime(2026, 10, 25, 7, 0, tzinfo=UTC)]


def test_slot_state_precedence(client: TestClient) -> None:
    """error > exit > entry > stop_moved > no_change > paused/missed."""
    state = _state(client)
    now = NOW + timedelta(hours=3)
    base = slot_floor(now - timedelta(hours=20))
    session = state.session_factory()
    try:
        session.add(_snapshot(base + timedelta(minutes=2), cycle=10))  # no_change
        session.add(_snapshot(base + H15 + timedelta(minutes=2), cycle=11))
        repo.add_stop_move(
            session,
            1,
            "SOL-EUR",
            base + H15 + timedelta(minutes=5),
            Decimal("96"),
            Decimal("100"),
            "breakeven",
        )
        session.add(_snapshot(base + 2 * H15 + timedelta(minutes=2), cycle=12))
        session.add(_trade_row(opened_at=base + 2 * H15 + timedelta(minutes=3)))
        session.add(_snapshot(base + 3 * H15 + timedelta(minutes=2), cycle=13))
        session.add(_trade_row(closed_at=base + 3 * H15 + timedelta(minutes=3)))
        session.add(_snapshot(base + 4 * H15 + timedelta(minutes=2), cycle=14))
        session.add(
            _trade_row(
                closed_at=base + 4 * H15 + timedelta(minutes=3),
                realised_eur="-20",
                realised_r="-0.8",
            )
        )
        # The everything slot: run + error decision + entry + exit + move.
        full = base + 5 * H15
        session.add(_snapshot(full + timedelta(minutes=2), cycle=15))
        session.add(_decision_row(at=full + timedelta(minutes=2), error="1h stale"))
        session.add(_trade_row(closed_at=full + timedelta(minutes=3)))
        repo.add_stop_move(
            session,
            1,
            "ETH-EUR",
            full + timedelta(minutes=4),
            Decimal("90"),
            Decimal("95"),
            "trailing",
        )
        repo.add_skipped_slot(session, base + 6 * H15, "paused", now)
        repo.add_skipped_slot(session, base + 7 * H15, "asleep", now)
        session.commit()
    finally:
        session.close()
    out = _build(state, now)
    states = {s.at: s for s in out.slots}
    assert states[base].state == "no_change"
    assert states[base + H15].state == "stop_moved"
    assert states[base + 2 * H15].state == "entry"
    assert states[base + 3 * H15].state == "exit_gain"
    assert states[base + 4 * H15].state == "exit_loss"
    assert states[full].state == "error"
    assert states[base + 6 * H15].state == "paused"
    assert states[base + 7 * H15].state == "missed"
    assert states[base + 7 * H15].missed_reason == "asleep"
    # A slot with no cycle and no record is missed without a reason.
    assert states[base + 8 * H15].state == "missed"
    stop_slot = states[base + H15]
    assert "→" in stop_slot.changes[0].detail and "breakeven" in stop_slot.changes[0].detail


def test_run_now_cycle_lands_in_its_quarter_hour(client: TestClient) -> None:
    state = _state(client)
    now = NOW + timedelta(hours=2)
    session = state.session_factory()
    try:
        odd = slot_floor(now - timedelta(hours=4)) + timedelta(minutes=7)
        session.add(_snapshot(odd, cycle=9))
        session.commit()
    finally:
        session.close()
    out = _build(state, now)
    slot = next(s for s in out.slots if s.at == slot_floor(odd))
    assert slot.state == "no_change" and slot.cycle == 9


def test_summary_counts_and_missed_ranges(client: TestClient) -> None:
    state = _state(client)
    now = NOW + timedelta(hours=2)
    ran_slot = slot_floor(now - timedelta(hours=6))
    session = state.session_factory()
    try:
        session.add(_snapshot(ran_slot + timedelta(minutes=1), cycle=42))
        repo.add_skipped_slot(session, ran_slot + H15, "asleep", now)
        repo.add_skipped_slot(session, ran_slot + 2 * H15, "asleep", now)
        # A later run closes the contiguous missed range, so the two
        # asleep slots form their own group.
        session.add(_snapshot(ran_slot + 3 * H15 + timedelta(minutes=1), cycle=43))
        session.commit()
    finally:
        session.close()
    out = _build(state, now)
    assert out.summary.scheduled == 96 and out.summary.ran == 2
    pair = next(r for r in out.summary.missed if r.reason == "asleep")
    assert pair.count == 2 and pair.from_at == ran_slot + H15
    assert pair.to_at == ran_slot + 2 * H15
    # Unrecorded gaps before the first run group as reason None.
    assert any(r.reason is None for r in out.summary.missed)
    assert out.summary.cycle_id == 0 or isinstance(out.summary.cycle_id, int)


# --------------------------------------------------------------- decisions ==


def test_decisions_grouped_by_cycle_action_and_reason(client: TestClient) -> None:
    from app.agent.broker.paper import HeldPosition

    state = _state(client)
    assert state.broker is not None
    # ETH-EUR is held, so its HOLD group reads "positions kept".
    state.broker.positions["ETH-EUR"] = HeldPosition(
        pair="ETH-EUR",
        qty=Decimal("1"),
        entry=Decimal("3000"),
        stop=Decimal("2900"),
        target=Decimal("3300"),
        opened_at=NOW - timedelta(hours=5),
        decision_id="seed",
        last=Decimal("3000"),
        r_unit=Decimal("100"),
    )
    now = NOW + timedelta(hours=2)
    slot = slot_floor(now - timedelta(hours=3))
    session = state.session_factory()
    try:
        session.add(_snapshot(slot + timedelta(minutes=2), cycle=20))
        rows = [
            _decision_row(
                id="b1",
                at=slot + timedelta(minutes=2),
                pair="BTC-EUR",
                action="BUY",
                status="executed",
                cycle=20,
                plan='{"entry":"61200","fill_entry":"61240","stop":"60000",'
                '"target":"64000","qty":"0.0807","notional":"4940","rr":"2.1"}',
            ),
            _decision_row(
                id="h1", at=slot + timedelta(minutes=2), pair="ETH-EUR", action="HOLD", cycle=20
            ),
            _decision_row(
                id="n1",
                at=slot + timedelta(minutes=2),
                pair="ADA-EUR",
                action="HOLD",
                cycle=20,
                prefilter=False,
                reasoning="Pre-filter: conditions met 2/6; failed: C1, C3.",
            ),
            _decision_row(id="old", at=now - timedelta(hours=25), pair="SOL-EUR", action="BUY"),
        ]
        for row in rows:
            session.add(row)
        session.add(_trade_row(opened_at=slot + timedelta(minutes=3), pair="BTC-EUR"))
        session.commit()
    finally:
        session.close()
    out = _build(state, now)
    entry = next(d for d in out.decisions if d.slot_at == slot)
    assert entry.cycle == 20 and entry.decision_count == 3
    labels = [g.label for g in entry.groups]
    assert labels == ["BUY", "HOLD", "NO CALL"]
    buy = entry.groups[0]
    assert buy.detail is not None and "R/R" in buy.detail and buy.change is True
    assert entry.groups[1].reason == "positions kept"
    assert entry.groups[2].reason == "pre-filter" and entry.groups[2].count == 1
    # Changes always present on the cycle entry.
    assert any(c.kind == "entry" for c in entry.changes)
    # The 25-hour-old decision is outside the rolling window.
    assert all(d.slot_at > slot_floor(now - timedelta(hours=24)) for d in out.decisions)


def test_cycles_endpoint(client: TestClient) -> None:
    body = client.get("/api/cycles/recent?hours=24").json()
    assert len(body["slots"]) == 108  # 96 past + 12 future
    assert body["summary"]["scheduled"] == 96
    assert client.get("/api/cycles/recent?hours=99").status_code == 422


# ------------------------------------------------------------- markets card ==


def test_markets_card_last_action_skips_prefilter_rows(client: TestClient) -> None:
    from tests.test_engine import HOUR, _series

    state = _state(client)
    now = datetime.now(UTC)
    session = state.session_factory()
    try:
        row = session.get(MarketRow, "SOL-EUR") or MarketRow(market="SOL-EUR")
        row.active = True
        row.eligible = True
        session.merge(row)
        # Enough stored history that the market is not warming up.
        repo.upsert_candles(
            session, "SOL-EUR", "1h", _series(800, HOUR, now, start_price=100.0, rate=1.0001)
        )
        repo.upsert_candles(
            session,
            "SOL-EUR",
            "4h",
            _series(300, 4 * HOUR, now, start_price=100.0, rate=1.0002),
        )
        repo.upsert_candles(
            session,
            "SOL-EUR",
            "1d",
            _series(300, timedelta(days=1), now, start_price=100.0, rate=1.0002),
        )
        # Newest decision is a Rules no-call; the action must be the older BUY.
        session.add(
            _decision_row(
                id="new-nc",
                at=now - timedelta(minutes=10),
                pair="SOL-EUR",
                decider="rules",
                model=None,
                prompt_version=None,
                action="HOLD",
                prefilter=False,
                reasoning="Pre-filter: conditions met 1/6; failed: C1, C2, C3.",
            )
        )
        session.add(
            _decision_row(
                id="old-buy",
                at=now - timedelta(hours=2),
                pair="SOL-EUR",
                action="BUY",
            )
        )
        session.commit()
    finally:
        session.close()
    body = client.get("/api/markets").json()
    card = next(m for m in body if m["market"] == "SOL-EUR")
    assert card["last_action_action"] == "BUY"
    assert card["rules_state"] == "failed"
    assert card["rules_detail"] == "C1, C2, C3"


# ------------------------------------------------------------ engine wiring ==


async def test_asleep_wake_records_skipped_slots(env: Path) -> None:
    """A running engine that wakes 45+ minutes late stores the elapsed
    boundaries as "asleep" — the spec's three slots."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.engine.status = "running"
    # Last accounted boundary is 11:45; waking at 12:46 leaves 12:00,
    # 12:15 and 12:30 elapsed without a run.
    e.engine._slot_cursor = NOW - H15
    e.clock.advance(minutes=46)
    e.engine._record_missed_slots(e.clock())
    session = e.session()
    rows = list(session.scalars(select(SkippedSlotRow)).all())
    session.close()
    assert [r.reason for r in rows] == ["asleep"] * 3
    assert repo.aware_utc(rows[0].slot_at) == NOW


async def test_paused_engine_records_paused_slots(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.engine._slot_cursor = NOW - H15
    e.clock.advance(minutes=46)
    e.engine._record_missed_slots(e.clock())
    session = e.session()
    rows = list(session.scalars(select(SkippedSlotRow)).all())
    session.close()
    assert rows and all(r.reason == "paused" for r in rows)


async def test_restart_backfills_stopped_slots(env: Path) -> None:
    """Startup marks fully elapsed slots after the last recorded cycle as
    "stopped" — the process was down, not asleep."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    session = e.session()
    session.add(_snapshot(NOW - timedelta(hours=1, minutes=45), cycle=7))
    session.commit()
    e.engine._recover_startup(session)
    session.commit()
    rows = list(session.scalars(select(SkippedSlotRow)).all())
    session.close()
    # The last snapshot sits in the 10:15 slot; every fully elapsed
    # boundary after it is backfilled as "stopped".
    assert rows and all(r.reason == "stopped" for r in rows)


async def test_breakeven_move_is_recorded(env: Path) -> None:
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.held("SOL-EUR", entry="100", stop="96")
    e.market.tickers["SOL-EUR"] = Decimal("105")
    await e.engine.tick()
    session = e.session()
    rows = repo.list_stop_moves(session, 1)
    session.close()
    assert len(rows) == 1
    assert rows[0].reason == "breakeven"
    assert rows[0].old_stop == "96" and Decimal(rows[0].new_stop) > Decimal("100")


async def test_clock_offset_is_sampled_not_blocking(env: Path) -> None:
    """SimMarket has no time() — the measurement is skipped, never fails."""
    e = EngineEnv(env / "agent.db", markets=["SOL-EUR"])
    e.market.add_uptrend("SOL-EUR")
    e.market.add_zigzag("BTC-EUR")
    e.market.add_zigzag("USDC-EUR")
    await e.engine.run_cycle()
    assert e.engine.clock_offset_s is None
    assert e.engine._last_ran_slot == slot_floor(NOW)


# ------------------------------------------------------- snapshot positions ==


def test_snapshot_position_distance_fields(client: TestClient) -> None:
    """stop_dist_pct / stop_dist_atr / pnl_r / stop_above_entry / stop_near
    are computed for the snapshot's open positions."""
    from app.agent.broker.paper import HeldPosition
    from app.agent.market.base import Candle

    state = _state(client)
    assert state.agent_engine is not None and state.broker is not None
    now = datetime.now(UTC)
    session = state.session_factory()
    try:
        candles = [
            Candle(
                open_time=now - timedelta(hours=60 - i),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("98"),
                close=Decimal("100"),
                volume=Decimal("10"),
            )
            for i in range(60)
        ]
        repo.upsert_candles(session, "SOL-EUR", "1h", candles)
        session.commit()
    finally:
        session.close()
    state.broker.positions["SOL-EUR"] = HeldPosition(
        pair="SOL-EUR",
        qty=Decimal("10"),
        entry=Decimal("100"),
        stop=Decimal("101"),  # above entry → breakeven moved it
        target=Decimal("110"),
        opened_at=now - timedelta(hours=2),
        decision_id="d1",
        last=Decimal("104"),
        r_unit=Decimal("4"),
    )
    snapshot = client.get("/api/snapshot").json()
    pos = next(p for p in snapshot["positions"] if p["pair"] == "SOL-EUR")
    # (104 - 101) / 104 * 100 ≈ 2.88% above the stop; ATR(1h) of ±2 bars = 4.
    assert abs(float(pos["stop_dist_pct"]) - 3 / 104 * 100) < 0.01
    assert abs(float(pos["stop_dist_atr"]) - 0.75) < 0.05
    # unrealised 40 / (10 * 4) = 1.0R
    assert abs(float(pos["pnl_r"]) - 1.0) < 0.01
    assert pos["stop_above_entry"] is True
    assert pos["stop_near"] is True  # 0.75 ATR < 1.5
    assert snapshot["account_started_at"] is not None
    assert "needs" in snapshot and "health" in snapshot["needs"]
