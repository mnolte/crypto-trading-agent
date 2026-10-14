"""The Insights evidence engine: decisions sliced by one dimension.

Every number is computed here from stored decisions, trades and outcomes —
the model never writes these. A slice needs ``insights_min_decisions``
(default 30) to count as a finding, shows greyed from 10 and is hidden
below that. ``dimension=confidence`` also returns the calibration chart
data shared with the Readiness criterion.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.market.base import Candle
from app.agent.signals import ATR_PCT_LOOKBACK, Floats, atr, atr_percentile
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
_ATR_WINDOW = 800  # candles behind each decision hour, as in the brief's read


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


def _hour_floor(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


class _AtrPctSeries:
    """One pair's 1h ATR% series, computed once per request.

    ``at(hour)`` reproduces the frozen per-hour read exactly: the last
    closed candle is the one that opened at ``hour``, the window is the
    last ``_ATR_WINDOW`` candles and the percentile needs at least 16.
    """

    def __init__(self, candles: list[Candle]) -> None:
        self.opens: Floats = np.array([c.open_time.timestamp() for c in candles])
        high = np.array([float(c.high) for c in candles])
        low = np.array([float(c.low) for c in candles])
        close = np.array([float(c.close) for c in candles])
        self.atr_pct = atr(high, low, close) / close * 100.0

    @classmethod
    def load(
        cls, session: Session, pair: str, first_hour: datetime, last_hour: datetime
    ) -> _AtrPctSeries:
        hours = int((last_hour - first_hour).total_seconds() // 3600)
        candles = repo.load_candles(
            session,
            pair,
            "1h",
            hours + _ATR_WINDOW + 8,
            end=last_hour + timedelta(hours=1),
        )
        return cls(candles)

    def at(self, hour: datetime) -> float | None:
        """ATR% 30-day percentile at ``hour``, or None with short history."""
        i = int(np.searchsorted(self.opens, hour.timestamp(), side="right")) - 1
        n = min(_ATR_WINDOW, i + 1)
        if i < 0 or n < 16:
            return None
        return atr_percentile(self.atr_pct[i + 1 - n : i + 1], ATR_PCT_LOOKBACK["1h"])


def _atr_series(session: Session, rows: list[DecisionRow]) -> dict[str, _AtrPctSeries]:
    """One ATR% series per pair that has decisions in the window."""
    hours: dict[str, list[datetime]] = {}
    for r in rows:
        hours.setdefault(r.pair, []).append(_hour_floor(repo.aware_utc(r.at)))
    return {pair: _AtrPctSeries.load(session, pair, min(ts), max(ts)) for pair, ts in hours.items()}


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
    end = repo.aware_utc(end or now).astimezone(UTC)
    if start is None:
        first = session.scalar(select(DecisionRow.at).order_by(DecisionRow.at.asc()).limit(1))
        start = repo.aware_utc(first) if first is not None else end - timedelta(days=30)
    else:
        start = repo.aware_utc(start).astimezone(UTC)

    shadows = repo.list_shadows(session)
    shadow_names = {s.id: s.name for s in shadows}

    rows = list(
        session.scalars(
            select(DecisionRow)
            .where(
                DecisionRow.at >= start.replace(tzinfo=None),
                DecisionRow.at <= end.replace(tzinfo=None),
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

    min_decisions = settings.learning.insights_min_decisions
    # The volatility slice and the findings scan need the ATR% percentile at
    # each decision's hour; below the finding bar no slice can be a finding,
    # so the candle work is skipped unless the dimension itself asks for it.
    atr_series = (
        _atr_series(session, rows)
        if dimension == "volatility" or len(rows) >= min_decisions
        else {}
    )
    slices: dict[str, _Slice] = {}
    for row in rows:
        atr_pct = (
            atr_series[row.pair].at(_hour_floor(repo.aware_utc(row.at)))
            if dimension == "volatility"
            else None
        )
        key = _bucket_of(row, dimension, atr_pct)
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
    findings = _findings(rows, outcomes, shadow_names, settings, atr_series)

    decider_options = [DeciderOption(id="all", label="All deciders")]
    decider_options.append(DeciderOption(id="openai", label="Live · model decisions"))
    decider_options.append(DeciderOption(id="rules", label="Rules baseline"))
    for s in shadows:
        decider_options.append(
            DeciderOption(id=f"shadow:{s.id}", label=f"{s.name} · {s.prompt_version}")
        )

    # The biggest slice sets the "N more to go" count — including slices
    # still hidden under SLICE_MIN, otherwise the count overstates the gap.
    largest = max(slices.values(), key=lambda s: s.decisions, default=None)
    largest_n = largest.decisions if largest is not None else 0
    return InsightsOut(
        dimension=dimension,
        start=start,
        end=end,
        decider=decider,
        prompt_version=prompt_version,
        min_decisions=min_decisions,
        slice_min=SLICE_MIN,
        total_decisions=len(rows),
        decisions_needed=max(0, min_decisions - largest_n),
        largest_slice_label=largest.label if largest is not None else None,
        largest_slice_decisions=largest_n,
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
    rows: list[DecisionRow],
    outcomes: dict[str, OutcomeRow],
    shadow_names: dict[str, str],
    settings: Settings,
    atr_series: dict[str, _AtrPctSeries],
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
    best: tuple[Decimal, _Slice, str] | None = None
    worst: tuple[Decimal, _Slice, str] | None = None
    # Below the finding bar no slice can qualify — skip the scan (and the
    # volatility bucketing) entirely.
    for dim in (
        ("pair", "hour", "weekday", "regime", "volatility", "confidence")
        if len(rows) >= min_decisions
        else ()
    ):
        grouped: dict[str, _Slice] = {}
        for row in rows:
            atr_pct = (
                atr_series[row.pair].at(_hour_floor(repo.aware_utc(row.at)))
                if dim == "volatility"
                else None
            )
            key = _bucket_of(row, dim, atr_pct)
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
