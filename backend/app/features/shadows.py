"""Shadow strategies: extra model/prompt variants paper-trading alongside.

A shadow runs the same decision loop on its own paper book — same cycle
snapshot as live (candles, indicators, regime, timestamps) but its own
account, positions, risk gates, throttle and drawdown counters. It never
touches the live book. A new shadow starts flat with cash equal to live
equity at creation; Compare measures both sides from that moment.

Shadow model calls count toward the shared monthly cost cap. At 80% of the
cap a shadow stops making new decisions — no Rules stand-in — until the
first of next month; the 30-second watcher keeps managing its open
positions. The pause window is stored and left out of both sides' return
and drawdown in Compare.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent import budget
from app.agent.brief import TF_KEYS
from app.agent.broker.paper import HeldPosition, PaperBroker
from app.agent.deciders.base import DecideContext, Draft, RiskView
from app.agent.deciders.openai_decider import OpenAIDecider
from app.agent.engine import CYCLE_S
from app.agent.limit import buy_fills, post_only_limit, sell_fills
from app.agent.market.base import Candle, MarketClient, MarketInfo, drop_forming
from app.agent.market.universe import entries_allowed
from app.agent.risk import BuyContext, check_buy, check_exit, update_trail
from app.agent.signals import (
    aligned_correlation,
    compute_pair_signals,
    compute_tf_values,
    decision_signals,
)
from app.db import (
    AMSTERDAM,
    AccountRow,
    DecisionRow,
    EquitySnapshotRow,
    OutcomeRow,
    PaperOrderRow,
    ShadowRow,
)
from app.models import Decision, Gate, Plan, Regime, Settings, ShadowOut, ShadowPause, Tf

log = logging.getLogger(__name__)

ZERO = Decimal("0")
CORR_BARS = 720
MAX_ACTIVE = 2  # spec: two shadows may run alongside


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def apply_shadow_overrides(base: Settings, overrides: dict[str, object]) -> Settings:
    """The What-if whitelist plus the entry order type — a shadow can only
    change risk, rules, market and paper leaves, the entry order type and
    the active pair list (Shadow B = decision.v1 + limit entries)."""
    from app.features.whatif import SHADOW_EXTRA_KEYS, apply_overrides

    return apply_overrides(base, overrides, extra_keys=SHADOW_EXTRA_KEYS)


@dataclass
class _Shadow:
    row: ShadowRow
    account: AccountRow
    broker: PaperBroker
    settings: Settings
    decider: OpenAIDecider
    throttle_dd: bool = False

    @property
    def paused(self) -> bool:
        pauses = json.loads(self.row.pauses or "[]")
        return bool(pauses) and pauses[-1].get("end") is None


class ShadowRunner:
    """Drives every active shadow: cycle decisions and watcher exits."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        market_client: MarketClient,
        market_rules: dict[str, MarketInfo],
        live_settings: Callable[[], Settings],
        clock: Callable[[], datetime],
        price_fn: Callable[[str], Awaitable[Decimal]],
        notify: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._sessions = session_factory
        self.client = market_client
        self.market_rules = market_rules
        self._live_settings = live_settings
        self.clock = clock
        self._price_fn = price_fn
        self._notify = notify
        self._cache: dict[str, _Shadow] = {}

    def set_client(self, client: MarketClient) -> None:
        """Swap the market client (data-source change) — live's client is
        rebuilt and closed, so every cached decider must see the new one."""
        self.client = client
        for shadow in self._cache.values():
            shadow.decider.client = client

    # ------------------------------------------------------------- loading

    def _shadows(self, session: Session) -> list[_Shadow]:
        out: list[_Shadow] = []
        for row in repo.list_shadows(session, active_only=True):
            account = session.get(AccountRow, row.account_id)
            if account is None:
                continue
            cached = self._cache.get(row.id)
            if cached is not None:
                # `row`/`account` are fresh session-bound instances on every
                # call — always swap them in so pause writes and broker
                # persist stay attached to this session.
                cached.row = row
                cached.account = account
                cached.broker.account = account
                settings = self._shadow_settings(session, row)
                if settings != cached.settings:
                    # Live settings or shadow config changed: rebuild the
                    # decider and broker around the new settings.
                    cached = self._build(session, row, account)
                    self._cache[row.id] = cached
                else:
                    cached.broker.load_positions(session)
                out.append(cached)
                continue
            shadow = self._build(session, row, account)
            self._cache[row.id] = shadow
            out.append(shadow)
        for sid in set(self._cache) - {s.row.id for s in out}:
            del self._cache[sid]
        return out

    def _shadow_settings(self, session: Session, row: ShadowRow) -> Settings:
        settings = apply_shadow_overrides(self._live_settings(), json.loads(row.overrides or "{}"))
        settings.model.model_id = row.model
        settings.model.prompt_version = row.prompt_version
        settings.model.daily_call_budget = row.daily_call_limit
        settings.execution_mode = "auto"  # shadows always auto-fill
        return settings

    def _build(self, session: Session, row: ShadowRow, account: AccountRow) -> _Shadow:
        settings = self._shadow_settings(session, row)
        # The shadow's own fee/slippage overrides drive its fills.
        broker = PaperBroker(account, settings.paper)
        broker.load_positions(session)

        def risk_view(s: Session, sh_row: ShadowRow = row, brk: PaperBroker = broker) -> RiskView:
            live = self._live_settings()
            streak = repo.losing_streak(s, brk.account_id)
            throttled = streak >= live.risk.throttle_consecutive_losses
            return RiskView(
                throttle_on=throttled,
                correlated_slots_left=max(
                    0,
                    settings.risk.correlated_max_positions - len(brk.positions),
                ),
                shock_brake_on=False,
            )

        decider = OpenAIDecider(
            session_factory=self._sessions,
            market_client=self.client,
            broker=lambda: broker,
            risk_view=risk_view,
            clock=self.clock,
            feature=f"shadow:{row.id}",
            notify=self._notify,
            market_rules=self.market_rules,
        )
        return _Shadow(row=row, account=account, broker=broker, settings=settings, decider=decider)

    # --------------------------------------------------------------- cycle

    async def run_cycle(self, now: datetime, cycle: int) -> None:
        """After the live pairs have decided: every active shadow decides on
        its own account, then gets an equity snapshot for Compare.

        Runs on its own session — a shadow failure can never roll back the
        live cycle's staged work."""
        live = self._live_settings()
        session = self._sessions()
        try:
            for shadow in self._shadows(session):
                try:
                    # The cap is re-read per shadow: an earlier shadow's
                    # calls this cycle may have pushed spend past 80%.
                    cap = budget.cap_state(
                        budget.month_spend_all(session, now),
                        live.model.monthly_cost_cap_eur,
                    )
                    self._tick_pause(session, shadow, now, cap)
                    if not shadow.paused:
                        for pair in shadow.settings.active_markets:
                            try:
                                await self._decide_pair(session, shadow, pair, now, cycle)
                            except Exception:
                                log.exception("shadow %s: %s decide failed", shadow.row.id, pair)
                        repo.add_equity_snapshot(
                            session,
                            shadow.account.id,
                            cycle,
                            shadow.broker.equity(),
                            shadow.broker.cash,
                            at=now,
                        )
                        shadow.broker.persist(session, now)
                    session.commit()
                except Exception:
                    session.rollback()
                    log.exception("shadow %s cycle failed", shadow.row.id)
        except Exception:
            session.rollback()
            log.exception("shadow cycle pass failed")
        finally:
            session.close()

    def _tick_pause(self, session: Session, shadow: _Shadow, now: datetime, cap: str) -> None:
        """Open/close the stored pause window at the 80% boundary."""
        pauses = json.loads(shadow.row.pauses or "[]")
        should_pause = cap != "ok"
        if should_pause and not shadow.paused:
            pauses.append({"start": now.isoformat(), "end": None})
            shadow.row.pauses = json.dumps(pauses)
            repo.add_activity(
                session,
                "shadow",
                f"{shadow.row.name} paused: model spend passed 80% of the monthly cap",
                at=now,
            )
        elif not should_pause and shadow.paused:
            pauses[-1]["end"] = now.isoformat()
            shadow.row.pauses = json.dumps(pauses)
            repo.add_activity(session, "shadow", f"{shadow.row.name} resumed (new month)", at=now)

    async def _decide_pair(
        self, session: Session, shadow: _Shadow, pair: str, now: datetime, cycle: int
    ) -> None:
        settings = shadow.settings
        # An unfilled limit entry dies at its pair's next cycle.
        self._cancel_entry_orders(session, shadow, now, "next cycle", pair=pair)
        candles: dict[str, list[Candle]] = {
            tf: drop_forming(repo.load_candles(session, pair, tf, 900, end=now), tf, now)
            for tf in TF_KEYS
        }
        if not candles["1h"] or not candles["4h"] or not candles["1d"]:
            self._record(
                session,
                shadow,
                pair,
                now,
                cycle,
                Draft(
                    action="HOLD",
                    confidence=0.0,
                    reasoning="stale or missing data; holding",
                    signals=[],
                ),
                error="stale data",
            )
            return
        price = candles["1h"][-1].close
        ctx_specs: tuple[tuple[str, str, Tf], ...] = (
            ("btc_1h", "BTC-EUR", "1h"),
            ("btc_4h", "BTC-EUR", "4h"),
            ("btc_1d", "BTC-EUR", "1d"),
            ("usdc_1h", "USDC-EUR", "1h"),
        )
        ctx_candles = {
            name: drop_forming(repo.load_candles(session, mkt, ctx_tf, 900, end=now), ctx_tf, now)
            for name, mkt, ctx_tf in ctx_specs
        }
        try:
            signals = compute_pair_signals(
                pair,
                candles["1h"],
                candles["4h"],
                candles["1d"],
                btc_1h=ctx_candles["btc_1h"],
                usdc_1h=ctx_candles["usdc_1h"],
                settings=settings.market,
                now=now,
            )
        except Exception:
            log.exception("shadow %s: signals failed for %s", shadow.row.id, pair)
            return
        btc_regime = None
        try:
            btc_regime = compute_pair_signals(
                "BTC-EUR",
                ctx_candles["btc_1h"],
                ctx_candles["btc_4h"],
                ctx_candles["btc_1d"],
                settings=settings.market,
                now=now,
            ).regime
        except Exception:
            pass
        prev_4h = compute_tf_values(candles["4h"][:-1], "4h") if len(candles["4h"]) > 1 else None
        pos = shadow.broker.positions.get(pair)
        pause = repo.active_entry_pause(session, pair, now)
        ctx = DecideContext(
            pair=pair,
            now=now,
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
            btc_regime=btc_regime,
            settings=settings,
            cycle=cycle,
            position=pos,
            context_candles=ctx_candles,
            entries_paused=pause is not None or not entries_allowed(session, pair),
        )
        session.commit()
        # The shadow's own daily call cap — beyond it the Rules baseline
        # answers, like the live daily budget.
        used = repo.shadow_calls_today(session, shadow.account.id, shadow.row.id, now)
        if used >= shadow.row.daily_call_limit:
            draft = await shadow.decider.rules.decide(ctx)
            draft.reasoning = (
                f"Shadow daily call limit {used}/{shadow.row.daily_call_limit} "
                f"reached; Rules answered. {draft.reasoning}"
            )
        else:
            draft = await shadow.decider.decide(ctx)
        await self._execute(
            session, shadow, pair, draft, signals, candles, price, now, cycle, btc_regime
        )

    async def _execute(
        self,
        session: Session,
        shadow: _Shadow,
        pair: str,
        draft: Draft,
        signals: Any,
        candles: dict[str, list[Candle]],
        price: Decimal,
        now: datetime,
        cycle: int,
        btc_regime: Any,
    ) -> None:
        broker = shadow.broker
        settings = shadow.settings
        gates = []
        plan = None
        error = None
        action = draft.action
        filled_buy = False
        filled_sell = False
        decision_id = uuid.uuid4().hex[:16]

        if draft.action == "BUY":
            limit_price: Decimal | None = None
            if settings.paper.entry_orders == "limit":
                # Post-only: the decision's entry moved to the bid when it
                # would cross; gates and sizing run at the placed limit.
                limit_price = await self._post_only_price(pair, draft.entry or price)
                if limit_price is None:
                    action = "SKIP"
                    error = "order book unavailable"
            if action != "SKIP":
                correlated = self._correlated(session, shadow, pair, candles["1h"], now)
                pause = repo.active_entry_pause(session, pair, now)
                atr = Decimal(str(signals.tf["1h"].atr14)) if signals.tf["1h"].atr14 else None
                verdict = check_buy(
                    BuyContext(
                        pair=pair,
                        now=now,
                        entry=(limit_price if limit_price is not None else (draft.entry or price)),
                        stop=draft.stop or ZERO,
                        target=draft.target or ZERO,
                        atr_1h=atr,
                        has_position=pair in broker.positions,
                        last_stop_out_at=self._last_stop_out(session, shadow, pair),
                        correlated_pairs=correlated,
                        daily_loss_hit=(
                            broker.today_loss_pct(now) >= settings.risk.daily_loss_limit_pct
                        ),
                        risk_pct=self._risk_pct(session, shadow),
                        entry_paused_until=(
                            repo.aware_utc(pause.until)
                            if pause is not None and pause.until
                            else None
                        ),
                        entry_paused_indefinite=pause is not None and pause.until is None,
                        entries_allowed=entries_allowed(session, pair),
                    ),
                    broker,
                    self.market_rules.get(pair),
                    settings,
                )
                gates = verdict.gates
                if verdict.outcome == "skip":
                    action = "SKIP"
                    error = verdict.skip_reason
                else:
                    plan = verdict.plan
                    assert plan is not None
                    if limit_price is not None:
                        # Resting order — the stored plan is what the
                        # fill-time recheck re-runs against.
                        self._place_entry_order(session, shadow, pair, decision_id, plan, now)
                    else:
                        broker.buy(
                            session,
                            pair,
                            price,
                            plan.qty,
                            plan.stop,
                            plan.target,
                            decision_id,
                            now,
                            invalidation_price=draft.invalidation_price,
                        )
                        repo.add_exit_tick(session, broker.account_id, pair, price, "fill", at=now)
                        repo.add_activity(
                            session,
                            "fill",
                            f"[{shadow.row.name}] BUY {pair} qty {plan.qty} @ "
                            f"{broker.positions[pair].entry}",
                            pair=pair,
                            at=now,
                        )
                        filled_buy = True
        elif draft.action == "SELL":
            if pair in broker.positions:
                await self._close_shadow_position(session, shadow, pair, price, now, "model_sell")
                filled_sell = pair not in broker.positions
        pos = broker.positions.get(pair)
        if draft.action != "BUY" and pos is not None and draft.invalidation_price is not None:
            pos.invalidation_price = draft.invalidation_price
        self._record(
            session,
            shadow,
            pair,
            now,
            cycle,
            draft,
            gates=gates,
            action=action,
            status="executed" if filled_buy or filled_sell else "none",
            plan=plan,
            error=error,
            decision_id=decision_id,
            signals=signals,
            btc_regime=btc_regime,
        )

    def _record(
        self,
        session: Session,
        shadow: _Shadow,
        pair: str,
        now: datetime,
        cycle: int,
        draft: Draft,
        *,
        gates: list[Gate] | None = None,
        action: str | None = None,
        status: str = "none",
        plan: Any = None,
        error: str | None = None,
        decision_id: str | None = None,
        signals: Any = None,
        btc_regime: Any = None,
    ) -> None:
        regime = (
            signals.regime
            if signals is not None
            else Regime(trend="sideways", volatility="normal", age_4h=0)
        )
        decision = Decision(
            id=decision_id or uuid.uuid4().hex[:16],
            cycle=cycle,
            at=now,
            pair=pair,
            decider="openai" if draft.decider == "openai" else "rules",
            model=draft.model,
            prompt_version=draft.prompt_version,
            action=action or draft.action,
            confidence=draft.confidence,
            reasoning=draft.reasoning,
            invalidation=draft.invalidation,
            signals=decision_signals(draft.signals, signals),
            plan=plan,
            gates=gates or [],
            status=status,
            execute_at=None,
            error=error or draft.error,
            regime=regime,
            btc_regime=btc_regime or Regime(trend="sideways", volatility="normal", age_4h=0),
            invalidation_price=draft.invalidation_price,
            risk_pct_used=self._risk_pct(session, shadow),
            account_id=shadow.account.id,
            settings_version=repo.settings_version(session),
            shadow_id=shadow.row.id,
            stand_in=False,
            prefilter=draft.prefilter,
        )
        repo.save_decision(
            session, decision, raw_brief=draft.raw_brief, raw_response=draft.raw_response
        )
        if draft.llm_call_id:
            call_row = repo.get_llm_call(session, draft.llm_call_id)
            if call_row is not None:
                call_row.decision_id = decision.id
        session.flush()

    # ------------------------------------------------------ limit orders ==

    async def _post_only_price(self, pair: str, entry: Decimal) -> Decimal | None:
        """The limit a post-only buy rests at — None when the book can't
        be read: any failure means no order is placed."""
        try:
            book = await self.client.order_book(pair, 1)
        except Exception:
            return None
        if book.best_bid is None or book.best_ask is None:
            return None
        return post_only_limit(entry, book.best_bid, book.best_ask)

    def _tick_size(self, pair: str) -> Decimal | None:
        info = self.market_rules.get(pair)
        return info.tick_size if info is not None else None

    def _place_entry_order(
        self,
        session: Session,
        shadow: _Shadow,
        pair: str,
        decision_id: str,
        plan: Any,
        now: datetime,
    ) -> PaperOrderRow:
        order = repo.add_paper_order(
            session,
            PaperOrderRow(
                decision_id=decision_id,
                account_id=shadow.broker.account_id,
                pair=pair,
                side="buy",
                leg="entry",
                limit_price=str(plan.fill_entry),
                qty=str(plan.qty),
                placed_at=now.replace(tzinfo=None),
                expires_at=(now + timedelta(seconds=CYCLE_S)).replace(tzinfo=None),
            ),
        )
        repo.add_activity(
            session,
            "order",
            f"[{shadow.row.name}] BUY {pair} limit {plan.fill_entry} placed (post-only)",
            pair=pair,
            decision_id=decision_id,
            at=now,
        )
        return order

    def _place_target_order(
        self, session: Session, shadow: _Shadow, pair: str, pos: HeldPosition, now: datetime
    ) -> PaperOrderRow:
        return repo.add_paper_order(
            session,
            PaperOrderRow(
                decision_id=pos.decision_id,
                account_id=shadow.broker.account_id,
                pair=pair,
                side="sell",
                leg="target",
                limit_price=str(pos.target),
                qty=str(pos.qty),
                placed_at=now.replace(tzinfo=None),
                expires_at=None,
            ),
        )

    def _cancel_order(
        self, session: Session, shadow: _Shadow, order: PaperOrderRow, now: datetime, reason: str
    ) -> None:
        repo.cancel_paper_order(session, order, now, reason)
        repo.add_activity(
            session,
            "order",
            f"[{shadow.row.name}] {order.pair} limit {order.limit_price} cancelled: {reason}",
            pair=order.pair,
            decision_id=order.decision_id,
            at=now,
        )
        if order.leg != "entry":
            return
        row = repo.get_decision_row(session, order.decision_id)
        if row is not None and row.status in ("none", "approved"):
            row.status = "unfilled"

    def _cancel_entry_orders(
        self,
        session: Session,
        shadow: _Shadow,
        now: datetime,
        reason: str,
        *,
        pair: str | None = None,
    ) -> int:
        count = 0
        for order in repo.open_paper_orders(session, shadow.broker.account_id, pair, leg="entry"):
            self._cancel_order(session, shadow, order, now, reason)
            count += 1
        return count

    def _entry_block_reason(
        self, session: Session, shadow: _Shadow, pair: str, now: datetime
    ) -> str | None:
        """The entry-blocking gates a shadow honours: entry pause and the
        daily loss limit (the shock brake is a live-engine concept)."""
        if repo.active_entry_pause(session, pair, now) is not None:
            return "entry pause"
        if shadow.broker.today_loss_pct(now) >= shadow.settings.risk.daily_loss_limit_pct:
            return "daily loss limit"
        return None

    async def _fill_entry_order(
        self, session: Session, shadow: _Shadow, order: PaperOrderRow, price: Decimal, now: datetime
    ) -> bool:
        """Trade-through: recheck every gate at the limit price, then fill
        at the exact limit or cancel."""
        broker = shadow.broker
        settings = shadow.settings
        row = repo.get_decision_row(session, order.decision_id)
        if row is None or row.plan is None:
            self._cancel_order(session, shadow, order, now, "missing plan")
            return False
        plan = Plan.model_validate_json(row.plan)
        candles_1h = drop_forming(
            repo.load_candles(session, order.pair, "1h", CORR_BARS, end=now), "1h", now
        )
        pause = repo.active_entry_pause(session, order.pair, now)
        # The order was placed post-only — recheck in limit mode even if
        # the shadow's overrides have since changed.
        gate_settings = settings.model_copy(
            update={"paper": settings.paper.model_copy(update={"entry_orders": "limit"})}
        )
        verdict = check_buy(
            BuyContext(
                pair=order.pair,
                now=now,
                entry=Decimal(order.limit_price),
                stop=plan.stop,
                target=plan.target,
                atr_1h=self._atr_1h(session, order.pair, now),
                has_position=order.pair in broker.positions,
                last_stop_out_at=self._last_stop_out(session, shadow, order.pair),
                correlated_pairs=self._correlated(session, shadow, order.pair, candles_1h, now),
                daily_loss_hit=(broker.today_loss_pct(now) >= settings.risk.daily_loss_limit_pct),
                risk_pct=self._risk_pct(session, shadow),
                entry_paused_until=(
                    repo.aware_utc(pause.until) if pause is not None and pause.until else None
                ),
                entry_paused_indefinite=pause is not None and pause.until is None,
                entries_allowed=entries_allowed(session, order.pair),
            ),
            broker,
            self.market_rules.get(order.pair),
            gate_settings,
        )
        if verdict.outcome == "skip" or verdict.plan is None:
            self._cancel_order(session, shadow, order, now, verdict.skip_reason or "gate")
            return False
        fill_plan = verdict.plan
        pos = broker.buy(
            session,
            order.pair,
            Decimal(order.limit_price),
            fill_plan.qty,
            fill_plan.stop,
            fill_plan.target,
            order.decision_id,
            now,
            invalidation_price=(
                Decimal(row.invalidation_price) if row.invalidation_price else None
            ),
            order_type="limit",
            last_price=price,
        )
        repo.fill_paper_order(session, order, now, fill_plan.qty)
        self._place_target_order(session, shadow, order.pair, pos, now)
        row.plan = fill_plan.model_dump_json()
        row.status = "executed"
        row.execute_at = now.replace(tzinfo=None)
        repo.add_exit_tick(session, broker.account_id, order.pair, price, "fill", at=now)
        repo.add_activity(
            session,
            "fill",
            f"[{shadow.row.name}] BUY {order.pair} qty {fill_plan.qty} @ {pos.entry} (limit)",
            pair=order.pair,
            decision_id=order.decision_id,
            at=now,
        )
        return True

    async def _tick_entry_orders(
        self, session: Session, shadow: _Shadow, now: datetime
    ) -> set[str]:
        filled: set[str] = set()
        orders = repo.open_paper_orders(session, shadow.broker.account_id, leg="entry")
        if shadow.settings.paper.entry_orders != "limit":
            for order in orders:
                self._cancel_order(session, shadow, order, now, "switched to market")
            return filled
        for order in orders:
            price = await self._price_fn(order.pair)
            if price <= 0:
                continue
            if order.min_price is None or price < Decimal(order.min_price):
                order.min_price = str(price)
            block = self._entry_block_reason(session, shadow, order.pair, now)
            if block is not None or (
                order.expires_at is not None and repo.aware_utc(order.expires_at) <= now
            ):
                self._cancel_order(session, shadow, order, now, block or "next cycle")
                continue
            if not buy_fills(price, Decimal(order.limit_price), self._tick_size(order.pair)):
                continue
            if await self._fill_entry_order(session, shadow, order, price, now):
                filled.add(order.pair)
        return filled

    async def _close_shadow_position(
        self,
        session: Session,
        shadow: _Shadow,
        pair: str,
        price: Decimal,
        now: datetime,
        reason: str,
        *,
        order_type: Literal["market", "limit"] = "market",
        filled_order: PaperOrderRow | None = None,
    ) -> None:
        for order in repo.open_paper_orders(session, shadow.broker.account_id, pair):
            if filled_order is not None and order.id == filled_order.id:
                repo.fill_paper_order(session, order, now)
            else:
                repo.cancel_paper_order(session, order, now, f"exit: {reason}")
        trade = shadow.broker.close(session, pair, price, now, reason, order_type=order_type)
        if trade is not None:
            repo.add_exit_tick(session, shadow.broker.account_id, pair, price, "fill", at=now)
            repo.add_activity(
                session,
                "exit",
                f"[{shadow.row.name}] {reason} {pair} @ {price} realised {trade.realised_eur} EUR",
                pair=pair,
                at=now,
            )

    # -------------------------------------------------------------- watcher

    async def tick(self, now: datetime) -> None:
        """The 30-second watcher pass: resting limit entries fill or
        cancel; stops, targets, breakeven, trailing and the time stop keep
        working during a pause.

        Runs on its own session with per-shadow commits — a shadow failure
        can never roll back the live watcher's staged work."""
        session = self._sessions()
        try:
            for shadow in self._shadows(session):
                try:
                    filled = await self._tick_entry_orders(session, shadow, now)
                    for pair, pos in list(shadow.broker.positions.items()):
                        if pair in filled:
                            # A just-filled entry's exits start next tick.
                            continue
                        price = await self._price_fn(pair)
                        if price <= 0:
                            continue
                        shadow.broker.mark(pair, price, now)
                        last_1h = drop_forming(repo.load_candles(session, pair, "1h", 2), "1h", now)
                        if last_1h:
                            update_trail(pos, last_1h[-1].close)
                        atr = self._atr_1h(session, pair, now)
                        # Target style follows the position's entry leg,
                        # not the shadow's current setting.
                        limit_targets = pos.entry_maker
                        target_order = next(
                            (
                                o
                                for o in repo.open_paper_orders(
                                    session, shadow.broker.account_id, pair, leg="target"
                                )
                            ),
                            None,
                        )
                        if not limit_targets and target_order is not None:
                            self._cancel_order(
                                session, shadow, target_order, now, "market position"
                            )
                            target_order = None
                        reason = check_exit(
                            pos, price, atr, now, shadow.settings, target_is_limit=limit_targets
                        )
                        if reason is not None:
                            # Stops, time stops and forced exits stay market.
                            await self._close_shadow_position(
                                session, shadow, pair, price, now, reason
                            )
                            continue
                        if limit_targets:
                            if sell_fills(price, pos.target, self._tick_size(pair)):
                                await self._close_shadow_position(
                                    session,
                                    shadow,
                                    pair,
                                    pos.target,
                                    now,
                                    "target",
                                    order_type="limit",
                                    filled_order=target_order,
                                )
                                continue
                            if target_order is None:
                                target_order = self._place_target_order(
                                    session, shadow, pair, pos, now
                                )
                        repo.save_position_row(
                            session,
                            pos.to_model(),
                            shadow.broker.account_id,
                            pos.invalidation_price,
                            pos.highest_close,
                            pos.r_unit,
                            pos.original_stop,
                            pos.entry_maker,
                        )
                        if self._near_level(pos, price):
                            repo.add_exit_tick(
                                session, shadow.broker.account_id, pair, price, "watch", at=now
                            )
                    shadow.broker.persist(session, now)
                    session.commit()
                except Exception:
                    session.rollback()
                    log.exception("shadow %s watcher pass failed", shadow.row.id)
        except Exception:
            session.rollback()
            log.exception("shadow watcher pass failed")
        finally:
            session.close()

    def _near_level(self, pos: HeldPosition, price: Decimal) -> bool:
        for level in (pos.stop, pos.target):
            if level > 0 and abs(price - level) / level * 100 <= Decimal("0.5"):
                return True
        return False

    def _atr_1h(self, session: Session, pair: str, now: datetime) -> Decimal | None:
        candles = drop_forming(repo.load_candles(session, pair, "1h", 60), "1h", now)
        if len(candles) < 16:
            return None
        values = compute_tf_values(candles, "1h")
        return Decimal(str(values.atr14)) if values.atr14 else None

    # ------------------------------------------------------------------ misc

    def _correlated(
        self, session: Session, shadow: _Shadow, pair: str, candles_1h: list[Candle], now: datetime
    ) -> list[str]:
        out = []
        for other in shadow.broker.positions:
            if other == pair:
                continue
            other_candles = drop_forming(
                repo.load_candles(session, other, "1h", CORR_BARS), "1h", now
            )
            corr = aligned_correlation(candles_1h, other_candles, CORR_BARS)
            if corr is not None and corr > float(shadow.settings.risk.correlated_threshold):
                out.append(other)
        return out

    def _risk_pct(self, session: Session, shadow: _Shadow) -> Decimal:
        streak = repo.losing_streak(session, shadow.broker.account_id)
        dd = shadow.broker.drawdown_pct()
        if dd > shadow.settings.risk.throttle_drawdown_pct:
            shadow.throttle_dd = True
        elif dd <= shadow.settings.risk.throttle_lift_drawdown_pct:
            shadow.throttle_dd = False
        throttled = streak >= shadow.settings.risk.throttle_consecutive_losses or shadow.throttle_dd
        return (
            shadow.settings.risk.throttled_risk_per_trade_pct
            if throttled
            else shadow.settings.risk.risk_per_trade_pct
        )

    def _last_stop_out(self, session: Session, shadow: _Shadow, pair: str) -> datetime | None:
        trade = repo.last_exit_for_pair(session, shadow.broker.account_id, pair, "stop")
        return repo.aware_utc(trade.closed_at) if trade else None


