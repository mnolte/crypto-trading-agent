"""Repositories: typed access to SQLAlchemy tables."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import Numeric, Table, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.agent.market.base import Candle
from app.db import (
    AMSTERDAM,
    AccountRow,
    ActivityRow,
    AlertRow,
    AskConversationRow,
    BriefingRow,
    CandleRow,
    DecisionRow,
    EntryPauseRow,
    EquitySnapshotRow,
    ErrorRow,
    ExitTickRow,
    FillRow,
    LlmCallRow,
    MarketContextRow,
    MarketRow,
    MetaRow,
    OutcomeFailureRow,
    OutcomeRow,
    PaperOrderRow,
    PositionRow,
    SettingsHistoryRow,
    SettingsRow,
    ShadowRow,
    SkippedSlotRow,
    StopMoveRow,
    TradeRow,
    WhatifRow,
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
    """Persist the settings document, log every changed leaf to the
    settings history and bump the version. Returns the version."""
    row = session.get(SettingsRow, SETTINGS_ROW_ID)
    if row is None:
        row = SettingsRow(id=SETTINGS_ROW_ID, version=0, doc="", updated_at=utcnow())
        session.add(row)
    if row.doc:
        try:
            record_settings_history(session, Settings.model_validate_json(row.doc), settings)
        except Exception as exc:  # noqa: BLE001 - history must never block a save
            logging.getLogger(__name__).warning(
                "settings history could not be written: %s", exc, extra={"event": "job.failed"}
            )
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
    # Epoch ms is computed once per candle and carried alongside.
    timed = [(c, _epoch_ms(c.open_time)) for c in candles]
    if newest is not None:
        head = [t for t in timed if t[1] <= newest]
        tail = [t for t in timed if t[1] > newest]
        if head:
            head_min = min(ms for _, ms in head)
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
        timed = head + tail
    if not timed:
        return
    # Raw executemany: cycles upsert hundreds of bars per pair and timeframe,
    # so the ORM insert's per-value bindparam work dominates the call.
    session.connection().exec_driver_sql(
        "INSERT OR IGNORE INTO candles "
        "(market, tf, open_time_ms, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                market,
                tf,
                ms,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
            )
            for c, ms in timed
        ],
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


# ----------------------------------------------------------- market context --


def upsert_market_context(session: Session, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    statement = sqlite_insert(cast(Table, MarketContextRow.__table__)).values(rows)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["source", "metric", "symbol", "ts"],
            set_={
                "value": statement.excluded.value,
                "meta": statement.excluded.meta,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )
    session.commit()
    return len(rows)


def list_market_context(
    session: Session,
    *,
    metric: str | None = None,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MarketContextRow]:
    conditions = []
    if metric is not None:
        conditions.append(MarketContextRow.metric == metric)
    if symbol is not None:
        conditions.append(MarketContextRow.symbol == symbol)
    if start is not None:
        conditions.append(MarketContextRow.ts >= _utc_naive(start))
    if end is not None:
        conditions.append(MarketContextRow.ts <= _utc_naive(end))
    return list(
        session.scalars(
            select(MarketContextRow).where(*conditions).order_by(MarketContextRow.ts)
        ).all()
    )


def newest_market_context(session: Session, source: str) -> datetime | None:
    value = session.scalar(
        select(func.max(MarketContextRow.ts)).where(MarketContextRow.source == source)
    )
    return aware_utc(value) if value is not None else None


def percentile_90d(session: Session, metric: str, symbol: str, at: datetime) -> float | None:
    end = _utc_naive(at)
    start = _utc_naive(at - timedelta(days=90))
    rows = session.execute(
        select(MarketContextRow.ts, MarketContextRow.value)
        .where(
            MarketContextRow.metric == metric,
            MarketContextRow.symbol == symbol,
            MarketContextRow.ts >= start,
            MarketContextRow.ts < end,
        )
        .order_by(MarketContextRow.ts)
    ).all()
    if not rows or aware_utc(at) - aware_utc(rows[0].ts) < timedelta(days=30):
        return None
    current = session.scalar(
        select(MarketContextRow.value).where(
            MarketContextRow.metric == metric,
            MarketContextRow.symbol == symbol,
            MarketContextRow.ts == end,
        )
    )
    if current is None:
        return None
    below_or_equal = sum(value <= current for _, value in rows)
    return below_or_equal / len(rows) * 100


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
    # Resolve the retired account's open orders and pending decisions —
    # the watcher and approval endpoints only ever see the active account,
    # so these rows would otherwise stay "open"/"pending" forever.
    now = utcnow()
    for order in open_paper_orders(session, old.id):
        order.status = "cancelled"
        order.cancelled_at = _utc_naive(now)
        order.cancel_reason = "paper reset"
    for row in pending_decisions(session, old.id):
        row.status = "expired"
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
    initial_stop: Decimal,
    entry_maker: bool | None = None,
) -> None:
    """Persist one account's open position on a pair.

    The primary key is (pair, account_id): a shadow's account may hold the
    same pair the live account holds.
    """
    row = session.get(PositionRow, (pos.pair, account_id))
    if row is None:
        row = PositionRow(
            pair=pos.pair,
            account_id=account_id,
            qty="0",
            entry="0",
            stop="0",
            initial_stop=str(initial_stop),
            target="0",
            opened_at=pos.opened_at,
            decision_id=pos.decision_id,
            last="0",
            invalidation_price=None,
            highest_close=None,
            r_unit="0",
            entry_maker=bool(entry_maker),
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
    if entry_maker is not None:
        # None means "persist everything else, keep the stored entry leg"
        # — ticker/invalidation saves must not clobber it.
        row.entry_maker = entry_maker
    session.merge(row)


def delete_position_row(session: Session, pair: str, account_id: int) -> None:
    row = session.get(PositionRow, (pair, account_id))
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
        r = Decimal(trade.realised_r)
        if r < 0:
            streak += 1
        elif r > 0:
            break
        # realised_r == 0 is neither a loss nor a win — the streak
        # continues past a scratch trade rather than resetting on it.
    return streak


def trades_today(session: Session, account_id: int, now: datetime | None = None) -> list[TradeRow]:
    """Trades closed today on the Amsterdam clock — the same day boundary
    the daily loss limit uses."""
    now = now or utcnow()
    midnight_ams = now.astimezone(AMSTERDAM).replace(hour=0, minute=0, second=0, microsecond=0)
    return list_trade_rows(session, account_id, start=midnight_ams, end=now, limit=500)


def trades_in_window(
    session: Session, account_id: int, after: datetime, before: datetime
) -> list[TradeRow]:
    """Trades overlapping [after, before]: opened at or before ``before``
    and closed at or after ``after``, oldest close first — the cycles
    card's entry/exit bucketing source."""
    return list(
        session.scalars(
            select(TradeRow)
            .where(
                TradeRow.account_id == account_id,
                TradeRow.opened_at <= _utc_naive(before),
                TradeRow.closed_at >= _utc_naive(after),
            )
            .order_by(TradeRow.closed_at.asc(), TradeRow.id.asc())
        ).all()
    )


