"""Logs API: listing, filters, FTS search, trace, problems, client
reports and the event registry."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import logs as logs_api
from app.logstore.record import COLUMNS, fingerprint

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


def _state(client: TestClient) -> Any:
    return cast(FastAPI, client.app).state.app_state


def _token(client: TestClient) -> str:
    return str(client.get("/api/session").json()["token"])


def _line(
    text: str,
    *,
    level: str = "INFO",
    logger: str = "app",
    event: str | None = None,
    ts: str | None = None,
    cycle_id: str | None = None,
    pair: str | None = None,
) -> dict[str, Any]:
    line: dict[str, Any] = dict.fromkeys(COLUMNS)
    line.update(
        {
            "ts": ts or NOW.isoformat(),
            "level": level,
            "levelno": {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}[level],
            "logger": logger,
            "event": event,
            "message": text,
            "process": "app",
            "pid": 1,
            "cycle_id": cycle_id,
            "pair": pair,
            "fingerprint": fingerprint(logger, event, text)
            if level in ("WARNING", "ERROR")
            else None,
            "version": "test",
        }
    )
    return line


def _write(client: TestClient, *lines: dict[str, Any]) -> None:
    _state(client).log_store.write_lines(list(lines))


def test_list_and_level_filter(client: TestClient) -> None:
    _write(
        client,
        _line("an info line"),
        _line("a warning line", level="WARNING"),
        _line("an error line", level="ERROR"),
    )
    body = client.get("/api/logs").json()
    assert {ln["message"] for ln in body["lines"]} >= {
        "an info line",
        "a warning line",
        "an error line",
    }
    errors = client.get("/api/logs?level=ERROR").json()["lines"]
    assert {ln["message"] for ln in errors} == {"an error line"}
    warned = client.get("/api/logs?level=WARNING,ERROR").json()["lines"]
    assert all(ln["levelno"] >= 30 for ln in warned) and len(warned) == 2


def test_full_text_search(client: TestClient) -> None:
    _write(
        client,
        _line("the broker rejected the order price"),
        _line("cycle finished quietly"),
    )
    hits = client.get("/api/logs?q=rejected").json()["lines"]
    assert [ln["message"] for ln in hits] == ["the broker rejected the order price"]


def test_cycle_trace_filter(client: TestClient) -> None:
    _write(
        client,
        _line("cycle c-0007 start", cycle_id="c-0007"),
        _line("cycle c-0007 pair decided", cycle_id="c-0007", pair="SOL-EUR"),
        _line("unrelated line", cycle_id="c-0008"),
    )
    hits = client.get("/api/logs?cycle_id=c-0007").json()["lines"]
    assert {ln["message"] for ln in hits} == {
        "cycle c-0007 start",
        "cycle c-0007 pair decided",
    }


def test_line_and_around(client: TestClient) -> None:
    _write(client, _line("first"), _line("second"), _line("third"))
    lines = client.get("/api/logs").json()["lines"]
    line_id = lines[1]["id"]
    one = client.get(f"/api/logs/{line_id}").json()
    assert one["message"] == lines[1]["message"]
    around = client.get(f"/api/logs/{line_id}/around").json()
    assert around["focus"] == line_id
    assert len(around["lines"]) >= 2


def test_problems_review_flow(client: TestClient) -> None:
    _write(
        client,
        _line("watcher tick failed", level="ERROR", event="task.crashed"),
        _line("watcher tick failed", level="ERROR", event="task.crashed"),
    )
    problems = client.get("/api/logs/problems").json()["problems"]
    assert len(problems) == 1 and problems[0]["count"] == 2
    fp = problems[0]["fingerprint"]
    # Reviewing requires the local token.
    denied = client.post(
        f"/api/logs/problems/{fp}/review", json={"state": "fixed"}
    )
    assert denied.status_code == 403
    reviewed = client.post(
        f"/api/logs/problems/{fp}/review",
        headers={"X-Local-Token": _token(client)},
        json={"state": "fixed", "note": "seen it"},
    )
    assert reviewed.status_code == 200
    assert client.get("/api/logs/problems?state=unreviewed").json()["problems"] == []
    fixed = client.get("/api/logs/problems?state=fixed").json()["problems"]
    assert len(fixed) == 1 and fixed[0]["note"] == "seen it"


def test_events_registry(client: TestClient) -> None:
    events = client.get("/api/logs/events").json()["events"]
    codes = {e["code"] for e in events}
    assert "cycle.pair_failed" in codes and "db.unwritable" in codes
    assert all(e["meaning"] for e in events)


def test_client_report_rate_limit(client: TestClient) -> None:
    logs_api._client_hits.clear()
    token = {"X-Local-Token": _token(client)}
    for i in range(10):
        res = client.post(
            "/api/logs/client",
            headers=token,
            json={"level": "error", "message": f"browser failure {i}"},
        )
        assert res.status_code == 200, i
    over = client.post(
        "/api/logs/client",
        headers=token,
        json={"level": "error", "message": "one too many"},
    )
    assert over.status_code == 429
    assert over.headers["Retry-After"] == "60"
    # Reports land as lines with the frontend.client logger.
    hits = client.get("/api/logs?q=browser").json()["lines"]
    assert all(ln["logger"] == "frontend.client" for ln in hits)


def test_summary_counts(client: TestClient) -> None:
    _write(
        client,
        _line("w1", level="WARNING"),
        _line("e1", level="ERROR"),
    )
    body = client.get("/api/logs/summary").json()
    assert body["counts_24h"]["ERROR"] >= 1
    assert body["total_lines"] >= 2
    assert body["broken"] is False


def test_error_shape_on_unknown_line(client: TestClient) -> None:
    res = client.get("/api/logs/999999")
    assert res.status_code == 404
    body = res.json()["error"]
    assert body["code"] == "not_found"
    assert res.headers["x-request-id"]