# ------------------------------------------------------------ api helpers --


def create_shadow(
    session: Session,
    *,
    live_account: AccountRow,
    live_equity: Decimal,
    name: str | None,
    model: str,
    prompt_version: str,
    overrides: dict[str, object],
    daily_call_limit: int,
    now: datetime,
) -> ShadowRow:
    """A new shadow starts flat with cash equal to live equity at creation."""
    if repo.count_active_shadows(session) >= MAX_ACTIVE:
        raise ValueError("two shadow strategies may run alongside")
    # The name defaults to Shadow A / Shadow B by creation order.
    taken = {s.name for s in repo.list_shadows(session)}
    if name is None:
        for cand in ("Shadow A", "Shadow B", "Shadow C", "Shadow D"):
            if cand not in taken:
                name = cand
                break
        else:
            name = f"Shadow {len(taken) + 1}"
    account = AccountRow(
        started_at=repo.aware_utc(now).replace(tzinfo=None),
        start_balance=str(live_equity),
        cash=str(live_equity),
        peak_equity=str(live_equity),
        day_start_equity=str(live_equity),
        day_start_date=repo.aware_utc(now).astimezone(AMSTERDAM).date().isoformat(),
        active=False,
    )
    session.add(account)
    session.flush()
    row = ShadowRow(
        id=uuid.uuid4().hex[:12],
        name=name,
        model=model,
        prompt_version=prompt_version,
        overrides=json.dumps(overrides, separators=(",", ":")),
        status="running",
        account_id=account.id,
        daily_call_limit=daily_call_limit,
        start_equity=str(live_equity),
        pauses="[]",
        created_at=repo.aware_utc(now).replace(tzinfo=None),
        stopped_at=None,
    )
    session.add(row)
    session.flush()
    repo.add_activity(
        session, "shadow", f"{name} created (model {model}, prompt {prompt_version})", at=now
    )
    return row


