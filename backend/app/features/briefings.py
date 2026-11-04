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
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent import budget
from app.agent.broker.paper import PaperBroker
from app.agent.openai_client import CallResult, OpenAIResponsesClient
from app.agent.prompts import load_prompt
from app.config import get_secret
from app.db import AMSTERDAM, AccountRow, BriefingRow, LlmCallRow, TradeRow, utcnow
from app.models import CheckOut, Settings

log = logging.getLogger(__name__)

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
                and s.get("tf") == "1h"
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
# A full stop ends a sentence only when it is not a decimal point.
_SENT_END = re.compile(r"[.!?](?!\d)|\n")
# Words that put a minus on an unsigned magnitude ("a decline of 2.30%",
# "down €12.50"). Consulted only when a written number matches no fact
# but its negation does — so "gained 2.30%" for a -2.30% fact still fails.
_NEGATIVE_WORDS = re.compile(
    r"\b(?:declin\w*|down\w*|dropp?\w*|fell|fall(?:s|ing|en)?|decreas\w*|"
    r"loss(?:es)?|lost|lower(?:ed|ing)?|minus|negativ\w*|shed|slid\w*|"
    r"slipp?\w*|dipp?\w*|below|underwater|red|retreat\w*|sank|sunk|"
    r"weaken\w*|drawdown|worse|worst|behind|lag\w*|trailing|underperform\w*|"
    r"deficit|off|tumble\w*|slump\w*|plunge\w*|crater\w*|pullback|"
    r"correction|shrank|shrunk|halv\w*|cut|gave back|pared|erased)\b",
    re.IGNORECASE,
)
# An ISO 8601 date or datetime inside a fact string ("2026-10-13",
# "2026-10-13T03:00:45.000025+00:00"). The tokeniser above reads a
# date's hyphens as minus signs and loses the day and month; every
# component is a fact number on its own, because the text may write
# the same moment in another format ("October 13, 2026", "13-10-2026").
_ISO_MOMENT = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)"
)
# A date or clock time as written in the text — ISO or day-first order
# with -, / or . separators, or HH:MM[:SS]. Checked part by part, so
# "13-10-2026" and "13.10.2026" pass the way "2026-10-13" does.
_TEXT_MOMENT = re.compile(
    r"(?<!\d)(?:\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{1,2}:?\d{2})?)?|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?!\d)"
)


def _number_tokens(text: str) -> Iterator[tuple[Decimal, int, int, bool]]:
    """Every numeric token: value, span and whether a sign was written."""
    for match in _NUMBER.finditer(text):
        raw = match.group(0)
        signed = raw[0] in "-+\u2212"
        cleaned = raw.replace(_MINUS, "-").replace("+", "").replace(",", "")
        try:
            yield Decimal(cleaned), match.start(), match.end(), signed
        except ArithmeticError:
            continue


def numbers_in_text(text: str) -> list[Decimal]:
    """Every numeric token in the text, normalised (signs, commas stripped)."""
    return [n for n, _, _, _ in _number_tokens(text)]


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence containing the match; decimal points don't end it."""
    left = 0
    for m in _SENT_END.finditer(text, 0, start):
        left = m.end()
    nxt = _SENT_END.search(text, end)
    right = nxt.start() if nxt else len(text)
    return text[left:right]


