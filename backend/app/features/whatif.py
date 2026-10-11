"""The What-if simulator: replay a stored window with changed settings.

Spec ("Improving with evidence"): replays the stored history. Model answers
are held fixed — a scenario can only block a real fill or fill a decision a
gate skipped. Vetoed, expired and pending decisions are human choices and
never fill. Rows the deterministic layer answered (decider "rules") and
pairs the scenario adds are re-run through Rules; fills that only exist in
the scenario are marked ``stand_in``. A trade that matches a real one uses
the stored real fill prices; exits replay on stored 1h candles plus the
recorded 30-second watcher ticks. Unchanged settings reproduce reality.

A kill, kill-reset or settings change inside the window pauses the scenario
at that point: no new entries afterwards, open positions keep their exits.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from bisect import bisect_right
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.brief import TF_KEYS
from app.agent.broker.paper import HUNDRED, HeldPosition, PaperBroker
from app.agent.deciders.base import DecideContext
from app.agent.deciders.rules import RulesDecider
from app.agent.market.base import Candle, MarketInfo, drop_forming
from app.agent.risk import BuyContext, BuyVerdict, check_buy, check_exit, update_trail
from app.agent.signals import aligned_correlation, compute_pair_signals, compute_tf_values
from app.db import (
    AMSTERDAM,
    AccountRow,
    ActivityRow,
    DecisionRow,
    EquitySnapshotRow,
    SettingsHistoryRow,
    TradeRow,
)
from app.models import (
    Plan,
    SeriesPoint,
    Settings,
    Tf,
    WhatIfMetric,
    WhatIfResult,
    WhatIfSpecIn,
    WhatIfTrade,
)

if TYPE_CHECKING:
    from app.db import WhatifRow

log = logging.getLogger(__name__)

ZERO = Decimal("0")
CORR_BARS = 720  # 30 days of 1h candles — same window as the live engine

# Setting leaves a scenario may override. Anything outside this list —
# execution mode, notifications, the API key — is refused up front.
OVERRIDABLE_PREFIXES = ("risk.", "rules.")
OVERRIDABLE_KEYS = {
    "active_markets",
    "market.adx_threshold",
    "paper.fee_pct",
    "paper.slippage_pct",
    "no_weekend_entries",  # scenario-only switch (the design's checkbox)
}

_pause_re = re.compile(r"entries paused on (.+?) (until resumed|until (\S+))$")
_resume_re = re.compile(r"entries resumed on (.+)$")


class WhatIfSpecError(ValueError):
    """An override path isn't a real overridable setting."""


def apply_overrides(base: Settings, overrides: dict[str, object]) -> Settings:
    """Return ``base`` with each dotted-path override applied.

    ``no_weekend_entries`` is not a live setting — the simulator reads it
    straight from the spec — so it's skipped here."""
    doc = base.model_dump(mode="json")
    for path, value in overrides.items():
        if path == "no_weekend_entries":
            continue
        if not (path in OVERRIDABLE_KEYS or path.startswith(OVERRIDABLE_PREFIXES)):
            raise WhatIfSpecError(f"setting {path} cannot be changed in What-if")
        node: dict[str, Any] = doc
        parts = path.split(".")
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                raise WhatIfSpecError(f"unknown setting: {path}")
            node = nxt
        leaf = parts[-1]
        if leaf not in node:
            raise WhatIfSpecError(f"unknown setting: {path}")
        node[leaf] = value
    return Settings.model_validate(doc)


def _pause_windows(
    session: Session, start: datetime, end: datetime
) -> dict[str, list[tuple[datetime, datetime | None]]]:
    """Rebuild per-pair entry-pause intervals overlapping [start, end] from
    the pause activity log ("entries paused on X until <iso>" /
    "entries resumed on X")."""
    rows = session.scalars(
        select(ActivityRow)
        .where(
            ActivityRow.kind == "pause",
            ActivityRow.at >= repo.aware_utc(start).replace(tzinfo=None) - timedelta(days=400),
            ActivityRow.at <= repo.aware_utc(end).replace(tzinfo=None),
        )
        .order_by(ActivityRow.at.asc())
    ).all()
    windows: dict[str, list[tuple[datetime, datetime | None]]] = {}
    for row in rows:
        at = repo.aware_utc(row.at)
        m = _pause_re.match(row.text or "")
        if m:
            key = "*" if m.group(1) == "all coins" else m.group(1)
            until = None if m.group(2) == "until resumed" else _parse_dt(m.group(3))
            windows.setdefault(key, []).append((at, until))
            continue
        m = _resume_re.match(row.text or "")
        if m:
            key = "*" if m.group(1) == "all coins" else m.group(1)
            for i, (pstart, pend) in enumerate(windows.get(key, [])):
                if pend is None or pend > at:
                    windows[key][i] = (pstart, at)
                    break
    return windows