def stop_shadow(session: Session, row: ShadowRow, now: datetime) -> None:
    row.status = "stopped"
    row.stopped_at = repo.aware_utc(now).replace(tzinfo=None)
    pauses = json.loads(row.pauses or "[]")
    if pauses and pauses[-1].get("end") is None:
        pauses[-1]["end"] = now.isoformat()
        row.pauses = json.dumps(pauses)
    repo.add_activity(session, "shadow", f"{row.name} stopped", at=now)


def shadow_out(session: Session, row: ShadowRow, live_account_id: int, now: datetime) -> ShadowOut:
    """Compare: returns since creation, pause windows excluded on both
    sides — a paused stretch simply doesn't count for either book."""
    pauses = [
        ShadowPause(
            start=_utc(datetime.fromisoformat(p["start"])),
            end=_utc(datetime.fromisoformat(p["end"])) if p.get("end") else None,
        )
        for p in json.loads(row.pauses or "[]")
    ]
    created = repo.aware_utc(row.created_at)
    segments = _unpaused_segments(
        created, repo.aware_utc(row.stopped_at) if row.stopped_at else now, pauses
    )
    start_eq = Decimal(row.start_equity)
    shadow_ret = _return_over(session, row.account_id, segments, start_eq)
    live_ret = _return_over(session, live_account_id, segments, None)
    equity_row = session.scalar(
        select(EquitySnapshotRow.equity)
        .where(EquitySnapshotRow.account_id == row.account_id)
        .order_by(EquitySnapshotRow.at.desc())
        .limit(1)
    )
    decisions = (
        session.scalar(
            select(func.count()).select_from(DecisionRow).where(DecisionRow.shadow_id == row.id)
        )
        or 0
    )
    overrides = json.loads(row.overrides or "{}")
    placed, filled = repo.count_paper_orders(session, row.account_id, leg="entry")
    return ShadowOut(
        id=row.id,
        name=row.name,
        model=row.model,
        prompt_version=row.prompt_version,
        overrides=overrides,
        status=row.status,
        paused=bool(pauses) and pauses[-1].end is None,
        pauses=pauses,
        created_at=created,
        stopped_at=repo.aware_utc(row.stopped_at) if row.stopped_at else None,
        account_id=row.account_id,
        start_equity=start_eq,
        equity=Decimal(equity_row) if equity_row else start_eq,
        return_pct=shadow_ret,
        live_return_pct=live_ret,
        decisions=decisions,
        daily_call_limit=row.daily_call_limit,
        calls_today=repo.shadow_calls_today(session, row.account_id, row.id, now),
        entry_orders="limit" if overrides.get("paper.entry_orders") == "limit" else "market",
        orders_placed=placed,
        orders_filled=filled,
        missed_fill_avg_r=_missed_fill_avg_r(session, row.account_id),
    )