def positions_opened_since(session: Session, account_id: int, after: datetime) -> list[PositionRow]:
    """Open positions whose entry falls at or after ``after`` — entries
    that have no trade row yet because the position is still open."""
    return list(
        session.scalars(
            select(PositionRow).where(
                PositionRow.account_id == account_id,
                PositionRow.opened_at >= _utc_naive(after),
            )
        ).all()
    )


# ------------------------------------------------------------- paper orders --


def add_paper_order(session: Session, row: PaperOrderRow) -> PaperOrderRow:
    session.add(row)
    session.flush()  # assign the autoincrement id
    return row


def open_paper_orders(
    session: Session,
    account_id: int | None = None,
    pair: str | None = None,
    leg: str | None = None,
) -> list[PaperOrderRow]:
    conds = [PaperOrderRow.status == "open"]
    if account_id is not None:
        conds.append(PaperOrderRow.account_id == account_id)
    if pair is not None:
        conds.append(PaperOrderRow.pair == pair)
    if leg is not None:
        conds.append(PaperOrderRow.leg == leg)
    return list(
        session.scalars(select(PaperOrderRow).where(*conds).order_by(PaperOrderRow.id)).all()
    )


def cancel_paper_order(
    session: Session, row: PaperOrderRow, now: datetime, reason: str
) -> PaperOrderRow:
    row.status = "cancelled"
    row.cancelled_at = _utc_naive(now)
    row.cancel_reason = reason
    return row


def fill_paper_order(
    session: Session, row: PaperOrderRow, now: datetime, qty: Decimal | None = None
) -> PaperOrderRow:
    """Mark the order filled at its limit price (the paper fill rule)."""
    row.status = "filled"
    row.filled_at = _utc_naive(now)
    row.fill_price = row.limit_price
    if qty is not None:
        row.qty = str(qty)
    return row


