"""The Insights evidence engine: decisions sliced by one dimension.

Every number is computed here from stored decisions, trades and outcomes —
the model never writes these. A slice needs ``insights_min_decisions``
(default 30) to count as a finding, shows greyed from 10 and is hidden
below that. ``dimension=confidence`` also returns the calibration chart
data shared with the Readiness criterion.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.market.base import drop_forming
from app.agent.signals import compute_tf_values
from app.db import AMSTERDAM, DecisionRow, OutcomeRow
from app.models import (
    CalibrationBucketOut,
    DeciderOption,
    InsightFinding,
    InsightSliceOut,
    InsightsOut,
    Settings,
)

InsightDimension = Literal[
    "pair", "hour", "weekday", "regime", "volatility", "confidence", "decider", "prompt_version"
]
DIMENSIONS = {
    "pair",
    "hour",
    "weekday",
    "regime",
    "volatility",
    "confidence",
    "decider",
    "prompt_version",
}

HOUR_BUCKETS = ((0, 6, "00-06"), (6, 12, "06-12"), (12, 18, "12-18"), (18, 24, "18-24"))
WEEKDAY_BUCKETS = {"workday": "Mon-Fri", "saturday": "Saturday", "sunday": "Sunday"}
VOL_BUCKETS = ((33.0, "Low (p0-33)"), (66.0, "Mid (p33-66)"), (101.0, "High (p66-100)"))
CONF_BUCKETS = (
    (0.0, 0.5, "<0.5"),
    (0.5, 0.6, "0.5-0.6"),
    (0.6, 0.7, "0.6-0.7"),
    (0.7, 0.8, "0.7-0.8"),
    (0.8, 1.01, "0.8+"),
)
CAL_BUCKETS = CONF_BUCKETS[1:]  # calibration uses the spec's four bands
SLICE_MIN = 10  # below this a slice is hidden entirely


class _Slice:
    __slots__ = (
        "avoided",
        "buys",
        "decisions",
        "key",
        "label",
        "missed",
        "r_n",
        "r_sum",
        "scored",
        "scored_buys",
        "wins",
    )

    def __init__(self, key: str, label: str) -> None:
        self.key = key
        self.label = label
        self.decisions = 0
        self.buys = 0
        self.scored_buys = 0  # BUYs carrying a 72h outcome
        self.wins = 0  # BUY outcomes labelled win
        self.r_sum = Decimal("0")
        self.r_n = 0
        self.avoided = 0
        self.missed = 0
        self.scored = 0  # non-BUY outcomes carrying an avoided/missed/flat label


def _bucket_of(row: DecisionRow, dimension: str, atr_pct: float | None) -> str | None:
    if dimension == "pair":
        return row.pair
    at = repo.aware_utc(row.at).astimezone(AMSTERDAM)
    if dimension == "hour":
        for lo, hi, label in HOUR_BUCKETS:
            if lo <= at.hour < hi:
                return label
        return None
    if dimension == "weekday":
        if at.weekday() == 5:
            return WEEKDAY_BUCKETS["saturday"]
        if at.weekday() == 6:
            return WEEKDAY_BUCKETS["sunday"]
        return WEEKDAY_BUCKETS["workday"]
    if dimension == "regime":
        try:
            trend = json.loads(row.regime or "{}").get("trend")
        except json.JSONDecodeError:
            trend = None
        return {"up": "Up", "sideways": "Sideways", "down": "Down"}.get(str(trend))
    if dimension == "volatility":
        if atr_pct is None:
            return None
        pct = atr_pct * 100
        for upper, label in VOL_BUCKETS:
            if pct < upper:
                return label
        return None
    if dimension == "confidence":
        for lower, upper, label in CONF_BUCKETS:
            if lower <= row.confidence < upper:
                return label
        return None
    if dimension == "decider":
        return row.shadow_id or row.decider
    if dimension == "prompt_version":
        return row.prompt_version or "none"
    return None


def _decider_label(row: DecisionRow, shadow_names: dict[str, str]) -> str:
    if row.shadow_id is not None:
        return f"{shadow_names.get(row.shadow_id, row.shadow_id)} · {row.prompt_version or '—'}"
    if row.decider == "openai":
        model = row.model or "openai"
        prompt = (row.prompt_version or "").removeprefix("decision.")
        return f"Live · {model} · {prompt or '—'}"
    return "Rules baseline"


def _atr_pct_at(
    session: Session, pair: str, at: datetime, cache: dict[tuple[str, datetime], float | None]
) -> float | None:
    """ATR(1h) 30-day percentile at the decision's hour — recomputed from
    stored candles (the percentile itself is not stored on the decision)."""
    hour = at.replace(minute=0, second=0, microsecond=0)
    key = (pair, hour)
    if key in cache:
        return cache[key]
    candles = drop_forming(
        repo.load_candles(session, pair, "1h", 800, end=hour + timedelta(hours=1)),
        "1h",
        hour + timedelta(hours=1),
    )
    value = compute_tf_values(candles, "1h").atr_percentile if len(candles) >= 16 else None
    cache[key] = value
    return value


def compute_insights(
    session: Session,
    *,
    now: datetime,
    settings: Settings,
    dimension: InsightDimension = "pair",
    start: datetime | None = None,
    end: datetime | None = None,
    decider: str = "all",
    prompt_version: str | None = None,
) -> InsightsOut:
    """Slice every stored decision (live and shadow) by ``dimension``."""
    end = end or now
    if start is None:
        first = session.scalar(select(DecisionRow.at).order_by(DecisionRow.at.asc()).limit(1))
        start = repo.aware_utc(first) if first is not None else end - timedelta(days=30)

    shadows = repo.list_shadows(session)
    shadow_names = {s.id: s.name for s in shadows}

    rows = list(
        session.scalars(
            select(DecisionRow)
            .where(
                DecisionRow.at >= repo.aware_utc(start).replace(tzinfo=None),
                DecisionRow.at <= repo.aware_utc(end).replace(tzinfo=None),
            )
            .order_by(DecisionRow.at.asc())
        ).all()
    )
    # Decider filter: "all", "openai", "rules" (live only) or "shadow:<id>".
    if decider == "openai":
        rows = [r for r in rows if r.decider == "openai" and r.shadow_id is None]
    elif decider == "rules":
        rows = [r for r in rows if r.decider == "rules" and r.shadow_id is None]
    elif decider.startswith("shadow:"):
        sid = decider.split(":", 1)[1]
        rows = [r for r in rows if r.shadow_id == sid]
    if prompt_version:
        rows = [r for r in rows if r.prompt_version == prompt_version]

    outcomes = {
        o.decision_id: o
        for o in session.scalars(
            select(OutcomeRow).where(
                OutcomeRow.decision_id.in_([r.id for r in rows] or ["-"]),
                OutcomeRow.horizon == 72,
            )
        ).all()
    }

    atr_cache: dict[tuple[str, datetime], float | None] = {}
    slices: dict[str, _Slice] = {}
    for row in rows:
        atr = (
            _atr_pct_at(session, row.pair, repo.aware_utc(row.at), atr_cache)
            if dimension == "volatility"
            else None
        )
        key = _bucket_of(row, dimension, atr)
        if key is None:
            continue
        label = key
        if dimension == "decider":
            label = _decider_label(row, shadow_names)
        sl = slices.setdefault(key, _Slice(key, label))
        sl.decisions += 1
        out = outcomes.get(row.id)
        if row.action == "BUY":
            sl.buys += 1
            if out is not None:
                sl.scored_buys += 1
                if out.label == "win":
                    sl.wins += 1
                if out.realised_r is not None:
                    sl.r_sum += Decimal(out.realised_r)
                    sl.r_n += 1
        elif out is not None:
            sl.scored += 1
            if out.label == "avoided":
                sl.avoided += 1
            elif out.label == "missed":
                sl.missed += 1

    min_decisions = settings.learning.insights_min_decisions
    slice_outs = [
        InsightSliceOut(
            key=s.key,
            label=s.label,
            decisions=s.decisions,
            buys=s.buys,
            buy_win_rate_pct=round(s.wins / s.scored_buys * 100, 1) if s.scored_buys else None,
            avg_r=round(float(s.r_sum / s.r_n), 2) if s.r_n else None,
            avoided_pct=round(s.avoided / s.scored * 100, 1) if s.scored else None,
            missed_pct=round(s.missed / s.scored * 100, 1) if s.scored else None,
            state="finding" if s.decisions >= min_decisions else "early",
        )
        for s in sorted(slices.values(), key=lambda s: (-s.decisions, s.key))
        if s.decisions >= SLICE_MIN
    ]

    calibration, gap = _calibration(rows, outcomes)
    findings = _findings(session, rows, outcomes, shadow_names, settings)

    decider_options = [DeciderOption(id="all", label="All deciders")]
    decider_options.append(DeciderOption(id="openai", label="Live · model decisions"))
    decider_options.append(DeciderOption(id="rules", label="Rules baseline"))
    for s in shadows:
        decider_options.append(
            DeciderOption(id=f"shadow:{s.id}", label=f"{s.name} · {s.prompt_version}")
        )

    largest = max((s.decisions for s in slice_outs), default=0)
    return InsightsOut(
        dimension=dimension,
        start=start,
        end=end,
        decider=decider,
        prompt_version=prompt_version,
        min_decisions=min_decisions,
        total_decisions=len(rows),
        decisions_needed=max(0, min_decisions - largest),
        decider_options=decider_options,
        findings=findings,
        slices=slice_outs,
        calibration=calibration,
        calibration_gap_pts=gap,
    )


def _calibration(
    rows: list[DecisionRow], outcomes: dict[str, OutcomeRow]
) -> tuple[list[CalibrationBucketOut], float | None]:
    """Stated confidence vs actual win rate over the BUYs with a 72h
    outcome, in the four spec bands."""
    buys = [r for r in rows if r.action == "BUY" and r.id in outcomes]
    out: list[CalibrationBucketOut] = []
    weighted = Decimal("0")
    weight = 0
    for lo, hi, label in CAL_BUCKETS:
        band = [r for r in buys if lo <= r.confidence < hi]
        if not band:
            out.append(CalibrationBucketOut(label=label, buys=0, stated_pct=None, actual_pct=None))
            continue
        stated = sum(r.confidence for r in band) / len(band) * 100
        wins = sum(1 for r in band if outcomes[r.id].label == "win")
        actual = wins / len(band) * 100
        out.append(
            CalibrationBucketOut(
                label=label,
                buys=len(band),
                stated_pct=round(stated, 1),
                actual_pct=round(actual, 1),
            )
        )
        # The gap aggregates like the readiness rule: skip bands under 10.
        if len(band) >= SLICE_MIN:
            weighted += Decimal(str(abs(stated - actual))) * len(band)
            weight += len(band)
    return out, (float(weighted / weight) if weight else None)


def _findings(
    session: Session,
    rows: list[DecisionRow],
    outcomes: dict[str, OutcomeRow],
    shadow_names: dict[str, str],
    settings: Settings,
) -> list[InsightFinding]:
    """The three cards: calibration-based confidence check, plus the best
    and worst finding-level slices across every dimension."""
    findings: list[InsightFinding] = []
    min_decisions = settings.learning.insights_min_decisions

    # --- calibration card ---------------------------------------------------
    buys = [r for r in rows if r.action == "BUY" and r.id in outcomes]
    high = [r for r in buys if r.confidence >= 0.7]
    if len(high) >= SLICE_MIN:
        stated = sum(r.confidence for r in high) / len(high) * 100
        actual = sum(1 for r in high if outcomes[r.id].label == "win") / len(high) * 100
        if stated - actual >= float(settings.readiness.calibration_gap_pts):
            findings.append(
                InsightFinding(
                    kind="overconfident",
                    title="Overconfident",
                    text=(f"Confidence above 0.7 wins {actual:.0f}% of BUYs, not {stated:.0f}%+"),
                    n=len(high),
                )
            )
        elif actual - stated >= float(settings.readiness.calibration_gap_pts):
            findings.append(
                InsightFinding(
                    kind="underconfident",
                    title="Underconfident",
                    text=(
                        f"Confidence above 0.7 wins {actual:.0f}% of BUYs, "
                        f"above the stated {stated:.0f}%"
                    ),
                    n=len(high),
                )
            )

    # --- best / worst slices --------------------------------------------------
    atr_cache: dict[tuple[str, datetime], float | None] = {}
    best: tuple[Decimal, _Slice, str] | None = None
    worst: tuple[Decimal, _Slice, str] | None = None
    for dim in ("pair", "hour", "weekday", "regime", "volatility", "confidence"):
        grouped: dict[str, _Slice] = {}
        for row in rows:
            atr = (
                _atr_pct_at(session, row.pair, repo.aware_utc(row.at), atr_cache)
                if dim == "volatility"
                else None
            )
            key = _bucket_of(row, dim, atr)
            if key is None:
                continue
            sl = grouped.setdefault(key, _Slice(key, key))
            sl.decisions += 1
            out = outcomes.get(row.id)
            if row.action == "BUY":
                sl.buys += 1
                if out is not None and out.realised_r is not None:
                    sl.r_sum += Decimal(out.realised_r)
                    sl.r_n += 1
        for sl in grouped.values():
            if sl.decisions < min_decisions or sl.r_n < SLICE_MIN:
                continue
            avg = sl.r_sum / sl.r_n
            if best is None or avg > best[0]:
                best = (avg, sl, dim)
            if worst is None or avg < worst[0]:
                worst = (avg, sl, dim)
    if worst is not None and worst[0] < 0:
        hint = (
            "Try skipping weekends in What-if"
            if worst[2] == "weekday" and worst[1].key != "Mon-Fri"
            else None
        )
        findings.append(
            InsightFinding(
                kind="weak_spot",
                title="Weak spot",
                text=f"{worst[1].label}: BUYs average {worst[0]:+.2f}R (n = {worst[1].decisions})",
                n=worst[1].decisions,
                whatif_hint=hint,
            )
        )
    if best is not None and best[0] > 0:
        findings.append(
            InsightFinding(
                kind="strength",
                title="Strength",
                text=f"{best[1].label}: BUYs average {best[0]:+.2f}R (n = {best[1].decisions})",
                n=best[1].decisions,
            )
        )
    return findings
