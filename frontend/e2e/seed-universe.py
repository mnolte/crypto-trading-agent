"""Seeds Universe-screen states into the throwaway e2e database: a failed
check run (amber check line) and a running backfill (open Data sync card).
Test data only; never runs against a real database.
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

DB = os.environ.get("TA_DB_PATH", "/tmp/ta-e2e/agent.db")
NOW = datetime.now(UTC).replace(tzinfo=None)


def seed_failed_check() -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT INTO market_checks"
            " (started_at, finished_at, trigger, status, error, thresholds,"
            "  funnel, kept, held_back)"
            " VALUES (?, ?, 'daily', 'failed', ?, '{}', '[]', '[]', '[]')",
            (
                NOW - timedelta(minutes=5),
                NOW - timedelta(minutes=4),
                "Bitvavo didn't answer",
            ),
        )
        con.commit()
    finally:
        con.close()


def seed_running_backfill() -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT INTO sync_runs"
            " (job, started_at, finished_at, status, done, total, failures,"
            "  updated_at)"
            " VALUES ('backfill:SHOT-EUR', ?, NULL, 'running', 400, 1400,"
            "  NULL, ?)",
            (NOW - timedelta(seconds=10), NOW),
        )
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        if arg == "failed-check":
            seed_failed_check()
        elif arg == "running-backfill":
            seed_running_backfill()
        else:
            raise SystemExit(f"unknown seed {arg}")
