"""Understanding and safety: entry pauses (no model call while flat, SELLs
still reach the model, the Entry pause gate, re-checks at fill), the
quiet-line ordering, the glossary, briefing fallbacks and the number check,
the readiness endpoint and the settings history.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.broker.paper import PaperBroker
from app.agent.openai_client import CallResult
from app.agent.risk import BuyContext, check_buy
from app.db import (
    AccountRow,
    ActivityRow,
    BriefingRow,
    DecisionRow,
    EntryPauseRow,
    LlmCallRow,
    MarketRow,
    make_engine,
    make_session_factory,
)
from app.features import briefings
from app.features.quiet import quiet_line
from app.models import PaperSettings, Settings
from tests.test_openai_decider import _calls, _ctx, _decider, _rules_draft

NOW = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)  # 12:00 Amsterdam (CEST)
HOUR = timedelta(hours=1)


@pytest.fixture
def sessions(tmp_path: Path, migrated_db: Path) -> Iterator[sessionmaker[Session]]:
    db = tmp_path / "agent.db"
    shutil.copyfile(migrated_db, db)
    yield make_session_factory(make_engine(db))


def _broker(cash: str = "10000") -> PaperBroker:
    return PaperBroker(
        AccountRow(
            id=1,
            started_at=NOW - timedelta(days=10),
            start_balance="10000",
            cash=cash,
            peak_equity="10000",
            day_start_equity="10000",
            day_start_date="2026-10-11",
            active=True,
        ),
        PaperSettings(),
    )


def _buy_ctx(**kw: object) -> BuyContext:
    base: dict[str, object] = {
        "pair": "X-EUR",
        "now": NOW,
        "entry": Decimal("100"),
        "stop": Decimal("96"),
        "target": Decimal("110"),
        "atr_1h": Decimal("1.5"),
        "has_position": False,
        "last_stop_out_at": None,
    }
    base.update(kw)
    return BuyContext(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- entry pause --


async def test_paused_flat_pair_makes_no_openai_call(
    sessions: sessionmaker[Session],
) -> None:
    """Paused + flat: Rules still answers but the model is never called —
    including the shadow-style calls the pre-filter would allow."""
    decider, rules = _decider(sessions, rules_draft=_rules_draft(action="HOLD", prefilter=True))
    draft = await decider.decide(_ctx(entries_paused=True, holding=False))
    assert draft is rules.draft
    assert _calls(sessions) == []


async def test_paused_held_pair_still_calls_openai(
    sessions: sessionmaker[Session],
) -> None:
    """Paused + holding: the model is still asked so SELL keeps working."""
    decider, _ = _decider(sessions)
    draft = await decider.decide(_ctx(entries_paused=True, holding=True))
    assert draft.decider == "openai"
    assert len(_calls(sessions)) == 1


def test_entry_pause_gate_skips_buy() -> None:
    timed = check_buy(_buy_ctx(entry_paused_until=NOW + HOUR), _broker(), None, Settings())
    assert timed.outcome == "skip" and timed.skip_reason == "Entry pause"
    assert timed.gates[0].name == "Entry pause" and timed.gates[0].status == "fail"

    indefinite = check_buy(_buy_ctx(entry_paused_indefinite=True), _broker(), None, Settings())
    assert indefinite.outcome == "skip" and indefinite.skip_reason == "Entry pause"
    assert indefinite.gates[0].value == "until resumed"


def test_expired_entry_pause_does_not_block() -> None:
    ok = check_buy(
        _buy_ctx(entry_paused_until=NOW - timedelta(minutes=1)),
        _broker(),
        None,
        Settings(),
    )
    assert ok.outcome == "fill"
    assert ok.gates[0].name == "Entry pause" and ok.gates[0].status == "pass"


def test_active_entry_pause_prefers_global(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        repo.set_entry_pause(session, "DOGE-EUR", NOW + 2 * HOUR, at=NOW)
        assert repo.active_entry_pause(session, "DOGE-EUR", NOW) is not None
        assert repo.active_entry_pause(session, "DOGE-EUR", NOW + 3 * HOUR) is None
        repo.set_entry_pause(session, "*", None, at=NOW)
        pause = repo.active_entry_pause(session, "DOGE-EUR", NOW)
        assert pause is not None and pause.pair == "*"
        repo.clear_entry_pause(session, "*")
        pause = repo.active_entry_pause(session, "DOGE-EUR", NOW)
        assert pause is not None and pause.pair == "DOGE-EUR"
    finally:
        session.close()


# ------------------------------------------------------------- quiet line --


def _decision_row(**kw: Any) -> DecisionRow:
    base: dict[str, Any] = {
        "id": "d1",
        "cycle": 1,
        "at": NOW,
        "account_id": 1,
        "pair": "SOL-EUR",
        "decider": "rules",
        "model": None,
        "prompt_version": None,
        "action": "HOLD",
        "confidence": 0.4,
        "reasoning": "nothing",
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
        "prefilter": False,
    }
    base.update(kw)
    return DecisionRow(**base)


def test_quiet_line_safety_suffix_order(sessions: sessionmaker[Session]) -> None:
    """Shock brake, then the global pause, then per-coin pauses — matching
    the spec example word for word."""
    session = sessions()
    try:
        repo.set_entry_pause(session, "*", NOW + 2 * HOUR, at=NOW)  # until 14:00 AMS
        repo.set_entry_pause(session, "DOGE-EUR", NOW + 18 * HOUR, at=NOW)
        session.commit()
        line = quiet_line(
            session, 1, now=NOW, shock_brake_until=NOW + timedelta(hours=4, minutes=30)
        )
    finally:
        session.close()
    assert line == (
        "Shock brake on until 16:30. All new entries paused until 14:00. DOGE paused for 18h."
    )


def test_quiet_line_appends_suffixes_after_the_cycle_text(
    sessions: sessionmaker[Session],
) -> None:
    session = sessions()
    try:
        session.add(_decision_row())
        repo.set_entry_pause(session, "DOGE-EUR", None, at=NOW)
        session.commit()
        line = quiet_line(session, 1, now=NOW)
    finally:
        session.close()
    assert "failed the pre-filter" in line
    assert line.endswith("DOGE paused until resumed.")


def test_quiet_line_empty_without_decisions(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        assert quiet_line(session, 1, now=NOW) == ""
    finally:
        session.close()


# ---------------------------------------------------------------- briefing --


def _account(sessions: sessionmaker[Session]) -> tuple[AccountRow, PaperBroker]:
    session = sessions()
    try:
        account = repo.current_account(session, Decimal("10000"), "2026-10-11")
        session.commit()
        return account, PaperBroker(account, PaperSettings())
    finally:
        session.close()


class _ScriptedClient:
    """Answers call() from a script; a str is a clean output, an Exception
    raises, a CallResult is returned verbatim."""

    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    async def call(self, payload: dict[str, Any]) -> CallResult:
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, CallResult):
            return out
        return CallResult(
            status="ok",
            request_bytes=json.dumps(payload).encode(),
            response_bytes=b'{"ok":true}',
            output_text=out,
        )

    async def aclose(self) -> None:
        pass


def _generate(
    sessions: sessionmaker[Session],
    *,
    api_key: str | None = "sk",
    settings: Settings | None = None,
    outputs: list[Any] | None = None,
) -> tuple[BriefingRow, _ScriptedClient]:
    account, broker = _account(sessions)
    client = _ScriptedClient(outputs or ["A quiet day on the markets."])
    session = sessions()
    try:
        import asyncio

        row = asyncio.run(
            briefings.generate_briefing(
                session,
                now=NOW,
                account=account,
                broker=broker,
                settings=settings or Settings(),
                api_key=api_key,
                client_factory=lambda _key: cast(Any, client),
            )
        )
        session.commit()
        session.expunge(row)
        return row, client
    finally:
        session.close()


def test_briefing_no_openai_key_uses_template(sessions: sessionmaker[Session]) -> None:
    row, client = _generate(sessions, api_key=None)
    assert row.source == "template"
    assert row.reason == "no OpenAI key"
    assert client.calls == 0  # no model call at all
    assert row.llm_call_id is None
    # The failure is logged for the activity feed.
    session = sessions()
    try:
        alerts = [
            a.text
            for a in session.scalars(select(ActivityRow).where(ActivityRow.kind == "alert")).all()
        ]
        assert any("no OpenAI key" in t for t in alerts)
    finally:
        session.close()


def test_briefing_model_call_failed_uses_template(
    sessions: sessionmaker[Session],
) -> None:
    row, _ = _generate(
        sessions,
        outputs=[CallResult(status="api_error", request_bytes=b"{}", error="HTTP 500")],
    )
    assert row.source == "template"
    assert row.reason == "model call failed"
    assert row.llm_call_id is not None  # the failed call is still audited


def test_briefing_transport_exception_uses_template(
    sessions: sessionmaker[Session],
) -> None:
    row, _ = _generate(sessions, outputs=[RuntimeError("boom")])
    assert row.source == "template"
    assert row.reason == "model call failed"


def test_briefing_monthly_cap_uses_template(sessions: sessionmaker[Session]) -> None:
    settings = Settings(model={"monthly_cost_cap_eur": "0"})
    row, client = _generate(sessions, settings=settings)
    assert row.source == "template"
    assert row.reason == "monthly cost cap reached"
    assert client.calls == 0  # the cap is checked before any call


def test_briefing_bad_number_retries_then_template(
    sessions: sessionmaker[Session],
) -> None:
    # €9,999,999 is not among the computed facts — twice.
    row, client = _generate(sessions, outputs=["Equity is €9,999,999 today.", "Still €9,999,999."])
    assert client.calls == 2  # one regeneration, not more
    assert row.source == "template"
    assert row.reason == "the number check rejected the model's text"
    # Both calls are audited with their own request/response bytes.
    session = sessions()
    try:
        calls = session.scalars(select(LlmCallRow)).all()
        assert len(calls) == 2
        assert {c.id for c in calls} >= {row.llm_call_id}
        assert len({c.request_json for c in calls}) == 2  # the retry kept its own payload
    finally:
        session.close()


def test_briefing_retry_with_good_numbers_is_used(
    sessions: sessionmaker[Session],
) -> None:
    row, client = _generate(
        sessions, outputs=["Equity is €9,999,999 today.", "A quiet day, no trades."]
    )
    assert client.calls == 2
    assert row.source == "model"
    assert row.reason is None
    assert row.text == "A quiet day, no trades."


def test_briefing_template_has_no_foreign_numbers(
    sessions: sessionmaker[Session],
) -> None:
    """The template itself survives the number check against its own facts."""
    row, _ = _generate(sessions, api_key=None)
    facts = json.loads(row.facts)
    assert briefings.text_numbers_ok(row.text, facts) == []


def test_number_check_accepts_fact_roundings() -> None:
    facts = {"equity_eur": "10412.80", "count": 3, "nested": {"r": 1.4}}
    assert briefings.text_numbers_ok("Equity €10,412.80, 3 trades, +1.4R.", facts) == []
    assert briefings.text_numbers_ok("Equity €10,412.81.", facts) == [
        __import__("decimal").Decimal("10412.81")
    ]


def test_number_check_accepts_float_as_serialised() -> None:
    """The brief serialises floats at full repr precision, so the model may
    copy that spelling verbatim — checking only the 6-decimal rounding
    rejects a number that is in the facts it was given."""
    facts = {"return_since_start_pct": -1.068054595481099}
    assert briefings.text_numbers_ok("Equity is -1.068054595481099% since the start.", facts) == []
    # Roundings of the same figure keep passing.
    assert briefings.text_numbers_ok("Equity is -1.07% since the start.", facts) == []
    assert briefings.text_numbers_ok("Equity is -1.0680546% since the start.", facts) == []


def test_number_check_sign_carried_by_words() -> None:
    """A negative fact may be written as magnitude + direction word."""
    facts = {"btc_week_pct": -2.3043523749738437, "equity_change_24h_eur": "-12.50"}
    for text in [
        "BTC experienced a decline of approximately 2.30% over the same period.",
        "BTC fell 2.30% this week; equity is down €12.50.",
        "The week closed 2.30% lower for BTC.",
        "BTC: -2.30%.",
    ]:
        assert briefings.text_numbers_ok(text, facts) == [], text
    # No direction word, a positive claim or an explicit wrong sign fail.
    for text in [
        "BTC moved 2.30% this week.",
        "BTC gained 2.30% this week.",
        "BTC moved +2.30% this week.",
    ]:
        assert briefings.text_numbers_ok(text, facts) == [Decimal("2.30")], text


def test_number_check_allows_other_date_and_time_formats() -> None:
    """A date or moment from the facts may be written in any format —
    the parts count, not the punctuation or the order."""
    facts = {
        "for_date": "2026-10-13",
        "generated_at": "2026-10-13T06:00:51.634857+00:00",
        "alerts": ["entries paused until 2026-10-13T03:00:45.000025+00:00"],
    }
    for text in [
        "As of October 13, 2026 nothing happened.",
        "As of 13 October 2026 nothing happened.",
        "Generated on 13-10-2026.",
        "Generated on 13/10/2026.",
        "Generated on 13.10.2026.",
        "Copied verbatim: 2026-10-13T06:00:51.634857+00:00.",
        "Entries paused until 03:00 today.",
        "Entries paused until 03:00:45 today.",
        # 03:00 UTC on 2026-10-13 is 05:00 in Europe/Amsterdam (CEST).
        "Entries paused until 05:00 local time.",
    ]:
        assert briefings.text_numbers_ok(text, facts) == [], text
    # A wrong day or minute still fails.
    assert briefings.text_numbers_ok("As of October 14, 2026.", facts) == [Decimal("14")]
    assert briefings.text_numbers_ok("Paused until 03:30.", facts) == [Decimal("30")]


# ------------------------------------------------------------- api routes --


def _token(client: TestClient) -> str:
    return str(client.get("/api/session").json()["token"])


def _state(client: TestClient) -> Any:
    return cast(Any, client.app).state.app_state


def test_pause_endpoints(sim_client: TestClient) -> None:
    token = _token(sim_client)
    headers = {"X-Local-Token": token}
    state = _state(sim_client)
    session = state.session_factory()
    try:
        session.merge(MarketRow(market="DOGE-EUR", eligible=True))
        session.commit()
    finally:
        session.close()

    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = sim_client.put("/api/markets/DOGE-EUR/pause", headers=headers, json={"until": until})
    assert out.status_code == 200, out.text
    assert out.json()["pair"] == "DOGE-EUR"

    snap = sim_client.get("/api/snapshot").json()
    assert [p["pair"] for p in snap["paused"]] == ["DOGE-EUR"]

    market = next(m for m in sim_client.get("/api/markets").json() if m["market"] == "DOGE-EUR")
    assert market["paused"] is True

    assert (
        sim_client.put("/api/agent/entry-pause", headers=headers, json={"until": None}).status_code
        == 200
    )
    snap = sim_client.get("/api/snapshot").json()
    assert {p["pair"] for p in snap["paused"]} == {"DOGE-EUR", "*"}

    assert sim_client.delete("/api/agent/entry-pause", headers=headers).status_code == 200
    assert sim_client.delete("/api/markets/DOGE-EUR/pause", headers=headers).status_code == 200
    assert sim_client.get("/api/snapshot").json()["paused"] == []


def test_pause_naive_until_is_treated_as_utc(sim_client: TestClient) -> None:
    """A timezone-naive ``until`` is read as UTC, not a 500."""
    state = _state(sim_client)
    session = state.session_factory()
    try:
        session.merge(MarketRow(market="DOGE-EUR", eligible=True))
        session.commit()
    finally:
        session.close()
    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    out = sim_client.put(
        "/api/markets/DOGE-EUR/pause",
        headers={"X-Local-Token": _token(sim_client)},
        json={"until": naive},
    )
    assert out.status_code == 200, out.text


def test_expired_pause_is_pruned_from_db(sessions: sessionmaker[Session]) -> None:
    session = sessions()
    try:
        repo.set_entry_pause(session, "DOGE-EUR", NOW - HOUR, at=NOW - 2 * HOUR)
        session.commit()
    finally:
        session.close()
    session = sessions()  # a fresh session — expired rows must not linger
    try:
        assert repo.list_entry_pauses(session, NOW) == []
        assert session.scalars(select(EntryPauseRow)).all() == []
    finally:
        session.close()


def test_pause_until_must_be_future(sim_client: TestClient) -> None:
    state = _state(sim_client)
    session = state.session_factory()
    try:
        session.merge(MarketRow(market="DOGE-EUR", eligible=True))
        session.commit()
    finally:
        session.close()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    out = sim_client.put(
        "/api/markets/DOGE-EUR/pause",
        headers={"X-Local-Token": _token(sim_client)},
        json={"until": past},
    )
    assert out.status_code == 422


def test_pause_unknown_market_404(sim_client: TestClient) -> None:
    out = sim_client.put(
        "/api/markets/NOPE-EUR/pause",
        headers={"X-Local-Token": _token(sim_client)},
        json={"until": None},
    )
    assert out.status_code == 404


def test_writes_require_token(sim_client: TestClient) -> None:
    assert sim_client.put("/api/agent/entry-pause", json={"until": None}).status_code == 403


def test_briefing_time_must_be_hh_mm(sim_client: TestClient) -> None:
    """A malformed briefing time is rejected at the settings boundary —
    it must never reach scheduled_today."""
    doc = sim_client.get("/api/settings").json()
    doc["learning"]["briefing_time"] = "25:99"
    out = sim_client.put(
        "/api/settings",
        headers={"X-Local-Token": _token(sim_client)},
        json={"settings": doc, "reason": "bad time"},
    )
    assert out.status_code == 422


def test_glossary_endpoint(sim_client: TestClient) -> None:
    out = sim_client.get("/api/glossary")
    assert out.status_code == 200
    body = out.json()
    assert body["plain_language"] is True
    terms = {t["term"]: t["definition"] for t in body["terms"]}
    assert "pre-filter" in terms and "drawdown" in terms


def test_readiness_endpoint(sim_client: TestClient) -> None:
    out = sim_client.get("/api/readiness")
    assert out.status_code == 200
    body = out.json()
    assert body["total"] == 9
    assert len(body["rows"]) == 9
    assert {r["id"] for r in body["rows"]} == {
        "closed_trades",
        "days_running",
        "beats_btc",
        "max_drawdown",
        "avg_r",
        "profit_factor",
        "calibration",
        "no_kill",
        "no_errors",
    }
    assert body["status"]


def test_settings_history_records_changes(client: TestClient) -> None:
    current = client.get("/api/settings").json()
    current["readiness"]["min_closed_trades"] = 80
    out = client.put(
        "/api/settings",
        headers={"X-Local-Token": _token(client)},
        json={"settings": current, "reason": "loosen readiness", "confirm_looser": True},
    )
    assert out.status_code == 200, out.text
    page = client.get("/api/settings/history").json()
    # The newest version carries the per-setting changes.
    latest = page["versions"][0]
    change = next(c for c in latest["changes"] if c["key"] == "readiness.min_closed_trades")
    assert change["new"] == 80
    assert change["direction"] == "looser"
    # The per-setting row is written too, linked to the version — Readiness
    # and What-if keep reading settings_history. "rows" is only the
    # pre-version history, so it stays empty on a fresh database.
    assert page["rows"] == []


def test_briefings_list_endpoint(sim_client: TestClient) -> None:
    state = _state(sim_client)
    session = state.session_factory()
    try:
        session.add(
            BriefingRow(
                at=NOW,
                account_id=1,
                type="daily",
                for_date="2026-10-11",
                facts="{}",
                text="Nothing much.",
                source="template",
                reason="no OpenAI key",
                llm_call_id=None,
            )
        )
        session.commit()
    finally:
        session.close()
    items = sim_client.get("/api/briefings?type=daily").json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "template"
    assert items[0]["reason"] == "no OpenAI key"
