"""Replay comparison report: ``python -m backend.tools.compare_prompts``.

Replays every stored live decision call made with ``--from`` against the
``--to`` prompt (each stored v1 brief upgraded first), then prints the
per-prompt comparison the spec's switch criteria need. Replays respect the
daily call budget and the monthly cost cap, and are resumable: calls that
already have a replay row for the target prompt are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent import budget
from app.agent.openai_client import OpenAIResponsesClient
from app.agent.prompts import PromptNotFoundError, load_prompt
from app.agent.replay import evaluate_replay, replay_request, replay_row
from app.config import get_env, get_secret
from app.db import LlmCallRow, make_engine, make_session_factory, run_migrations, utcnow
from app.models import Settings

# Confidence histogram bands the report prints.
_CONF_BINS = ("<0.5", "0.5-0.6", "0.6-0.7", "0.7-0.8", "0.8-1.0")


def _parsed(row: LlmCallRow) -> dict[str, Any]:
    if not row.parsed:
        return {}
    try:
        out = json.loads(row.parsed)
    except json.JSONDecodeError:
        return {}
    return out if isinstance(out, dict) else {}


def _checks(row: LlmCallRow) -> list[dict[str, Any]]:
    try:
        out = json.loads(row.output_checks or "[]")
    except json.JSONDecodeError:
        return []
    return out if isinstance(out, list) else []


def _rules_action(row: LlmCallRow) -> str | None:
    """The Rules baseline recorded on a live call, e.g. 'model BUY vs Rules
    HOLD' or 'agrees (HOLD)'."""
    for check in _checks(row):
        if check.get("name") != "Rules baseline":
            continue
        detail = check.get("detail") or ""
        if detail.startswith("agrees ("):
            return detail[len("agrees (") : -1] or None
        if " vs Rules " in detail:
            return detail.rsplit(" vs Rules ", 1)[-1] or None
    return None


