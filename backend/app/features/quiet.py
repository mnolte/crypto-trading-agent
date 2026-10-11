"""The "Why so quiet?" line — one sentence, computed in code each cycle.

Reads the decisions of the latest cycle and reports, in order: pre-filter
failures grouped by reason, stale pairs, how many pairs reached the model
and what it decided, the gates that blocked trades, and how long since the
last trade. Safety suffixes follow: the shock brake, a global entry pause,
then per-coin pauses. No model is involved.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import repo
from app.db import AMSTERDAM, DecisionRow, EntryPauseRow


def _age(delta: timedelta) -> str:
    """ "31h" / "1 min" / "45 s" / "3d 2h" — the same shape the UI uses."""
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{max(1, s // 60)} min"
    h = s // 3600
    m = (s % 3600) // 60
    if h >= 24:
        d = h // 24
        rem = h % 24
        return f"{d}d {rem}h" if rem else f"{d}d"
    return f"{h}h {m}m" if m else f"{h}h"


def _until_text(until: datetime, now: datetime) -> str:
    local_u = until.astimezone(AMSTERDAM)
    local_n = now.astimezone(AMSTERDAM)
    if local_u.date() == local_n.date():
        return local_u.strftime("%H:%M")
    return local_u.strftime("%d %b %H:%M")


def _c1_failed(row: DecisionRow) -> bool:
    try:
        signals = json.loads(row.signals)
    except (TypeError, json.JSONDecodeError):
        return False
    for signal in signals:
        if signal.get("name") == "C1":
            return bool(signal.get("read") != "bullish")
    return False


def _is_stale(row: DecisionRow) -> bool:
    return bool(row.error and "stale" in row.error.lower())


def _base(pair: str) -> str:
    return pair.split("-")[0]


def quiet_line(
    session: Session,
    account_id: int,
    *,
    now: datetime,
    shock_brake_until: datetime | None = None,
) -> str:
    """Compose the Dashboard status line for the latest cycle with
    decisions, or "" when nothing has run yet."""
    cycle = repo.latest_decision_cycle(session, account_id)
    sentences: list[str] = []
    if cycle is not None:
        decisions = repo.decisions_in_cycle(session, account_id, cycle)
        n = len(decisions)
        stale = [d for d in decisions if _is_stale(d)]
        pre = [d for d in decisions if not _is_stale(d) and d.prefilter is False]
        model = [d for d in decisions if d.decider == "openai"]
        rules_asked = [
            d for d in decisions if d.decider == "rules" and d.prefilter and not _is_stale(d)
        ]
        skipped = [d for d in decisions if d.action == "SKIP" and d.error]

        if stale:
            sentences.append(f"{len(stale)} of {n} pairs had stale data")
        if pre:
            trend_down = sum(1 for d in pre if _c1_failed(d))
            weak = len(pre) - trend_down
            bits = []
            if trend_down:
                bits.append(f"4h trend down on {trend_down}")
            if weak:
                bits.append(f"too weak on {weak}")
            detail = f" ({', '.join(bits)})" if bits else ""
            sentences.append(f"{len(pre)} of {n} pairs failed the pre-filter{detail}")
        if model:
            bits = []
            buys = [d for d in model if d.action == "BUY"]
            sells = [d for d in model if d.action == "SELL"]
            held = len(model) - len(buys) - len(sells) - sum(1 for d in model if d.action == "SKIP")
            if buys:
                pairs = ", ".join(_base(d.pair) for d in buys)
                bits.append(f"{len(buys)} BUY ({pairs})")
            if sells:
                pairs = ", ".join(_base(d.pair) for d in sells)
                bits.append(f"{len(sells)} SELL ({pairs})")
            if held:
                bits.append(f"{held} held" if len(bits) or held > 1 else "held")
            sentences.append(f"{len(model)} reached the model: {', '.join(bits)}")
        elif rules_asked:
            held = sum(1 for d in rules_asked if d.action in ("HOLD", "SKIP"))
            tail = "all held" if held == len(rules_asked) else ""
            sentences.append(
                f"{len(rules_asked)} passed the pre-filter; Rules answered"
                + (f" ({tail})" if tail else "")
            )
        gates: dict[str, int] = {}
        for row in skipped:
            if row.error:
                gates[row.error] = gates.get(row.error, 0) + 1
        for gate_name, count in sorted(gates.items(), key=lambda kv: -kv[1]):
            sentences.append(
                f"the {gate_name} gate blocked {count}" + (" buy" if count == 1 else " buys")
            )
        last = repo.last_trade_time(session, account_id)
        sentences.append(
            f"Last trade {_age(now - last)} ago" if last is not None else "No trades yet"
        )

    # Safety suffixes, global first: shock brake, all-coins pause, per-coin.
    if shock_brake_until is not None and shock_brake_until > now:
        sentences.append(f"Shock brake on until {_until_text(shock_brake_until, now)}")
    global_pause: EntryPauseRow | None = None
    per_coin: list[EntryPauseRow] = []
    for pause in repo.list_entry_pauses(session, now):
        if pause.pair == "*":
            global_pause = pause
        else:
            per_coin.append(pause)
    if global_pause is not None:
        if global_pause.until is None:
            sentences.append("All new entries paused until resumed")
        else:
            until = repo.aware_utc(global_pause.until)
            sentences.append(f"All new entries paused until {_until_text(until, now)}")
    for pause in per_coin:
        if pause.until is None:
            sentences.append(f"{_base(pause.pair)} paused until resumed")
        else:
            remaining = repo.aware_utc(pause.until) - now
            sentences.append(f"{_base(pause.pair)} paused for {_age(remaining)}")
    return ". ".join(sentences) + ("." if sentences else "")
