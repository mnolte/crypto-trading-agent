"""The one-time rebuild of paper fills from trades and positions.

Fills existed only as positions' entries and trades' exits before the
``fills`` table; the first start after its migration writes one entry fill
per trade and per open position and one exit fill per trade, marked
``source="rebuilt"`` with no reference price or slippage. A trade's stored
``fee_eur`` (both sides) is split over entry and exit in proportion to
notional x fee rate — maker where a filled ``paper_orders`` row exists for
that leg, taker otherwise — so the two rebuilt fees add up to ``fee_eur``.
Trades and positions are never changed. The ``meta`` marker makes it run
once.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.db import FillRow, PaperOrderRow, PositionRow, TradeRow
from app.models import PaperSettings

log = logging.getLogger(__name__)

HUNDRED = Decimal("100")
REBUILT_KEY = "fills_rebuilt_v1"


def _filled_order(
    session: Session,
    account_id: int,
    pair: str,
    leg: str,
    decision_id: str | None = None,
    near: datetime | None = None,
) -> PaperOrderRow | None:
    """The filled paper order for one leg: the entry order of the trade's
    decision, or the target order filled closest to the trade's close."""
    conds = [
        PaperOrderRow.account_id == account_id,
        PaperOrderRow.pair == pair,
        PaperOrderRow.leg == leg,
        PaperOrderRow.status == "filled",
    ]
    if decision_id is not None:
        conds.append(PaperOrderRow.decision_id == decision_id)
    orders = list(session.scalars(select(PaperOrderRow).where(*conds)).all())
    if not orders:
        return None
    if near is None:
        return orders[-1]
    return min(
        orders,
        key=lambda o: (
            abs((repo.aware_utc(o.filled_at) - near).total_seconds())
            if o.filled_at is not None
            else float("inf")
        ),
    )


def _entry_fill(
    session: Session,
    *,
    account_id: int,
    at: datetime,
    pair: str,
    qty: Decimal,
    price: Decimal,
    order_type: str,
    fee_eur: Decimal,
    fee_kind: str,
    reason: str,
    decision_id: str,
    trade_id: int | None,
    paper_order_id: int | None,
) -> FillRow:
    return repo.add_fill(
        session,
        FillRow(
            account_id=account_id,
            at=at,
            pair=pair,
            side="buy",
            qty=str(qty),
            reference_price=None,
            fill_price=str(price),
            order_type=order_type,
            fee_eur=str(fee_eur),
            fee_kind=fee_kind,
            slippage_eur=None,
            reason=reason,
            decision_id=decision_id,
            trade_id=trade_id,
            paper_order_id=paper_order_id,
            source="rebuilt",
        ),
    )


def _exit_fill(
    session: Session,
    *,
    account_id: int,
    at: datetime,
    pair: str,
    qty: Decimal,
    price: Decimal,
    order_type: str,
    fee_eur: Decimal,
    fee_kind: str,
    reason: str,
    decision_id: str,
    trade_id: int,
    paper_order_id: int | None,
) -> FillRow:
    return repo.add_fill(
        session,
        FillRow(
            account_id=account_id,
            at=at,
            pair=pair,
            side="sell",
            qty=str(qty),
            reference_price=None,
            fill_price=str(price),
            order_type=order_type,
            fee_eur=str(fee_eur),
            fee_kind=fee_kind,
            slippage_eur=None,
            reason=reason,
            decision_id=decision_id,
            trade_id=trade_id,
            paper_order_id=paper_order_id,
            source="rebuilt",
        ),
    )


def rebuild_fills(session: Session, paper: PaperSettings) -> int:
    """Write the rebuilt fills once; returns how many rows were added."""
    if repo.meta_get(session, REBUILT_KEY) is not None:
        return 0
    count = 0
    taker = paper.taker_fee_pct
    maker = paper.maker_fee_pct

    trades = list(session.scalars(select(TradeRow).order_by(TradeRow.id)).all())
    for trade in trades:
        qty = Decimal(trade.qty)
        entry_price = Decimal(trade.entry)
        exit_price = Decimal(trade.exit)
        closed_at = repo.aware_utc(trade.closed_at)
        opened_at = repo.aware_utc(trade.opened_at)
        entry_order = _filled_order(
            session, trade.account_id, trade.pair, "entry", decision_id=trade.decision_id
        )
        exit_order = _filled_order(session, trade.account_id, trade.pair, "target", near=closed_at)
        entry_maker = entry_order is not None
        exit_maker = exit_order is not None
        entry_rate = maker if entry_maker else taker
        exit_rate = maker if exit_maker else taker
        total_fee = Decimal(trade.fee_eur)
        w_entry = qty * entry_price * entry_rate
        w_exit = qty * exit_price * exit_rate
        if w_entry + w_exit > 0:
            entry_fee = total_fee * w_entry / (w_entry + w_exit)
        else:
            entry_fee = total_fee / 2
        exit_fee = total_fee - entry_fee
        _entry_fill(
            session,
            account_id=trade.account_id,
            at=opened_at,
            pair=trade.pair,
            qty=qty,
            price=entry_price,
            order_type="limit" if entry_maker else "market",
            fee_eur=entry_fee,
            fee_kind="maker" if entry_maker else "taker",
            reason="decision",
            decision_id=trade.decision_id,
            trade_id=trade.id,
            paper_order_id=entry_order.id if entry_order is not None else None,
        )
        _exit_fill(
            session,
            account_id=trade.account_id,
            at=closed_at,
            pair=trade.pair,
            qty=qty,
            price=exit_price,
            order_type="limit" if exit_maker else "market",
            fee_eur=exit_fee,
            fee_kind="maker" if exit_maker else "taker",
            reason=trade.exit_reason,
            decision_id=trade.decision_id,
            trade_id=trade.id,
            paper_order_id=exit_order.id if exit_order is not None else None,
        )
        count += 2

    positions = list(session.scalars(select(PositionRow)).all())
    for pos in positions:
        qty = Decimal(pos.qty)
        entry_price = Decimal(pos.entry)
        opened_at = repo.aware_utc(pos.opened_at)
        entry_order = _filled_order(
            session, pos.account_id, pos.pair, "entry", decision_id=pos.decision_id
        )
        maker_fill = entry_order is not None or pos.entry_maker
        rate = maker if maker_fill else taker
        fee = qty * entry_price * rate / HUNDRED
        _entry_fill(
            session,
            account_id=pos.account_id,
            at=opened_at,
            pair=pos.pair,
            qty=qty,
            price=entry_price,
            order_type="limit" if maker_fill else "market",
            fee_eur=fee,
            fee_kind="maker" if maker_fill else "taker",
            reason="decision",
            decision_id=pos.decision_id,
            trade_id=None,
            paper_order_id=entry_order.id if entry_order is not None else None,
        )
        count += 1

    repo.meta_set(session, REBUILT_KEY, repo.utcnow().isoformat())
    session.commit()
    log.info("rebuilt %d paper fill(s) from trades and positions", count)
    return count
