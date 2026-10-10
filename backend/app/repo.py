"""Repositories: typed access to SQLAlchemy tables."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SettingsRow, utcnow
from app.models import Settings

SETTINGS_ROW_ID = 1


def load_settings(session: Session) -> Settings:
    """Return the stored settings document, or defaults on first run."""
    row = session.get(SettingsRow, SETTINGS_ROW_ID)
    if row is None:
        settings = Settings()
        save_settings(session, settings)
        return settings
    return Settings.model_validate_json(row.doc)


def save_settings(session: Session, settings: Settings) -> int:
    """Persist the settings document and bump its version. Returns the version."""
    row = session.get(SettingsRow, SETTINGS_ROW_ID)
    if row is None:
        row = SettingsRow(id=SETTINGS_ROW_ID, version=0, doc="", updated_at=utcnow())
        session.add(row)
    row.version += 1
    row.doc = settings.model_dump_json()
    row.updated_at = utcnow()
    session.commit()
    return row.version


def settings_version(session: Session) -> int:
    return session.scalar(select(SettingsRow.version).where(SettingsRow.id == SETTINGS_ROW_ID)) or 0