def _levels(row: LlmCallRow) -> tuple[float, float, float] | None:
    parsed = _parsed(row)
    if parsed.get("action") != "BUY":
        return None
    try:
        return (
            float(parsed["entry"]),
            float(parsed["stop"]),
            float(parsed["target"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _same_levels(a: tuple[float, ...] | None, b: tuple[float, ...] | None) -> bool:
    """Within one tick of each other; without the market's tick the report
    uses a fraction of the level (BTC's 1 EUR tick ≈ 1.3e-5 of price)."""
    if a is None or b is None:
        return False
    return all(abs(x - y) <= max(1e-9, abs(x) * 1e-5) for x, y in zip(a, b, strict=True))


class _Stats:
    """Per-prompt tallies the report prints."""

    def __init__(self) -> None:
        self.calls = 0
        self.invalid = 0
        self.check_fails = 0
        self.signal_warns = 0
        self.buys = 0
        self.buys_with_inv = 0
        self.repeated_buys = 0
        self.buys_clean = 0
        self.agree_rules = 0
        self.rules_known = 0
        self.regime_fails = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.cost_usd = Decimal("0")
        self.cost_eur = Decimal("0")
        self.conf = [0] * len(_CONF_BINS)
        self._prev_levels: dict[str, tuple[float, ...] | None] = {}

    def conf_bin(self, c: float | None) -> None:
        if c is None:
            return
        idx = 0 if c < 0.5 else min(4, int(c * 10) - 4)
        self.conf[idx] += 1

    def add(self, row: LlmCallRow, *, rules_action: str | None, repeated: bool) -> None:
        parsed = _parsed(row)
        checks = _checks(row)
        self.calls += 1
        if row.status == "invalid_output":
            self.invalid += 1
        fails = [c for c in checks if c.get("status") == "fail"]
        self.check_fails += 1 if fails else 0
        self.signal_warns += (
            1
            if any(c.get("name") == "Signal claims" and c.get("status") == "warn" for c in checks)
            else 0
        )
        self.regime_fails += (
            1
            if any(c.get("name") == "Regime rule" and c.get("status") == "fail" for c in checks)
            else 0
        )
        if parsed.get("action") == "BUY":
            self.buys += 1
            if parsed.get("invalidation") and parsed.get("invalidation_price") is not None:
                self.buys_with_inv += 1
            if not fails:
                self.buys_clean += 1
        if repeated:
            self.repeated_buys += 1
        if rules_action is not None:
            self.rules_known += 1
            if parsed.get("action") == rules_action:
                self.agree_rules += 1
        self.conf_bin(parsed.get("confidence"))
        self.in_tokens += row.input_tokens or 0
        self.out_tokens += row.output_tokens or 0
        if row.cost_usd:
            self.cost_usd += Decimal(row.cost_usd)
        if row.cost_eur:
            self.cost_eur += Decimal(row.cost_eur)


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.0f}%" if d else "—"


async def run_report(
    session: Session,
    *,
    from_version: str,
    to_version: str,
    settings: Settings,
    client: Any,
    limit: int | None = None,
    pair: str | None = None,
    dry_run: bool = False,
) -> str:
    """Replay stored live ``from_version`` calls with ``to_version`` and
    print the comparison. Returns the report text."""
    prompt = load_prompt(to_version)
    conds = [
        LlmCallRow.feature == "decision",
        LlmCallRow.prompt_version == from_version,
        LlmCallRow.replay_of.is_(None),
        LlmCallRow.request_json.isnot(None),
    ]
    if pair:
        conds.append(LlmCallRow.pair == pair)
    originals = list(
        session.scalars(
            select(LlmCallRow).where(*conds).order_by(LlmCallRow.at.asc(), LlmCallRow.id.asc())
        ).all()
    )
    already = {
        row.replay_of
        for row in session.scalars(
            select(LlmCallRow).where(LlmCallRow.prompt_version == to_version)
        ).all()
        if row.replay_of
    }
    todo = [row for row in originals if row.id not in already]
    if limit is not None:
        todo = todo[:limit]

    v1, v2 = _Stats(), _Stats()
    lines: list[str] = []
    done = 0
    # Per pair: the previous rejected BUY's levels per prompt series —
    # "repeated rejected BUY" means the same levels proposed again.
    prev_skipped_v1: dict[str, tuple[float, ...] | None] = {}
    prev_skipped_v2: dict[str, tuple[float, ...] | None] = {}
    stopped = ""
    for orig in todo:
        if not dry_run:
            now = utcnow()
            if budget.calls_today(session, orig.account_id, now) >= (
                settings.model.daily_call_budget
            ):
                stopped = f"stopped: daily call budget ({settings.model.daily_call_budget})"
                break
            spent = budget.month_spend_strict(session, now)
            if budget.cap_state(spent, settings.model.monthly_cost_cap_eur) != "ok":
                stopped = "stopped: monthly cost cap at 80% (replays pause)"
                break
        # ---- v1 side of the comparison, from the stored row itself.
        rules = _rules_action(orig)
        lvl1 = _levels(orig)
        repeated1 = bool(lvl1 is not None and _same_levels(lvl1, prev_skipped_v1.get(orig.pair)))
        v1.add(orig, rules_action=rules, repeated=repeated1)
        if parsed_action(orig) == "BUY":
            prev_skipped_v1[orig.pair] = lvl1  # live BUYs were all gate-skipped
        # ---- v2 replay.
        payload, brief, meta, _upg = replay_request(session, orig, prompt, None, settings=settings)
        if dry_run:
            done += 1
            continue
        result = await client.call(payload)
        out_checks, status = evaluate_replay(result, brief, meta, settings, prompt.brief_version)
        now = utcnow()
        row = replay_row(
            orig,
            prompt,
            payload["model"],
            result,
            payload,
            out_checks,
            status,
            now=now,
            session=session,
            settings=settings,
        )
        usd, eur = budget.cost_estimates(
            budget.ModelPrices(),
            payload["model"],
            result.input_tokens,
            result.output_tokens,
            budget.usdc_eur_rate(session, now),
        )
        row.cost_usd = str(usd) if usd is not None else None
        row.cost_eur = str(eur) if eur is not None else None
        repo.add_llm_call(session, row)
        session.commit()
        lvl2 = _levels(row)
        repeated2 = bool(lvl2 is not None and _same_levels(lvl2, prev_skipped_v2.get(orig.pair)))
        v2.add(row, rules_action=rules, repeated=repeated2)
        if parsed_action(row) == "BUY":
            # A v2 BUY surviving its checks would have reached the gates;
            # a failed one is the rejected BUY the next repeat compares to.
            prev_skipped_v2[orig.pair] = lvl2
        done += 1

    def pct(n: int, d: int) -> str:
        return f"{n} ({_pct(n, d)})"

    lines.append(f"=== prompt comparison: {from_version} → {to_version} ===")
    lines.append(
        f"replayed {done} of {len(todo)} stored calls"
        + (f" ({len(already & {r.id for r in originals})} already replayed)" if already else "")
        + (f" — {stopped}" if stopped else "")
    )
    lines.append("")
    rows = [
        ("calls", str(v1.calls), str(v2.calls)),
        ("invalid outputs", str(v1.invalid), str(v2.invalid)),
        ("post-check failures", str(v1.check_fails), str(v2.check_fails)),
        ("signal-claims warnings", pct(v1.signal_warns, v1.calls), pct(v2.signal_warns, v2.calls)),
        (
            "invalidation on BUYs",
            f"{v1.buys_with_inv}/{v1.buys} ({_pct(v1.buys_with_inv, v1.buys)})",
            f"{v2.buys_with_inv}/{v2.buys} ({_pct(v2.buys_with_inv, v2.buys)})",
        ),
        ("repeated rejected BUYs", str(v1.repeated_buys), str(v2.repeated_buys)),
        ("BUYs passing all checks", str(v1.buys_clean), str(v2.buys_clean)),
        (
            "agrees with Rules baseline",
            _pct(v1.agree_rules, v1.rules_known),
            _pct(v2.agree_rules, v2.rules_known),
        ),
        ("regime violations", str(v1.regime_fails), str(v2.regime_fails)),
        ("input tokens", str(v1.in_tokens), str(v2.in_tokens)),
        ("output tokens", str(v1.out_tokens), str(v2.out_tokens)),
        ("cost (est.)", f"${v1.cost_usd} / €{v1.cost_eur}", f"${v2.cost_usd} / €{v2.cost_eur}"),
    ]
    lines.append(f"{'metric':<28}{from_version:>18}{to_version:>18}")
    for name, a, b in rows:
        lines.append(f"{name:<28}{a:>18}{b:>18}")
    lines.append("")
    lines.append(f"confidence histogram ({'/'.join(_CONF_BINS)}):")
    lines.append(f"  {from_version}: {v1.conf}")
    lines.append(f"  {to_version}: {v2.conf}")
    lines.append("")
    inv_pct = v2.buys_with_inv / v2.buys * 100 if v2.buys else None
    criteria = [
        ("no more invalid outputs than v1", v2.invalid <= v1.invalid),
        ("signal-claims warnings under 10% of calls", v2.signal_warns < v2.calls * 0.10),
        (
            "invalidation on >= 95% of BUYs",
            (inv_pct is not None and inv_pct >= 95) if v2.buys else None,
        ),
        ("no repeated rejected BUYs", v2.repeated_buys == 0),
        ("no regime violations", v2.regime_fails == 0),
    ]
    lines.append("switch criteria (SPEC.md → Prompt v2 → Validation):")
    for label, ok in criteria:
        verdict = "PASS" if ok is True else ("n/a" if ok is None else "FAIL")
        lines.append(f"  {label:<45} {verdict}")
    return "\n".join(lines)


def parsed_action(row: LlmCallRow) -> str | None:
    action = _parsed(row).get("action")
    return action if action in ("BUY", "SELL", "HOLD") else None


async def _amain(args: argparse.Namespace) -> int:
    env = get_env()
    run_migrations(env.ta_db_path)
    engine = make_engine(env.ta_db_path)
    sessions = make_session_factory(engine)
    session = sessions()
    try:
        settings = repo.load_settings(session)
        try:
            load_prompt(args.to_version)
        except PromptNotFoundError:
            print(f"prompt {args.to_version} not found")
            return 2
        if args.dry_run:
            client = None
        else:
            api_key = get_secret("openai")
            if not api_key:
                print("no OpenAI key set (Settings → API keys)")
                return 2
            client = OpenAIResponsesClient(api_key)
        try:
            report = await run_report(
                session,
                from_version=args.from_version,
                to_version=args.to_version,
                settings=settings,
                client=client,
                limit=args.limit,
                pair=args.pair,
                dry_run=args.dry_run,
            )
        finally:
            if client is not None:
                await client.aclose()
        print(report)
        return 0
    finally:
        session.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_version", default="decision.v1")
    parser.add_argument("--to", dest="to_version", default="decision.v2")
    parser.add_argument("--limit", type=int, default=None, help="replay at most N calls")
    parser.add_argument("--pair", default=None, help="only this market")
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be replayed, call nothing"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