def _missed_fill_avg_r(session: Session, account_id: int) -> Decimal | None:
    """The average R the unfilled limit entries would have made — first
    touch of the planned target/stop, else the 72h forward move in R —
    over the latest computed outcome per cancelled entry."""
    rows = session.scalars(
        select(DecisionRow).where(
            DecisionRow.account_id == account_id,
            DecisionRow.status == "unfilled",
            DecisionRow.action == "BUY",
        )
    ).all()
    rs: list[Decimal] = []
    for row in rows:
        if not row.plan:
            continue
        plan = Plan.model_validate_json(row.plan)
        r_dist = plan.fill_entry - plan.stop
        if r_dist <= 0:
            continue
        outcome = session.scalar(
            select(OutcomeRow)
            .where(OutcomeRow.decision_id == row.id)
            .order_by(OutcomeRow.horizon.desc())
            .limit(1)
        )
        if outcome is None:
            continue
        if outcome.first_touch == "target":
            rs.append((plan.target - plan.fill_entry) / r_dist)
        elif outcome.first_touch == "stop":
            rs.append(Decimal("-1"))
        else:
            stop_pct = r_dist / plan.fill_entry * 100
            if stop_pct > 0:
                rs.append(Decimal(outcome.forward_return_pct) / stop_pct)
    if not rs:
        return None
    return sum(rs, Decimal("0")) / Decimal(len(rs))