def paper_orders_for_decision(session: Session, decision_id: str) -> list[PaperOrderRow]:
    return list(
        session.scalars(
            select(PaperOrderRow)
            .where(PaperOrderRow.decision_id == decision_id)
            .order_by(PaperOrderRow.id)
        ).all()
    )


def count_paper_orders(
    session: Session, account_id: int, leg: str | None = None
) -> tuple[int, int]:
    """(placed, filled) order counts for a shadow's fill-rate display."""
    conds = [PaperOrderRow.account_id == account_id]
    if leg is not None:
        conds.append(PaperOrderRow.leg == leg)
    placed = session.scalar(select(func.count()).select_from(PaperOrderRow).where(*conds))
    filled = session.scalar(
        select(func.count())
        .select_from(PaperOrderRow)
        .where(*conds, PaperOrderRow.status == "filled")
    )
    return placed or 0, filled or 0


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
    """The newest decision row per pair — two queries for all pairs, not
    one scan of ``decisions`` per pair (item 034)."""
    if not pairs:
        return {}
    newest = (
        select(DecisionRow.pair, func.max(DecisionRow.at).label("mx"))
        .where(DecisionRow.account_id == account_id, DecisionRow.pair.in_(pairs))
        .group_by(DecisionRow.pair)
        .subquery()
    )
    rows = session.scalars(
        select(DecisionRow)
        .where(DecisionRow.account_id == account_id)
        .join(
            newest,
            (DecisionRow.pair == newest.c.pair) & (DecisionRow.at == newest.c.mx),
        )
    ).all()
    out: dict[str, DecisionRow] = {}
    for row in rows:
        out[row.pair] = row
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
    account_id: int | None = None,
    pair: str | None = None,
    action: str | None = None,
    status: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int | None = 50,
) -> list[DecisionRow]:
    """Newest-first decision history for the drawer and activity links."""
    conds = []
    if account_id is not None:
        conds.append(DecisionRow.account_id == account_id)
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


def decision_actions(session: Session, ids: list[str]) -> dict[str, str]:
    """id → action for a batch of decision ids (inspector list rendering)."""
    if not ids:
        return {}
    rows = session.execute(
        select(DecisionRow.id, DecisionRow.action).where(DecisionRow.id.in_(ids))
    ).all()
    return dict(rows)


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
    elif outcome is not None:
        conds.append(
            LlmCallRow.decision_id.in_(
                select(OutcomeRow.decision_id).where(OutcomeRow.label == outcome)
            )
        )
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


def llm_unknown_eur_cost(session: Session, start: datetime, end: datetime | None = None) -> bool:
    """Any successful call in the window whose EUR cost couldn't be
    estimated — the monthly cap must not treat unknown spend as free."""
    conds = [
        LlmCallRow.at >= _utc_naive(start),
        LlmCallRow.status == "ok",
        LlmCallRow.cost_eur.is_(None),
    ]
    if end is not None:
        conds.append(LlmCallRow.at < _utc_naive(end))
    return bool(session.scalar(select(func.count()).select_from(LlmCallRow).where(*conds)))


def llm_costs_by_feature_all(
    session: Session, start: datetime, end: datetime | None = None
) -> dict[str, Decimal]:
    """Estimated EUR spend in the window across every account, grouped by
    the raw feature label (``decision``, ``ask``, ``shadow:<id>``, ...)."""
    conds = [LlmCallRow.at >= _utc_naive(start)]
    if end is not None:
        conds.append(LlmCallRow.at < _utc_naive(end))
    out: dict[str, Decimal] = {}
    for feature, cost in session.execute(
        select(LlmCallRow.feature, LlmCallRow.cost_eur).where(*conds)
    ).all():
        if cost is not None:
            out[feature] = out.get(feature, Decimal("0")) + Decimal(cost)
    return out


def llm_cost_usd_between(
    session: Session,
    account_id: int,
    start: datetime,
    end: datetime | None = None,
) -> Decimal | None:
    """Estimated USD spend in the window.

    ``None`` when any successful call's price was unknown — failed or
    input-blocked calls sent nothing billable and count as 0."""
    conds = [
        LlmCallRow.account_id == account_id,
        LlmCallRow.at >= _utc_naive(start),
    ]
    if end is not None:
        conds.append(LlmCallRow.at < _utc_naive(end))
    total = Decimal("0")
    rows = session.execute(select(LlmCallRow.cost_usd, LlmCallRow.status).where(*conds)).all()
    for cost, status in rows:
        if cost is not None:
            total += Decimal(cost)
        elif status == "ok":
            return None
    return total


