"""The paper account: cash, positions, realised and unrealised P&L, equity,
peak equity and day-start equity.

Every fill is a market fill at the last price plus slippage against the
taker, with the configured taker fee per side. Money is ``Decimal``
everywhere. The broker mirrors the ``positions``/``trades``/``accounts``
tables; the engine passes it a session and controls commit batching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import AccountRow, TradeRow
from app.models import PaperSettings, Position
from app.repo import (
    add_trade,
    delete_position_row,
    list_position_rows,
    save_position_row,
)

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

HUNDRED = Decimal("100")


@dataclass
class HeldPosition:
    """An open paper position (in-memory mirror of the positions table)."""

    pair: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    opened_at: datetime
    decision_id: str
    last: Decimal
    invalidation_price: Decimal | None = None
    highest_close: Decimal | None = None  # highest 1h close since entry
    r_unit: Decimal = Decimal("0")  # entry - stop at fill (the R for exits)

    @property
    def notional(self) -> Decimal:
        return self.qty * self.last

    @property
    def open_risk(self) -> Decimal:
        """Risk still on the table; a stop at or above entry is zero risk."""
        return max(Decimal("0"), self.entry - self.stop) * self.qty

    @property
    def unrealised(self) -> Decimal:
        return self.qty * (self.last - self.entry)

    def to_model(self) -> Position:
        return Position(
            pair=self.pair,
            qty=self.qty,
            entry=self.entry,
            stop=self.stop,
            target=self.target,
            opened_at=self.opened_at,
            decision_id=self.decision_id,
            last=self.last,
            unrealised=self.unrealised,
        )


class PaperBroker:
    """The paper account bound to one ``accounts`` row."""

    def __init__(
        self,
        account: AccountRow,
        paper: PaperSettings,
    ) -> None:
        self.account_id = account.id
        self.account = account
        self.fee_pct = paper.fee_pct
        self.slippage_pct = paper.slippage_pct
        self.cash = Decimal(account.cash)
        self.peak_equity = Decimal(account.peak_equity)
        self.day_start_equity = Decimal(account.day_start_equity)
        self._day_start_date = (
            date.fromisoformat(account.day_start_date)
            if account.day_start_date
            else self._ams_date(datetime.now(UTC))
        )
        self.positions: dict[str, HeldPosition] = {}

    # ------------------------------------------------------------------ load

    def load_positions(self, session: Session) -> None:
        """Restore open positions from the database (restart path)."""
        self.positions.clear()
        for row in list_position_rows(session, self.account_id):
            self.positions[row.pair] = HeldPosition(
                pair=row.pair,
                qty=Decimal(row.qty),
                entry=Decimal(row.entry),
                stop=Decimal(row.stop),
                target=Decimal(row.target),
                opened_at=row.opened_at,
                decision_id=row.decision_id,
                last=Decimal(row.last),
                invalidation_price=(
                    Decimal(row.invalidation_price) if row.invalidation_price is not None else None
                ),
                highest_close=(
                    Decimal(row.highest_close) if row.highest_close is not None else None
                ),
                r_unit=Decimal(row.r_unit),
            )

    # ---------------------------------------------------------------- derived

    @staticmethod
    def _ams_date(now: datetime) -> date:
        return now.astimezone(AMSTERDAM).date()

    def equity(self) -> Decimal:
        """Cash plus every position valued at its last price."""
        return self.cash + sum(p.notional for p in self.positions.values())

    def exposure(self) -> Decimal:
        return sum((p.notional for p in self.positions.values()), Decimal("0"))

    def open_risk(self) -> Decimal:
        """Total max(0, entry - stop) x qty over all open positions."""
        return sum((p.open_risk for p in self.positions.values()), Decimal("0"))

    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal("0")
        return (self.peak_equity - self.equity()) / self.peak_equity * HUNDRED

    def today_loss_pct(self, now: datetime) -> Decimal:
        """Loss vs the day-start equity, as a positive percent when losing.

        Day-start resets at 00:00 Europe/Amsterdam.
        """
        self._roll_day(now)
        if self.day_start_equity <= 0:
            return Decimal("0")
        loss = self.day_start_equity - self.equity()
        return max(Decimal("0"), loss / self.day_start_equity * HUNDRED)

    def today_pnl(self, now: datetime) -> Decimal:
        self._roll_day(now)
        return self.equity() - self.day_start_equity

    def _roll_day(self, now: datetime) -> None:
        today = self._ams_date(now)
        if today != self._day_start_date:
            self._day_start_date = today
            self.day_start_equity = self.equity()

    # ------------------------------------------------------------------ marks

    def mark(self, pair: str, price: Decimal, now: datetime) -> None:
        """Update a position's last price and refresh peak/day-start."""
        pos = self.positions.get(pair)
        if pos is not None:
            pos.last = price
        eq = self.equity()
        if eq > self.peak_equity:
            self.peak_equity = eq
        self._roll_day(now)

    def mark_all(self, prices: dict[str, Decimal], now: datetime) -> None:
        for pair, price in prices.items():
            if pair in self.positions:
                self.positions[pair].last = price
        eq = self.equity()
        if eq > self.peak_equity:
            self.peak_equity = eq
        self._roll_day(now)

    # ------------------------------------------------------------------- fills

    def buy_fill_price(self, price: Decimal) -> Decimal:
        return price * (1 + self.slippage_pct / HUNDRED)

    def sell_fill_price(self, price: Decimal) -> Decimal:
        return price * (1 - self.slippage_pct / HUNDRED)

    def buy(
        self,
        session: Session,
        pair: str,
        price: Decimal,
        qty: Decimal,
        stop: Decimal,
        target: Decimal,
        decision_id: str,
        now: datetime,
        invalidation_price: Decimal | None = None,
    ) -> HeldPosition:
        """Open a long position at the slipped price, fee included in cost."""
        fill = self.buy_fill_price(price)
        fee = qty * fill * self.fee_pct / HUNDRED
        self.cash -= qty * fill + fee
        pos = HeldPosition(
            pair=pair,
            qty=qty,
            entry=fill,
            stop=stop,
            target=target,
            opened_at=now,
            decision_id=decision_id,
            last=price,
            invalidation_price=invalidation_price,
            highest_close=price,
            r_unit=fill - stop,
        )
        self.positions[pair] = pos
        save_position_row(
            session,
            pos.to_model(),
            self.account_id,
            invalidation_price,
            pos.highest_close,
            pos.r_unit,
        )
        return pos

    def close(
        self,
        session: Session,
        pair: str,
        price: Decimal,
        now: datetime,
        reason: str,
    ) -> TradeRow | None:
        """Close a position at the slipped price; returns the trade."""
        pos = self.positions.pop(pair, None)
        if pos is None:
            return None
        fill = self.sell_fill_price(price)
        buy_fee = pos.qty * pos.entry * self.fee_pct / HUNDRED
        sell_fee = pos.qty * fill * self.fee_pct / HUNDRED
        self.cash += pos.qty * fill - sell_fee
        realised = pos.qty * (fill - pos.entry) - buy_fee - sell_fee
        risk = pos.qty * (pos.entry - pos.stop)
        realised_r = realised / risk if risk > 0 else Decimal("0")
        delete_position_row(session, pair)
        trade = add_trade(
            session,
            TradeRow(
                account_id=self.account_id,
                pair=pair,
                qty=str(pos.qty),
                entry=str(pos.entry),
                exit=str(fill),
                opened_at=pos.opened_at,
                closed_at=now,
                fee_eur=str(buy_fee + sell_fee),
                realised_eur=str(realised),
                realised_r=str(realised_r),
                exit_reason=reason,
                decision_id=pos.decision_id,
            ),
        )
        return trade

    # ---------------------------------------------------------------- persist

    def persist(self, session: Session, now: datetime) -> None:
        """Write cash, peak and day-start back onto the account row."""
        self._roll_day(now)
        self.account.cash = str(self.cash)
        self.account.peak_equity = str(self.peak_equity)
        self.account.day_start_equity = str(self.day_start_equity)
        self.account.day_start_date = self._day_start_date.isoformat()
        session.merge(self.account)