def _decimals(value: Decimal) -> int:
    exp = value.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _moment_numbers(text: str) -> set[Decimal]:
    """The components of each ISO date or datetime inside a fact string —
    year, month, day, hour, minute, second and zone offset — as separate
    numbers, plus, for an aware moment, the same components in
    Europe/Amsterdam, the display timezone."""
    out: set[Decimal] = set()

    def parts(moment: datetime, with_time: bool) -> None:
        out.update(Decimal(p) for p in (moment.year, moment.month, moment.day))
        if with_time:
            out.update(Decimal(p) for p in (moment.hour, moment.minute))
            out.add(Decimal(moment.second))
            if moment.microsecond:
                out.add(Decimal(moment.second) + Decimal(moment.microsecond) / 1_000_000)

    for match in _ISO_MOMENT.finditer(text):
        try:
            moment = datetime.fromisoformat(match.group(0))
        except ValueError:
            continue
        parts(moment, ":" in match.group(0))
        offset = moment.utcoffset()
        if offset is not None:
            secs = int(offset.total_seconds())
            hours, rem = divmod(abs(secs), 3600)
            out.add(Decimal(hours if secs >= 0 else -hours))
            out.add(Decimal(rem // 60))
            parts(moment.astimezone(AMSTERDAM), ":" in match.group(0))
    return out


def fact_numbers(facts: object) -> set[Decimal]:
    """Every numeric value in the facts tree; strings that carry numbers
    (e.g. "08:00") contribute their digits too, and ISO dates and
    datetimes contribute their components in every format."""
    out: set[Decimal] = set()
    if isinstance(facts, bool):
        return out
    if isinstance(facts, (int, Decimal)):
        out.add(Decimal(str(facts)))
    elif isinstance(facts, float):
        out.add(Decimal(str(round(facts, 6))))
        # The model sees the float's full repr in the JSON brief, so the
        # verbatim spelling is a fact too — not only its 6-decimal rounding.
        out.add(Decimal(str(facts)))
    elif isinstance(facts, str):
        out.update(numbers_in_text(facts))
        out.update(_moment_numbers(facts))
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
    An empty list means the text passed the number check. A date or clock
    time is checked part by part, so a fact's moment may be written in
    another format ("October 13, 2026", "13-10-2026", "05:00"). A
    magnitude written without a sign may stand in for a negative fact
    when the words around it carry the sign ("a decline of 2.30%",
    "down €12.50"); without such a word it fails, so a wrongly signed
    claim is caught."""
    allowed = fact_numbers(facts)
    bad: list[Decimal] = []
    quantum = Decimal(1)

    def matches(n: Decimal) -> bool:
        return any(f.quantize(quantum.scaleb(-_decimals(n))) == n for f in allowed)

    moments = list(_TEXT_MOMENT.finditer(text))
    for moment in moments:
        for piece in re.findall(r"\d+(?:\.\d+)?", moment.group(0)):
            n = Decimal(piece)
            head, dot, tail = piece.partition(".")
            if not matches(n) and not (dot and matches(Decimal(head)) and matches(Decimal(tail))):
                bad.append(n)
    for n, start, end, signed in _number_tokens(text):
        if any(m.start() <= start and end <= m.end() for m in moments):
            continue
        if matches(n):
            continue
        if (
            not signed
            and any(f < 0 and f.quantize(quantum.scaleb(-_decimals(n))) == -n for f in allowed)
            and _NEGATIVE_WORDS.search(_sentence_around(text, start, end))
        ):
            continue
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
    feature: str = BRIEFING_FEATURE,
) -> LlmCallRow:
    return LlmCallRow(
        id=uuid.uuid4().hex[:16],
        at=now,
        cycle=cycle,
        account_id=account_id,
        pair="",
        decision_id=None,
        feature=feature,
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
    rejected text twice in a row. Weekly reviews use their own facts,
    prompt and template."""
    if type == "weekly":
        facts = compute_weekly_facts(
            session, now=now, account=account, broker=broker, settings=settings
        )
    else:
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
        spent = budget.month_spend_strict(session, now)
        if budget.cap_state(spent, settings.model.monthly_cost_cap_eur) == "cap":
            reason = "monthly cost cap reached"

    if reason is None:
        try:
            prompt = load_prompt(REVIEW_PROMPT if type == "weekly" else BRIEFING_PROMPT)
        except Exception:  # noqa: BLE001 - a missing prompt file is an expected condition
            prompt = None
            reason = "review prompt missing" if type == "weekly" else "briefing prompt missing"
    if reason is None and prompt is not None:
        assert key is not None  # "no OpenAI key" was set above otherwise
        feature = REVIEW_FEATURE if type == "weekly" else BRIEFING_FEATURE
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
                feature=feature,
            )
            call_rows.append(call_row)
            # No transaction may stay open across the model call — the
            # rate read below would otherwise upgrade a stale snapshot.
            session.commit()
            try:
                result = await client.call(payload)
            except Exception as exc:  # noqa: BLE001 - the transport itself blew up
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
        # The last rate read's transaction ends here — the briefing row
        # and call rows write in a fresh one.
        session.commit()
        if text is None and reason is None:
            reason = "the number check rejected the model's text"

    row = BriefingRow(
        at=now,
        account_id=account.id,
        type=type,
        for_date=facts["for_date"],
        facts=json.dumps(facts, separators=(",", ":"), ensure_ascii=False),
        text=text or (weekly_template_text(facts) if type == "weekly" else template_text(facts)),
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
                    await maybe_weekly_review(
                        session,
                        now=clock(),
                        account=account,
                        broker=state.broker,
                        settings=state.settings,
                        dispatcher_notify=notify,
                    )
            finally:
                session.close()
        except Exception:
            log.exception("briefing loop tick failed", extra={"event": "job.failed"})
        await asyncio.sleep(LOOP_TICK_S)


# --------------------------------------------------------- weekly review --

REVIEW_PROMPT = "review.v1"
REVIEW_FEATURE = "review"
WEEK_S = 7 * 24 * 3600
# ``learning.weekly_review`` enum → (Amsterdam weekday, Amsterdam hour).
WEEKLY_SCHEDULE = {"mon_08": (0, 8), "sun_20": (6, 20), "off": None}


def compute_weekly_facts(
    session: Session,
    *,
    now: datetime,
    account: AccountRow,
    broker: PaperBroker,
    settings: Settings,
) -> dict[str, Any]:
    """The week's metrics vs the previous week and BTC, the three best and
    worst trades, patterns in Wrong-marked decisions grouped by the output
    check that fired, the insights that changed with finding-level data and
    one evidence-backed suggestion."""
    account_id = account.id
    week_start = now - timedelta(seconds=WEEK_S)
    prev_start = week_start - timedelta(seconds=WEEK_S)

    eq_now = broker.equity()
    eq_week = _equity_near(session, account_id, week_start)
    eq_prev = _equity_near(session, account_id, prev_start)
    btc_then = repo.load_candles(session, "BTC-EUR", "1d", 10, end=week_start)
    btc_now = repo.load_candles(session, "BTC-EUR", "1d", 5, end=now)

    closed_week = [
        t
        for t in repo.list_trade_rows(session, account_id, start=week_start, end=now, limit=200)
        if t.closed_at is not None
    ]
    closed_prev = [
        t
        for t in repo.list_trade_rows(
            session, account_id, start=prev_start, end=week_start, limit=200
        )
        if t.closed_at is not None
    ]

    def _trade(t: TradeRow) -> dict[str, Any]:
        return {
            "id": t.id,
            "pair": t.pair,
            "realised_r": t.realised_r,
            "realised_eur": t.realised_eur,
            "exit_reason": t.exit_reason,
            "decision_id": t.decision_id,
            "decision_href": f"/?decision={t.decision_id}" if t.decision_id else None,
            "trade_href": f"/performance?trade={t.id}",
        }

    by_r = sorted(closed_week, key=lambda t: Decimal(t.realised_r or "0"), reverse=True)
    best = [_trade(t) for t in by_r[:3] if Decimal(t.realised_r or "0") > 0]
    worst = [_trade(t) for t in by_r[-3:] if Decimal(t.realised_r or "0") < 0]
    worst.reverse()

    # Decisions the owner marked Wrong, grouped by the output check that
    # fired on that call ("all checks passed" gets its own bucket).
    wrong_groups: dict[str, int] = {}
    wrong_call_count = 0
    for row in session.scalars(
        select(LlmCallRow).where(
            LlmCallRow.account_id == account_id,
            LlmCallRow.at >= week_start.replace(tzinfo=None),
            LlmCallRow.verdict == "wrong",
        )
    ).all():
        wrong_call_count += 1
        try:
            checks = json.loads(row.output_checks or "[]")
        except json.JSONDecodeError:
            checks = []
        fired = [c.get("name", "?") for c in checks if c.get("status") != "pass"]
        for name in fired or ["no failed output check"]:
            wrong_groups[name] = wrong_groups.get(name, 0) + 1
    wrong_patterns = [
        {"check": k, "count": v} for k, v in sorted(wrong_groups.items(), key=lambda kv: -kv[1])
    ]

    # Finding-level insights on the week's decisions; "changed" compares
    # them with the previous stored review's findings.
    from app.features.insights import compute_insights

    insights_now = compute_insights(
        session, now=now, settings=settings, dimension="weekday", start=week_start, end=now
    )
    insights_pair = compute_insights(
        session, now=now, settings=settings, dimension="pair", start=week_start, end=now
    )
    findings_now = [
        {
            "dimension": dim,
            "key": s.key,
            "avg_r": s.avg_r,
            "win_rate_pct": s.buy_win_rate_pct,
        }
        for dim, view in (("weekday", insights_now), ("pair", insights_pair))
        for s in view.slices
        if s.state == "finding"
    ]
    prev_review = repo.latest_briefing(session, "weekly")
    prev_findings: dict[tuple[str, str], float | None] = {}
    if prev_review is not None:
        try:
            prev_facts = json.loads(prev_review.facts)
            for f in prev_facts.get("insights_findings", []):
                prev_findings[(f["dimension"], f["key"])] = f.get("avg_r")
        except (json.JSONDecodeError, KeyError, TypeError):
            prev_findings = {}

    def _changed(f: dict[str, Any]) -> bool:
        prev = prev_findings.get((str(f["dimension"]), str(f["key"])))
        if (f["dimension"], f["key"]) not in prev_findings:
            return True
        return prev is not None and f["avg_r"] is not None and abs(float(f["avg_r"]) - prev) >= 0.3

    insights_changed = [f for f in findings_now if _changed(f)]

    # One evidence-backed suggestion: the weakest finding-level slice gets
    # a "Try in What-if" pointer.
    suggestion = None
    weakest = min(
        (
            s
            for s in insights_pair.slices + insights_now.slices
            if s.state == "finding" and s.avg_r is not None and s.avg_r < 0
        ),
        key=lambda s: s.avg_r or 0.0,
        default=None,
    )
    if weakest is not None:
        hint = (
            "Try skipping weekends in What-if"
            if weakest.key in ("Saturday", "Sunday")
            else f"Try removing {weakest.key} in What-if"
        )
        suggestion = {
            "text": (
                f"{weakest.label} BUYs average {weakest.avg_r:+.2f}R over "
                f"{weakest.decisions} decisions this week"
            ),
            "whatif_hint": hint,
            "whatif_href": "/whatif",
        }

    def _avg_r(trades: list[TradeRow]) -> float | None:
        rs = [Decimal(t.realised_r) for t in trades if t.realised_r is not None]
        return float(sum(rs) / len(rs)) if rs else None

    def _drawdown(start: datetime, end: datetime) -> float | None:
        """Worst peak-to-trough move in the equity snapshots of the window."""
        snaps = repo.list_equity_snapshots(session, account_id, start, end)
        peak: Decimal | None = None
        worst_dd = Decimal("0")
        for snap in snaps:
            eq = Decimal(snap.equity)
            if peak is None or eq > peak:
                peak = eq
            if peak and peak > 0:
                dd = (eq - peak) / peak * 100
                if dd < worst_dd:
                    worst_dd = dd
        return float(worst_dd) if snaps else None

    week_spend = repo.llm_costs_by_feature_all(session, week_start, now)
    calls_week = session.scalar(
        select(func.count())
        .select_from(LlmCallRow)
        .where(LlmCallRow.at >= week_start.replace(tzinfo=None))
    )

    return {
        "for_date": now.astimezone(AMSTERDAM).date().isoformat(),
        "generated_at": now.isoformat(),
        "type": "weekly",
        "period_hours": WEEK_S // 3600,
        "week_start": week_start.isoformat(),
        "equity_eur": str(eq_now.quantize(Decimal("0.01"))),
        "return_week_pct": _pct(eq_now, eq_week) if eq_week else None,
        "return_prev_week_pct": _pct(eq_week, eq_prev) if eq_week and eq_prev else None,
        "btc_week_pct": (
            _pct(btc_now[-1].close, btc_then[-1].close) if btc_then and btc_now else None
        ),
        "trades_closed_count": len(closed_week),
        "trades_closed_prev_count": len(closed_prev),
        "won_count": sum(1 for t in closed_week if Decimal(t.realised_r or "0") > 0),
        "lost_count": sum(1 for t in closed_week if Decimal(t.realised_r or "0") < 0),
        "avg_r_week": _avg_r(closed_week),
        "avg_r_prev": _avg_r(closed_prev),
        "drawdown_week_pct": _drawdown(week_start, now),
        "drawdown_prev_pct": _drawdown(prev_start, week_start),
        "model_cost_eur": str(sum(week_spend.values(), Decimal("0")).quantize(Decimal("0.01"))),
        "model_calls": int(calls_week or 0),
        "trades_best": best,
        "trades_worst": worst,
        "wrong_patterns": wrong_patterns,
        "wrong_decisions_count": wrong_call_count,
        "insights_findings": findings_now,
        "insights_changed": insights_changed,
        "insights_changed_count": len(insights_changed),
        "suggestion": suggestion,
        "open_positions_count": len(broker.positions),
    }


def weekly_template_text(facts: dict[str, Any]) -> str:
    """The plain-text weekly review built only from computed facts."""
    parts: list[str] = []
    rw = facts.get("return_week_pct")
    rp = facts.get("return_prev_week_pct")
    btc = facts.get("btc_week_pct")
    line = "The week"
    if isinstance(rw, (int, float)):
        line += f" returned {rw:+.2f}%"
        if isinstance(rp, (int, float)):
            line += f" (previous week {rp:+.2f}%)"
        if isinstance(btc, (int, float)):
            line += f"; BTC moved {btc:+.2f}% over the same span"
    parts.append(line + ".")

    closed = facts.get("trades_closed_count", 0)
    parts.append(
        f"{closed} trades closed this week "
        f"({facts.get('trades_closed_prev_count', 0)} the week before)."
    )
    for label, key in (("Best", "trades_best"), ("Worst", "trades_worst")):
        trades = facts.get(key) or []
        if trades:
            detail = ", ".join(
                f"{t['pair'].split('-')[0]} {Decimal(str(t['realised_r'])):+.2f}R" for t in trades
            )
            parts.append(f"{label}: {detail}.")

    wrong = facts.get("wrong_patterns") or []
    if wrong:
        detail = ", ".join(f"{w['check']} x{w['count']}" for w in wrong[:4])
        parts.append(f"Decisions marked Wrong: {detail}.")

    changed = facts.get("insights_changed") or []
    if changed:
        detail = "; ".join(
            f"{f['key']} avg {f['avg_r']:+.2f}R" for f in changed[:4] if f.get("avg_r") is not None
        )
        if detail:
            parts.append(f"Insights that changed: {detail}.")

    sug = facts.get("suggestion")
    if isinstance(sug, dict) and sug.get("text"):
        parts.append(f"Suggestion: {sug['text']}. {sug.get('whatif_hint', '')}.")
    return " ".join(parts)


def weekly_scheduled(now: datetime, setting: str) -> datetime | None:
    """This week's scheduled review instant in UTC, or None when off."""
    spec = WEEKLY_SCHEDULE.get(setting, (0, 8))
    if spec is None:
        return None
    weekday, hour = spec
    ams = now.astimezone(AMSTERDAM)
    day = ams.replace(hour=hour, minute=0, second=0, microsecond=0)
    day -= timedelta(days=(ams.weekday() - weekday) % 7)
    return day.astimezone(UTC)


async def maybe_weekly_review(
    session: Session,
    *,
    now: datetime,
    account: AccountRow,
    broker: PaperBroker,
    settings: Settings,
    dispatcher_notify: Callable[[str, str, str], Awaitable[None]] | None,
) -> BriefingRow | None:
    """Run this week's review when its scheduled time has come and it is
    not more than 4 hours late (same missed-window rule as the briefing)."""
    sched = weekly_scheduled(now, settings.learning.weekly_review)
    if sched is None or now < sched or now - sched > MISSED_GRACE:
        return None
    for_date = sched.astimezone(AMSTERDAM).date().isoformat()
    if repo.briefing_for_date(session, "weekly", for_date) is not None:
        return None
    row = await generate_briefing(
        session, now=now, account=account, broker=broker, settings=settings, type="weekly"
    )
    if dispatcher_notify is not None:
        label = f"Plain review: {row.reason}. " if row.reason else ""
        await dispatcher_notify(
            "review",
            f"Weekly review · {for_date}",
            label + row.text,
        )
    return row
