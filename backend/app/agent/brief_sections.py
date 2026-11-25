"""Build the inspector's labelled brief sections from the stored request.

The sections are a view over the exact JSON sent to the model. No value is
recomputed: numbers and strings are only formatted for reading, reads are
copied from the brief, and any key this module does not know lands in
``Other`` so nothing sent is ever hidden.
"""

from __future__ import annotations

import json
from typing import Any

SECTION_ORDER = (
    "Call",
    "Regime",
    "1h",
    "4h",
    "Daily",
    "Structure",
    "Market context",
    "Liquidity",
    "Position",
    "Previous decision",
    "Limits",
    "Data age",
)

# Fixed top-level destinations. Everything else is intentionally "Other":
# pair_history, account, portfolio, time and trade_math are still shown,
# just not presented as one of the design's named sections.
_DESTINATIONS = {
    "regime": "Regime",
    "structure": "Structure",
    "market_context": "Market context",
    "liquidity": "Liquidity",
    "position": "Position",
    "previous_decision": "Previous decision",
    "limits": "Limits",
    "data_age_s": "Data age",
}
_TF_TITLES = {"1h": "1h", "4h": "4h", "1d": "Daily"}
_CALL_KEYS = ("pair", "now", "price")
_PERCENT_WORDS = ("pct", "percent", "share", "dist", "gap")


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _format(value: Any, path: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        out = _number(value)
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(word in leaf for word in _PERCENT_WORDS):
            out += "%"
        return out
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _label(path: str) -> str:
    leaf = path.rsplit(".", 1)[-1]
    if leaf.isdigit():
        leaf = path.rsplit(".", 2)[-2] + " " + str(int(leaf) + 1)
    return leaf.replace("_", " ")


def _rows(value: Any, path: str, out: list[dict[str, Any]]) -> None:
    """Flatten one brief value into label/value/read/path rows."""
    if isinstance(value, dict):
        if "value" in value and ("read" in value or "source" in value or "as_of" in value):
            out.append(
                {
                    "label": _label(path),
                    "value": _format(value.get("value"), path),
                    "read": value.get("read"),
                    "path": path,
                }
            )
            # source/as_of are part of what was sent; keep them visible as
            # their own rows rather than dropping them because the value
            # row consumed the dict.
            for extra in ("source", "as_of"):
                if extra in value:
                    child = f"{path}.{extra}"
                    out.append(
                        {
                            "label": _label(child),
                            "value": _format(value.get(extra), child),
                            "read": None,
                            "path": child,
                        }
                    )
            return
        for key in value:
            child = f"{path}.{key}" if path else str(key)
            _rows(value[key], child, out)
        return
    if isinstance(value, list):
        if not value:
            out.append({"label": _label(path), "value": "[]", "read": None, "path": path})
            return
        for index, item in enumerate(value):
            _rows(item, f"{path}.{index}", out)
        return
    out.append({"label": _label(path), "value": _format(value, path), "read": None, "path": path})


def _brief_from_request(request_json: str | dict[str, Any]) -> dict[str, Any]:
    try:
        request = json.loads(request_json) if isinstance(request_json, str) else request_json
    except json.JSONDecodeError:
        return {}
    if not isinstance(request, dict):
        return {}
    data = request.get("input")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def sections(request_json: str | dict[str, Any]) -> list[dict[str, Any]]:
    """The design's sections for brief v1/v2, plus ``Other`` when needed."""
    brief = _brief_from_request(request_json)
    grouped: dict[str, list[dict[str, Any]]] = {title: [] for title in SECTION_ORDER}
    other: list[dict[str, Any]] = []

    for key in brief:
        value = brief[key]
        rows: list[dict[str, Any]] = []
        if key in _CALL_KEYS:
            _rows(value, key, rows)
            grouped["Call"].extend(rows)
        elif key == "tf" and isinstance(value, dict):
            for tf in ("1h", "4h", "1d"):
                if tf not in value:
                    continue
                tf_rows: list[dict[str, Any]] = []
                _rows(value[tf], f"tf.{tf}", tf_rows)
                grouped[_TF_TITLES[tf]].extend(tf_rows)
        elif key in _DESTINATIONS:
            _rows(value, key, rows)
            grouped[_DESTINATIONS[key]].extend(rows)
        else:
            _rows(value, key, rows)
            other.extend(rows)

    out = [{"title": title, "rows": grouped[title]} for title in SECTION_ORDER if grouped[title]]
    if other:
        out.append({"title": "Other", "rows": other})
    return out
