"""Database engine, the settings table, migrations and the single-engine lock.

The backend takes an exclusive lock file next to the database at startup, so
two engine instances can never trade the same account.
"""

from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from alembic import command
from app.config import BACKEND_DIR

ALEMBIC_DIR = BACKEND_DIR / "alembic"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


class Base(DeclarativeBase):
    pass


class SettingsRow(Base):
    """The settings document: one row, one validated JSON blob."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=1)
    doc: Mapped[str]
    updated_at: Mapped[datetime]


class CandleRow(Base):
    """One closed candle per market and timeframe.

    Prices and volume are stored as their decimal strings so stored candles
    stay exact; they only become floats inside indicator maths.
    """

    __tablename__ = "candles"

    market: Mapped[str] = mapped_column(primary_key=True)
    tf: Mapped[str] = mapped_column(primary_key=True)
    open_time_ms: Mapped[int] = mapped_column(primary_key=True)
    open: Mapped[str]
    high: Mapped[str]
    low: Mapped[str]
    close: Mapped[str]
    volume: Mapped[str]


class MarketRow(Base):
    """Eligibility and activation state for one Bitvavo EUR market.

    The streak counters implement the anti-flapping buffer: a market fails
    only after ``eligibility_fail_days`` consecutive daily checks below a
    threshold (or at once when 20% beyond it), and needs
    ``eligibility_recover_days`` consecutive passing checks to return.
    """

    __tablename__ = "markets"

    market: Mapped[str] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(default="")
    quote: Mapped[str] = mapped_column(default="EUR")
    status: Mapped[str] = mapped_column(default="")
    eligible: Mapped[bool] = mapped_column(default=False)
    failed_rule: Mapped[str | None]
    volume_30d_eur: Mapped[float | None]
    spread_pct: Mapped[float | None]
    daily_candles: Mapped[int | None]
    below_streak: Mapped[int] = mapped_column(default=0)
    within_streak: Mapped[int] = mapped_column(default=0)
    checked_at: Mapped[datetime | None]
    active: Mapped[bool] = mapped_column(default=False)
    activated_at: Mapped[datetime | None]


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def run_migrations(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


class EngineAlreadyRunningError(RuntimeError):
    pass


class EngineLock:
    """Exclusive advisory lock on a file next to the database.

    Uses fcntl.flock so the lock is released automatically if the process dies
    (no stale pid files). Refuses to start while another instance holds it.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock_path = db_path.with_suffix(db_path.suffix + ".lock")
        self._fh: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._lock_path

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._lock_path.open("w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            msg = (
                f"another trading-agent instance already holds the engine lock "
                f"at {self._lock_path}; refusing to start"
            )
            raise EngineAlreadyRunningError(msg) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def utcnow() -> datetime:
    return datetime.now(UTC)
