"""PR-4 screen endpoints: candles, decisions, indicators, performance,
trades, activity, flatten preview, prompts, exports and delete-all-data.

The startup backfill task is stubbed out so a test's database contains only
what the test seeded — no reliance on background timing.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import repo
from app.agent.broker.paper import HeldPosition
from app.agent.market.base import Candle
from app.agent.market.sim import SimMarket
from app.api.deps import AppState
from app.db import (
    AccountRow,
    ActivityRow,
    DecisionRow,
    EquitySnapshotRow,
    LlmCallRow,
    OutcomeFailureRow,
    TradeRow,
    utcnow,
)
from app.main import create_app

HOUR = timedelta(hours=1)


def _closed_hour() -> datetime:
    """The open time of the most recently closed hourly bar."""
    return utcnow().replace(minute=0, second=0, microsecond=0) - HOUR


def _series(
    n: int,
    step: timedelta,
    end: datetime,
    *,
    start_price: float = 100.0,
    rate: float = 1.0008,
    wick: float = 0.004,
    volume: float = 100.0,
) -> list[Candle]:
    """A synthetic closed-candle series whose last bar opens at ``end``."""
    out: list[Candle] = []
    price = start_price
    for i in range(n):
        price *= rate
        out.append(
            Candle(
                open_time=end - (n - 1 - i) * step,
                open=Decimal(f"{price / rate:.6f}"),
                high=Decimal(f"{price * (1 + wick):.6f}"),
                low=Decimal(f"{price * (1 - wick):.6f}"),
                close=Decimal(f"{price:.6f}"),
                volume=Decimal(f"{volume}"),
            )
        )
    return out


@pytest.fixture
def screens_client(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, SimMarket]]:
    """The app on the simulated market with the startup backfill stubbed —
    the database contains only what a test seeds."""
    sim = SimMarket(seed=11)
    monkeypatch.setattr("app.api.deps.make_market_client", lambda settings: sim)

    async def _no_refresh(state: AppState) -> None:
        return None

    monkeypatch.setattr("app.main.refresh_market_data", _no_refresh)
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c, sim


def _state(client: TestClient) -> AppState:
    app = cast(FastAPI, client.app)
    state: AppState = app.state.app_state
    return state


def _db(client: TestClient) -> Session:
    return _state(client).session_factory()


def _account_id(client: TestClient) -> int:
    return _state(client).account_id


def _token(client: TestClient) -> str:
    token: str = client.get("/api/session").json()["token"]
    return token


def _add_decision(
    session: Session,
    decision_id: str,
    at: datetime,
    *,
    account_id: int,
    pair: str = "BTC-EUR",
    action: str = "BUY",
    status: str = "executed",
    cycle: int = 1,
    confidence: float = 0.55,
    plan: dict[str, Any] | None = None,
    prefilter: bool = True,
    commit: bool = True,
) -> DecisionRow:
    row = DecisionRow(
        id=decision_id,
        cycle=cycle,
        at=at,
        account_id=account_id,
        pair=pair,
        decider="rules",
        model=None,
        prompt_version="decision.v1",
        action=action,
        confidence=confidence,
        reasoning="seeded reasoning",
        invalidation="invalid below stop",
        signals=json.dumps([{"name": "RSI 14", "tf": "1h", "read": "bullish"}]),
        plan=json.dumps(plan) if plan else None,
        gates=json.dumps([{"name": "risk gate", "status": "pass", "value": "ok"}]),
        status=status,
        execute_at=None,
        error=None,
        regime=json.dumps({"trend": "up", "volatility": "normal", "age_4h": 4}),
        btc_regime=json.dumps({"trend": "up", "volatility": "normal", "age_4h": 2}),
        invalidation_price=None,
        risk_pct_used="0.75",
        settings_version=1,
        shadow_id=None,
        stand_in=False,
        raw_brief=None,
        raw_response=None,
        prefilter=prefilter,
    )
    session.add(row)
    if commit:
        session.commit()
    return row


def _add_trade(
    session: Session,
    *,
    account_id: int,
    pair: str = "BTC-EUR",
    opened: datetime,
    closed: datetime,
    entry: str = "100",
    exit_: str = "110",
    qty: str = "0.5",
    fee: str = "0.5",
    realised: str = "4.5",
    realised_r: str = "0.9",
    reason: str = "target",
    decision_id: str = "d-buy",
) -> TradeRow:
    row = TradeRow(
        account_id=account_id,
        pair=pair,
        qty=qty,
        entry=entry,
        exit=exit_,
        opened_at=opened,
        closed_at=closed,
        fee_eur=fee,
        realised_eur=realised,
        realised_r=realised_r,
        exit_reason=reason,
        decision_id=decision_id,
    )
    session.add(row)
    session.commit()
    return row


# ------------------------------------------------------------------- candles


def test_candles_returns_seeded_bars(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    end = _closed_hour()
    session = _db(client)
    try:
        repo.upsert_candles(session, "BTC-EUR", "1h", _series(50, HOUR, end))
    finally:
        session.close()
    data = client.get("/api/candles/BTC-EUR?tf=1h&bars=200").json()
    assert data["pair"] == "BTC-EUR" and data["tf"] == "1h"
    assert len(data["candles"]) == 50
    times = [c["time"] for c in data["candles"]]
    assert times == sorted(times)
    assert data["position"] is None


def test_candles_drops_the_forming_bar(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    end = _closed_hour()
    session = _db(client)
    try:
        bars = _series(10, HOUR, end)
        bars.append(
            Candle(
                open_time=end + HOUR,  # still forming right now
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=Decimal("1"),
            )
        )
        repo.upsert_candles(session, "BTC-EUR", "1h", bars)
    finally:
        session.close()
    data = client.get("/api/candles/BTC-EUR?tf=1h").json()
    assert len(data["candles"]) == 10
    assert data["candles"][-1]["time"].startswith(end.isoformat()[:19])


def test_candles_marks_trades_and_position_lines(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    end = _closed_hour()
    account_id = _account_id(client)
    session = _db(client)
    try:
        repo.upsert_candles(session, "BTC-EUR", "1h", _series(50, HOUR, end))
        _add_trade(session, account_id=account_id, opened=end - 20 * HOUR, closed=end - 5 * HOUR)
        engine = _state(client).agent_engine
        assert engine is not None
        engine.broker.positions["ETH-EUR"] = HeldPosition(
            pair="ETH-EUR",
            qty=Decimal("1"),
            entry=Decimal("50"),
            stop=Decimal("45"),
            target=Decimal("60"),
            opened_at=end - 10 * HOUR,
            decision_id="d-buy",
            last=Decimal("55"),
        )
    finally:
        session.close()

    data = client.get("/api/candles/BTC-EUR?tf=1h").json()
    kinds = sorted(m["kind"] for m in data["markers"])
    assert kinds == ["buy", "sell"]

    eth = client.get("/api/candles/ETH-EUR?tf=1h").json()
    assert eth["position"] == {"entry": "50", "stop": "45", "target": "60"}


# ----------------------------------------------------------------- decisions


def test_decisions_list_filters_and_pagination(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    base = _closed_hour() - 10 * HOUR
    try:
        _add_decision(session, "d1", base, account_id=account_id, pair="BTC-EUR", action="BUY")
        _add_decision(
            session,
            "d2",
            base + HOUR,
            account_id=account_id,
            pair="ETH-EUR",
            action="HOLD",
            status="none",
            prefilter=False,
        )
        _add_decision(
            session,
            "d3",
            base + 2 * HOUR,
            account_id=account_id,
            pair="BTC-EUR",
            action="SKIP",
            status="none",
        )
    finally:
        session.close()

    items = client.get("/api/decisions").json()["items"]
    assert [d["id"] for d in items] == ["d3", "d2", "d1"]  # newest first
    by_id = {d["id"]: d for d in items}
    assert by_id["d2"]["prefilter"] is False  # prefilter survives decision_out
    assert by_id["d1"]["prefilter"] is True

    btc = client.get("/api/decisions?pair=BTC-EUR").json()["items"]
    assert [d["id"] for d in btc] == ["d3", "d1"]

    buys = client.get("/api/decisions?outcome=buy").json()["items"]
    assert [d["id"] for d in buys] == ["d1"]

    filled = client.get("/api/decisions?outcome=filled").json()["items"]
    assert [d["id"] for d in filled] == ["d1"]  # only executed decisions

    older = client.get("/api/decisions", params={"before": (base + 2 * HOUR).isoformat()}).json()[
        "items"
    ]
    assert [d["id"] for d in older] == ["d2", "d1"]


def test_decisions_stay_within_the_account(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    base = _closed_hour() - 10 * HOUR
    try:
        _add_decision(session, "own", base, account_id=account_id)
        _add_decision(session, "other", base + HOUR, account_id=account_id + 1)
    finally:
        session.close()

    items = client.get("/api/decisions").json()["items"]
    assert [d["id"] for d in items] == ["own"]  # the other account's row stays out
    assert client.get("/api/decisions/other").status_code == 404


def test_decision_detail_includes_stepper_and_fill(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    base = _closed_hour() - 10 * HOUR
    try:
        _add_decision(session, "d1", base, account_id=account_id)
        _add_decision(
            session,
            "d2",
            base + HOUR,
            account_id=account_id,
            plan={
                "entry": "99",
                "fill_entry": "99.5",
                "stop": "95",
                "target": "110",
                "qty": "1",
                "notional": "99.5",
                "rr": "2.3",
            },
            action="BUY",
        )
        _add_decision(session, "d3", base + 2 * HOUR, account_id=account_id)
        _add_trade(
            session,
            account_id=account_id,
            opened=base + HOUR,
            closed=base + 3 * HOUR,
            entry="99.5",
            decision_id="d2",
        )
        _add_decision(
            session,
            "d-plan",
            base + 4 * HOUR,
            account_id=account_id,
            action="BUY",
            status="none",
            plan={
                "entry": "88",
                "fill_entry": "88.5",
                "stop": "84",
                "target": "96",
                "qty": "1",
                "notional": "88.5",
                "rr": "2.0",
            },
        )
    finally:
        session.close()

    detail = client.get("/api/decisions/d2").json()
    assert detail["prev_id"] == "d1" and detail["next_id"] == "d3"
    assert detail["decision"]["id"] == "d2"
    assert detail["decision"]["signals"][0]["name"] == "RSI 14"
    assert detail["decision"]["gates"][0]["name"] == "risk gate"
    assert detail["fill_price"] == "99.5"
    assert detail["execution_mode"] == _state(client).settings.execution_mode

    planned_only = client.get("/api/decisions/d-plan").json()
    assert planned_only["decision"]["action"] == "BUY"
    assert planned_only["fill_price"] is None

    assert client.get("/api/decisions/d3").json()["next_id"] == "d-plan"
    assert client.get("/api/decisions/d-plan").json()["next_id"] is None
    assert client.get("/api/decisions/nope").status_code == 404


def test_decision_detail_marks_terminal_outcomes_unavailable(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    """A horizon that exhausted its retries must not still look pending."""
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    try:
        _add_decision(session, "d-fail", _closed_hour() - 100 * HOUR, account_id=account_id)
        for horizon in (4, 24, 72):
            session.add(
                OutcomeFailureRow(
                    decision_id="d-fail",
                    horizon=horizon,
                    attempts=24,
                    last_error="no closed candles in outcome horizon",
                    last_at=utcnow(),
                    terminal=True,
                )
            )
        session.commit()
    finally:
        session.close()

    detail = client.get("/api/decisions/d-fail").json()
    assert detail["outcome_failures"] == [
        {"horizon": h, "reason": "no closed candles in outcome horizon"} for h in (4, 24, 72)
    ]
    assert detail["outcome_pending_until"] is None


# ---------------------------------------------------------------- indicators


def _seed_pair(session: Session, pair: str, end: datetime, *, price: float = 100.0) -> None:
    repo.upsert_candles(session, pair, "1h", _series(400, HOUR, end, start_price=price))
    repo.upsert_candles(session, pair, "4h", _series(300, 4 * HOUR, end, start_price=price))
    repo.upsert_candles(
        session, pair, "1d", _series(250, timedelta(days=1), end, start_price=price)
    )


def test_indicators_payload(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    end = _closed_hour()
    session = _db(client)
    try:
        _seed_pair(session, "BTC-EUR", end)
    finally:
        session.close()

    data = client.get("/api/indicators/BTC-EUR?tf=1h&bars=120").json()
    assert data["frozen"] is False and data["frozen_at"] is None
    assert len(data["candles"]) == 120
    for name in ("ema20", "ema50", "ema200", "vwap", "rsi14", "adx14", "atr_pct", "obv"):
        assert name in data["series"], name
        assert len(data["series"][name]) == 120
    assert len(data["regime_band"]) == 120
    assert {p["value"] for p in data["regime_band"]} <= {"up", "down", "sideways"}
    assert data["regime"]["trend"] in ("up", "down", "sideways")
    assert data["btc_regime"]["trend"] in ("up", "down", "sideways")

    names = [r["name"] for r in data["current"]]
    assert names == [
        "RSI 14",
        "EMA 20 vs 50",
        "Price vs EMA 200",
        "Price vs VWAP",
        "OBV trend",
        "ADX 14",
        "ATR % of price",
        "Volume \u00d7 avg",
    ]
    for row in data["current"]:
        assert set(row["cells"]) == {"1h", "4h", "1d"}
    rsi_row = data["current"][0]
    assert rsi_row["cells"]["1h"]["text"] is not None
    assert rsi_row["cells"]["1h"]["read"] in ("bullish", "bearish", "neutral")

    assert isinstance(data["levels"], list)
    assert isinstance(data["structure"], list)
    assert data["health"]["bars_loaded"] == 120
    assert data["health"]["stale"] is False
    assert data["health"]["missing_bars"] == 0
    assert data["liquidity"]["spread_pct"] is not None  # sim order book
    assert data["adx_threshold"] > 0


def test_indicators_frozen_mode(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    end = _closed_hour()
    account_id = _account_id(client)
    session = _db(client)
    try:
        _seed_pair(session, "BTC-EUR", end)
        frozen_at = end - 100 * HOUR
        _add_decision(session, "d-frozen", frozen_at, account_id=account_id, cycle=42)
        _add_decision(session, "d-eth", frozen_at, account_id=account_id, pair="ETH-EUR")
        session.add(
            LlmCallRow(
                id="c-1",
                at=frozen_at,
                cycle=42,
                account_id=account_id,
                pair="BTC-EUR",
                decision_id="d-frozen",
                feature="decision",
                replay_of=None,
                model="gpt-4o-mini",
                prompt_version="decision.v1",
                status="ok",
                attempts="[]",
                latency_ms=10,
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
            )
        )
        session.commit()
    finally:
        session.close()

    data = client.get("/api/indicators/BTC-EUR?tf=1h&at=d-frozen").json()
    assert data["frozen"] is True
    assert data["frozen_cycle"] == 42
    assert data["frozen_decision_id"] == "d-frozen"
    newest = max(datetime.fromisoformat(c["time"]) for c in data["candles"])
    assert newest <= frozen_at  # no bar opens after the decision
    assert data["liquidity"]["spread_pct"] is None  # no live book when frozen

    # A call id resolves to its linked decision, and the response reports
    # that decision's id — not the call id the caller passed.
    by_call = client.get("/api/indicators/BTC-EUR?tf=1h&at=c-1").json()
    assert by_call["frozen"] is True
    assert by_call["frozen_decision_id"] == "d-frozen"
    assert by_call["frozen_cycle"] == 42

    assert client.get("/api/indicators/BTC-EUR?at=missing").status_code == 404
    assert client.get("/api/indicators/BTC-EUR?at=d-eth").status_code == 400


def test_indicators_decision_markers_use_the_chart_window(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    end = _closed_hour()
    account_id = _account_id(client)
    session = _db(client)
    try:
        _seed_pair(session, "BTC-EUR", end)
        oldest = end - 9 * HOUR
        _add_decision(
            session,
            "d-oldest",
            oldest,
            account_id=account_id,
            action="BUY",
            commit=False,
        )
        for i in range(500):
            _add_decision(
                session,
                f"d-new-{i:03d}",
                oldest + (i + 1) * timedelta(seconds=60),
                account_id=account_id,
                action="BUY",
                commit=False,
            )
        session.commit()
    finally:
        session.close()

    data = client.get("/api/indicators/BTC-EUR?tf=1h&bars=10").json()
    ids = {m["decision_id"] for m in data["markers"] if m["kind"] == "decision"}
    assert "d-oldest" in ids


def test_indicators_export_csv(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    end = _closed_hour()
    session = _db(client)
    try:
        repo.upsert_candles(session, "BTC-EUR", "1h", _series(60, HOUR, end))
    finally:
        session.close()

    response = client.get("/api/indicators/BTC-EUR/export.csv?tf=1h")
    assert response.status_code == 200
    lines = response.text.strip().splitlines()
    header = lines[0].split(",")
    assert header[:6] == ["time", "open", "high", "low", "close", "volume"]
    assert "rsi14" in header and "ema20" in header and "atr_pct" in header
    assert len(lines) == 61  # header + 60 bars


# ---------------------------------------------------------------- performance


def test_performance_defaults_to_account_start(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    """Omitting ``from`` means "since start": the window opens at the
    account's creation, not at a fixed lookback."""
    client, _ = screens_client
    data = client.get("/api/performance").json()
    account = _db(client).get(AccountRow, _account_id(client))
    assert account is not None
    assert data["start"].startswith(repo.aware_utc(account.started_at).isoformat()[:19])
    assert data["totals"]["trades"] == 0


