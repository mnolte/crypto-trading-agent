"""Repositories: typed access to SQLAlchemy tables."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import Table, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.agent.market.base import Candle
from app.db import (
    AMSTERDAM,
    AccountRow,
    ActivityRow,
    CandleRow,
    DecisionRow,
    EquitySnapshotRow,
    ExitTickRow,
    LlmCallRow,
    MarketRow,
    PositionRow,
    SettingsRow,
    TradeRow,
    utcnow,
)
from app.models import Decision, Position, Settings, Tf

SETTINGS_ROW_ID = 1


def _utc_naive(dt: datetime) -> datetime:
    """SQLite stores datetimes naive; an aware bound is normalised to naive
    UTC so comparisons stay correct regardless of the client's offset."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def aware_utc(dt: datetime) -> datetime:
    """Naive datetimes read back from SQLite are UTC by convention."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _epoch_ms(dt: datetime) -> int:
    """UTC epoch milliseconds; naive input is UTC by the same convention."""
    return int(aware_utc(dt).timestamp() * 1000)


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
    # Candles are append-mostly: bars newer than the stored max always go in.
    # Bars at or below it are almost all duplicates — skip them unless the
    # table is missing some of those slots (a gap a later fetch can heal).
    newest = session.scalar(
        select(func.max(CandleRow.open_time_ms)).where(
            CandleRow.market == market, CandleRow.tf == tf
        )
    )
    if newest is not None:
        head = [c for c in candles if _epoch_ms(c.open_time) <= newest]
        tail = [c for c in candles if _epoch_ms(c.open_time) > newest]
        if head:
            head_min = min(_epoch_ms(c.open_time) for c in head)
            stored = session.scalar(
                select(func.count())
                .select_from(CandleRow)
                .where(
                    CandleRow.market == market,
                    CandleRow.tf == tf,
                    CandleRow.open_time_ms >= head_min,
                    CandleRow.open_time_ms <= newest,
                )
            )
            if stored == len(head):
                head = []
        candles = head + tail
    if not candles:
        return
    rows = [
        {
            "market": market,
            "tf": tf,
            "open_time_ms": _epoch_ms(c.open_time),
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "volume": str(c.volume),
        }
        for c in candles
    ]
    session.execute(
        sqlite_insert(cast(Table, CandleRow.__table__)).values(rows).on_conflict_do_nothing()
    )
    session.commit()


def load_candles(
    session: Session, market: str, tf: Tf, limit: int, end: datetime | None = None
) -> list[Candle]:
    """The newest ``limit`` candles for a market/timeframe, ascending.

    ``end`` caps the view at candle opens at or before that instant — the
    frozen read the Indicators screen uses for a past decision.
    """
    conds = [CandleRow.market == market, CandleRow.tf == tf]
    if end is not None:
        conds.append(CandleRow.open_time_ms <= _epoch_ms(end))
    rows = session.scalars(
        select(CandleRow).where(*conds).order_by(CandleRow.open_time_ms.desc()).limit(limit)
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
    """Persist the current account's open position.

    ``pair`` is the positions table's sole primary key because v0.1 allows one
    open position per pair. A stale row from an old account is removed before
    the current account's row is written; account reset is the normal path
    that clears old rows.
    """
    row = session.scalar(
        select(PositionRow).where(
            PositionRow.pair == pos.pair,
            PositionRow.account_id == account_id,
        )
    )
    if row is None:
        session.execute(delete(PositionRow).where(PositionRow.pair == pos.pair))
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


def delete_position_row(session: Session, pair: str, account_id: int) -> None:
    row = session.get(PositionRow, pair)
    if row is not None and row.account_id == account_id:
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


def list_trade_rows(
    session: Session,
    account_id: int,
    pair: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 500,
) -> list[TradeRow]:
    """Closed trades for the account, newest first. ``start``/``end`` filter
    on the close time."""
    conds = [TradeRow.account_id == account_id]
    if pair is not None:
        conds.append(TradeRow.pair == pair)
    if start is not None:
        conds.append(TradeRow.closed_at >= _utc_naive(start))
    if end is not None:
        conds.append(TradeRow.closed_at <= _utc_naive(end))
    return list(
        session.scalars(
            select(TradeRow)
            .where(*conds)
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


def trades_today(session: Session, account_id: int, now: datetime | None = None) -> list[TradeRow]:
    """Trades closed today on the Amsterdam clock — the same day boundary
    the daily loss limit uses."""
    now = now or utcnow()
    midnight_ams = now.astimezone(AMSTERDAM).replace(hour=0, minute=0, second=0, microsecond=0)
    return list_trade_rows(session, account_id, start=midnight_ams, end=now, limit=500)


# ---------------------------------------------------------------- decisions --


def save_decision(
    session: Session,
    decision: Decision,
    raw_brief: str | None = None,
    raw_response: str | None = None,
) -> None:
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
            raw_brief=None,
            raw_response=None,
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
    row.prefilter = decision.prefilter
    if raw_brief is not None:
        row.raw_brief = raw_brief
    if raw_response is not None:
        row.raw_response = raw_response
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


def list_decision_rows(
    session: Session,
    pair: str | None = None,
    action: str | None = None,
    status: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int | None = 50,
) -> list[DecisionRow]:
    """Newest-first decision history for the drawer and activity links."""
    conds = []
    if pair is not None:
        conds.append(DecisionRow.pair == pair)
    if action is not None:
        conds.append(DecisionRow.action == action)
    if status is not None:
        conds.append(DecisionRow.status == status)
    if after is not None:
        conds.append(DecisionRow.at >= _utc_naive(after))
    if before is not None:
        conds.append(DecisionRow.at < _utc_naive(before))
    stmt = select(DecisionRow).where(*conds).order_by(DecisionRow.at.desc(), DecisionRow.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def list_prefilter_skips(
    session: Session,
    account_id: int,
    pair: str | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[DecisionRow]:
    """Decisions where the Rules pre-filter passed nothing to the model —
    the inspector's "skipped" rows."""
    conds = [
        DecisionRow.account_id == account_id,
        DecisionRow.prefilter.is_(False),
    ]
    if pair is not None:
        conds.append(DecisionRow.pair == pair)
    if before is not None:
        conds.append(DecisionRow.at < _utc_naive(before))
    return list(
        session.scalars(
            select(DecisionRow)
            .where(*conds)
            .order_by(DecisionRow.at.desc(), DecisionRow.id.desc())
            .limit(limit)
        ).all()
    )


