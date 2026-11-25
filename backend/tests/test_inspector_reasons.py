"""Classification and brief-section units for item 043."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.brief_sections import sections
from app.agent.reasons import REASONS, classify


def row(**kw: Any) -> Any:
    base = {
        "status": "ok",
        "input_checks": "[]",
        "output_checks": "[]",
        "attempts": json.dumps([{"outcome": "sent", "latency_ms": 100}]),
        "latency_ms": 100,
        "parsed": '{"action":"HOLD"}',
        "feature": "decision",
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("timeout", "timeout"),
        ("api_error", "api_error"),
        ("invalid_output", "invalid_output"),
        ("input_blocked", "input_blocked"),
    ],
)
def test_classify_error_statuses(status: str, code: str) -> None:
    outcome, found = classify(row(status=status), "HOLD", 15)
    assert outcome == "error"
    assert found[0]["code"] == code


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"parsed": '{"action":"BUY"}'}, "changed_by_code"),
        (
            {
                "output_checks": json.dumps(
                    [{"name": "Signal claims", "status": "warn", "detail": "not in brief"}]
                )
            },
            "claim_mismatch",
        ),
        (
            {
                "output_checks": json.dumps(
                    [{"name": "Levels sane", "status": "warn", "detail": "x"}]
                )
            },
            "check_warning",
        ),
        (
            {"input_checks": json.dumps([{"name": "Fresh", "status": "warn", "detail": "stale"}])},
            "input_warning",
        ),
        (
            {
                "attempts": json.dumps(
                    [{"outcome": "error", "error": "x"}, {"outcome": "sent", "latency_ms": 100}]
                )
            },
            "retried",
        ),
        (
            {
                "latency_ms": 16_000,
                "attempts": json.dumps([{"outcome": "sent", "latency_ms": 16_000}]),
            },
            "slow",
        ),
        (
            {
                "feature": "briefing",
                "output_checks": json.dumps(
                    [{"name": "Numbers match facts", "status": "fail", "detail": "12"}]
                ),
            },
            "number_check_failed",
        ),
    ],
)
def test_classify_each_warning(changes: dict[str, Any], code: str) -> None:
    outcome, found = classify(row(**changes), "HOLD", 15)
    assert outcome == "warning"
    assert code in {item["code"] for item in found}
    assert code in REASONS


def test_rules_baseline_disagreement_is_not_a_reason() -> None:
    checks = json.dumps([{"name": "Rules baseline", "status": "warn", "detail": "differs"}])
    assert classify(row(output_checks=checks), "HOLD", 15) == ("success", [])


def test_error_outranks_warning_and_two_warnings_can_share_one_call() -> None:
    checks = json.dumps([{"name": "Signal claims", "status": "warn", "detail": "x"}])
    assert classify(row(status="timeout", output_checks=checks), "HOLD", 15)[0] == "error"
    slow = {
        "latency_ms": 16_000,
        "attempts": json.dumps([{"outcome": "sent", "latency_ms": 16_000}]),
        "output_checks": checks,
    }
    assert len(classify(row(**slow), "HOLD", 15)[1]) == 2


def test_sections_order_unknown_other_and_values_unchanged() -> None:
    brief = {
        "pair": "SOL-EUR",
        "now": "2026-10-11T12:00:00Z",
        "price": 100.25,
        "regime": {"trend": {"value": "up", "read": "rising"}},
        "tf": {"1h": {"close": 100.25}, "4h": {"close": 99}, "1d": {"close": 98}},
        "unknown": {"x": 1},
    }
    request = json.dumps({"input": json.dumps(brief)})
    out = sections(request)
    titles = [item["title"] for item in out]
    assert titles[:4] == ["Call", "Regime", "1h", "4h"]
    assert titles[-1] == "Other"
    assert out[0]["rows"][2]["value"] == "100.25"
    assert any(row["path"] == "unknown.x" and row["value"] == "1" for row in out[-1]["rows"])