def test_performance_totals_and_series(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    end = _closed_hour()
    start = end - 24 * HOUR
    session = _db(client)
    try:
        for i in range(5):
            session.add(
                EquitySnapshotRow(
                    account_id=account_id,
                    at=start + i * 4 * HOUR,
                    cycle=i + 1,
                    equity=str(Decimal("10000") + i * 10),
                    cash=str(Decimal("9000") + i * 10),
                )
            )
        _add_trade(session, account_id=account_id, opened=start + HOUR, closed=start + 3 * HOUR)
        _add_trade(
            session,
            account_id=account_id,
            pair="ETH-EUR",
            opened=start + 2 * HOUR,
            closed=start + 4 * HOUR,
            realised="-2",
            realised_r="-0.4",
            decision_id="d2",
        )
        repo.upsert_candles(session, "BTC-EUR", "1h", _series(60, HOUR, end))
        session.commit()
    finally:
        session.close()

    data = client.get("/api/performance", params={"from": start.isoformat()}).json()
    assert len(data["equity"]) >= 5
    assert data["totals"]["trades"] == 2
    assert data["totals"]["wins"] == 1
    assert data["totals"]["win_rate_pct"] == 50.0
    assert data["totals"]["return_pct"] is not None
    assert len(data["per_pair"]) == 2
    assert {p["pair"] for p in data["per_pair"]} == {"BTC-EUR", "ETH-EUR"}
    assert data["kill_line_pct"] == float(_state(client).settings.risk.kill_drawdown_pct)
    assert data["drawdown"] and all(p["pct"] <= 0 for p in data["drawdown"])
    assert data["totals"]["time_in_market_pct"] is not None


def test_performance_empty_window_is_flat(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    start = (utcnow() - 2 * HOUR).isoformat()
    data = client.get("/api/performance", params={"from": start}).json()
    assert len(data["equity"]) == 2  # flat line at the account balance
    assert data["totals"]["trades"] == 0


def test_performance_reports_relative_return(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    end = (utcnow() + 1000 * HOUR).replace(microsecond=0)
    start = end - 4 * HOUR
    session = _db(client)
    try:
        for at, equity in ((start, "10000"), (end, "10100")):
            session.add(
                EquitySnapshotRow(
                    account_id=account_id,
                    at=at,
                    cycle=1,
                    equity=equity,
                    cash=equity,
                )
            )
        repo.upsert_candles(
            session,
            "BTC-EUR",
            "1h",
            [
                Candle(
                    open_time=start,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100"),
                    volume=Decimal("10"),
                ),
                Candle(
                    open_time=end,
                    open=Decimal("109"),
                    high=Decimal("111"),
                    low=Decimal("108"),
                    close=Decimal("110"),
                    volume=Decimal("10"),
                ),
            ],
        )
        session.commit()
        seeded = repo.load_candles(session, "BTC-EUR", "1h", 10, end=end)
        assert [str(c.close) for c in seeded[-2:]] == ["100", "110"]
    finally:
        session.close()

    data = client.get(
        "/api/performance",
        params={"from": start.isoformat(), "end": end.isoformat()},
    ).json()
    assert data["totals"]["return_pct"] == pytest.approx(1.0)
    assert data["totals"]["benchmark_return_pct"] == pytest.approx(10.0)
    assert data["totals"]["vs_btc_pct"] == pytest.approx(-9.0)


def test_performance_accepts_a_restored_naive_position_time(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    engine = _state(client).agent_engine
    assert engine is not None
    engine.broker.positions["BTC-EUR"] = HeldPosition(
        pair="BTC-EUR",
        qty=Decimal("1"),
        entry=Decimal("100"),
        stop=Decimal("90"),
        target=Decimal("120"),
        opened_at=(utcnow() - HOUR).replace(tzinfo=None),
        decision_id="d1",
        last=Decimal("110"),
    )
    response = client.get("/api/performance")
    assert response.status_code == 200
    assert response.json()["totals"]["time_in_market_pct"] is not None


# ---------------------------------------------------------------- trades/activity


def test_trades_endpoint_filters(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    end = _closed_hour()
    session = _db(client)
    try:
        _add_trade(session, account_id=account_id, opened=end - 30 * HOUR, closed=end - 20 * HOUR)
        _add_trade(
            session,
            account_id=account_id,
            pair="ETH-EUR",
            opened=end - 10 * HOUR,
            closed=end - 5 * HOUR,
        )
    finally:
        session.close()

    items = client.get("/api/trades").json()["items"]
    assert len(items) == 2
    assert items[0]["pair"] == "ETH-EUR"  # newest first
    assert items[0]["realised_eur"] == "4.5"
    assert items[0]["realised_r"] == 0.9

    eth = client.get("/api/trades?pair=ETH-EUR").json()["items"]
    assert len(eth) == 1 and eth[0]["pair"] == "ETH-EUR"

    window = client.get("/api/trades", params={"from": (end - 15 * HOUR).isoformat()}).json()[
        "items"
    ]
    assert [t["pair"] for t in window] == ["ETH-EUR"]  # BTC closed before the window


def test_activity_endpoint(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    session = _db(client)
    base = _closed_hour() - 10 * HOUR
    try:
        for i in range(5):
            session.add(
                ActivityRow(
                    at=base + i * HOUR,
                    kind="fill" if i % 2 else "decision",
                    pair="BTC-EUR" if i % 2 else None,
                    text=f"event {i}",
                    decision_id=None,
                )
            )
        session.commit()
    finally:
        session.close()

    page1 = client.get("/api/activity?limit=3").json()
    assert len(page1["items"]) == 3
    assert page1["items"][0]["text"] == "event 4"
    assert page1["next_before"] is not None

    page2 = client.get(f"/api/activity?limit=3&before={page1['next_before']}").json()
    assert [i["text"] for i in page2["items"]] == ["event 1", "event 0"]

    fills = client.get("/api/activity?kind=fill").json()["items"]
    assert all(i["kind"] == "fill" for i in fills)


def test_snapshot_activity_newest_first_last_24h(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    """The dashboard feed is the last 24 hours, newest first."""
    client, _ = screens_client
    session = _db(client)
    now = utcnow()
    try:
        for i, age in enumerate([30 * HOUR, 2 * HOUR, HOUR]):
            session.add(
                ActivityRow(
                    at=now - age,
                    kind="decision",
                    pair="BTC-EUR",
                    text=f"event {i}",
                    decision_id=None,
                )
            )
        session.commit()
    finally:
        session.close()

    items = client.get("/api/snapshot").json()["activity"]
    assert [i["text"] for i in items] == ["event 2", "event 1"]


# ----------------------------------------------------------------- agent bits


def test_flatten_preview_empty_and_with_position(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    empty = client.get("/api/agent/flatten-preview").json()
    assert empty["count"] == 0 and empty["pairs"] == []
    assert empty["est_fees_eur"] == "0" and empty["unrealised_eur"] == "0"

    engine = _state(client).agent_engine
    assert engine is not None
    engine.broker.positions["BTC-EUR"] = HeldPosition(
        pair="BTC-EUR",
        qty=Decimal("2"),
        entry=Decimal("100"),
        stop=Decimal("90"),
        target=Decimal("120"),
        opened_at=utcnow() - HOUR,
        decision_id="d1",
        last=Decimal("110"),
    )
    preview = client.get("/api/agent/flatten-preview").json()
    assert preview["count"] == 1 and preview["pairs"] == ["BTC-EUR"]
    assert Decimal(preview["unrealised_eur"]) == Decimal("20")  # 2 * (110-100)
    assert Decimal(preview["est_fees_eur"]) > 0


def test_prompts_lists_versions(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    prompts = client.get("/api/prompts").json()
    v1 = next((p for p in prompts if p["id"] == "decision.v1"), None)
    assert v1 is not None
    assert v1["temperature"] == 0.0
    assert v1["max_output_tokens"] > 0


# -------------------------------------------------------------------- exports


def test_export_trades_and_decisions_csv(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    end = _closed_hour()
    session = _db(client)
    try:
        _add_trade(session, account_id=account_id, opened=end - 30 * HOUR, closed=end - 20 * HOUR)
        _add_decision(session, "d-buy", end - 30 * HOUR, account_id=account_id)
    finally:
        session.close()

    trades = client.get("/api/export/trades.csv").text
    lines = trades.strip().splitlines()
    assert lines[0].startswith("id,account_id,pair,")
    assert len(lines) == 2 and "BTC-EUR" in lines[1]

    decisions = client.get("/api/export/decisions.csv").text
    lines = decisions.strip().splitlines()
    assert lines[0].startswith("id,cycle,at,")
    assert len(lines) == 2 and "d-buy" in lines[1]


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_decisions_csv_sanitizes_formula_prefixes(
    screens_client: tuple[TestClient, SimMarket], prefix: str
) -> None:
    """Text cells that would run as a spreadsheet formula get a leading '."""
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    try:
        row = _add_decision(session, "d-inj", _closed_hour() - 50 * HOUR, account_id=account_id)
        row.reasoning = f"{prefix}SUM(A1:A2)+cmd()"
        row.invalidation = f"{prefix}invalid"
        row.error = f"{prefix}bad"
        session.commit()
    finally:
        session.close()

    body = list(csv.reader(io.StringIO(client.get("/api/export/decisions.csv").text)))
    header, data = body[0], next(r for r in body[1:] if r[0] == "d-inj")
    for field in ("reasoning", "invalidation", "error"):
        assert data[header.index(field)].startswith(f"'{prefix}")


def test_csv_exports_keep_negative_numbers_numeric(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    """A losing trade's negative P&L stays a plain number, not text."""
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    try:
        _add_trade(
            session,
            account_id=account_id,
            opened=_closed_hour() - 30 * HOUR,
            closed=_closed_hour() - 20 * HOUR,
            realised="-3",
            realised_r="-0.6",
        )
    finally:
        session.close()

    body = list(csv.reader(io.StringIO(client.get("/api/export/trades.csv").text)))
    header, data = body[0], body[1]
    assert data[header.index("realised_eur")] == "-3"
    assert data[header.index("realised_r")] == "-0.6"


def test_decisions_csv_uses_one_outcome_query(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    """Outcomes load in one grouped query — not one query per decision."""
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    try:
        for i in range(3):
            _add_decision(
                session, f"d-{i}", _closed_hour() - (50 + i) * HOUR, account_id=account_id
            )
        engine = session.get_bind()
    finally:
        session.close()

    outcome_queries: list[str] = []

    def count(conn: object, cursor: object, statement: str, *_args: object, **_kw: object) -> None:
        if "FROM outcomes" in statement:
            outcome_queries.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        client.get("/api/export/decisions.csv")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(outcome_queries) == 1


# ------------------------------------------------------------------ snapshot


def test_snapshot_today_counters(screens_client: tuple[TestClient, SimMarket]) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    session = _db(client)
    try:
        _add_trade(session, account_id=account_id, opened=utcnow() - 2 * HOUR, closed=utcnow())
        _add_trade(
            session,
            account_id=account_id,
            pair="ETH-EUR",
            opened=utcnow() - 3 * HOUR,
            closed=utcnow() - HOUR,
            realised="-3",
            realised_r="-0.6",
        )
    finally:
        session.close()

    snap = client.get("/api/snapshot").json()
    assert snap["today_trades"] == 2
    assert snap["today_won"] == 1


def test_snapshot_return_since_start(screens_client: tuple[TestClient, SimMarket]) -> None:
    """The dashboard "% since start" KPI is backend-owned, not computed in
    the UI from the equity strings."""
    client, _ = screens_client
    engine = _state(client).agent_engine
    assert engine is not None
    start = Decimal(engine.broker.account.start_balance)
    engine.broker.cash = start * Decimal("1.02")

    snap = client.get("/api/snapshot").json()
    assert snap["return_since_start_pct"] == pytest.approx(2.0, abs=0.01)


def test_run_now_refused_while_killed(screens_client: tuple[TestClient, SimMarket]) -> None:
    """Kill switch: no new cycle — manual or scheduled — until acknowledged."""
    client, _ = screens_client
    engine = _state(client).agent_engine
    assert engine is not None
    engine.status = "killed"

    response = client.post("/api/agent/run-now", headers={"X-Local-Token": _token(client)})
    assert response.status_code == 409
    assert "Killed" in response.json()["detail"]


# -------------------------------------------------------------- danger zone


def test_delete_all_data_requires_token_and_confirm(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    assert client.request("DELETE", "/api/data", json={"confirm": "DELETE"}).status_code == 403
    assert (
        client.request(
            "DELETE",
            "/api/data",
            json={"confirm": "yes"},
            headers={"X-Local-Token": _token(client)},
        ).status_code
        == 422
    )


def test_delete_all_data_wipes_and_recreates(
    screens_client: tuple[TestClient, SimMarket],
) -> None:
    client, _ = screens_client
    account_id = _account_id(client)
    end = _closed_hour()
    session = _db(client)
    try:
        _seed_pair(session, "BTC-EUR", end)
        _add_decision(session, "d1", end - HOUR, account_id=account_id)
        _add_trade(session, account_id=account_id, opened=end - 30 * HOUR, closed=end - 20 * HOUR)
    finally:
        session.close()

    response = client.request(
        "DELETE",
        "/api/data",
        json={"confirm": "DELETE"},
        headers={"X-Local-Token": _token(client)},
    )
    assert response.status_code == 200 and response.json()["ok"] is True

    session = _db(client)
    try:
        assert repo.list_decision_rows(session, limit=10) == []
        assert repo.load_candles(session, "BTC-EUR", "1h", 10) == []
        fresh = session.scalars(select(AccountRow)).all()
        assert len(fresh) == 1 and fresh[0].active is True
    finally:
        session.close()

    state = _state(client)
    assert state.agent_engine is not None
    assert state.agent_engine.broker.positions == {}
    assert state.broker is not None
    assert Decimal(state.broker.account.cash) == state.settings.paper.start_balance_eur

    # The agent is paused and a fresh snapshot is served.
    assert client.get("/api/snapshot").json()["status"] == "paused"
    # Idempotent: a second delete on the fresh account still succeeds.
    again = client.request(
        "DELETE",
        "/api/data",
        json={"confirm": "DELETE"},
        headers={"X-Local-Token": _token(client)},
    )
    assert again.status_code == 200