# ---------------------------------------------------------------- llm calls --


def add_llm_call(session: Session, row: LlmCallRow) -> LlmCallRow:
    session.add(row)
    return row


def get_llm_call(session: Session, call_id: str) -> LlmCallRow | None:
    return session.get(LlmCallRow, call_id)


def list_replays(session: Session, replay_of: str) -> list[LlmCallRow]:
    return list(
        session.scalars(
            select(LlmCallRow)
            .where(LlmCallRow.replay_of == replay_of)
            .order_by(LlmCallRow.at.asc())
        ).all()
    )


def list_llm_calls(
    session: Session,
    account_id: int,
    pair: str | None = None,
    status: str | None = None,
    verdict: str | None = None,
    outcome: str | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[LlmCallRow]:
    """Newest-first stored model calls for the inspector.

    ``verdict`` accepts "correct", "wrong" or "unreviewed" (NULL). ``outcome``
    accepts "accepted" (no failed output check) or "rejected".
    """
    conds = [LlmCallRow.account_id == account_id]
    if pair is not None:
        conds.append(LlmCallRow.pair == pair)
    if status is not None:
        conds.append(LlmCallRow.status == status)
    if verdict == "unreviewed":
        conds.append(LlmCallRow.verdict.is_(None))
    elif verdict is not None:
        conds.append(LlmCallRow.verdict == verdict)
    if outcome == "accepted":
        conds.append(LlmCallRow.output_checks.not_like('%"status": "fail"%'))
    elif outcome == "rejected":
        conds.append(LlmCallRow.output_checks.like('%"status": "fail"%'))
    if before is not None:
        conds.append(LlmCallRow.at < _utc_naive(before))
    return list(
        session.scalars(
            select(LlmCallRow)
            .where(*conds)
            .order_by(LlmCallRow.at.desc(), LlmCallRow.id.desc())
            .limit(limit)
        ).all()
    )


def list_llm_calls_with_issues(
    session: Session,
    account_id: int,
    pair: str | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[LlmCallRow]:
    """Calls with at least one warning or failed output check."""
    conds = [
        LlmCallRow.account_id == account_id,
        (
            LlmCallRow.output_checks.like('%"status": "warn"%')
            | LlmCallRow.output_checks.like('%"status": "fail"%')
        ),
    ]
    if pair is not None:
        conds.append(LlmCallRow.pair == pair)
    if before is not None:
        conds.append(LlmCallRow.at < _utc_naive(before))
    return list(
        session.scalars(
            select(LlmCallRow)
            .where(*conds)
            .order_by(LlmCallRow.at.desc(), LlmCallRow.id.desc())
            .limit(limit)
        ).all()
    )


def llm_calls_since(
    session: Session,
    account_id: int,
    since: datetime,
    features: tuple[str, ...],
) -> int:
    """Calls actually sent (blocked input doesn't count) since ``since`` —
    the daily budget counter."""
    return (
        session.scalar(
            select(func.count())
            .select_from(LlmCallRow)
            .where(
                LlmCallRow.account_id == account_id,
                LlmCallRow.at >= _utc_naive(since),
                LlmCallRow.feature.in_(features),
                LlmCallRow.status != "input_blocked",
            )
        )
        or 0
    )


def llm_cost_between(
    session: Session,
    account_id: int,
    start: datetime,
    end: datetime | None = None,
    feature: str | None = None,
) -> Decimal:
    """Estimated EUR spend in the window; unknown-priced calls count as 0."""
    conds = [
        LlmCallRow.account_id == account_id,
        LlmCallRow.at >= _utc_naive(start),
    ]
    if end is not None:
        conds.append(LlmCallRow.at < _utc_naive(end))
    if feature is not None:
        conds.append(LlmCallRow.feature == feature)
    total = Decimal("0")
    for cost in session.scalars(select(LlmCallRow.cost_eur).where(*conds)).all():
        if cost is not None:
            total += Decimal(cost)
    return total


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


def list_activity(
    session: Session,
    pair: str | None = None,
    kind: str | None = None,
    before: int | None = None,
    limit: int = 50,
) -> list[ActivityRow]:
    """Newest-first activity rows; ``before`` is a row-id cursor."""
    conds = []
    if pair is not None:
        conds.append(ActivityRow.pair == pair)
    if kind is not None:
        conds.append(ActivityRow.kind == kind)
    if before is not None:
        conds.append(ActivityRow.id < before)
    return list(
        session.scalars(
            select(ActivityRow).where(*conds).order_by(ActivityRow.id.desc()).limit(limit)
        ).all()
    )


def list_equity_snapshots(
    session: Session, account_id: int, start: datetime, end: datetime
) -> list[EquitySnapshotRow]:
    return list(
        session.scalars(
            select(EquitySnapshotRow)
            .where(
                EquitySnapshotRow.account_id == account_id,
                EquitySnapshotRow.at >= _utc_naive(start),
                EquitySnapshotRow.at <= _utc_naive(end),
            )
            .order_by(EquitySnapshotRow.at.asc(), EquitySnapshotRow.id.asc())
        ).all()
    )


def delete_all_data(session: Session) -> None:
    """Wipe every table except settings (``DELETE /api/data``)."""
    for table in (
        CandleRow,
        MarketRow,
        AccountRow,
        PositionRow,
        TradeRow,
        DecisionRow,
        ActivityRow,
        EquitySnapshotRow,
        ExitTickRow,
        LlmCallRow,
    ):
        session.execute(delete(table))
    session.commit()


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
