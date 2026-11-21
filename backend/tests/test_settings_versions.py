"""Settings versions (item 025): ranges, directions, reasons, the
looser-change confirmation, preview money, meta and history paging."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SettingsHistoryRow, SettingsVersionRow, utcnow
from app.models import Settings
from app.settings_directions import DIRECTIONS, covered, tag_change
from app.settings_service import history_page
from app.settings_spec import OFF_SCREEN, SECTIONS, flatten_doc


def _token(client: TestClient) -> dict[str, str]:
    return {"X-Local-Token": client.get("/api/session").json()["token"]}


def _put(client: TestClient, doc: dict[str, Any], **extra: Any) -> Any:
    return client.put(
        "/api/settings",
        json={"settings": doc, "reason": "test save", **extra},
        headers=_token(client),
    )


def _doc(client: TestClient) -> dict[str, Any]:
    doc: dict[str, Any] = client.get("/api/settings").json()
    return doc


def _versions(client: TestClient) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = client.get("/api/settings/history").json()["versions"]
    return versions


# ------------------------------------------------------------- directions --


def test_direction_map_covers_every_covered_leaf() -> None:
    """Fail closed: every covered leaf has a DIRECTIONS entry (the
    notifications.events.* channel leaves are covered by their pattern)."""
    leaves = flatten_doc(Settings().model_dump(mode="json"))
    missing = [
        key
        for key in leaves
        if covered(key) and key not in DIRECTIONS and not key.startswith("notifications.events.")
    ]
    assert missing == []


def test_tag_change_classification() -> None:
    assert tag_change("risk.risk_per_trade_pct", "1.0", "1.5") == "looser"
    assert tag_change("risk.risk_per_trade_pct", "1.5", "1.0") == "tighter"
    assert tag_change("risk.risk_per_trade_pct", "1.0", "1.0") == "neutral"
    assert tag_change("risk.min_rr", "2.0", "1.5") == "looser"  # down = looser
    assert tag_change("execution_mode", "manual", "auto") == "looser"
    assert tag_change("execution_mode", "auto", "manual") == "tighter"
    assert tag_change("active_markets", ["BTC-EUR"], ["BTC-EUR", "ETH-EUR"]) == "looser"
    # A channel switched off an event is looser; switched on is tighter.
    assert tag_change("notifications.events.trade.email", True, False) == "looser"
    assert tag_change("notifications.events.trade.email", False, True) == "tighter"
    # Uncovered keys are neutral; covered keys without a map entry fail closed.
    assert tag_change("logging.max_db_mb", "100", "5000") == "neutral"
    assert tag_change("risk.not_a_real_key", "1", "2") == "looser"


# ------------------------------------------------------------------- meta --


def test_meta_lists_every_leaf_with_section_default_and_range(client: TestClient) -> None:
    meta = client.get("/api/settings/meta").json()
    leaves = flatten_doc(Settings().model_dump(mode="json"))
    assert set(meta["items"]) == set(leaves)
    for key, item in meta["items"].items():
        assert item["default"] == leaves[key]
        assert item["section"] in set(SECTIONS.values())
        if key in OFF_SCREEN:
            assert item["section"] is None
    # Spot-check the spec's headline ranges and units.
    risk = meta["items"]["risk.risk_per_trade_pct"]
    assert risk["min"] == "0.1" and risk["max"] == "2" and risk["unit"] == "%"
    assert meta["items"]["risk.kill_drawdown_pct"]["direction"] == "up"
    assert meta["items"]["market.stablecoin_bases"]["fixed"] is True


# ----------------------------------------------------------------- preview --


def test_preview_money_and_changes(client: TestClient) -> None:
    doc = _doc(client)
    doc["risk"]["risk_per_trade_pct"] = 1.5  # stored is 1.0 -> looser
    out = client.post("/api/settings/preview", json=doc)
    assert out.status_code == 200, out.text
    body = out.json()
    # Equity is the paper start balance 10000: 1.5% -> €150.00.
    assert body["equivalents"]["risk.risk_per_trade_pct"] == "€150.00"
    # The daily limit is a % of the day-start equity (also 10000).
    assert body["equivalents"]["risk.daily_loss_limit_pct"] == "€300.00"
    # The kill level is the equity left after an 8% drawdown from the peak.
    assert body["equivalents"]["risk.kill_drawdown_pct"] == "€9,200.00"
    assert body["summary"]["equity"] == "10000"
    assert body["summary"]["positions_at_full_risk"] == 1  # 2.5 // 1.5
    changes = {c["key"]: c for c in body["changes"]}
    assert changes["risk.risk_per_trade_pct"]["direction"] == "looser"
    # Nothing was saved.
    assert Decimal(_doc(client)["risk"]["risk_per_trade_pct"]) == Decimal("1")


def test_preview_reports_range_and_cross_field_errors(client: TestClient) -> None:
    doc = _doc(client)
    doc["risk"]["min_rr"] = 9  # range is 1 to 5
    doc["risk"]["throttled_risk_per_trade_pct"] = 5  # > risk_per_trade_pct
    out = client.post("/api/settings/preview", json=doc)
    assert out.status_code == 200, out.text
    errors = out.json()["errors"]
    assert "risk.min_rr" in errors
    assert "risk.throttled_risk_per_trade_pct" in errors
    # An out-of-range draft is reported, never persisted.
    assert Decimal(_doc(client)["risk"]["min_rr"]) == Decimal("2")


# ------------------------------------------------------------------- PUT ---


def test_reason_is_required_and_bounded(client: TestClient) -> None:
    doc = _doc(client)
    for reason in (None, "", "  ", "ab", "x" * 201):
        body: dict[str, Any] = {"settings": doc}
        if reason is not None:
            body["reason"] = reason
        out = client.put("/api/settings", json=body, headers=_token(client))
        assert out.status_code == 422, (reason, out.text)


def test_tighter_change_saves_without_confirmation(client: TestClient) -> None:
    doc = _doc(client)
    doc["risk"]["min_rr"] = 2.5  # tighter: min R/R goes up
    before = _versions(client)[0]["version"]
    out = _put(client, doc)
    assert out.status_code == 200, out.text
    assert out.json()["version"] == before + 1
    latest = _versions(client)[0]
    assert latest["reason"] == "test save"
    assert latest["source"] == "screen"
    change = next(c for c in latest["changes"] if c["key"] == "risk.min_rr")
    assert change["direction"] == "tighter"


def test_looser_change_needs_confirmation(client: TestClient) -> None:
    doc = _doc(client)
    doc["risk"]["risk_per_trade_pct"] = 1.5  # 1.0 -> 1.5 is looser
    refused = _put(client, doc)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "looser_needs_confirm"
    assert any(c["key"] == "risk.risk_per_trade_pct" for c in error["changes"])
    # The refused save wrote nothing.
    assert Decimal(_doc(client)["risk"]["risk_per_trade_pct"]) == Decimal("1")

    accepted = _put(client, doc, confirm_looser=True)
    assert accepted.status_code == 200, accepted.text
    assert _doc(client)["risk"]["risk_per_trade_pct"] == "1.5"


def test_model_change_always_asks(client: TestClient) -> None:
    doc = _doc(client)
    doc["model"]["model_id"] = "gpt-4o"
    refused = _put(client, doc)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "looser_needs_confirm"
    accepted = _put(client, doc, confirm_looser=True)
    assert accepted.status_code == 200


def test_notification_channel_removal_asks(client: TestClient) -> None:
    doc = _doc(client)
    doc["notifications"]["events"]["trade"]["email"] = True
    saved = _put(client, doc, confirm_looser=True)
    assert saved.status_code == 200, saved.text
    doc = _doc(client)
    doc["notifications"]["events"]["trade"]["email"] = False
    refused = _put(client, doc)
    assert refused.status_code == 409


def test_execution_mode_toward_auto_asks(client: TestClient) -> None:
    doc = _doc(client)
    doc["execution_mode"] = "manual"
    saved = _put(client, doc, confirm_looser=True)
    assert saved.status_code == 200
    doc = _doc(client)
    doc["execution_mode"] = "auto"  # manual -> auto is looser
    refused = _put(client, doc)
    assert refused.status_code == 409


# ------------------------------------------------------------- persistence --


def test_base_version_written_once_at_startup(client: TestClient) -> None:
    """The first boot after item 025 records the stored document as a
    migration version; later saves add numbered versions on top."""
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        rows = list(session.scalars(select(SettingsVersionRow)).all())
    finally:
        session.close()
    migration = [r for r in rows if r.source == "migration"]
    assert len(migration) == 1
    assert migration[0].reason
    # TA_DATA_SOURCE=sim at startup is the second version, source "action".
    action = [r for r in rows if r.source == "action"]
    assert action and "TA_DATA_SOURCE" in action[0].reason


def test_history_rows_link_to_their_version(client: TestClient) -> None:
    doc = _doc(client)
    doc["risk"]["min_rr"] = 2.5
    version = _put(client, doc).json()["version"]
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        row = session.scalar(
            select(SettingsHistoryRow)
            .where(SettingsHistoryRow.key == "risk.min_rr")
            .order_by(SettingsHistoryRow.id.desc())
        )
        assert row is not None and row.version == version
        # A fresh database has no pre-version rows.
        nulls = session.scalar(
            select(func.count())
            .select_from(SettingsHistoryRow)
            .where(SettingsHistoryRow.version.is_(None))
        )
        assert nulls == 0
    finally:
        session.close()


def test_action_sources_write_versions(client: TestClient) -> None:
    """A source toggle is a settings save with source "action" and a
    readable reason."""
    token = _token(client)
    before = _versions(client)[0]["version"]
    out = client.put("/api/sources/openai", json={"enabled": False}, headers=token)
    assert out.status_code == 200, out.text
    latest = _versions(client)[0]
    assert latest["version"] == before + 1
    assert latest["source"] == "action"
    assert "openai" in latest["reason"]


# -------------------------------------------------------------- secrets ----


def test_no_secret_ever_reaches_settings_surfaces(client: TestClient) -> None:
    headers = _token(client)
    password = "sup3r-secret-smtp"
    client.put(
        "/api/notifications/smtp-password",
        headers=headers,
        json={"password": password},
    )
    doc = _doc(client)
    doc["notifications"]["smtp"].update(
        {"host": "smtp.example.test", "username": "agent", "sender": "a@b.c"}
    )
    assert _put(client, doc).status_code == 200
    for path in (
        "/api/settings",
        "/api/settings/meta",
        "/api/settings/history",
    ):
        assert password not in client.get(path).text
    preview = client.post("/api/settings/preview", json=doc)
    assert password not in preview.text


# --------------------------------------------------------------- paging ----


def test_history_pages_versions_then_old_rows(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        for i in range(1, 4):
            session.add(
                SettingsVersionRow(
                    version=100 + i,
                    at=utcnow(),
                    reason=f"page test {i}",
                    source="action",
                    doc="{}",
                    changes="[]",
                )
            )
        for i in range(5):
            session.add(
                SettingsHistoryRow(at=utcnow(), key=f"old.key{i}", old="1", new="2", version=None)
            )
        session.commit()
        expected_versions = [
            r.version
            for r in session.scalars(
                select(SettingsVersionRow).order_by(SettingsVersionRow.version.desc())
            )
        ]
        expected_rows = [f"old.key{i}" for i in range(4, -1, -1)]

        seen_versions: list[int] = []
        seen_rows: list[str] = []
        cursor: str | None = None
        for _ in range(20):
            page = history_page(session, cursor, limit=2)
            seen_versions += [v.version for v in page.versions]
            seen_rows += [r.key for r in page.rows]
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen_versions == expected_versions
        assert seen_rows == expected_rows
        # The cursor moved from versions to rows exactly once.
        assert seen_versions[0] == 103
    finally:
        session.close()


def test_history_endpoint_pages(client: TestClient) -> None:
    out = client.get("/api/settings/history?limit=1")
    assert out.status_code == 200
    body = out.json()
    assert len(body["versions"]) <= 1
    assert "next_cursor" in body
