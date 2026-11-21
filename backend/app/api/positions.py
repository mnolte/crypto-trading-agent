"""Positions & orders (016): one read-only screen's endpoints.

``GET /api/positions`` shows one account's open positions with every fact
the card needs (shared ``position_facts``), its working paper orders and
totals; for ``account=live`` it adds the Today and Paper account blocks.
``GET /api/fills`` pages the live account's fills newest first and
``GET /api/fills/export.csv`` downloads every matching row. Nothing here
changes a position.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent import budget
from app.agent.broker.paper import PaperBroker
from app.agent.engine import Engine
from app.api.deps import AppState, get_state
from app.api.screens import _paper_order_out
from app.db import AMSTERDAM, AccountRow, DecisionRow, FillRow, PaperOrderRow, TradeRow, utcnow
from app.features import positions as positions_mod
from app.features import shadows as shadows_mod
from app.models import (
    FillOut,
    FillsPageOut,
    LatestDecisionOut,
    PaperAccountOut,
    PaperSettings,
    PositionCardOut,
    PositionsOut,
    PositionsTotalsOut,
    ShadowRefOut,
    StopHistoryItem,
    TodayItemOut,
    TodayOut,
)

router = APIRouter()

HUNDRED = Decimal("100")
FILL_PAGE = 50
FillReason = Literal[
    "decision", "stop", "target", "time_stop", "model_sell", "flatten", "manual", "kill"
]


def _engine(state: AppState) -> Engine | None:
    return state.agent_engine


def _broker_for_account(
    state: AppState, session: Session, account: str
) -> tuple[str, AccountRow, PaperBroker, datetime | None]:
    """Resolve ``account`` ("live" or a shadow id) to its row, broker and
    the watcher's last pass time. A stopped shadow gets a read-only broker
    rebuilt from its account row."""
    engine = _engine(state)
    if account == "live":
        broker = state.broker
        if broker is None:
            raise HTTPException(status_code=503, detail="engine not started")
        return (
            "live",
            broker.account,
            broker,
            engine.watcher_checked_at if engine is not None else None,
        )
    row = repo.get_shadow(session, account)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown shadow")
    account_row = session.get(AccountRow, row.account_id)
    if account_row is None:
        raise HTTPException(status_code=404, detail="unknown shadow")
    broker = None
    watcher_at = None
    if engine is not None and engine.shadows is not None:
        broker = engine.shadows.broker_for(row.id)
        watcher_at = engine.shadows.watcher_checked_at.get(account_row.id)
    if broker is None:
        settings = shadows_mod.apply_shadow_overrides(
            state.settings, json.loads(row.overrides or "{}")
        )
        broker = PaperBroker(account_row, settings.paper)
        broker.load_positions(session)
    return account, account_row, broker, watcher_at


def _shadow_refs(session: Session) -> list[ShadowRefOut]:
    """Every shadow that exists, running or stopped, by name — for the
    Live / Shadow switch."""
    out: list[ShadowRefOut] = []
    for row in repo.list_shadows(session):
        account = session.get(AccountRow, row.account_id)
        if account is None:
            continue
        out.append(
            ShadowRefOut(
                id=row.id,
                name=row.name,
                model=row.model,
                prompt_version=row.prompt_version,
                status=row.status,
                started_at=repo.aware_utc(account.started_at),
            )
        )
    return out


def _entry_order_limit(session: Session, decision_ids: list[str]) -> dict[str, Decimal]:
    """decision_id -> the limit price of its entry paper order, if any."""
    if not decision_ids:
        return {}
    rows = session.scalars(
        select(PaperOrderRow)
        .where(
            PaperOrderRow.leg == "entry",
            PaperOrderRow.decision_id.in_(decision_ids),
        )
        .order_by(PaperOrderRow.id.desc())
    ).all()
    out: dict[str, Decimal] = {}
    for row in rows:
        out.setdefault(row.decision_id, Decimal(row.limit_price))
    return out


def _round_trip_fees(
    session: Session,
    account_id: int,
    held_pair: str,
    decision_id: str,
    entry_maker: bool,
    qty: Decimal,
    entry: Decimal,
    stop: Decimal,
    paper: PaperSettings,
) -> Decimal:
    """Entry fee paid plus the estimated exit fee at the stop — the "covers
    both fees" figure on the stop-hit line. The exit side is maker when a
    target order rests for the pair, taker otherwise."""
    entry_fill = session.scalar(
        select(FillRow)
        .where(
            FillRow.account_id == account_id,
            FillRow.pair == held_pair,
            FillRow.side == "buy",
            FillRow.decision_id == decision_id,
        )
        .order_by(FillRow.at.desc(), FillRow.id.desc())
        .limit(1)
    )
    if entry_fill is not None:
        entry_fee = Decimal(entry_fill.fee_eur)
    else:
        rate = paper.maker_fee_pct if entry_maker else paper.taker_fee_pct
        entry_fee = qty * entry * rate / HUNDRED
    target_open = session.scalar(
        select(PaperOrderRow.id)
        .where(
            PaperOrderRow.account_id == account_id,
            PaperOrderRow.pair == held_pair,
            PaperOrderRow.leg == "target",
            PaperOrderRow.status == "open",
        )
        .limit(1)
    )
    exit_rate = paper.maker_fee_pct if target_open is not None else paper.taker_fee_pct
    return entry_fee + qty * stop * exit_rate / HUNDRED


def _position_cards(
    state: AppState,
    session: Session,
    broker: PaperBroker,
    account_row: AccountRow,
    watcher_at: datetime | None,
    now: datetime,
) -> tuple[list[PositionCardOut], PositionsTotalsOut]:
    engine = _engine(state)
    settings = state.settings
    equity = broker.equity()
    cards: list[PositionCardOut] = []
    facts_by_pair: dict[str, positions_mod.PositionFacts] = {}
    latest = repo.latest_decisions(
        session, account_row.id, [p.pair for p in broker.positions.values()]
    )
    limit_prices = _entry_order_limit(session, [row.id for row in latest.values()])
    for held in sorted(broker.positions.values(), key=lambda p: p.notional, reverse=True):
        model = held.to_model()
        atr = engine._atr_1h(session, held.pair, now) if engine is not None else None
        facts = positions_mod.position_facts(
            model,
            held,
            atr,
            equity=equity,
            stop_near_atr=settings.ui.stop_near_atr,
            time_stop_days=settings.risk.time_stop_days,
        )
        facts_by_pair[held.pair] = facts
        moves = [
            m
            for m in repo.list_stop_moves(
                session, account_row.id, after=repo.aware_utc(held.opened_at)
            )
            if m.pair == held.pair
        ]
        history = [
            StopHistoryItem(
                kind="initial",
                stop=held.original_stop,
                at=repo.aware_utc(held.opened_at),
            )
        ]
        history += [
            StopHistoryItem(
                kind="breakeven" if m.reason == "breakeven" else "trailing",
                stop=Decimal(m.new_stop),
                at=repo.aware_utc(m.at),
            )
            for m in moves
        ]
        decision_row = latest.get(held.pair)
        latest_decision = None
        if decision_row is not None:
            latest_decision = LatestDecisionOut(
                id=decision_row.id,
                at=repo.aware_utc(decision_row.at),
                action=decision_row.action,
                confidence=decision_row.confidence,
                limit_price=limit_prices.get(decision_row.id),
            )
        cards.append(
            PositionCardOut(
                pair=held.pair,
                qty=held.qty,
                entry=held.entry,
                stop=held.stop,
                target=held.target,
                opened_at=repo.aware_utc(held.opened_at),
                decision_id=held.decision_id,
                last=held.last,
                unrealised=held.unrealised,
                stop_dist_pct=facts.stop_dist_pct,
                stop_dist_atr=facts.stop_dist_atr,
                target_dist_pct=facts.target_dist_pct,
                target_dist_atr=facts.target_dist_atr,
                pnl_r=facts.pnl_r,
                stop_above_entry=facts.stop_above_entry,
                stop_near=facts.stop_near,
                stop_hit_eur=facts.stop_hit_eur,
                stop_hit_r=facts.stop_hit_r,
                stop_hit_pct_equity=facts.stop_hit_pct_equity,
                rr_at_entry=facts.rr_at_entry,
                time_stop_at=facts.time_stop_at,
                round_trip_fees_eur=_round_trip_fees(
                    session,
                    account_row.id,
                    held.pair,
                    held.decision_id,
                    held.entry_maker,
                    held.qty,
                    held.entry,
                    held.stop,
                    settings.paper,
                ),
                stop_history=history,
                latest_decision=latest_decision,
                watcher_checked_at=watcher_at,
            )
        )
    day_start = budget.amsterdam_day_start(now)
    today_fills = repo.fills_between(session, account_row.id, day_start)
    fees_today = sum((Decimal(f.fee_eur) for f in today_fills), Decimal("0"))
    fees_total, _slippage_total = repo.fills_totals(session, account_row.id)
    closest_pair = None
    closest_atr: Decimal | None = None
    for held in broker.positions.values():
        atr_value = facts_by_pair[held.pair].stop_dist_atr
        if atr_value is not None and (closest_atr is None or atr_value < closest_atr):
            closest_atr = atr_value
            closest_pair = held.pair
    exposure = broker.exposure()
    totals = PositionsTotalsOut(
        count=len(cards),
        unrealised=sum((c.unrealised for c in cards), Decimal("0")),
        stop_hit_total=sum((c.stop_hit_eur for c in cards), Decimal("0")),
        open_risk_pct=broker.open_risk() / equity * HUNDRED if equity > 0 else Decimal("0"),
        open_risk_cap_pct=settings.risk.total_open_risk_pct,
        exposure=exposure,
        exposure_pct=float(exposure / equity * HUNDRED) if equity > 0 else 0.0,
        cash=broker.cash,
        fees_today=fees_today,
        fees_total=fees_total,
        closest_pair=closest_pair,
        closest_atr=closest_atr,
    )
    return cards, totals


def _today_block(session: Session, account_id: int, now: datetime) -> TodayOut:
    """The live account's fills since 00:00 Amsterdam, enriched with the
    trade an exit closed and the decision an entry came from."""
    fills = repo.fills_between(session, account_id, budget.amsterdam_day_start(now))
    trade_ids = {f.trade_id for f in fills if f.trade_id is not None}
    trades = (
        {
            t.id: t
            for t in session.scalars(
                select(TradeRow).where(
                    TradeRow.account_id == account_id, TradeRow.id.in_(trade_ids)
                )
            ).all()
        }
        if trade_ids
        else {}
    )
    decision_ids = {f.decision_id for f in fills}
    decisions = (
        {
            d.id: d
            for d in session.scalars(
                select(DecisionRow).where(DecisionRow.id.in_(decision_ids))
            ).all()
        }
        if decision_ids
        else {}
    )
    items: list[TodayItemOut] = []
    realised = Decimal("0")
    fees = Decimal("0")
    entries = 0
    exits = 0
    seen_trades: set[int] = set()
    for fill in fills:
        fees += Decimal(fill.fee_eur)
        at = repo.aware_utc(fill.at)
        decision = decisions.get(fill.decision_id)
        rr = None
        if decision is not None and decision.plan:
            plan = json.loads(decision.plan)
            if plan.get("rr") is not None:
                rr = Decimal(str(plan["rr"]))
        if fill.side == "buy":
            entries += 1
            items.append(
                TodayItemOut(
                    at=at,
                    side="buy",
                    pair=fill.pair,
                    qty=Decimal(fill.qty),
                    price=Decimal(fill.fill_price),
                    reason=fill.reason,
                    decision_id=fill.decision_id,
                    trade_id=fill.trade_id,
                    still_open=fill.trade_id is None,
                    action=decision.action if decision is not None else None,
                    confidence=decision.confidence if decision is not None else None,
                    rr=rr,
                )
            )
            continue
        exits += 1
        trade = trades.get(fill.trade_id) if fill.trade_id is not None else None
        held_seconds = None
        if trade is not None:
            held_seconds = int(
                (repo.aware_utc(trade.closed_at) - repo.aware_utc(trade.opened_at)).total_seconds()
            )
            if trade.id not in seen_trades:
                seen_trades.add(trade.id)
                realised += Decimal(trade.realised_eur)
        items.append(
            TodayItemOut(
                at=at,
                side="sell",
                pair=fill.pair,
                qty=Decimal(fill.qty),
                price=Decimal(fill.fill_price),
                reason=fill.reason,
                decision_id=fill.decision_id,
                trade_id=fill.trade_id,
                held_seconds=held_seconds,
                realised_eur=Decimal(trade.realised_eur) if trade is not None else None,
                realised_r=Decimal(trade.realised_r) if trade is not None else None,
                fees_eur=Decimal(trade.fee_eur) if trade is not None else None,
            )
        )
    return TodayOut(items=items, entries=entries, exits=exits, realised=realised, fees=fees)


def _paper_account_out(
    session: Session, account_row: AccountRow, broker: PaperBroker
) -> PaperAccountOut:
    equity = broker.equity()
    realised, closed = repo.account_trade_totals(session, account_row.id)
    fees_total, slippage_total = repo.fills_totals(session, account_row.id)
    return PaperAccountOut(
        id=account_row.id,
        started_at=repo.aware_utc(account_row.started_at),
        start_balance=Decimal(account_row.start_balance),
        cash=broker.cash,
        coins_value=equity - broker.cash,
        equity=equity,
        realised_pnl=realised,
        closed_trades=closed,
        fees_total=fees_total,
        slippage_total=slippage_total,
    )


@router.get("/api/positions", response_model=PositionsOut)
def get_positions(
    request: Request,
    account: str = "live",
) -> PositionsOut:
    """One account's positions, working orders and totals; the Today and
    Paper account blocks only for the live account."""
    state = get_state(request)
    session = state.session_factory()
    try:
        label, account_row, broker, watcher_at = _broker_for_account(state, session, account)
        now = _engine(state).clock() if _engine(state) is not None else utcnow()
        cards, totals = _position_cards(state, session, broker, account_row, watcher_at, now)
        orders = [
            out
            for row in repo.open_paper_orders(session, account_row.id)
            if (out := _paper_order_out(row, state)) is not None
        ]
        today = _today_block(session, account_row.id, now) if label == "live" else None
        paper_account = (
            _paper_account_out(session, account_row, broker) if label == "live" else None
        )
        return PositionsOut(
            account=label,
            shadows=_shadow_refs(session),
            totals=totals,
            positions=cards,
            orders=orders,
            today=today,
            paper_account=paper_account,
        )
    finally:
        session.close()


# ------------------------------------------------------------------- fills --


def _fill_out(row: FillRow) -> FillOut:
    return FillOut(
        id=row.id,
        at=repo.aware_utc(row.at),
        pair=row.pair,
        side=cast("Literal['buy', 'sell']", row.side),
        qty=Decimal(row.qty),
        reference_price=Decimal(row.reference_price) if row.reference_price else None,
        fill_price=Decimal(row.fill_price),
        order_type=cast("Literal['market', 'limit']", row.order_type),
        fee_eur=Decimal(row.fee_eur),
        fee_kind=cast("Literal['maker', 'taker']", row.fee_kind),
        slippage_eur=Decimal(row.slippage_eur) if row.slippage_eur else None,
        reason=row.reason,
        decision_id=row.decision_id,
        trade_id=row.trade_id,
        paper_order_id=row.paper_order_id,
        source=cast("Literal['live', 'rebuilt']", row.source),
    )


def _decode_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=422, detail="invalid cursor") from None


def _encode_cursor(fill_id: int) -> str:
    return base64.urlsafe_b64encode(str(fill_id).encode()).decode()


def _live_account_id(state: AppState) -> int:
    broker = state.broker
    if broker is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return broker.account_id


@router.get("/api/fills", response_model=FillsPageOut)
def get_fills(
    request: Request,
    pair: str | None = None,
    side: Literal["buy", "sell"] | None = None,
    reason: FillReason | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    cursor: str | None = None,
) -> FillsPageOut:
    """The live account's fills, newest first, 50 per page."""
    state = get_state(request)
    session = state.session_factory()
    try:
        rows = repo.list_fills(
            session,
            _live_account_id(state),
            pair=pair,
            side=side,
            reason=reason,
            start=from_,
            end=to,
            before_id=_decode_cursor(cursor),
            limit=FILL_PAGE + 1,
        )
        page = rows[:FILL_PAGE]
        return FillsPageOut(
            items=[_fill_out(r) for r in page],
            next_cursor=_encode_cursor(page[-1].id) if len(rows) > FILL_PAGE else None,
        )
    finally:
        session.close()


