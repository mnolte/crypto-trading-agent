"""Since-start performance figures shared by the Dashboard tile and the
Readiness check — the same BTC buy-and-hold comparison both places."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app import repo
from app.db import AccountRow


def since_start(
    session: Session, account: AccountRow, equity: Decimal, now: datetime
) -> tuple[Decimal, Decimal | None]:
    """(agent return %, BTC buy-and-hold % since the account started).

    The agent leg is equity against the account's start balance; the BTC
    leg compares the newest stored 1d close with the last 1d close stored
    at or before the account's start — the same method the Readiness
    "Beats holding BTC" criterion uses. ``None`` until BTC daily candles
    exist on both ends.
    """
    started = repo.aware_utc(account.started_at)
    start_balance = Decimal(account.start_balance)
    agent_pct = (equity / start_balance - 1) * 100 if start_balance > 0 else Decimal("0")
    btc_then = repo.load_candles(session, "BTC-EUR", "1d", 400, end=started)
    btc_now = repo.load_candles(session, "BTC-EUR", "1d", 5, end=now)
    btc_pct: Decimal | None = None
    if btc_then and btc_now and btc_then[-1].close > 0:
        btc_pct = (btc_now[-1].close / btc_then[-1].close - 1) * 100
    return agent_pct, btc_pct
