"""Settings saves as numbered versions (item 025).

Every change to the settings document goes through :func:`save`: it
validates the draft, diffs it against the stored document, tags each
change tighter / looser / neutral from ``settings_directions``, refuses a
missing reason, asks once more before a looser change, and writes the
settings row, the ``settings_versions`` row and the ``settings_history``
rows in one transaction.

:func:`preview` answers the screen's money questions — the euro amount
next to every percentage, the "What these limits mean today" figures and
the per-field errors — without saving anything. All money is ``Decimal``,
rounded to cents like every other euro amount in the app.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import repo
from app.agent.prompts import list_prompt_versions
from app.db import SettingsHistoryRow, SettingsRow, SettingsVersionRow, utcnow
from app.models import (
    Settings,
    SettingsChangeOut,
    SettingsHistoryPageOut,
    SettingsHistoryRowOut,
    SettingsHistoryVersionOut,
    SettingsMetaItem,
    SettingsMetaOut,
    SettingsPreviewOut,
    SettingsPreviewSummary,
)
from app.settings_directions import confirm_always, direction_for, tag_change
from app.settings_spec import (
    CHOICES,
    EQUIVALENTS,
    FIXED,
    cross_field_errors,
    flatten_doc,
    range_for,
    section_for,
    unit_for,
)

REASON_MIN = 3
REASON_MAX = 200
SOURCES = frozenset({"screen", "promote", "whatif", "action", "migration"})
MIGRATION_REASON = "Settings when versions started (item 025)"

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
PAGE_SIZE = 50
MAX_PAGE = 200


class ReasonInvalid(Exception):
    """The save reason is missing or outside 3-200 characters."""


class LooserNeedsConfirm(Exception):
    """The draft has a looser (or always-confirm) change and the request did
    not carry ``confirm_looser``. ``changes`` lists what must be confirmed."""

    def __init__(self, changes: list[dict[str, Any]]) -> None:
        super().__init__("settings changes need confirmation")
        self.changes = changes


def diff_settings(old: Settings, new: Settings) -> list[dict[str, Any]]:
    """Every leaf that differs, tagged by the directions map."""
    old_flat = flatten_doc(old.model_dump(mode="json"))
    new_flat = flatten_doc(new.model_dump(mode="json"))
    changes: list[dict[str, Any]] = []
    for key in sorted(set(old_flat) | set(new_flat)):
        old_v, new_v = old_flat.get(key), new_flat.get(key)
        if old_v == new_v:
            continue
        changes.append(
            {
                "key": key,
                "old": old_v,
                "new": new_v,
                "direction": tag_change(key, old_v, new_v),
            }
        )
    return changes


def _stored_settings(session: Session) -> tuple[SettingsRow | None, Settings]:
    row = session.get(SettingsRow, repo.SETTINGS_ROW_ID)
    if row is None or not row.doc:
        return row, Settings()
    return row, Settings.model_validate_json(row.doc)


def save(
    session: Session,
    draft: Settings,
    *,
    reason: str,
    source: str,
    confirm_looser: bool,
) -> int:
    """Persist ``draft`` as the next settings version. Returns the version.

    Raises :class:`ReasonInvalid` for a bad reason and
    :class:`LooserNeedsConfirm` when a looser change (or an always-confirm
    key) lacks ``confirm_looser``. Everything commits in one transaction.
    """
    reason = reason.strip()
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        raise ReasonInvalid(f"reason must be {REASON_MIN} to {REASON_MAX} characters")
    if source not in SOURCES:
        raise ValueError(f"unknown settings source: {source}")

    row, old = _stored_settings(session)
    changes = diff_settings(old, draft)
    needs_confirm = [
        c
        for c in changes
        if c["direction"] == "looser" or confirm_always(str(c["key"]), c["old"], c["new"])
    ]
    if needs_confirm and not confirm_looser:
        raise LooserNeedsConfirm(needs_confirm)

    if row is None:
        row = SettingsRow(id=repo.SETTINGS_ROW_ID, version=0, doc="", updated_at=utcnow())
        session.add(row)
    version = row.version + 1
    doc_json = draft.model_dump_json()
    at = utcnow()
    row.version = version
    row.doc = doc_json
    row.updated_at = at
    session.add(
        SettingsVersionRow(
            version=version,
            at=at,
            reason=reason,
            source=source,
            doc=doc_json,
            changes=json.dumps(changes),
        )
    )
    repo.record_settings_history(session, old, draft, at=at, version=version)
    session.commit()
    return version


def ensure_base_version(session: Session, settings: Settings) -> None:
    """First start after item 025: the current document becomes a version
    row with source ``migration``. Writes nothing once versions exist."""
    if session.scalar(select(func.count()).select_from(SettingsVersionRow)):
        return
    _row, old = _stored_settings(session)
    version = repo.settings_version(session)
    session.add(
        SettingsVersionRow(
            version=version,
            at=utcnow(),
            reason=MIGRATION_REASON,
            source="migration",
            doc=old.model_dump_json(),
            changes="[]",
        )
    )
    session.commit()


# -------------------------------------------------------------------- meta --


def _choices_for(key: str) -> list[str] | None:
    if key == "model.prompt_version":
        return list_prompt_versions()
    return CHOICES.get(key)


def settings_meta(version: int) -> SettingsMetaOut:
    """Every leaf of the settings document with its section, default,
    range, unit, choices, direction and fixed flag."""
    defaults = flatten_doc(Settings().model_dump(mode="json"))
    items: dict[str, SettingsMetaItem] = {}
    for key in sorted(defaults):
        bounds = range_for(key)
        direction = direction_for(key)
        items[key] = SettingsMetaItem(
            section=section_for(key),
            default=defaults[key],
            min=bounds[0] if bounds else None,
            max=bounds[1] if bounds else None,
            step=None,
            unit=unit_for(key),
            choices=_choices_for(key),
            direction=direction[0] if direction else None,
            fixed=key in FIXED,
        )
    return SettingsMetaOut(version=version, items=items)


# ----------------------------------------------------------------- preview --


def _eur(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _eur_text(value: Decimal) -> str:
    return f"€{_eur(value):,.2f}"


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _validation_errors(exc: ValidationError) -> dict[str, str]:
    """Pydantic errors keyed by dotted leaf. Cross-field failures surface
    at the section level, so the leaf is read back out of the message."""
    errors: dict[str, str] = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        msg = err.get("msg", "invalid")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        key = ".".join(str(part) for part in loc)
        if len(loc) == 1 and loc[0] in ("risk", "rules") and ":" in msg:
            key = msg.split(":", 1)[0].strip()
        errors[key] = msg
    return errors


def _equivalents(
    draft: Settings, equity: Decimal, day_start: Decimal, peak: Decimal
) -> dict[str, str]:
    """The euro amount next to every percentage-of-X leaf, from the draft's
    own values — the same arithmetic the gates and sizing use."""
    flat = flatten_doc(draft.model_dump(mode="json"))
    risk = draft.risk
    out: dict[str, str] = {}
    for key, base in EQUIVALENTS.items():
        pct = _num(flat.get(key))
        if pct is None:
            continue
        if base == "equity":
            amount = equity * pct / HUNDRED
        elif base == "day_start":
            amount = day_start * pct / HUNDRED
        elif base == "peak_level":
            amount = peak * (HUNDRED - pct) / HUNDRED
        elif base == "peak_drop":
            amount = peak * pct / HUNDRED
        elif base == "max_cost":
            amount = _eur(equity * risk.risk_per_trade_pct / HUNDRED) * pct / HUNDRED
        else:
            continue
        out[key] = _eur_text(amount)
    return out


def _summary(
    draft: Settings, equity: Decimal, day_start: Decimal, peak: Decimal
) -> SettingsPreviewSummary:
    risk = draft.risk
    full = int(risk.total_open_risk_pct // risk.risk_per_trade_pct)
    return SettingsPreviewSummary(
        equity=equity,
        day_start_equity=day_start,
        peak_equity=peak,
        loss_at_stop_eur=_eur(equity * risk.risk_per_trade_pct / HUNDRED),
        throttled_loss_eur=_eur(equity * risk.throttled_risk_per_trade_pct / HUNDRED),
        positions_at_full_risk=full,
        next_position_pct=risk.total_open_risk_pct - full * risk.risk_per_trade_pct,
        daily_stop_eur=_eur(day_start * risk.daily_loss_limit_pct / HUNDRED),
        kill_level_eur=_eur(peak * (HUNDRED - risk.kill_drawdown_pct) / HUNDRED),
    )


def _set_path(doc: dict[str, Any], key: str, value: Any) -> None:
    """``doc["risk"]["min_rr"] = value`` for a dotted leaf; missing
    intermediate objects are skipped."""
    parts = key.split(".")
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return
        node = child
    if parts[-1] in node:
        node[parts[-1]] = value


def preview(
    session: Session,
    draft_raw: dict[str, Any],
    *,
    equity: Decimal,
    day_start_equity: Decimal,
    peak_equity: Decimal,
) -> SettingsPreviewOut:
    """Money, tags and errors for an unsaved draft. Saves nothing.

    An invalid draft still answers: ``errors`` names every bad leaf —
    field errors plus the cross-field rules, computed on a copy where the
    bad leaves take their stored values — and the amounts fall back to the
    stored settings so the screen keeps its sentences while a field is
    being fixed.
    """
    _row, current = _stored_settings(session)
    try:
        draft = Settings.model_validate(draft_raw)
    except ValidationError as exc:
        errors = _validation_errors(exc)
        # Pydantic skips the cross-field validators when a field fails, so
        # rebuild a valid document — bad leaves take their stored values —
        # and ask the cross-field rules what else is wrong.
        current_flat = flatten_doc(current.model_dump(mode="json"))
        repaired = json.loads(json.dumps(draft_raw))
        for key in errors:
            if key in current_flat:
                _set_path(repaired, key, current_flat[key])
        try:
            partial = Settings.model_validate(repaired)
            for key, message in cross_field_errors(partial).items():
                errors.setdefault(key, message)
        except ValidationError:
            pass
        return SettingsPreviewOut(
            equivalents=_equivalents(current, equity, day_start_equity, peak_equity),
            summary=_summary(current, equity, day_start_equity, peak_equity),
            changes=[],
            errors=errors,
        )
    errors = cross_field_errors(draft)
    return SettingsPreviewOut(
        equivalents=_equivalents(draft, equity, day_start_equity, peak_equity),
        summary=_summary(draft, equity, day_start_equity, peak_equity),
        changes=[SettingsChangeOut(**c) for c in diff_settings(current, draft)],
        errors=errors,
    )


# ----------------------------------------------------------------- history --


def _version_out(row: SettingsVersionRow) -> SettingsHistoryVersionOut:
    try:
        changes = [SettingsChangeOut(**c) for c in json.loads(row.changes)]
    except (json.JSONDecodeError, TypeError):
        changes = []
    return SettingsHistoryVersionOut(
        version=row.version,
        at=repo.aware_utc(row.at),
        reason=row.reason,
        source=row.source,
        changes=changes,
    )


def history_page(
    session: Session, cursor: str | None, limit: int = PAGE_SIZE
) -> SettingsHistoryPageOut:
    """Versions newest first, then the per-setting rows without a version
    (everything from before item 025). ``limit`` per page, 50 by default.

    The cursor is ``v:<version>`` while paging versions and ``r:<row id>``
    once the page has moved on to the older rows; a page may straddle the
    two.
    """
    limit = max(1, min(limit, MAX_PAGE))
    phase, _, mark = (cursor or "").partition(":")
    versions: list[SettingsHistoryVersionOut] = []
    rows: list[SettingsHistoryRowOut] = []
    next_cursor: str | None = None
    row_mark: int | None = None

    if phase != "r":
        after = int(mark) if phase == "v" and mark.isdigit() else None
        query = select(SettingsVersionRow).order_by(SettingsVersionRow.version.desc())
        if after is not None:
            query = query.where(SettingsVersionRow.version < after)
        batch = list(session.scalars(query.limit(limit)).all())
        versions = [_version_out(v) for v in batch]
        if len(batch) == limit:
            # A full page of versions; the next one continues below them.
            return SettingsHistoryPageOut(
                versions=versions, rows=[], next_cursor=f"v:{batch[-1].version}"
            )
        # Versions exhausted mid-page: the rest of the page is older rows.
        remaining = limit - len(batch)
    else:
        remaining = limit
        row_mark = int(mark) if mark.isdigit() else None

    query = (
        select(SettingsHistoryRow)
        .where(SettingsHistoryRow.version.is_(None))
        .order_by(SettingsHistoryRow.id.desc())
        .limit(remaining)
    )
    if row_mark is not None:
        query = query.where(SettingsHistoryRow.id < row_mark)
    batch_rows = list(session.scalars(query).all())
    rows = [
        SettingsHistoryRowOut(at=repo.aware_utc(r.at), key=r.key, old=r.old, new=r.new)
        for r in batch_rows
    ]
    if len(batch_rows) == remaining:
        next_cursor = f"r:{batch_rows[-1].id}"
    return SettingsHistoryPageOut(versions=versions, rows=rows, next_cursor=next_cursor)