def llm_cost_usd_between_all(
    session: Session,
    start: datetime,
    end: datetime | None = None,
) -> Decimal | None:
    """``llm_cost_usd_between`` across every account."""
    conds = [LlmCallRow.at >= _utc_naive(start)]
    if end is not None:
        conds.append(LlmCallRow.at < _utc_naive(end))
    total = Decimal("0")
    rows = session.execute(select(LlmCallRow.cost_usd, LlmCallRow.status).where(*conds)).all()
    for cost, status in rows:
        if cost is not None:
            total += Decimal(cost)
        elif status == "ok":
            return None
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
    if kind == "error":
        # The retired errors table is replaced by logs.db; the event was
        # already logged by the caller, so nothing else is written.
        return row
    session.add(row)
    return row


def cycle_summary_counts(session: Session, account_id: int, cycle: int) -> dict[str, int]:
    """The counts for a cycle's INFO end line: pairs seen, model calls,
    entries, exits and errors — from the stored decision rows."""
    rows = session.execute(
        select(DecisionRow.action, DecisionRow.decider, DecisionRow.error).where(
            DecisionRow.account_id == account_id,
            DecisionRow.cycle == cycle,
        )
    ).all()
    actions = {"BUY": 0, "SELL": 0, "HOLD": 0, "SKIP": 0}
    calls = errors = 0
    for action, decider, error in rows:
        actions[action] = actions.get(action, 0) + 1
        if decider == "openai":
            calls += 1
        if error:
            errors += 1
    return {
        "pairs": len(rows),
        "model_calls": calls,
        "entries": actions["BUY"],
        "exits": actions["SELL"],
        "holds": actions["HOLD"],
        "skips": actions["SKIP"],
        "errors": errors,
    }


def error_count_since(session: Session, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ErrorRow)
            .where(ErrorRow.resolved_at.is_(None), ErrorRow.last_at >= _utc_naive(since))
        )
        or 0
    )


def unresolved_error_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(ErrorRow).where(ErrorRow.resolved_at.is_(None))
        )
        or 0
    )


def list_errors_unresolved(session: Session) -> list[ErrorRow]:
    """Every unresolved error row, oldest first — the Needs you items."""
    return list(
        session.scalars(
            select(ErrorRow).where(ErrorRow.resolved_at.is_(None)).order_by(ErrorRow.first_at)
        ).all()
    )


def resolve_matching_errors(session: Session, text: str, note: str) -> int:
    rows = session.scalars(
        select(ErrorRow).where(ErrorRow.resolved_at.is_(None), ErrorRow.text == text)
    ).all()
    now = utcnow()
    for row in rows:
        row.resolved_at = now
        row.resolution_note = note
    return len(rows)


def list_activity(
    session: Session,
    pair: str | None = None,
    kind: str | None = None,
    before: int | None = None,
    since: datetime | None = None,
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
    if since is not None:
        conds.append(ActivityRow.at >= _utc_naive(since))
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
        OutcomeRow,
        OutcomeFailureRow,
        ErrorRow,
        DecisionRow,
        ActivityRow,
        EquitySnapshotRow,
        ExitTickRow,
        LlmCallRow,
        EntryPauseRow,
        BriefingRow,
        AlertRow,
        SettingsHistoryRow,
        ShadowRow,
        WhatifRow,
        AskConversationRow,
        PaperOrderRow,
        SkippedSlotRow,
        StopMoveRow,
        FillRow,
        MetaRow,
    ):
        session.execute(delete(table))
    session.commit()


# -------------------------------------------------------------------- fills --


def add_fill(session: Session, row: FillRow) -> FillRow:
    session.add(row)
    session.flush()  # assign the autoincrement id
    return row


def link_entry_fill(session: Session, fill: FillRow, trade_id: int) -> None:
    """A position's entry fill learns its trade id once the trade closes."""
    fill.trade_id = trade_id