def _parse_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _paused_at(
    windows: dict[str, list[tuple[datetime, datetime | None]]], pair: str, at: datetime
) -> bool:
    for key in ("*", pair):
        for pstart, pend in windows.get(key, []):
            if pstart <= at and (pend is None or at < pend):
                return True
    return False


def _scenario_pause_from(session: Session, start: datetime, end: datetime) -> datetime | None:
    """The earliest in-window kill, kill-reset or settings change — the
    point where the scenario stops taking new entries."""
    points: list[datetime] = []
    for kind, pattern in (("alert", "KILL SWITCH:%"), ("system", "Kill acknowledged%")):
        for row in session.scalars(
            select(ActivityRow).where(
                ActivityRow.kind == kind,
                ActivityRow.at >= repo.aware_utc(start).replace(tzinfo=None),
                ActivityRow.at <= repo.aware_utc(end).replace(tzinfo=None),
                ActivityRow.text.like(pattern),
            )
        ).all():
            points.append(repo.aware_utc(row.at))
    first_change = session.scalar(
        select(SettingsHistoryRow.at)
        .where(
            SettingsHistoryRow.at >= repo.aware_utc(start).replace(tzinfo=None),
            SettingsHistoryRow.at <= repo.aware_utc(end).replace(tzinfo=None),
        )
        .order_by(SettingsHistoryRow.at.asc())
        .limit(1)
    )
    if first_change is not None:
        points.append(repo.aware_utc(first_change))
    return min(points) if points else None


def _levels_from_raw(raw_response: str | None) -> tuple[Decimal, Decimal, Decimal] | None:
    """Pull entry/stop/target out of a stored Responses API body."""
    if not raw_response:
        return None
    try:
        body = json.loads(raw_response)
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parsed = json.loads(content.get("text", "{}"))
                    if all(parsed.get(k) is not None for k in ("entry", "stop", "target")):
                        return (
                            Decimal(str(parsed["entry"])),
                            Decimal(str(parsed["stop"])),
                            Decimal(str(parsed["target"])),
                        )
    except (json.JSONDecodeError, TypeError, KeyError, ArithmeticError):
        return None
    return None


def _proposed_levels(
    row: DecisionRow, price: Decimal, atr_1h: Decimal | None
) -> tuple[Decimal, Decimal, Decimal]:
    """(entry, stop, target) the decision proposed — the stored plan, else
    the stored raw model output, else the outcome rules' hypothetical
    levels (stop 1.5 x ATR below, target 2R up)."""
    if row.plan:
        try:
            plan = Plan.model_validate_json(row.plan)
            return plan.entry, plan.stop, plan.target
        except Exception:
            pass
    levels = _levels_from_raw(row.raw_response)
    if levels is not None:
        return levels
    dist = atr_1h * Decimal("1.5") if atr_1h else price * Decimal("0.03")
    return price, price - dist, price + 2 * dist


