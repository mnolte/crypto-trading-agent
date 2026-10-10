"""Repositories: typed access to SQLAlchemy tables."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.agent.market.base import Candle
from app.db import (
    AccountRow,
    ActivityRow,
    CandleRow,
    DecisionRow,
    EquitySnapshotRow,
    ExitTickRow,
    MarketRow,
    PositionRow,
    SettingsRow,
    TradeRow,
    utcnow,
)
from app.models import Decision, Position, Settings, Tf

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


# ----------------------------------------------------------------- accounts --


def current_account(
    session: Session, start_balance: Decimal | None = None, day_start_date: str | None = None
) -> AccountRow:
    """The active paper account; created on first run."""
    row = session.scalar(select(AccountRow).where(AccountRow.active.is_(True)))
    if row is None:
        balance = start_balance if start_balance is not None else Decimal("10000")
        row = AccountRow(
            started_at=utcnow(),
            start_balance=str(balance),
            cash=str(balance),
            peak_equity=str(balance),
            day_start_equity=str(balance),
            day_start_date=day_start_date or "",
            active=True,
        )
        session.add(row)
        session.commit()
    return row


def reset_account(session: Session, start_balance: Decimal, day_start_date: str) -> AccountRow:
    """Start a new paper account: deactivate the old one, drop open
    positions, clear the Killed state. Trades and decisions stay tagged
    with the account they belonged to."""
    old = current_account(session)
    old.active = False
    old.kill_at = None
    session.execute(delete(PositionRow).where(PositionRow.account_id == old.id))
    fresh = AccountRow(
        started_at=utcnow(),
        start_balance=str(start_balance),
        cash=str(start_balance),
        peak_equity=str(start_balance),
        day_start_equity=str(start_balance),
        day_start_date=day_start_date,
        active=True,
    )
    session.add(fresh)
    session.commit()
    return fresh


# --------------------------------------------------------------- positions --


def list_position_rows(session: Session, account_id: int) -> list[PositionRow]:
    return list(
        session.scalars(select(PositionRow).where(PositionRow.account_id == account_id)).all()
    )


def save_position_row(
    session: Session,
    pos: Position,
    account_id: int,
    invalidation_price: Decimal | None,
    highest_close: Decimal | None,
    r_unit: Decimal,
) -> None:
    row = session.get(PositionRow, pos.pair)
    if row is None:
        row = PositionRow(
            pair=pos.pair,
            account_id=account_id,
            qty="0",
            entry="0",
            stop="0",
            target="0",
            opened_at=pos.opened_at,
            decision_id=pos.decision_id,
            last="0",
            invalidation_price=None,
            highest_close=None,
            r_unit="0",
        )
    row.account_id = account_id
    row.qty = str(pos.qty)
    row.entry = str(pos.entry)
    row.stop = str(pos.stop)
    row.target = str(pos.target)
    row.opened_at = pos.opened_at
    row.decision_id = pos.decision_id
    row.last = str(pos.last)
    row.invalidation_price = str(invalidation_price) if invalidation_price is not None else None
    row.highest_close = str(highest_close) if highest_close is not None else None
    row.r_unit = str(r_unit)
    session.merge(row)


def delete_position_row(session: Session, pair: str) -> None:
    row = session.get(PositionRow, pair)
    if row is not None:
        session.delete(row)


# ------------------------------------------------------------------- trades --


def add_trade(session: Session, row: TradeRow) -> TradeRow:
    session.add(row)
    return row


def list_trades(session: Session, account_id: int, limit: int = 500) -> list[TradeRow]:
    return list(
        session.scalars(
            select(TradeRow)
            .where(TradeRow.account_id == account_id)
            .order_by(TradeRow.closed_at.desc(), TradeRow.id.desc())
            .limit(limit)
        ).all()
    )


def last_exit_for_pair(
    session: Session, account_id: int, pair: str, reason: str
) -> TradeRow | None:
    return session.scalar(
        select(TradeRow)
        .where(
            TradeRow.account_id == account_id,
            TradeRow.pair == pair,
            TradeRow.exit_reason == reason,
        )
        .order_by(TradeRow.closed_at.desc())
        .limit(1)
    )


def losing_streak(session: Session, account_id: int) -> int:
    """Consecutive most-recent losing trades (realised_r < 0)."""
    streak = 0
    for trade in list_trades(session, account_id, limit=50):
        if Decimal(trade.realised_r) < 0:
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------- decisions --


def save_decision(session: Session, decision: Decision) -> None:
    row = session.get(DecisionRow, decision.id)
    if row is None:
        row = DecisionRow(
            id=decision.id,
            cycle=decision.cycle,
            at=decision.at,
            account_id=decision.account_id,
            pair=decision.pair,
            decider=decision.decider,
            action=decision.action,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            signals="[]",
            gates="[]",
            status="none",
            regime="{}",
            btc_regime="{}",
            risk_pct_used="0",
            settings_version=0,
        )
    row.cycle = decision.cycle
    row.at = decision.at
    row.account_id = decision.account_id
    row.pair = decision.pair
    row.decider = decision.decider
    row.model = decision.model
    row.prompt_version = decision.prompt_version
    row.action = decision.action
    row.confidence = decision.confidence
    row.reasoning = decision.reasoning
    row.invalidation = decision.invalidation
    row.signals = "[" + ",".join(s.model_dump_json() for s in decision.signals) + "]"
    row.plan = decision.plan.model_dump_json() if decision.plan is not None else None
    row.gates = "[" + ",".join(g.model_dump_json() for g in decision.gates) + "]"
    row.status = decision.status
    row.execute_at = decision.execute_at
    row.error = decision.error
    row.regime = decision.regime.model_dump_json()
    row.btc_regime = decision.btc_regime.model_dump_json()
    row.invalidation_price = (
        str(decision.invalidation_price) if decision.invalidation_price is not None else None
    )
    row.risk_pct_used = str(decision.risk_pct_used)
    row.settings_version = decision.settings_version
    row.shadow_id = decision.shadow_id
    row.stand_in = decision.stand_in
    session.merge(row)


def get_decision_row(session: Session, decision_id: str) -> DecisionRow | None:
    return session.get(DecisionRow, decision_id)


def latest_decisions(session: Session, account_id: int, pairs: list[str]) -> dict[str, DecisionRow]:
    """The newest decision row per pair for the snapshot."""
    out: dict[str, DecisionRow] = {}
    for pair in pairs:
        row = session.scalar(
            select(DecisionRow)
            .where(DecisionRow.account_id == account_id, DecisionRow.pair == pair)
            .order_by(DecisionRow.at.desc())
            .limit(1)
        )
        if row is not None:
            out[pair] = row
    return out


def pending_decisions(session: Session, account_id: int) -> list[DecisionRow]:
    return list(
        session.scalars(
            select(DecisionRow).where(
                DecisionRow.account_id == account_id,
                DecisionRow.status == "pending",
            )
        ).all()
    )


def expire_pending(session: Session, account_id: int) -> int:
    """Mark every pending decision expired (restart / veto deadline)."""
    rows = pending_decisions(session, account_id)
    for row in rows:
        row.status = "expired"
    return len(rows)


# ----------------------------------------------------------------- activity --


def add_activity(
    session: Session,
    kind: str,
    text: str,
    pair: str | None = None,
    decision_id: str | None = None,
    at: datetime | None = None,
) -> ActivityRow:
    row = ActivityRow(
        at=at or utcnow(),
        kind=kind,
        pair=pair,
        text=text,
        decision_id=decision_id,
    )
    session.add(row)
    return row


def recent_activity(session: Session, limit: int = 100) -> list[ActivityRow]:
    return list(
        session.scalars(select(ActivityRow).order_by(ActivityRow.id.desc()).limit(limit)).all()
    )


# ------------------------------------------------------------ equity snaps --


def add_equity_snapshot(
    session: Session,
    account_id: int,
    cycle: int,
    equity: Decimal,
    cash: Decimal,
    at: datetime | None = None,
) -> None:
    session.add(
        EquitySnapshotRow(
            account_id=account_id,
            at=at or utcnow(),
            cycle=cycle,
            equity=str(equity),
            cash=str(cash),
        )
    )


# --------------------------------------------------------------- exit ticks --


def add_exit_tick(
    session: Session,
    account_id: int,
    pair: str,
    price: Decimal,
    kind: str,
    at: datetime | None = None,
) -> None:
    session.add(
        ExitTickRow(
            account_id=account_id,
            pair=pair,
            at=at or utcnow(),
            price=str(price),
            kind=kind,
        )
    )
