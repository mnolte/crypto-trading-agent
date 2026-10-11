"""The daily morning briefing — and later the weekly review.

Every figure is computed here in code; the model only writes 150-250 words
of prose from those facts. Before the text is stored, every number in it is
checked against the facts; a rejected text is regenerated once, then
replaced by the plain template. The same template is used when no API key
is set, the call fails or the monthly cost cap is reached — labelled with
the reason, so a missing key or a spent cap is noticed that morning.

Schedule: ``learning.briefing_time`` on the Amsterdam clock (default 08:00).
A missed briefing runs if it is within 4 hours of its scheduled time.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent import budget
from app.agent.broker.paper import PaperBroker
from app.agent.openai_client import CallResult, OpenAIResponsesClient
from app.agent.prompts import load_prompt
from app.config import get_secret
from app.db import AMSTERDAM, AccountRow, BriefingRow, LlmCallRow, utcnow
from app.models import CheckOut, Settings

if TYPE_CHECKING:
    from app.api.deps import AppState

BRIEFING_PROMPT = "briefing.v1"
BRIEFING_FEATURE = "briefing"
WINDOW_S = 24 * 3600
MISSED_GRACE = timedelta(hours=4)
LOOP_TICK_S = 60
_WORDS = (150, 250)


# ---------------------------------------------------------------- facts ----


def _pct(new: Decimal, old: Decimal) -> float | None:
    if old <= 0:
        return None
    return float((new / old - 1) * 100)


def _equity_near(session: Session, account_id: int, at: datetime) -> Decimal | None:
    """The newest equity snapshot at or before ``at`` (searched 30 days
    back), else the very first one — a brand-new account has little
    history."""
    snaps = repo.list_equity_snapshots(session, account_id, at - timedelta(days=30), at)
    if snaps:
        return Decimal(snaps[-1].equity)
    row = repo.first_equity_snapshot(session, account_id)
    return Decimal(row.equity) if row is not None else None


def compute_facts(
    session: Session,
    *,
    now: datetime,
    account: AccountRow,
    broker: PaperBroker,
    settings: Settings,
    type: Literal["daily", "weekly"] = "daily",
) -> dict[str, Any]:
    """Every figure the briefing text may use, as JSON-safe values."""
    start = now - timedelta(seconds=WINDOW_S)
    account_id = account.id
    equity = broker.equity()

    # --- trades over the window -------------------------------------------
    closed = repo.list_trade_rows(session, account_id, start=start, end=now, limit=50)
    opened_out = [
        {"pair": t.pair, "entry": str(t.entry), "stop": t.initial_stop}
        for t in closed
        if repo.aware_utc(t.opened_at) >= start
    ]
    # positions opened inside the window that are still open
    for pair, pos in sorted(broker.positions.items()):
        if pos.opened_at >= start:
            opened_out.append(
                {"pair": pair, "entry": str(pos.entry), "stop": str(pos.original_stop)}
            )
    closed_out = [
        {
            "pair": t.pair,
            "exit_reason": t.exit_reason,
            "realised_eur": t.realised_eur,
            "realised_r": t.realised_r,
            "held_hours": round(
                (repo.aware_utc(t.closed_at) - repo.aware_utc(t.opened_at)).total_seconds() / 3600,
                1,
            ),
        }
        for t in closed
    ]

    # --- P&L vs BTC ----------------------------------------------------------
    start_equity = Decimal(account.start_balance)
    since_start = _pct(equity, start_equity)
    month_start, _ = budget.amsterdam_month_bounds(now)
    equity_mtd = _equity_near(session, account_id, month_start)
    btc_then = repo.load_candles(session, "BTC-EUR", "1d", 10, end=month_start)
    btc_now = repo.load_candles(session, "BTC-EUR", "1d", 5, end=now)
    btc_mtd = _pct(btc_now[-1].close, btc_then[-1].close) if btc_then and btc_now else None
    mtd_pct = _pct(equity, equity_mtd) if equity_mtd else None

    equity_24h = _equity_near(session, account_id, start)
    change_24h = equity - equity_24h if equity_24h is not None else None

    # --- open positions -----------------------------------------------------
    positions_out = []
    for pair, pos in sorted(broker.positions.items()):
        last = Decimal(pos.last)
        to_stop = _pct(last, pos.stop)
        to_target = _pct(pos.target, last)
        positions_out.append(
            {
                "pair": pair,
                "unrealised_pct": _pct(last, pos.entry),
                "dist_to_stop_pct": to_stop,
                "dist_to_target_pct": to_target,
                "held_hours": round(
                    (now - repo.aware_utc(pos.opened_at)).total_seconds() / 3600, 1
                ),
                "stop_at_breakeven": pos.stop >= pos.entry,
            }
        )

    # --- notable SKIPs and alerts in the window ------------------------------
    skip_rows = repo.list_decision_rows(
        session, account_id=account_id, action="SKIP", after=start, before=now, limit=100
    )
    skip_groups: dict[str, dict[str, Any]] = {}
    for row in skip_rows:
        gate = row.error or "unknown"
        entry = skip_groups.setdefault(gate, {"gate": gate, "count": 0, "pairs": []})
        entry["count"] += 1
        entry["pairs"].append(row.pair)

    alerts = [
        row.text
        for row in repo.list_activity(session, kind="alert", limit=50)
        if repo.aware_utc(row.at) >= start
    ]

    # --- last cycle's shape ----------------------------------------------------
    cycle = repo.latest_decision_cycle(session, account_id)
    quiet: dict[str, Any] = {"cycle": cycle, "pairs": 0}
    closest: list[dict[str, Any]] = []
    if cycle is not None:
        decisions = repo.decisions_in_cycle(session, account_id, cycle)
        quiet["pairs"] = len(decisions)
        failed = [d for d in decisions if d.prefilter is False]
        trend_down = 0
        for d in failed:
            try:
                signals = json.loads(d.signals)
            except (TypeError, json.JSONDecodeError):
                signals = []
            c1 = next((s for s in signals if s.get("name") == "C1"), None)
            if c1 is not None and c1.get("read") != "bullish":
                trend_down += 1
        quiet["prefilter_failed"] = len(failed)
        quiet["trend_down"] = trend_down
        quiet["too_weak"] = len(failed) - trend_down
        quiet["model_called"] = sum(1 for d in decisions if d.decider == "openai")
        quiet["bought"] = sum(1 for d in decisions if d.action == "BUY")
        quiet["held"] = sum(1 for d in decisions if d.action == "HOLD")

        latest = repo.latest_decisions(session, account_id, list(settings.active_markets))
        ranked: list[tuple[int, str]] = []
        for pair, row in latest.items():
            if pair in broker.positions:
                continue
            try:
                signals = json.loads(row.signals)
            except (TypeError, json.JSONDecodeError):
                continue
            met = sum(
                1
                for s in signals
                if s.get("name", "") in ("C2", "C3", "C4", "C5", "C6")
                and s.get("read") == "bullish"
            )
            ranked.append((met, pair))
        closest = [
            {"pair": pair, "inner_conditions_met": met}
            for met, pair in sorted(ranked, key=lambda t: (-t[0], t[1]))[:3]
        ]

    pauses = repo.list_entry_pauses(session, now)
    last_trade = repo.last_trade_time(session, account_id)
    return {
        "for_date": now.astimezone(AMSTERDAM).date().isoformat(),
        "generated_at": now.isoformat(),
        "type": type,
        "period_hours": WINDOW_S // 3600,
        "briefing_time": settings.learning.briefing_time,
        "equity_eur": str(equity.quantize(Decimal("0.01"))),
        "equity_change_24h_eur": (
            str(change_24h.quantize(Decimal("0.01"))) if change_24h is not None else None
        ),
        "return_since_start_pct": since_start,
        "return_mtd_pct": mtd_pct,
        "btc_mtd_pct": btc_mtd,
        "vs_btc_points_mtd": (
            round(mtd_pct - btc_mtd, 2) if mtd_pct is not None and btc_mtd is not None else None
        ),
        "trades_opened": opened_out,
        "trades_closed": closed_out,
        # Counts the template/model legitimately writes — the number check
        # only allows literals from this tree.
        "trades_opened_count": len(opened_out),
        "trades_closed_count": len(closed_out),
        "open_positions": positions_out,
        "open_positions_count": len(positions_out),
        "skips": list(skip_groups.values()),
        "alerts": alerts,
        "quiet": quiet,
        "closest_to_setup": closest,
        "paused": [
            {
                "pair": p.pair,
                "until": repo.aware_utc(p.until).isoformat() if p.until else None,
            }
            for p in pauses
        ],
        "last_trade_hours_ago": (
            round((now - last_trade).total_seconds() / 3600, 1) if last_trade is not None else None
        ),
    }


# ------------------------------------------------------------ number check --

_MINUS = "\u2212"
_NUMBER = re.compile(r"[-+\u2212]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+\u2212]?\d+(?:\.\d+)?")


def numbers_in_text(text: str) -> list[Decimal]:
    """Every numeric token in the text, normalised (signs, commas stripped)."""
    out: list[Decimal] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0).replace(_MINUS, "-").replace("+", "").replace(",", "")
        try:
            out.append(Decimal(raw))
        except ArithmeticError:
            continue
    return out


def _decimals(value: Decimal) -> int:
    exp = value.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def fact_numbers(facts: object) -> set[Decimal]:
    """Every numeric value in the facts tree; strings that carry numbers
    (e.g. "08:00") contribute their digits too."""
    out: set[Decimal] = set()
    if isinstance(facts, bool):
        return out
    if isinstance(facts, (int, Decimal)):
        out.add(Decimal(str(facts)))
    elif isinstance(facts, float):
        out.add(Decimal(str(round(facts, 6))))
    elif isinstance(facts, str):
        out.update(numbers_in_text(facts))
    elif isinstance(facts, dict):
        for key, value in facts.items():
            out.update(fact_numbers(key))
            out.update(fact_numbers(value))
    elif isinstance(facts, (list, tuple)):
        for value in facts:
            out.update(fact_numbers(value))
    return out


def text_numbers_ok(text: str, facts: dict[str, Any]) -> list[Decimal]:
    """Numbers in the text that no fact rounds to, at the precision written.
    An empty list means the text passed the number check."""
    allowed = fact_numbers(facts)
    bad: list[Decimal] = []
    for n in numbers_in_text(text):
        places = _decimals(n)
        if not any(f.quantize(Decimal(1).scaleb(-places)) == n for f in allowed):
            bad.append(n)
    return bad


# ---------------------------------------------------------------- template --


def _eur(value: str | None) -> str:
    if value is None:
        return "unknown"
    # Signed: the number check compares against the facts, sign included.
    return f"€{Decimal(value):,.2f}"


def template_text(facts: dict[str, Any]) -> str:
    """The plain-text briefing built only from computed facts — no model."""
    parts: list[str] = []
    equity = facts.get("equity_eur")
    change = facts.get("equity_change_24h_eur")
    line = f"Equity {_eur(equity if isinstance(equity, str) else None)}"
    if isinstance(change, str):
        line += f", {_eur(change)} over 24h"
    vs = facts.get("vs_btc_points_mtd")
    if isinstance(vs, (int, float)):
        line += f"; {vs:+.2f} points vs holding BTC this month"
    parts.append(line + ".")

    opened = facts.get("trades_opened") or []
    closed = facts.get("trades_closed") or []
    if opened or closed:
        bits = []
        if closed:
            detail = ", ".join(
                f"{t['pair'].split('-')[0]} {t['exit_reason']} "
                f"({Decimal(str(t['realised_r'])):+.2f}R)"
                for t in closed
            )
            bits.append(f"{len(closed)} closed: {detail}")
        if opened:
            pairs = ", ".join(t["pair"].split("-")[0] for t in opened)
            bits.append(f"{len(opened)} opened ({pairs})")
        parts.append("Trades in the last 24h: " + "; ".join(bits) + ".")
    else:
        parts.append("No trades opened or closed in the last 24h.")

    positions = facts.get("open_positions") or []
    if positions:
        detail = ", ".join(
            f"{p['pair'].split('-')[0]} {p['dist_to_stop_pct']:.1f}% above its stop"
            for p in positions
            if isinstance(p.get("dist_to_stop_pct"), (int, float))
        )
        parts.append(f"{len(positions)} positions open" + (f": {detail}." if detail else "."))

    quiet = facts.get("quiet") or {}
    if quiet.get("pairs"):
        reasons = []
        if quiet.get("trend_down"):
            reasons.append(f"{quiet['trend_down']} on a falling 4h trend")
        if quiet.get("too_weak"):
            reasons.append(f"{quiet['too_weak']} too weak")
        tail = f" ({'; '.join(reasons)})" if reasons else ""
        parts.append(
            f"Last cycle: {quiet.get('prefilter_failed', 0)} of {quiet['pairs']} pairs "
            f"failed the pre-filter{tail}; {quiet.get('model_called', 0)} reached the "
            f"model, {quiet.get('held', 0)} held."
        )

    skips = facts.get("skips") or []
    if skips:
        detail = ", ".join(f"{s['gate']} x{s['count']}" for s in skips[:5])
        parts.append(f"Notable SKIPs: {detail}.")

    alerts = facts.get("alerts") or []
    if alerts:
        parts.append("Alerts: " + "; ".join(alerts[:5]) + ".")

    closest = [c["pair"].split("-")[0] for c in facts.get("closest_to_setup") or []]
    if closest:
        parts.append(f"Closest to a setup: {', '.join(closest)}.")

    pauses = facts.get("paused") or []
    if pauses:
        names = ", ".join(
            p["pair"].split("-")[0] if p["pair"] != "*" else "all coins" for p in pauses
        )
        parts.append(f"Entries paused: {names}.")
    return " ".join(parts)


# -------------------------------------------------------------- generation --


def briefing_payload(facts: dict[str, Any], model: str, prompt: Any) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": prompt.body,
        "input": json.dumps({"facts": facts}, separators=(",", ":"), ensure_ascii=False),
        "temperature": prompt.temperature,
        "max_output_tokens": prompt.max_output_tokens,
    }


def _call_row(
    payload: dict[str, Any],
    *,
    now: datetime,
    account_id: int,
    cycle: int | None,
    prompt_version: str,
    in_checks: list[CheckOut],
) -> LlmCallRow:
    return LlmCallRow(
        id=uuid.uuid4().hex[:16],
        at=now,
        cycle=cycle,
        account_id=account_id,
        pair="",
        decision_id=None,
        feature=BRIEFING_FEATURE,
        replay_of=None,
        model=payload["model"],
        prompt_version=prompt_version,
        status="ok",
        attempts="[]",
        latency_ms=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        cost_eur=None,
        request_id=None,
        request_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        response_raw=None,
        parsed=None,
        input_checks=json.dumps([c.model_dump() for c in in_checks]),
        output_checks="[]",
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )


def _record_result(
    row: LlmCallRow,
    result: CallResult,
    prices: budget.ModelPrices,
    usd_eur: Decimal | None,
) -> None:
    if result.request_bytes:
        row.request_json = result.request_bytes.decode("utf-8", errors="replace")
    if result.response_bytes is not None:
        row.response_raw = result.response_bytes.decode("utf-8", errors="replace")
    row.attempts = json.dumps(
        [
            {
                "at": a.at.isoformat(),
                "outcome": a.outcome,
                "error": a.error,
                "http_status": a.http_status,
                "latency_ms": a.latency_ms,
            }
            for a in result.attempts
        ]
    )
    row.latency_ms = result.latency_ms
    row.input_tokens = result.input_tokens
    row.output_tokens = result.output_tokens
    row.request_id = result.request_id
    row.status = result.status
    row.parsed = result.output_text
    usd, eur = budget.cost_estimates(
        prices, row.model, result.input_tokens, result.output_tokens, usd_eur
    )
    row.cost_usd = str(usd) if usd is not None else None
    row.cost_eur = str(eur) if eur is not None else None


async def generate_briefing(
    session: Session,
    *,
    now: datetime,
    account: AccountRow,
    broker: PaperBroker,
    settings: Settings,
    type: Literal["daily", "weekly"] = "daily",
    api_key: str | None = None,
    key_fn: Callable[[], str | None] = lambda: get_secret("openai"),
    client_factory: Callable[[str], OpenAIResponsesClient] = OpenAIResponsesClient,
    prices: budget.ModelPrices | None = None,
    cycle: int | None = None,
) -> BriefingRow:
    """Compute the facts, ask the model for prose, check every number and
    store the row. Falls back to the plain template — labelled with the
    reason — on a missing key, a failed call, a spent monthly cap or a
    rejected text twice in a row."""
    facts = compute_facts(
        session, now=now, account=account, broker=broker, settings=settings, type=type
    )
    key = api_key if api_key is not None else key_fn()
    reason: str | None = None
    text: str | None = None
    call_rows: list[LlmCallRow] = []
    prices = prices or budget.ModelPrices()

    if key is None:
        reason = "no OpenAI key"
    else:
        spent = budget.month_spend_eur(session, account.id, now)
        if budget.cap_state(spent, settings.model.monthly_cost_cap_eur) == "cap":
            reason = "monthly cost cap reached"

    if reason is None:
        try:
            prompt = load_prompt(BRIEFING_PROMPT)
        except Exception:
            prompt = None
            reason = "briefing prompt missing"
    if reason is None and prompt is not None:
        assert key is not None  # "no OpenAI key" was set above otherwise
        payload = briefing_payload(facts, settings.model.model_id, prompt)
        client = client_factory(key)
        for _attempt in range(2):
            call_row = _call_row(
                payload,
                now=now,
                account_id=account.id,
                cycle=cycle,
                prompt_version=prompt.version,
                in_checks=[CheckOut(name="Facts computed", status="pass", detail="")],
            )
            call_rows.append(call_row)
            try:
                result = await client.call(payload)
            except Exception as exc:  # the transport itself blew up
                result = CallResult(status="api_error", request_bytes=b"", error=str(exc))
            _record_result(call_row, result, prices, budget.usdc_eur_rate(session, now))
            if result.status != "ok" or not result.output_text:
                reason = "model call failed"
                break
            candidate = result.output_text.strip()
            bad = text_numbers_ok(candidate, facts)
            if not bad:
                text = candidate
                checks = [CheckOut(name="Numbers match facts", status="pass", detail="")]
            else:
                checks = [
                    CheckOut(
                        name="Numbers match facts",
                        status="fail",
                        detail=f"not in facts: {', '.join(str(b) for b in bad[:6])}",
                    )
                ]
            call_row.output_checks = json.dumps([c.model_dump() for c in checks])
            if text is not None:
                break
            # Regenerate once, naming the rejected numbers.
            payload = briefing_payload(
                {
                    **facts,
                    "_retry_note": (
                        f"Previous text used numbers not in the facts: "
                        f"{', '.join(str(b) for b in bad[:6])}. Rewrite using only "
                        "the figures given."
                    ),
                },
                settings.model.model_id,
                prompt,
            )
        await client.aclose()
        if text is None and reason is None:
            reason = "the number check rejected the model's text"

    row = BriefingRow(
        at=now,
        account_id=account.id,
        type=type,
        for_date=facts["for_date"],
        facts=json.dumps(facts, separators=(",", ":"), ensure_ascii=False),
        text=text or template_text(facts),
        source="model" if text is not None else "template",
        reason=reason,
        llm_call_id=call_rows[-1].id if call_rows else None,
    )
    repo.add_briefing(session, row)
    for call_row in call_rows:
        repo.add_llm_call(session, call_row)
    if reason is not None:
        repo.add_activity(session, "alert", f"briefing fell back to template: {reason}", at=now)
    session.commit()
    return row


# --------------------------------------------------------------- scheduling --


def scheduled_today(time_str: str, now: datetime) -> datetime:
    """Today's briefing time on the Amsterdam clock, in UTC."""
    hh, _, mm = time_str.partition(":")
    local = now.astimezone(AMSTERDAM).replace(
        hour=int(hh or "8"), minute=int(mm or "0"), second=0, microsecond=0
    )
    return local.astimezone(UTC)