@router.get("/api/fills/export.csv")
def export_fills(
    request: Request,
    pair: str | None = None,
    side: Literal["buy", "sell"] | None = None,
    reason: FillReason | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> Response:
    """Every fill matching the current filters as CSV, for download."""
    state = get_state(request)
    session = state.session_factory()
    try:
        rows = repo.list_fills(
            session,
            _live_account_id(state),
            pair=pair,
            side=side,
            reason=reason,
            start=from_,
            end=to,
            limit=1_000_000,
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "at_utc",
                "at_amsterdam",
                "pair",
                "side",
                "qty",
                "reference_price",
                "fill_price",
                "order_type",
                "slippage_eur",
                "fee_eur",
                "fee_kind",
                "reason",
                "decision_id",
                "trade_id",
                "source",
            ]
        )
        for row in rows:
            at = repo.aware_utc(row.at)
            writer.writerow(
                [
                    at.isoformat(),
                    at.astimezone(AMSTERDAM).isoformat(),
                    row.pair,
                    row.side,
                    row.qty,
                    row.reference_price if row.reference_price is not None else "",
                    row.fill_price,
                    row.order_type,
                    row.slippage_eur if row.slippage_eur is not None else "",
                    row.fee_eur,
                    row.fee_kind,
                    row.reason,
                    row.decision_id,
                    row.trade_id if row.trade_id is not None else "",
                    row.source,
                ]
            )
        day = utcnow().astimezone(AMSTERDAM).date().isoformat()
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="fills-{day}.csv"'},
        )
    finally:
        session.close()