def find_entry_fill(
    session: Session, account_id: int, pair: str, decision_id: str
) -> FillRow | None:
    """The unlinked buy fill a position's close should point at."""
    return session.scalar(
        select(FillRow)
        .where(
            FillRow.account_id == account_id,
            FillRow.pair == pair,
            FillRow.side == "buy",
            FillRow.decision_id == decision_id,
            FillRow.trade_id.is_(None),
        )
        .order_by(FillRow.at.desc(), FillRow.id.desc())
        .limit(1)
    )


def list_fills(
    session: Session,
    account_id: int,
    *,
    pair: str | None = None,
    side: str | None = None,
    reason: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[FillRow]:
    """Fills for one account, newest first; ``before_id`` is the opaque
    cursor — the id of the last row of the previous page."""
    conds = [FillRow.account_id == account_id]
    if pair is not None:
        conds.append(FillRow.pair == pair)
    if side is not None:
        conds.append(FillRow.side == side)
    if reason is not None:
        conds.append(FillRow.reason == reason)
    if start is not None:
        conds.append(FillRow.at >= _utc_naive(start))
    if end is not None:
        conds.append(FillRow.at <= _utc_naive(end))
    if before_id is not None:
        conds.append(FillRow.id < before_id)
    return list(
        session.scalars(
            select(FillRow)
            .where(*conds)
            .order_by(FillRow.at.desc(), FillRow.id.desc())
            .limit(limit)
        ).all()
    )


def count_fills(
    session: Session,
    account_id: int,
    *,
    pair: str | None = None,
    side: str | None = None,
    reason: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """How many fills match the order-history filters (all pages)."""
    conds = [FillRow.account_id == account_id]
    if pair is not None:
        conds.append(FillRow.pair == pair)
    if side is not None:
        conds.append(FillRow.side == side)
    if reason is not None:
        conds.append(FillRow.reason == reason)
    if start is not None:
        conds.append(FillRow.at >= _utc_naive(start))
    if end is not None:
        conds.append(FillRow.at <= _utc_naive(end))
    return int(session.scalar(select(func.count()).select_from(FillRow).where(*conds)) or 0)


def fills_between(
    session: Session, account_id: int, start: datetime, end: datetime | None = None
) -> list[FillRow]:
    """Every fill at or after ``start`` (and before ``end``), oldest first."""
    conds = [FillRow.account_id == account_id, FillRow.at >= _utc_naive(start)]
    if end is not None:
        conds.append(FillRow.at <= _utc_naive(end))
    return list(
        session.scalars(
            select(FillRow).where(*conds).order_by(FillRow.at.asc(), FillRow.id.asc())
        ).all()
    )


def fills_totals(session: Session, account_id: int) -> tuple[Decimal, Decimal]:
    """(fees, slippage) summed over every fill of the account."""
    fees = session.scalar(
        select(func.coalesce(func.sum(FillRow.fee_eur.cast(Numeric)), 0)).where(
            FillRow.account_id == account_id
        )
    )
    slip = session.scalar(
        select(func.coalesce(func.sum(FillRow.slippage_eur.cast(Numeric)), 0)).where(
            FillRow.account_id == account_id,
            FillRow.slippage_eur.is_not(None),
        )
    )
    return Decimal(str(fees or 0)), Decimal(str(slip or 0))


def account_trade_totals(session: Session, account_id: int) -> tuple[Decimal, int]:
    """(realised P&L after fees, closed trade count) for one account."""
    realised = session.scalar(
        select(func.coalesce(func.sum(TradeRow.realised_eur.cast(Numeric)), 0)).where(
            TradeRow.account_id == account_id
        )
    )
    count = session.scalar(
        select(func.count()).select_from(TradeRow).where(TradeRow.account_id == account_id)
    )
    return Decimal(str(realised or 0)), int(count or 0)


def meta_get(session: Session, key: str) -> str | None:
    row = session.get(MetaRow, key)
    return row.value if row is not None else None


def meta_set(session: Session, key: str, value: str) -> None:
    row = session.get(MetaRow, key)
    if row is None:
        session.add(MetaRow(key=key, value=value))
    else:
        row.value = value


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


def latest_equity_snapshot(session: Session, account_id: int) -> EquitySnapshotRow | None:
    return session.scalar(
        select(EquitySnapshotRow)
        .where(EquitySnapshotRow.account_id == account_id)
        .order_by(EquitySnapshotRow.at.desc(), EquitySnapshotRow.id.desc())
        .limit(1)
    )


# ------------------------------------------------------------ skipped slots --


def add_skipped_slot(
    session: Session, slot_at: datetime, reason: str, at: datetime | None = None
) -> SkippedSlotRow:
    """Record a quarter-hour slot whose cycle never ran. The slot boundary
    is the primary key, so a slot already recorded is left untouched."""
    key = _utc_naive(slot_at)
    row = session.get(SkippedSlotRow, key)
    if row is None:
        row = SkippedSlotRow(
            slot_at=key,
            reason=reason,
            recorded_at=_utc_naive(at or utcnow()),
        )
        session.add(row)
    return row


def list_skipped_slots(
    session: Session,
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[SkippedSlotRow]:
    conds = []
    if after is not None:
        conds.append(SkippedSlotRow.slot_at >= _utc_naive(after))
    if before is not None:
        conds.append(SkippedSlotRow.slot_at <= _utc_naive(before))
    return list(
        session.scalars(select(SkippedSlotRow).where(*conds).order_by(SkippedSlotRow.slot_at)).all()
    )


# ---------------------------------------------------------------- stop moves --


def add_stop_move(
    session: Session,
    account_id: int,
    pair: str,
    at: datetime,
    old_stop: Decimal,
    new_stop: Decimal,
    reason: str,
) -> StopMoveRow:
    row = StopMoveRow(
        account_id=account_id,
        pair=pair,
        at=_utc_naive(at),
        old_stop=str(old_stop),
        new_stop=str(new_stop),
        reason=reason,
    )
    session.add(row)
    return row


def list_stop_moves(
    session: Session,
    account_id: int,
    after: datetime | None = None,
    before: datetime | None = None,
) -> list[StopMoveRow]:
    conds = [StopMoveRow.account_id == account_id]
    if after is not None:
        conds.append(StopMoveRow.at >= _utc_naive(after))
    if before is not None:
        conds.append(StopMoveRow.at <= _utc_naive(before))
    return list(
        session.scalars(
            select(StopMoveRow).where(*conds).order_by(StopMoveRow.at.asc(), StopMoveRow.id.asc())
        ).all()
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


# ------------------------------------------------------------- entry pauses --


def list_entry_pauses(session: Session, now: datetime | None = None) -> list[EntryPauseRow]:
    """Active pauses only — expired rows are pruned as a side effect."""
    now = now or utcnow()
    rows = list(session.scalars(select(EntryPauseRow).order_by(EntryPauseRow.pair)).all())
    active: list[EntryPauseRow] = []
    for row in rows:
        if row.until is not None and aware_utc(row.until) <= now:
            session.delete(row)
        else:
            active.append(row)
    if len(active) < len(rows):
        session.commit()  # actually persist the prune; callers only read
    return active


def active_entry_pause(session: Session, pair: str, now: datetime) -> EntryPauseRow | None:
    """The pause row in force for ``pair``: the global ``"*"`` pause wins
    over a per-coin one. ``row.until`` NULL means "until I resume"."""
    rows = {
        row.pair: row
        for row in session.scalars(
            select(EntryPauseRow).where(EntryPauseRow.pair.in_(["*", pair]))
        ).all()
    }
    for key in ("*", pair):
        row = rows.get(key)
        if row is not None and (row.until is None or aware_utc(row.until) > now):
            return row
    return None


def set_entry_pause(
    session: Session, pair: str, until: datetime | None, at: datetime | None = None
) -> EntryPauseRow:
    row = session.get(EntryPauseRow, pair)
    if row is None:
        row = EntryPauseRow(pair=pair, until=None, created_at=at or utcnow())
    row.until = _utc_naive(until) if until is not None else None
    session.merge(row)
    return row


def clear_entry_pause(session: Session, pair: str) -> None:
    row = session.get(EntryPauseRow, pair)
    if row is not None:
        session.delete(row)


# --------------------------------------------------------------- briefings --


def add_briefing(session: Session, row: BriefingRow) -> BriefingRow:
    session.add(row)
    return row


def list_briefings(session: Session, type: str | None = None, limit: int = 30) -> list[BriefingRow]:
    conds = []
    if type is not None:
        conds.append(BriefingRow.type == type)
    return list(
        session.scalars(
            select(BriefingRow).where(*conds).order_by(BriefingRow.at.desc()).limit(limit)
        ).all()
    )


def get_briefing(session: Session, briefing_id: int) -> BriefingRow | None:
    return session.get(BriefingRow, briefing_id)


def briefing_for_date(session: Session, type: str, for_date: str) -> BriefingRow | None:
    return session.scalar(
        select(BriefingRow)
        .where(BriefingRow.type == type, BriefingRow.for_date == for_date)
        .limit(1)
    )


# --------------------------------------------------------- settings history --


def _flatten_doc(doc: dict[str, object], prefix: str = "") -> dict[str, object]:
    """Nested dicts become ``"a.b"`` leaf paths; lists are leaf values."""
    out: dict[str, object] = {}
    for key, value in doc.items():
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(_flatten_doc(value, path))
        else:
            out[path] = value
    return out


def record_settings_history(
    session: Session,
    old: Settings,
    new: Settings,
    at: datetime | None = None,
    version: int | None = None,
) -> int:
    """Log every leaf that changed between two settings documents."""
    at = at or utcnow()
    old_flat = _flatten_doc(old.model_dump(mode="json"))
    new_flat = _flatten_doc(new.model_dump(mode="json"))
    count = 0
    for key in sorted(set(old_flat) | set(new_flat)):
        old_v, new_v = old_flat.get(key), new_flat.get(key)
        if old_v == new_v:
            continue
        session.add(
            SettingsHistoryRow(
                at=at,
                key=key,
                old=json.dumps(old_v) if old_v is not None else None,
                new=json.dumps(new_v) if new_v is not None else None,
                version=version,
            )
        )
        count += 1
    return count


def list_settings_history(
    session: Session, key_prefix: str | None = None, limit: int = 200
) -> list[SettingsHistoryRow]:
    conds = []
    if key_prefix is not None:
        conds.append(SettingsHistoryRow.key.like(f"{key_prefix}%"))
    return list(
        session.scalars(
            select(SettingsHistoryRow)
            .where(*conds)
            .order_by(SettingsHistoryRow.id.desc())
            .limit(limit)
        ).all()
    )


def latest_settings_change(session: Session, key: str) -> SettingsHistoryRow | None:
    return session.scalar(
        select(SettingsHistoryRow)
        .where(SettingsHistoryRow.key == key)
        .order_by(SettingsHistoryRow.id.desc())
        .limit(1)
    )


# ------------------------------------------------------------------- alerts --


def add_alert(
    session: Session,
    event: str,
    channel: str,
    title: str,
    repeats: int = 0,
    at: datetime | None = None,
) -> AlertRow:
    row = AlertRow(
        at=at or utcnow(),
        event=event,
        channel=channel,
        title=title,
        repeats=repeats,
    )
    session.add(row)
    return row


def last_alert_at(session: Session, event: str, channel: str, title: str) -> datetime | None:
    row = session.scalar(
        select(AlertRow)
        .where(
            AlertRow.event == event,
            AlertRow.channel == channel,
            AlertRow.title == title,
        )
        .order_by(AlertRow.at.desc())
        .limit(1)
    )
    return aware_utc(row.at) if row is not None else None


# ----------------------------------------------------- quiet line helpers --


def latest_decision_cycle(session: Session, account_id: int) -> int | None:
    return session.scalar(
        select(func.max(DecisionRow.cycle)).where(DecisionRow.account_id == account_id)
    )


def latest_persisted_cycle(session: Session, account_id: int) -> int:
    """The highest cycle number ever recorded on this account, over both
    tables that key on it — the engine resumes numbering from here so
    cycles never repeat across restarts."""
    decision_cycle = session.scalar(
        select(func.max(DecisionRow.cycle)).where(DecisionRow.account_id == account_id)
    )
    snapshot_cycle = session.scalar(
        select(func.max(EquitySnapshotRow.cycle)).where(EquitySnapshotRow.account_id == account_id)
    )
    return max(decision_cycle or 0, snapshot_cycle or 0)


def decisions_in_cycle(session: Session, account_id: int, cycle: int) -> list[DecisionRow]:
    return list(
        session.scalars(
            select(DecisionRow).where(
                DecisionRow.account_id == account_id, DecisionRow.cycle == cycle
            )
        ).all()
    )


def last_trade_time(session: Session, account_id: int) -> datetime | None:
    """The most recent open or close time on this account's trades."""
    opened = session.scalar(
        select(func.max(TradeRow.opened_at)).where(TradeRow.account_id == account_id)
    )
    closed = session.scalar(
        select(func.max(TradeRow.closed_at)).where(TradeRow.account_id == account_id)
    )
    times = [t for t in (opened, closed) if t is not None]
    return aware_utc(max(times)) if times else None


# ------------------------------------------------------------- readiness --


def unresolved_errors_affecting(session: Session, since: datetime) -> list[ErrorRow]:
    """Unresolved errors that touched a decision or a fill, last seen since."""
    return list(
        session.scalars(
            select(ErrorRow).where(
                ErrorRow.resolved_at.is_(None),
                ErrorRow.affected_decision_or_fill.is_(True),
                ErrorRow.last_at >= _utc_naive(since),
            )
        ).all()
    )


def first_equity_snapshot(session: Session, account_id: int) -> EquitySnapshotRow | None:
    return session.scalar(
        select(EquitySnapshotRow)
        .where(EquitySnapshotRow.account_id == account_id)
        .order_by(EquitySnapshotRow.at.asc())
        .limit(1)
    )


# ------------------------------------------------------------- evidence ----


def add_shadow(session: Session, row: ShadowRow) -> ShadowRow:
    session.add(row)
    return row


def get_shadow(session: Session, shadow_id: str) -> ShadowRow | None:
    return session.get(ShadowRow, shadow_id)


def list_shadows(session: Session, active_only: bool = False) -> list[ShadowRow]:
    stmt = select(ShadowRow).order_by(ShadowRow.created_at.asc())
    if active_only:
        stmt = stmt.where(ShadowRow.status == "running")
    return list(session.scalars(stmt).all())


def count_active_shadows(session: Session) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(ShadowRow).where(ShadowRow.status == "running")
        )
        or 0
    )


def shadow_calls_today(session: Session, account_id: int, shadow_id: str, now: datetime) -> int:
    """A shadow's own daily model calls (its per-shadow limit)."""
    from app.agent import budget

    return llm_calls_since(
        session, account_id, budget.amsterdam_day_start(now), (f"shadow:{shadow_id}",)
    )


def add_whatif(session: Session, row: WhatifRow) -> WhatifRow:
    session.add(row)
    return row


def get_whatif(session: Session, scenario_id: str) -> WhatifRow | None:
    return session.get(WhatifRow, scenario_id)


def list_whatifs(session: Session, saved_only: bool = True, limit: int = 50) -> list[WhatifRow]:
    stmt = select(WhatifRow).order_by(WhatifRow.created_at.desc()).limit(limit)
    if saved_only:
        stmt = stmt.where(WhatifRow.saved.is_(True))
    return list(session.scalars(stmt).all())


def get_conversation(session: Session, conversation_id: str) -> AskConversationRow | None:
    return session.get(AskConversationRow, conversation_id)


def list_conversations(session: Session, limit: int = 50) -> list[AskConversationRow]:
    return list(
        session.scalars(
            select(AskConversationRow).order_by(AskConversationRow.updated_at.desc()).limit(limit)
        ).all()
    )


def ask_questions_today(session: Session, now: datetime) -> int:
    """Questions asked today (Amsterdam day) — the Ask daily limit counts
    user turns, one JSON message each."""
    from app.agent import budget

    start = budget.amsterdam_day_start(now)
    rows = session.scalars(
        select(AskConversationRow.messages).where(
            AskConversationRow.updated_at >= _utc_naive(start)
        )
    ).all()
    total = 0
    for raw in rows:
        try:
            messages = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "user" or not m.get("at"):
                continue
            try:
                asked_at = aware_utc(datetime.fromisoformat(str(m["at"])))
            except (ValueError, TypeError):
                continue
            if asked_at >= start:
                total += 1
    return total


def exit_ticks_for(
    session: Session,
    pair: str,
    start: datetime,
    end: datetime,
    account_id: int | None = None,
) -> list[ExitTickRow]:
    """Stored 30-second watcher ticks within [start, end] — the What-if
    exit replay's sub-candle price checks. ``account_id`` scopes to one
    book; without it ticks from every account (live and shadows) mix."""
    stmt = select(ExitTickRow).where(
        ExitTickRow.pair == pair,
        ExitTickRow.at >= _utc_naive(start),
        ExitTickRow.at <= _utc_naive(end),
    )
    if account_id is not None:
        stmt = stmt.where(ExitTickRow.account_id == account_id)
    return list(session.scalars(stmt.order_by(ExitTickRow.at.asc(), ExitTickRow.id.asc())).all())


def latest_briefing(session: Session, type: str) -> BriefingRow | None:
    return session.scalar(
        select(BriefingRow).where(BriefingRow.type == type).order_by(BriefingRow.at.desc()).limit(1)
    )