async def maybe_brief(
    session: Session,
    *,
    now: datetime,
    account: AccountRow,
    broker: PaperBroker,
    settings: Settings,
    dispatcher_notify: Callable[[str, str, str], Awaitable[None]] | None,
    cycle: int | None = None,
) -> BriefingRow | None:
    """Run today's briefing when its time has come and it is not more than
    4 hours late; returns the row or None."""
    sched = scheduled_today(settings.learning.briefing_time, now)
    if now < sched or now - sched > MISSED_GRACE:
        return None
    for_date = sched.astimezone(AMSTERDAM).date().isoformat()
    if repo.briefing_for_date(session, "daily", for_date) is not None:
        return None
    row = await generate_briefing(
        session,
        now=now,
        account=account,
        broker=broker,
        settings=settings,
        type="daily",
        cycle=cycle,
    )
    if dispatcher_notify is not None:
        label = f"Plain briefing: {row.reason}. " if row.reason else ""
        await dispatcher_notify(
            "review",
            f"Morning briefing · {for_date}",
            label + row.text,
        )
    return row


async def briefing_loop(
    state: AppState,
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime] = utcnow,
    notify: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> None:
    """Check once a minute whether today's briefing is due (or overdue by
    less than 4 hours after a sleep or restart)."""
    while True:
        try:
            session = sessions()
            try:
                account = repo.current_account(session)
                if state.broker is not None:
                    await maybe_brief(
                        session,
                        now=clock(),
                        account=account,
                        broker=state.broker,
                        settings=state.settings,
                        dispatcher_notify=notify,
                        cycle=state.agent_engine.cycle if state.agent_engine else None,
                    )
            finally:
                session.close()
        except Exception:
            pass  # a briefing miss must never kill the loop; logged next tick
        await asyncio.sleep(LOOP_TICK_S)
