from __future__ import annotations

import argparse
import asyncio

from app import repo
from app.config import get_env
from app.context.collector import ContextCollector
from app.db import make_engine, make_session_factory, run_migrations
from app.sources import SourceRegistry


async def run(days: int) -> None:
    env = get_env()
    run_migrations(env.ta_db_path)
    engine = make_engine(env.ta_db_path)
    sessions = make_session_factory(engine)
    session = sessions()
    try:
        settings = repo.load_settings(session)
    finally:
        session.close()
    collector = ContextCollector(sessions, SourceRegistry(settings.disabled_sources))
    try:
        counts = await collector.collect(days=days)
    finally:
        await collector.aclose()
        engine.dispose()
    print(f"Market-context backfill requested: {days} days")
    for metric in (
        "funding_rate_settled",
        "open_interest_usd",
        "long_short_account_ratio",
        "dvol",
    ):
        print(f"{metric}: {counts.get(metric, 0)} rows fetched")
    print("skew_25d: cannot be backfilled; option greeks are point-in-time only")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    asyncio.run(run(args.days))


if __name__ == "__main__":
    main()
