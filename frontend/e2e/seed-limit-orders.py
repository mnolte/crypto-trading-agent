"""Seeds three decisions with a limit entry order each — waiting, filled
and unfilled — into the throwaway e2e database so the drawer-state
screenshots are deterministic. Test data only; never runs against a real
database.
"""

import os
import sqlite3
from datetime import UTC, datetime, timedelta

NOW = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)
REGIME = '{"trend":"up","volatility":"normal","age_4h":2}'
# The waiting order rests far below the simulated BTC-EUR last (~33k), so
# `buy_fills` never fires, the fill-time gate recheck is never reached and
# the 30-second watcher leaves the order open for the whole suite — a
# resting order at the market would fill or be gate-cancelled before the
# screenshot runs. `expires_at` lands in the seeded future, past wall time.
PLAN = (
    '{"entry":"75180","fill_entry":"75170","stop":"74040","target":"77430",'
    '"qty":"0.0885","notional":"6652.845","rr":"2.00"}'
)
WAIT_PLAN = (
    '{"entry":"30300","fill_entry":"30000","stop":"29400","target":"31500",'
    '"qty":"0.0885","notional":"2655","rr":"2.50"}'
)
COLUMNS = (
    "id, cycle, at, account_id, pair, decider, model, prompt_version, action,"
    " confidence, reasoning, invalidation, signals, plan, gates, status,"
    " execute_at, error, regime, btc_regime, invalidation_price, risk_pct_used,"
    " settings_version, shadow_id, stand_in, raw_brief, raw_response, prefilter"
)


def _decision(
    row_id: str,
    minutes_ago: int,
    status: str,
    execute_at: datetime | None,
    pair: str = "BTC-EUR",
    plan: str = PLAN,
) -> tuple[object, ...]:
    return (
        row_id,
        1,
        NOW - timedelta(minutes=minutes_ago),
        1,
        pair,
        "openai",
        "gpt-4o-mini",
        "decision.v1",
        "BUY",
        0.8,
        "Higher low held; entry rests as a post-only limit.",
        "1h close below the nearest support",
        "[]",
        plan,
        "[]",
        status,
        execute_at,
        None,
        REGIME,
        REGIME,
        None,
        "1",
        1,
        None,
        False,
        None,
        None,
        True,
    )


def _order(
    decision_id: str,
    placed: datetime,
    expires: datetime | None,
    status: str,
    filled_at: datetime | None = None,
    fill_price: str | None = None,
    cancelled_at: datetime | None = None,
    cancel_reason: str | None = None,
    min_price: str | None = None,
    pair: str = "BTC-EUR",
    limit: str = "75170",
    qty: str = "0.0885",
) -> tuple[object, ...]:
    return (
        decision_id,
        1,
        pair,
        "buy",
        "entry",
        limit,
        qty,
        placed,
        expires,
        status,
        filled_at,
        fill_price,
        cancelled_at,
        cancel_reason,
        min_price,
    )


def main() -> None:
    db = os.environ["TA_DB_PATH"]
    conn = sqlite3.connect(db)
    try:
        conn.execute("BEGIN")
        ids = ("e2e-limit-wait", "e2e-limit-fill", "e2e-limit-unf")
        conn.execute(f"DELETE FROM decisions WHERE id IN ({','.join('?' * 3)})", ids)
        conn.execute(
            f"DELETE FROM paper_orders WHERE decision_id IN ({','.join('?' * 3)})",
            ids,
        )
        conn.executemany(
            f"INSERT INTO decisions ({COLUMNS}) VALUES ({','.join('?' * 28)})",
            [
                _decision(
                    "e2e-limit-wait",
                    15,
                    "none",
                    None,
                    plan=WAIT_PLAN,
                ),
                _decision(
                    "e2e-limit-fill",
                    12,
                    "executed",
                    NOW - timedelta(minutes=9),
                ),
                _decision("e2e-limit-unf", 6, "unfilled", None),
            ],
        )
        conn.executemany(
            "INSERT INTO paper_orders (decision_id, account_id, pair, side, leg,"
            " limit_price, qty, placed_at, expires_at, status, filled_at,"
            " fill_price, cancelled_at, cancel_reason, min_price) VALUES "
            f"({','.join('?' * 15)})",
            [
                _order(
                    "e2e-limit-wait",
                    NOW - timedelta(minutes=15),
                    NOW + timedelta(minutes=10),
                    "open",
                    limit="30000",
                    qty="0.0885",
                ),
                _order(
                    "e2e-limit-fill",
                    NOW - timedelta(minutes=15),
                    NOW - timedelta(minutes=2),
                    "filled",
                    filled_at=NOW - timedelta(minutes=9),
                    fill_price="75170",
                ),
                _order(
                    "e2e-limit-unf",
                    NOW - timedelta(minutes=16),
                    NOW - timedelta(minutes=1),
                    "cancelled",
                    cancelled_at=NOW - timedelta(minutes=1),
                    cancel_reason="next cycle",
                    min_price="75176",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
