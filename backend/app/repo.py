"""Repositories: typed access to SQLAlchemy tables."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.agent.market.base import Candle
from app.db import CandleRow, MarketRow, SettingsRow, utcnow
from app.models import Settings, Tf

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


# ------------------------------------------------------------------ candles --


def upsert_candles(session: Session, market: str, tf: Tf, candles: list[Candle]) -> None:
    """Insert closed candles; existing bars are left untouched."""
    if not candles:
        return
    rows = [
        {
            "market": market,
            "tf": tf,
            "open_time_ms": int(c.open_time.timestamp() * 1000),
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "volume": str(c.volume),
        }
        for c in candles
    ]
    session.execute(sqlite_insert(CandleRow).values(rows).on_conflict_do_nothing())
    session.commit()


def load_candles(session: Session, market: str, tf: Tf, limit: int) -> list[Candle]:
    """The newest ``limit`` candles for a market/timeframe, ascending."""
    rows = session.scalars(
        select(CandleRow)
        .where(CandleRow.market == market, CandleRow.tf == tf)
        .order_by(CandleRow.open_time_ms.desc())
        .limit(limit)
    ).all()
    return [
        Candle(
            open_time=datetime.fromtimestamp(r.open_time_ms / 1000, UTC),
            open=Decimal(r.open),
            high=Decimal(r.high),
            low=Decimal(r.low),
            close=Decimal(r.close),
            volume=Decimal(r.volume),
        )
        for r in reversed(rows)
    ]


def candle_counts(session: Session, market: str) -> dict[str, int]:
    """Stored candle count per timeframe for a market (warming-up check)."""
    rows = session.execute(
        select(CandleRow.tf, func.count()).where(CandleRow.market == market).group_by(CandleRow.tf)
    ).all()
    return dict(rows)


# ------------------------------------------------------------------ markets --


def get_market_row(session: Session, market: str) -> MarketRow | None:
    return session.get(MarketRow, market)


def list_market_rows(session: Session) -> list[MarketRow]:
    return list(session.scalars(select(MarketRow).order_by(MarketRow.market)).all())


def upsert_market_row(session: Session, row: MarketRow) -> MarketRow:
    merged = session.merge(row)
    session.commit()
    return merged
