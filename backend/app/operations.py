"""Daily SQLite backups."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from app.db import utcnow


def backup_due(data_dir: Path, now: datetime) -> bool:
    scheduled = now.replace(hour=3, minute=0, second=0, microsecond=0)
    target = data_dir / "backups" / f"agent-{now:%Y-%m-%d}.db"
    return now >= scheduled and not target.exists()


def next_backup_at(now: datetime) -> datetime:
    scheduled = now.replace(hour=3, minute=0, second=0, microsecond=0)
    return scheduled if now < scheduled else scheduled + timedelta(days=1)


def backup_database(db_path: Path, data_dir: Path, now: datetime) -> Path:
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"agent-{now:%Y-%m-%d}.db"
    if not target.exists():
        with sqlite3.connect(db_path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
    backups = sorted(backup_dir.glob("agent-*.db"), reverse=True)
    for old in backups[14:]:
        old.unlink()
    return target


async def backup_loop(
    db_path: Path,
    data_dir: Path,
    clock: Callable[[], datetime] = utcnow,
) -> None:
    while True:
        now = clock()
        if backup_due(data_dir, now):
            await asyncio.to_thread(backup_database, db_path, data_dir, now)
        await asyncio.sleep(max(60, (next_backup_at(now) - now).total_seconds()))