def _unpaused_segments(
    start: datetime, end: datetime, pauses: list[ShadowPause]
) -> list[tuple[datetime, datetime]]:
    """[start, end] minus the stored pause windows."""
    segments: list[tuple[datetime, datetime]] = []
    cursor = start
    for pause in sorted(pauses, key=lambda p: p.start):
        p_end = pause.end or end
        if pause.start > cursor:
            segments.append((cursor, min(pause.start, end)))
        cursor = max(cursor, p_end)
    if cursor < end:
        segments.append((cursor, end))
    return segments


def _equity_at(session: Session, account_id: int, at: datetime) -> Decimal | None:
    row = session.scalar(
        select(EquitySnapshotRow.equity)
        .where(
            EquitySnapshotRow.account_id == account_id,
            EquitySnapshotRow.at <= at.replace(tzinfo=None),
        )
        .order_by(EquitySnapshotRow.at.desc())
        .limit(1)
    )
    return Decimal(row) if row is not None else None


def _return_over(
    session: Session,
    account_id: int,
    segments: list[tuple[datetime, datetime]],
    start_fallback: Decimal | None,
) -> float | None:
    """Compounded return across the unpaused segments only."""
    if not segments:
        return None
    total = Decimal("1")
    for seg_start, seg_end in segments:
        e0 = _equity_at(session, account_id, seg_start) or start_fallback
        e1 = _equity_at(session, account_id, seg_end)
        if e0 is None or e0 <= 0:
            return None
        if e1 is None:
            e1 = e0
        total *= e1 / e0
    return float((total - 1) * 100)