class _Sim:
    """One scenario replay over a stored window. The caller runs it inside
    a savepoint it rolls back — nothing here may leak into real data."""

    def __init__(
        self,
        session: Session,
        spec: WhatIfSpecIn,
        live_settings: Settings,
        scenario: Settings,
        live_account: AccountRow,
        market_rules: dict[str, MarketInfo],
    ) -> None:
        self.session = session
        self.spec = spec
        self.live_settings = live_settings
        self.settings = scenario
        self.live_account = live_account
        self.market_rules = market_rules
        self.rules = RulesDecider()
        self.no_weekend = bool(spec.overrides.get("no_weekend_entries"))
        self.broker: PaperBroker  # set in run()
        self.throttle_dd = False
        self.trades: list[WhatIfTrade] = []
        self.added: list[WhatIfTrade] = []
        self.removed: list[WhatIfTrade] = []
        self.stand_ins = 0
        self.equity_curve: list[SeriesPoint] = []
        # Samples land keyed by timestamp: pairs are replayed one after
        # another, so several positions can contribute a sample at the same
        # candle end — last write wins keeps the curve strictly ordered.
        self._curve: dict[datetime, float] = {}
        self.fees = Decimal("0")
        self.real_trades: dict[str, TradeRow] = {}
        self._candle_cache: dict[str, list[Candle]] = {}
        self._candle_times: dict[str, list[datetime]] = {}

    # ---------------------------------------------------------------- driver

    async def run(self, progress: Callable[[float], None] | None = None) -> WhatIfResult:
        start, end = repo.aware_utc(self.spec.start), repo.aware_utc(self.spec.end)
        live_id = self.live_account.id

        decisions = list(
            self.session.scalars(
                select(DecisionRow)
                .where(
                    DecisionRow.account_id == live_id,
                    DecisionRow.at >= start.replace(tzinfo=None),
                    DecisionRow.at <= end.replace(tzinfo=None),
                )
                .order_by(DecisionRow.at.asc(), DecisionRow.id.asc())
            ).all()
        )
        all_live = list(
            self.session.scalars(select(TradeRow).where(TradeRow.account_id == live_id)).all()
        )
        self.real_trades = {t.decision_id: t for t in all_live if t.decision_id}
        carry = [
            t
            for t in all_live
            if repo.aware_utc(t.opened_at) < start
            and (t.closed_at is None or repo.aware_utc(t.closed_at) > start)
        ]

        # Scenario account: equity at window start; carry-in positions hold
        # their real size and their cost comes out of cash.
        snap = self.session.scalar(
            select(EquitySnapshotRow.equity)
            .where(
                EquitySnapshotRow.account_id == live_id,
                EquitySnapshotRow.at <= start.replace(tzinfo=None),
            )
            .order_by(EquitySnapshotRow.at.desc())
            .limit(1)
        )
        start_equity = (
            Decimal(str(snap)) if snap is not None else Decimal(self.live_account.start_balance)
        )
        acc = AccountRow(
            started_at=start,
            active=False,
            start_balance=str(start_equity),
            cash=str(start_equity),
            peak_equity=str(start_equity),
            day_start_equity=str(start_equity),
            day_start_date=start.astimezone(AMSTERDAM).date().isoformat(),
        )
        self.session.add(acc)
        self.session.flush()
        self.broker = PaperBroker(acc, self.settings.paper)
        self.broker.cash = start_equity
        for trade in carry:
            self._seed_carry(trade)

        pauses = _pause_windows(self.session, start, end)
        pause_from = _scenario_pause_from(self.session, start, end)
        close_events = [
            trade
            for trade in all_live
            if trade.closed_at is not None
            and start <= repo.aware_utc(trade.closed_at) <= end
        ]
        close_events.sort(key=lambda trade: repo.aware_utc(trade.closed_at or start))

        # Merge decision events and real close events in time order.
        cursor = start
        d_i = c_i = 0
        while d_i < len(decisions) or c_i < len(close_events):
            d_at = repo.aware_utc(decisions[d_i].at) if d_i < len(decisions) else None
            c_at = repo.aware_utc(close_events[c_i].closed_at) if c_i < len(close_events) else None
            if c_at is not None and (d_at is None or c_at <= d_at):
                self._advance(cursor, c_at)
                self._matched_close(close_events[c_i])
                cursor = c_at
                c_i += 1
            else:
                assert d_at is not None
                self._advance(cursor, d_at)
                await self._decision(decisions[d_i], pauses, pause_from)
                cursor = d_at
                d_i += 1
            if progress and (d_i + c_i) % 50 == 0:
                progress((d_i + c_i) / max(len(decisions) + len(close_events), 1))

        # Stand-in slots: pairs the scenario trades that reality never
        # decided on at that cycle get a fresh Rules look.
        if pause_from is None:
            decided = {(repo.aware_utc(d.at), d.pair) for d in decisions}
            for slot in sorted({repo.aware_utc(d.at) for d in decisions}):
                self._advance(cursor, slot)
                for pair in self.settings.active_markets:
                    if (slot, pair) in decided or pair in self.broker.positions:
                        continue
                    if _paused_at(pauses, pair, slot) or self._weekend_blocked(slot):
                        continue
                    await self._rules_slot(pair, slot)
                cursor = slot

        self._advance(cursor, end)
        # Positions still open at the window's edge close on the last candle.
        for pair in list(self.broker.positions):
            candles = self._candles(pair)
            mark = candles[-1].close if candles else self.broker.positions[pair].last
            self._close(pair, mark, end, "window_end")
        self._sample(end)
        self.equity_curve = [
            SeriesPoint(time=t, value=v) for t, v in sorted(self._curve.items())
        ]

        return self._result(start, end, start_equity)

    # ------------------------------------------------------------- decisions

    async def _decision(
        self,
        row: DecisionRow,
        pauses: dict[str, list[tuple[datetime, datetime | None]]],
        pause_from: datetime | None,
    ) -> None:
        t = repo.aware_utc(row.at)
        real_trade = self.real_trades.get(row.id)
        if row.status in ("vetoed", "expired", "pending"):
            return  # a human choice the scenario never overrides

        paused = pause_from is not None and t >= pause_from
        entry_paused = _paused_at(pauses, row.pair, t)
        weekend = self._weekend_blocked(t)
        in_market = row.pair in self.settings.active_markets

        if row.decider == "rules":
            # The deterministic layer is recomputed under scenario settings —
            # whether it filled, was gate-skipped or held in reality.
            if paused or entry_paused or weekend or not in_market:
                if real_trade:
                    self._removed(
                        row,
                        real_trade,
                        self._block_reason(paused, entry_paused, weekend, in_market),
                    )
                return
            await self._rules_slot(row.pair, t, row=row)
            return

        # Model rows: the answer is held fixed.
        if row.action == "SELL":
            pos = self.broker.positions.get(row.pair)
            if pos is not None:
                # When a real close for this position is still ahead, let
                # _matched_close book the stored exit price; a position the
                # real book never had closes here at the signal price.
                real_close = self.real_trades.get(pos.decision_id)
                if real_close is None or (
                    real_close.closed_at is not None
                    and repo.aware_utc(real_close.closed_at) <= t
                ):
                    self._close(row.pair, self._price_at(row.pair, t), t, "model_sell")
            return
        if row.action not in ("BUY", "SKIP"):
            return  # HOLD — nothing to replay
        if paused or entry_paused or weekend or not in_market:
            if real_trade:
                self._removed(
                    row, real_trade, self._block_reason(paused, entry_paused, weekend, in_market)
                )
            return
        await self._try_buy(row, t, self._price_at(row.pair, t), real_trade)

    def _block_reason(
        self, paused: bool, entry_paused: bool, weekend: bool, in_market: bool
    ) -> str:
        if paused:
            return "scenario paused (kill/reset/settings change)"
        if entry_paused:
            return "entry pause"
        if weekend:
            return "no weekend entries"
        return "pair not in scenario markets"

    async def _try_buy(
        self, row: DecisionRow, t: datetime, price: Decimal, real_trade: TradeRow | None
    ) -> None:
        entry, stop, target = _proposed_levels(row, price, self._atr_at(row.pair, t))
        verdict = self._buy_verdict(row.pair, t, entry, stop, target)
        if verdict.outcome == "skip":
            if real_trade:
                self._removed(row, real_trade, verdict.skip_reason or "gate")
            return
        assert verdict.plan is not None
        # A decision that really filled keeps its stored fill price; a
        # gate-skipped one fills at the slipped signal price. Sizing is
        # always the scenario's.
        fill = Decimal(real_trade.entry) if real_trade else self.broker.buy_fill_price(price)
        self._open(row.pair, fill, verdict.plan.qty, stop, target, t, row.id)
        if real_trade is None:
            self._log_added(
                row.pair,
                t,
                fill,
                row.id,
                reason=f"gate '{row.error or '?'}' passes in scenario",
                stand_in=False,
            )

    async def _rules_slot(self, pair: str, t: datetime, row: DecisionRow | None = None) -> None:
        """Re-run the deterministic layer under scenario settings."""
        candles = {
            tf: drop_forming(
                self._candles_1h_to(pair, t)
                if tf == "1h"
                else self._candles_to(pair, tf, t),
                tf,
                t,
            )
            for tf in TF_KEYS
        }
        if not candles["1h"] or not candles["4h"] or not candles["1d"]:
            return
        price = candles["1h"][-1].close
        ctx_specs: tuple[tuple[str, str, Tf], ...] = (
            ("btc_1h", "BTC-EUR", "1h"),
            ("btc_4h", "BTC-EUR", "4h"),
            ("btc_1d", "BTC-EUR", "1d"),
            ("usdc_1h", "USDC-EUR", "1h"),
        )
        ctx_candles = {
            name: drop_forming(
                self._candles_1h_to(mkt, t)
                if tf == "1h"
                else self._candles_to(mkt, tf, t),
                tf,
                t,
            )
            for name, mkt, tf in ctx_specs
        }
        try:
            signals = compute_pair_signals(
                pair,
                candles["1h"],
                candles["4h"],
                candles["1d"],
                btc_1h=ctx_candles["btc_1h"],
                usdc_1h=ctx_candles["usdc_1h"],
                settings=self.settings.market,
                now=t,
            )
        except Exception:
            log.exception("whatif: signal computation failed for %s at %s", pair, t)
            return
        prev_4h = compute_tf_values(candles["4h"][:-1], "4h") if len(candles["4h"]) > 1 else None
        pos = self.broker.positions.get(pair)
        draft = await self.rules.decide(
            DecideContext(
                pair=pair,
                now=t,
                price=price,
                signals=signals,
                holding=pos is not None,
                position_entry=pos.entry if pos else None,
                position_stop=pos.stop if pos else None,
                position_target=pos.target if pos else None,
                candles_1h=candles["1h"],
                candles_4h=candles["4h"],
                candles_1d=candles["1d"],
                prev_4h_ema20=prev_4h.ema20 if prev_4h else None,
                prev_4h_ema50=prev_4h.ema50 if prev_4h else None,
                settings=self.settings,
                position=pos,
                context_candles=ctx_candles,
            )
        )
        real_trade = self.real_trades.get(row.id) if row is not None else None
        if draft.action == "BUY":
            if self._weekend_blocked(t):
                return
            verdict = self._buy_verdict(
                pair, t, draft.entry or price, draft.stop or ZERO, draft.target or ZERO
            )
            if verdict.outcome != "fill" or verdict.plan is None:
                if real_trade is not None and row is not None:
                    self._removed(row, real_trade, verdict.skip_reason or "gate")
                return
            if real_trade is not None and row is not None:
                # A decision that really filled keeps its stored entry and
                # stop; sizing stays the scenario's — same as model rows.
                r_unit = Decimal(real_trade.r_unit)
                self._open(
                    pair,
                    Decimal(real_trade.entry),
                    verdict.plan.qty,
                    Decimal(real_trade.initial_stop),
                    Decimal(real_trade.entry) + 2 * r_unit if r_unit > 0 else None,
                    t,
                    row.id,
                )
            else:
                fill = self.broker.buy_fill_price(price)
                decision_id = row.id if row else f"standin-{uuid.uuid4().hex[:8]}"
                self._open(
                    pair, fill, verdict.plan.qty, draft.stop, draft.target, t, decision_id
                )
                self._log_added(
                    pair, t, fill, row.id if row else None, reason="stand-in", stand_in=True
                )
        elif draft.action == "SELL" and pair in self.broker.positions:
            self._close(pair, price, t, "rules_sell")
        elif real_trade is not None and row is not None:
            self._removed(row, real_trade, "scenario rules held")

    def _weekend_blocked(self, t: datetime) -> bool:
        return self.no_weekend and t.astimezone(AMSTERDAM).weekday() >= 5

    def _buy_verdict(
        self, pair: str, t: datetime, entry: Decimal, stop: Decimal, target: Decimal
    ) -> BuyVerdict:
        c1h = self._candles_1h_to(pair, t)
        atr_val = compute_tf_values(c1h, "1h").atr14 if c1h else None
        atr = Decimal(str(atr_val)) if atr_val else None
        ctx = BuyContext(
            pair=pair,
            now=t,
            entry=entry,
            stop=stop,
            target=target,
            atr_1h=atr,
            has_position=pair in self.broker.positions,
            last_stop_out_at=self._last_stop_out(pair),
            correlated_pairs=self._correlated(pair, c1h, t),
            daily_loss_hit=(
                self.broker.today_loss_pct(t) >= self.settings.risk.daily_loss_limit_pct
            ),
            risk_pct=self._risk_pct(),
        )
        return check_buy(ctx, self.broker, self.market_rules.get(pair), self.settings)

    def _correlated(self, pair: str, candles_1h: list[Candle], t: datetime) -> list[str]:
        out = []
        for other in self.broker.positions:
            if other == pair:
                continue
            other_candles = self._candles_1h_to(other, t)
            corr = aligned_correlation(candles_1h, other_candles, CORR_BARS)
            if corr is not None and corr > float(self.settings.risk.correlated_threshold):
                out.append(other)
        return out

    def _risk_pct(self) -> Decimal:
        """The throttle meter replayed on the scenario book: a losing
        streak on scenario trades or the scenario drawdown."""
        streak = 0
        for tr in reversed(self.trades):
            if tr.realised_r is None:
                continue
            if Decimal(str(tr.realised_r)) < 0:
                streak += 1
            else:
                break
        dd = self.broker.drawdown_pct()
        if dd > self.settings.risk.throttle_drawdown_pct:
            self.throttle_dd = True
        elif dd <= self.settings.risk.throttle_lift_drawdown_pct:
            self.throttle_dd = False
        throttled = streak >= self.settings.risk.throttle_consecutive_losses or self.throttle_dd
        return (
            self.settings.risk.throttled_risk_per_trade_pct
            if throttled
            else self.settings.risk.risk_per_trade_pct
        )

    def _last_stop_out(self, pair: str) -> datetime | None:
        for tr in reversed(self.trades):
            if tr.pair == pair and tr.exit_reason == "stop":
                return tr.closed_at
        return None

    # ---------------------------------------------------------------- fills

    def _seed_carry(self, trade: TradeRow) -> None:
        """A position already open at window start, restored at cost. The
        stop replays from the recorded initial stop; the target is the
        stored exit target proxy (entry + 2R) when unknown."""
        r_unit = Decimal(trade.r_unit)
        pos = HeldPosition(
            pair=trade.pair,
            qty=Decimal(trade.qty),
            entry=Decimal(trade.entry),
            stop=Decimal(trade.initial_stop),
            initial_stop=Decimal(trade.initial_stop),
            target=Decimal(trade.entry) + 2 * r_unit if r_unit > 0 else Decimal(trade.entry),
            opened_at=repo.aware_utc(trade.opened_at),
            decision_id=trade.decision_id or "",
            last=Decimal(trade.entry),
            r_unit=r_unit,
        )
        self.broker.positions[trade.pair] = pos
        self.broker.cash -= pos.qty * pos.entry  # keep equity tied to the snapshot
        self._rebuild_carry_state(pos)

    def _rebuild_carry_state(self, pos: HeldPosition) -> None:
        """Replay closed 1h candles from entry to the window start so the
        trailing/breakeven ratchet and ``last`` land roughly where the real
        watcher left them — the positions table stores only the initial
        stop, not the ratcheted one. Exit triggers are ignored: the real
        position demonstrably survived to the window start. Rebuilt under
        the LIVE settings — divergence starts at the window edge."""
        start = repo.aware_utc(self.spec.start)
        opened = repo.aware_utc(pos.opened_at)
        for candle in self._candles(pos.pair):  # ascending open_time
            candle_end = candle.open_time + timedelta(hours=1)
            if candle_end <= opened:
                continue
            if candle.open_time >= start:
                break
            atr = self._atr_at(pos.pair, candle.open_time + timedelta(seconds=1))
            update_trail(pos, candle.close)
            check_exit(pos, candle.close, atr, candle_end, self.live_settings)
            pos.last = candle.close

    def _open(
        self,
        pair: str,
        fill: Decimal,
        qty: Decimal,
        stop: Decimal | None,
        target: Decimal | None,
        at: datetime,
        decision_id: str,
    ) -> None:
        """Open a scenario position — identical money math to
        PaperBroker.buy but the fill price is taken verbatim (real or
        slipped), so matched trades reproduce the stored fill exactly."""
        stop = stop or ZERO
        target = target or ZERO
        fee = qty * fill * self.broker.fee_pct / HUNDRED
        self.broker.cash -= qty * fill + fee
        self.fees += fee
        self.broker.positions[pair] = HeldPosition(
            pair=pair,
            qty=qty,
            entry=fill,
            stop=stop,
            initial_stop=stop,
            target=target,
            opened_at=at,
            decision_id=decision_id,
            last=fill,
            highest_close=fill,
            r_unit=fill - stop,
        )
        self._sample(at)

    def _close(self, pair: str, price: Decimal, at: datetime, reason: str) -> None:
        """Close a scenario position — identical money math to
        PaperBroker.close, plus the What-if trade record."""
        pos = self.broker.positions.pop(pair, None)
        if pos is None or price <= 0:
            if pos is not None:
                self.broker.positions[pair] = pos
            return
        fill = self.broker.sell_fill_price(price)
        self._book(pair, pos, fill, at, reason, reason, decision_id=pos.decision_id)

    def _matched_close(self, trade: TradeRow) -> None:
        """Close the scenario copy of a real trade at its stored exit —
        exact price, time and reason, no slippage re-applied."""
        pos = self.broker.positions.get(trade.pair)
        if pos is None or pos.decision_id != trade.decision_id:
            return
        del self.broker.positions[trade.pair]
        self._book(
            trade.pair,
            pos,
            Decimal(trade.exit),
            repo.aware_utc(trade.closed_at),
            "matched real exit",
            trade.exit_reason,
            decision_id=trade.decision_id,
        )

    def _book(
        self,
        pair: str,
        pos: HeldPosition,
        fill: Decimal,
        at: datetime,
        reason: str,
        exit_reason: str | None,
        decision_id: str | None,
    ) -> None:
        buy_fee = pos.qty * pos.entry * self.broker.fee_pct / HUNDRED
        sell_fee = pos.qty * fill * self.broker.fee_pct / HUNDRED
        self.broker.cash += pos.qty * fill - sell_fee
        self.fees += sell_fee
        realised = pos.qty * (fill - pos.entry) - buy_fee - sell_fee
        risk = pos.qty * pos.r_unit
        is_standin = (decision_id or "").startswith("standin-")
        self.trades.append(
            WhatIfTrade(
                pair=pair,
                opened_at=pos.opened_at,
                closed_at=at,
                entry=pos.entry,
                exit=fill,
                realised_r=realised / risk if risk > 0 else ZERO,
                realised_eur=realised,
                reason=reason,
                exit_reason=exit_reason,
                stand_in=is_standin,
                decision_id=None if is_standin else decision_id,
            )
        )
        self._sample(at)

    def _removed(self, row: DecisionRow, trade: TradeRow, reason: str) -> None:
        self.removed.append(
            WhatIfTrade(
                pair=trade.pair,
                opened_at=repo.aware_utc(trade.opened_at),
                closed_at=repo.aware_utc(trade.closed_at) if trade.closed_at else None,
                entry=Decimal(trade.entry),
                exit=Decimal(trade.exit) if trade.exit else None,
                realised_r=Decimal(trade.realised_r) if trade.realised_r else None,
                realised_eur=Decimal(trade.realised_eur) if trade.realised_eur else None,
                reason=reason,
                exit_reason=trade.exit_reason,
                stand_in=False,
                decision_id=row.id,
            )
        )

    def _log_added(
        self,
        pair: str,
        t: datetime,
        fill: Decimal,
        decision_id: str | None,
        *,
        reason: str,
        stand_in: bool,
    ) -> None:
        if stand_in:
            self.stand_ins += 1
        self.added.append(
            WhatIfTrade(
                pair=pair,
                opened_at=t,
                closed_at=None,
                entry=fill,
                exit=None,
                realised_r=None,
                realised_eur=None,
                reason=reason,
                exit_reason=None,
                stand_in=stand_in,
                decision_id=decision_id,
            )
        )

    # ------------------------------------------------------------ replay --

    def _advance(self, frm: datetime, to: datetime) -> None:
        """Replay exits over (frm, to] for every pair the scenario holds:
        stored watcher ticks inside each 1h candle, then candle extremes
        (stop first on a tie), then the close for trail/breakeven/time-stop."""
        for pair in list(self.broker.positions):
            # One query per position per interval — the live account's ticks
            # only; a shadow's near-stop marks must not close scenario trades.
            ticks = repo.exit_ticks_for(
                self.session,
                pair,
                frm + timedelta(seconds=1),
                to,
                account_id=self.live_account.id,
            )
            tick_i = 0
            for candle in self._candles(pair):  # ascending open_time
                candle_end = candle.open_time + timedelta(hours=1)
                if candle_end <= frm:
                    continue
                if candle.open_time >= to:
                    break
                pos = self.broker.positions.get(pair)
                if pos is None:
                    break
                atr = self._atr_at(pair, candle.open_time + timedelta(seconds=1))
                # 1) watcher ticks inside the candle, clamped to (frm, to]
                tick_hi = min(candle_end, to)
                exited = False
                while tick_i < len(ticks) and repo.aware_utc(ticks[tick_i].at) <= tick_hi:
                    tick = ticks[tick_i]
                    tick_i += 1
                    reason = check_exit(
                        pos, Decimal(tick.price), atr, repo.aware_utc(tick.at), self.settings
                    )
                    if reason:
                        self._close(pair, Decimal(tick.price), repo.aware_utc(tick.at), reason)
                        exited = True
                        break
                if exited or pair not in self.broker.positions:
                    break
                # 2)+3) only when the candle is fully inside (frm, to]: a
                # candle still open at `to` would book exits on prices the
                # scenario has not reached yet.
                if candle_end > to:
                    continue
                if candle.open_time >= frm:
                    if candle.low <= pos.stop:
                        self._close(pair, pos.stop, candle_end, "stop")
                        break
                    if pos.target > 0 and candle.high >= pos.target:
                        self._close(pair, pos.target, candle_end, "target")
                        break
                update_trail(pos, candle.close)
                reason = check_exit(pos, candle.close, atr, candle_end, self.settings)
                if reason:
                    self._close(pair, candle.close, candle_end, reason)
                    break
                pos.last = candle.close
                self._sample(candle_end)

    def _candles(self, pair: str) -> list[Candle]:
        """All stored 1h candles up to the window end (cached per pair)."""
        if pair not in self._candle_cache:
            self._candle_cache[pair] = repo.load_candles(
                self.session, pair, "1h", 6000, end=self.spec.end
            )
            self._candle_times[pair] = [c.open_time for c in self._candle_cache[pair]]
        return self._candle_cache[pair]

    def _candles_1h_to(self, pair: str, at: datetime, limit: int = 900) -> list[Candle]:
        """Closed 1h candles up to ``at``, sliced from the cached list — no
        query per call the way ``_candles_to`` would make."""
        self._candles(pair)  # fills both caches
        times = self._candle_times[pair]
        candles = self._candle_cache[pair]
        idx = bisect_right(times, at)
        return drop_forming(candles[max(0, idx - limit) : idx], "1h", at)

    def _candles_to(self, pair: str, tf: str, at: datetime) -> list[Candle]:
        return repo.load_candles(self.session, pair, tf, 900, end=at)  # type: ignore[arg-type]

    def _price_at(self, pair: str, at: datetime) -> Decimal:
        candles = self._candles_1h_to(pair, at, limit=5)
        return candles[-1].close if candles else ZERO

    def _atr_at(self, pair: str, at: datetime) -> Decimal | None:
        candles = self._candles_1h_to(pair, at)
        if not candles:
            return None
        val = compute_tf_values(candles, "1h").atr14
        return Decimal(str(val)) if val else None

    def _sample(self, at: datetime) -> None:
        self._curve[at] = float(self.broker.equity())

    # --------------------------------------------------------------- result

    def _result(self, start: datetime, end: datetime, start_equity: Decimal) -> WhatIfResult:
        end_equity = self.broker.equity()
        ret_pct = float((end_equity / start_equity - 1) * 100) if start_equity > 0 else None
        peak = start_equity
        max_dd = Decimal("0")
        for p in self.equity_curve:
            v = Decimal(str(p.value or 0))
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100)
        scored = [t for t in self.trades if t.realised_r is not None]
        wins = [t for t in scored if Decimal(str(t.realised_r)) > 0]
        avg_r = (
            float(sum(Decimal(str(t.realised_r)) for t in scored) / len(scored)) if scored else None
        )

        # --- what actually happened ---------------------------------------
        live_snaps = repo.list_equity_snapshots(self.session, self.live_account.id, start, end)
        actual_curve = [
            SeriesPoint(time=repo.aware_utc(s.at), value=float(s.equity)) for s in live_snaps
        ]
        actual_start = Decimal(str(actual_curve[0].value)) if actual_curve else start_equity
        actual_end = Decimal(str(actual_curve[-1].value)) if actual_curve else actual_start
        actual_ret = float((actual_end / actual_start - 1) * 100) if actual_start > 0 else None
        apeak = actual_start
        actual_dd = Decimal("0")
        for p in actual_curve:
            v = Decimal(str(p.value or 0))
            apeak = max(apeak, v)
            if apeak > 0:
                actual_dd = max(actual_dd, (apeak - v) / apeak * 100)
        window_real = list(
            self.session.scalars(
                select(TradeRow).where(
                    TradeRow.account_id == self.live_account.id,
                    TradeRow.closed_at >= start.replace(tzinfo=None),
                    TradeRow.closed_at <= end.replace(tzinfo=None),
                )
            ).all()
        )
        a_scored = [t for t in window_real if t.realised_r]
        a_wins = [t for t in a_scored if Decimal(t.realised_r) > 0]
        a_avg = (
            float(sum(Decimal(t.realised_r) for t in a_scored) / len(a_scored))
            if a_scored
            else None
        )
        a_fees = sum(Decimal(t.fee_eur) for t in window_real)

        def diff(scen: float | None, act: float | None) -> float | None:
            return round(scen - act, 2) if scen is not None and act is not None else None

        actual_wr = round(len(a_wins) / len(a_scored) * 100, 1) if a_scored else None
        scen_wr = round(len(wins) / len(scored) * 100, 1) if scored else None
        metrics = [
            WhatIfMetric(
                label="Return",
                unit="pct",
                actual=actual_ret,
                scenario=ret_pct,
                change=diff(ret_pct, actual_ret),
            ),
            WhatIfMetric(
                label="Max drawdown",
                unit="pct",
                actual=float(actual_dd),
                scenario=float(max_dd),
                change=diff(float(max_dd), float(actual_dd)),
            ),
            WhatIfMetric(
                label="Trades",
                unit="count",
                actual=len(window_real),
                scenario=len(self.trades),
                change=float(len(self.trades) - len(window_real)),
            ),
            WhatIfMetric(
                label="Win rate",
                unit="pct",
                actual=actual_wr,
                scenario=scen_wr,
                change=diff(scen_wr, actual_wr),
            ),
            WhatIfMetric(
                label="Average R",
                unit="r",
                actual=a_avg,
                scenario=avg_r,
                change=diff(avg_r, a_avg),
            ),
            WhatIfMetric(
                label="Fees paid",
                unit="eur",
                actual=float(a_fees),
                scenario=float(self.fees),
                change=float(self.fees) - float(a_fees),
            ),
        ]
        return WhatIfResult(
            metrics=metrics,
            equity=self.equity_curve,
            actual=actual_curve,
            removed=self.removed,
            added=self.added,
            stand_ins=self.stand_ins,
            trades=len(self.trades),
        )


async def run_whatif(session: Session, row: WhatifRow, market_rules: dict[str, MarketInfo]) -> None:
    """Run one queued scenario to done/failed. The replay's scratch account
    writes stay inside a savepoint that is always rolled back — What-if
    never leaks trades into real tables. Progress stays in memory (a mid-run
    commit would persist the savepoint's scratch rows)."""
    spec = WhatIfSpecIn.model_validate(json.loads(row.spec))
    row.status = "running"
    session.commit()
    try:
        settings = repo.load_settings(session)
        scenario = apply_overrides(settings, spec.overrides)
        account = repo.current_account(session)

        def progress(f: float) -> None:
            row.progress = f

        sp = session.begin_nested()
        try:
            sim = _Sim(session, spec, settings, scenario, account, market_rules)
            result = await sim.run(progress)
        finally:
            sp.rollback()
        row.result = result.model_dump_json()
        row.status = "done"
        row.progress = 1.0
    except Exception as exc:
        log.exception("whatif %s failed", row.id)
        session.rollback()
        row.status = "failed"
        row.error = str(exc)
    row.updated_at = repo.aware_utc(datetime.now(UTC)).replace(tzinfo=None)
    session.commit()
