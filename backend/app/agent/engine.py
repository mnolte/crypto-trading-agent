"""The agent engine: the cycle scheduler and the 30-second exit watcher.

Cycles start ``cycle_offset_s`` seconds after each quarter hour so the
hourly candle has closed; up to 3 pairs run in parallel inside one cycle.
The exit watcher checks stops, targets, breakeven, trailing, the time stop,
the kill drawdown and the shock brake on the ticker every 30 seconds — it
keeps running while the agent is Paused, so a stop is never up to 15
minutes late.

Time is injected (``clock``/``sleep``) so engine tests run on a fake clock
against the simulated market.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.broker.paper import PaperBroker
from app.agent.deciders.base import DecideContext, Decider, Draft, RiskView
from app.agent.market.base import (
    Candle,
    MarketClient,
    MarketDataError,
    MarketInfo,
    drop_forming,
)
from app.agent.market.universe import warming_up
from app.agent.risk import BuyContext, check_buy, check_exit, update_trail
from app.agent.signals import (
    PairSignals,
    aligned_correlation,
    compute_pair_signals,
    compute_tf_values,
)
from app.db import AccountRow, DecisionRow, utcnow
from app.models import (
    Decision,
    Gate,
    KillInfo,
    PaperSettings,
    Plan,
    PriceInfo,
    Regime,
    RiskBudget,
    Settings,
    Tf,
    Throttle,
)

CYCLE_S = 900  # cycles are fixed at 15 minutes in v0.1
WATCH_S = 30  # the exit watcher interval, spec-fixed
STALE_RETRIES = 3
STALE_RETRY_S = 10
MAX_PARALLEL_PAIRS = 3
TF_LIMITS: dict[Tf, int] = {"1h": 800, "4h": 300, "1d": 300}
TF_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
SHOCK_WINDOW_S = 3600
EXIT_TICK_PCT = Decimal("0.5")  # watcher prices within 0.5% of stop/target
CORR_BARS = 720

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
Notifier = Callable[[str, str, str], Awaitable[None]]


def _stale_limit_s(tf: str, settings: Settings) -> int:
    """Freshness limit: one interval plus the configured grace."""
    return TF_SECONDS[tf] + settings.market.stale_grace_s


def candle_age_s(candle: Candle, tf: str, now: datetime) -> float:
    """Seconds since the candle closed (open timestamp + interval)."""
    return (now - (candle.open_time + timedelta(seconds=TF_SECONDS[tf]))).total_seconds()


class Engine:
    """Scheduler, cycle runner and exit watcher bound to one paper account."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        market_client: MarketClient,
        market_rules: dict[str, MarketInfo],
        settings: Callable[[], Settings],
        settings_version: Callable[[], int],
        broker: PaperBroker,
        decider: Decider,
        openai_decider: Decider | None = None,
        clock: Clock = utcnow,
        sleep: Sleeper = asyncio.sleep,
        broadcast: Callable[[], Awaitable[None]] | None = None,
        notify: Notifier | None = None,
    ) -> None:
        self._sessions = session_factory
        self.client = market_client
        self.market_rules = market_rules
        self._settings = settings
        self._settings_version = settings_version
        self.broker = broker
        self.decider = decider
        self.openai_decider = openai_decider
        self.clock = clock
        self._sleep = sleep
        self._broadcast = broadcast
        self._notify_event = notify

        self.status: Literal["running", "paused", "killed", "error"] = "paused"
        self.cycle = 0
        self.next_run_at: datetime | None = None
        self.cycle_in_progress = False
        self.kill: KillInfo | None = None
        self.shock_brake_until: datetime | None = None
        self.prices: dict[str, PriceInfo] = {}
        self.latest: dict[str, Decision] = {}
        self.pending: dict[str, Decision] = {}
        self.tasks: list[asyncio.Task[None]] = []

        self._btc_window: deque[tuple[datetime, Decimal]] = deque()
        self._stale_cycles: dict[str, int] = {}
        self._daily_loss_notified = False
        self._notified_throttle: tuple[str, ...] = ()
        self._throttle_dd = False
        self._cycle_lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    # ============================================================ lifecycle ==

    async def start(self) -> None:
        """Restore kill state, then start the scheduler and the watcher."""
        session = self._sessions()
        try:
            account = self.broker.account
            if account.kill_at is not None:
                self.status = "killed"
                self.kill = KillInfo(
                    at=account.kill_at,
                    peak=Decimal(account.kill_peak or "0"),
                    equity=Decimal(account.kill_equity or "0"),
                    drawdown_pct=self.broker.drawdown_pct(),
                    closed=json.loads(account.kill_closed or "[]"),
                )
            expired = repo.expire_pending(session, self.broker.account_id)
            if expired:
                repo.add_activity(
                    session,
                    "system",
                    f"{expired} pending decision(s) expired on restart",
                )
            session.commit()
        finally:
            session.close()
        self._stop.clear()
        self.tasks = [
            asyncio.create_task(self._scheduler_loop()),
            asyncio.create_task(self._watcher_loop()),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    # ------------------------------------------------------------- controls

    def pause(self) -> None:
        """Stop the scheduler. Exits keep being watched."""
        if self.status == "running":
            self.status = "paused"

    def resume(self) -> None:
        """Resume cycles; refused while Killed until the kill is acknowledged."""
        if self.status == "killed":
            raise KilledRefusal("agent is Killed; acknowledge the kill first")
        if self.status == "paused":
            self.status = "running"

    async def run_now(self) -> bool:
        """Start a cycle now if none is running. Refused while Killed: no
        new trades are possible until the kill is acknowledged."""
        if self.status == "killed":
            raise KilledRefusal("agent is Killed; acknowledge the kill first")
        if self.cycle_in_progress:
            return False
        await self.run_cycle()
        return True

    async def flatten(self) -> list[str]:
        """Close every position at market and pause the agent."""
        session = self._sessions()
        closed: list[str] = []
        now = self.clock()
        try:
            for pair in list(self.broker.positions):
                price = await self._price(pair)
                if price <= 0:
                    repo.add_activity(
                        session,
                        "error",
                        f"flatten skipped {pair}: no current price",
                        pair=pair,
                        at=now,
                    )
                    continue
                await self._close_position(session, pair, price, now, "flatten")
                closed.append(pair)
            self.pause()
            self.broker.persist(session, now)
            repo.add_activity(session, "exit", f"Stop & flatten: closed {len(closed)} position(s)")
            session.commit()
        finally:
            session.close()
        await self._push()
        return closed

    async def close_position(self, pair: str) -> bool:
        """Close one open position at market (the dashboard Close button)."""
        if pair not in self.broker.positions:
            return False
        session = self._sessions()
        try:
            price = await self._price(pair)
            if price <= 0:
                raise MarketDataError(f"no current price for {pair}")
            await self._close_position(session, pair, price, self.clock(), "manual")
            self.broker.persist(session, self.clock())
            session.commit()
        finally:
            session.close()
        await self._push()
        return True

    async def acknowledge_kill(self) -> None:
        """Confirm the kill: reset peak equity to current, move to Paused."""
        session = self._sessions()
        try:
            account = self.broker.account
            account.kill_at = None
            account.kill_peak = None
            account.kill_equity = None
            account.kill_closed = None
            self.broker.peak_equity = self.broker.equity()
            self.broker.persist(session, self.clock())
            self.kill = None
            self.status = "paused"
            repo.add_activity(session, "system", "Kill acknowledged; agent Paused")
            session.commit()
        finally:
            session.close()
        await self._push()

    def reset_runtime(self, account: AccountRow, paper: PaperSettings) -> None:
        """Fresh in-memory state after DELETE /api/data: new broker on the
        new account, all transient state cleared, agent paused."""
        self.broker = PaperBroker(account, paper)
        self.status = "paused"
        self.cycle = 0
        self.next_run_at = None
        self.kill = None
        self.shock_brake_until = None
        self.prices.clear()
        self.latest.clear()
        self.pending.clear()
        self._btc_window.clear()
        self._stale_cycles.clear()
        self._daily_loss_notified = False
        self._notified_throttle = ()
        self._throttle_dd = False

    # ================================================================= loops ==

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            if self.status != "running":
                await self._sleep(1)
                continue
            now = self.clock()
            due = self.next_run_at or self._next_due(now)
            # Woke more than one interval late: skip missed cycles, run fresh.
            if now > due + timedelta(seconds=CYCLE_S):
                due = now
            self.next_run_at = due
            if now < due:
                await self._sleep(min(1.0, max(0.0, (due - now).total_seconds())))
                continue
            await self.run_cycle()

    async def _watcher_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                # The watcher must never die on a bad tick.
                pass
            await self._sleep(WATCH_S)

    def _next_due(self, now: datetime) -> datetime:
        """Next quarter-hour boundary plus the configured cycle offset."""
        base = now.replace(second=0, microsecond=0)
        minute = (base.minute // 15) * 15
        base = base.replace(minute=minute)
        due = base + timedelta(seconds=self._settings().cycle_offset_s)
        while due <= now:
            due += timedelta(seconds=CYCLE_S)
        return due

    # ================================================================= cycle ==

    async def run_cycle(self) -> None:
        """One cycle: per pair gather data, decide, gate, execute, record."""
        async with self._cycle_lock:
            self.cycle_in_progress = True
            self.cycle += 1
            cycle_no = self.cycle
            now = self.clock()
            session = self._sessions()
            try:
                settings = self._settings()
                repo.add_activity(session, "cycle", f"cycle {cycle_no} started", at=now)
                session.commit()
                context = await self._load_context(session, settings, now)
                pairs = [
                    m
                    for m in settings.active_markets
                    if not warming_up(session, m, settings.market)
                ]
                sem = asyncio.Semaphore(MAX_PARALLEL_PAIRS)

                async def guarded(pair: str) -> None:
                    async with sem:
                        pair_session = self._sessions()
                        try:
                            await self._decide_pair(pair_session, pair, context, now)
                        finally:
                            pair_session.close()

                await asyncio.gather(*(guarded(p) for p in pairs))
                repo.add_equity_snapshot(
                    session,
                    self.broker.account_id,
                    cycle_no,
                    self.broker.equity(),
                    self.broker.cash,
                    at=now,
                )
                self.broker.persist(session, now)
                session.commit()
            finally:
                session.close()
                self.cycle_in_progress = False
                self.next_run_at = self._next_due(self.clock() + timedelta(seconds=1))
            await self._push()

    async def _load_context(
        self, session: Session, settings: Settings, now: datetime
    ) -> dict[str, list[Candle] | Regime | None]:
        """Market-wide context fetched once per cycle: BTC and USDC-EUR."""
        ctx: dict[str, list[Candle] | Regime | None] = {
            "btc_1h": None,
            "btc_4h": None,
            "btc_1d": None,
            "usdc_1h": None,
            "btc_regime": None,
        }
        try:
            btc_1h = drop_forming(
                await self.client.candles("BTC-EUR", "1h", TF_LIMITS["1h"]), "1h", now
            )
            btc_4h = drop_forming(
                await self.client.candles("BTC-EUR", "4h", TF_LIMITS["4h"]), "4h", now
            )
            btc_1d = drop_forming(
                await self.client.candles("BTC-EUR", "1d", TF_LIMITS["1d"]), "1d", now
            )
            repo.upsert_candles(session, "BTC-EUR", "1h", btc_1h)
            repo.upsert_candles(session, "BTC-EUR", "4h", btc_4h)
            repo.upsert_candles(session, "BTC-EUR", "1d", btc_1d)
            ctx["btc_1h"], ctx["btc_4h"], ctx["btc_1d"] = btc_1h, btc_4h, btc_1d
            btc_sig = compute_pair_signals(
                "BTC-EUR",
                btc_1h,
                btc_4h,
                btc_1d,
                settings=settings.market,
                now=now,
            )
            ctx["btc_regime"] = btc_sig.regime
        except MarketDataError:
            pass
        try:
            usdc = drop_forming(await self.client.candles("USDC-EUR", "1h", 48), "1h", now)
            repo.upsert_candles(session, "USDC-EUR", "1h", usdc)
            ctx["usdc_1h"] = usdc
        except MarketDataError:
            pass
        return ctx

    # ------------------------------------------------------------- per pair ==

    async def _fetch_closed(
        self, session: Session, pair: str, tf: Tf, now: datetime
    ) -> list[Candle]:
        """Closed candles for one timeframe, newest stored."""
        fetched = drop_forming(await self.client.candles(pair, tf, TF_LIMITS[tf]), tf, now)
        repo.upsert_candles(session, pair, tf, fetched)
        if fetched:
            return fetched
        return drop_forming(repo.load_candles(session, pair, tf, TF_LIMITS[tf]), tf, now)

    async def _decide_pair(
        self,
        session: Session,
        pair: str,
        context: dict[str, list[Candle] | Regime | None],
        now: datetime,
    ) -> Decision:
        """One pair's cycle: data, signals, decide, gates, execute, record."""
        settings = self._settings()
        self._expire_pending_for_pair(session, pair, now)
        btc_regime = cast("Regime | None", context.get("btc_regime"))

        candles: dict[str, list[Candle]] = {}
        try:
            candles["1h"] = await self._fresh_1h(session, pair, now)
            candles["4h"] = await self._fetch_closed(session, pair, "4h", now)
            candles["1d"] = await self._fetch_closed(session, pair, "1d", now)
        except MarketDataError:
            candles = {}

        if not candles.get("1h") or not candles.get("4h") or not candles.get("1d"):
            await self._note_stale_pair(pair)
            return self._record_decision(
                session,
                pair,
                now,
                Draft(
                    action="HOLD",
                    confidence=0.0,
                    reasoning="stale or missing data; holding",
                    signals=[],
                ),
                None,
                [],
                btc_regime=btc_regime,
                error="stale data",
            )
        stale_tf = next(
            (
                tf
                for tf in ("1h", "4h", "1d")
                if candle_age_s(candles[tf][-1], tf, now) > _stale_limit_s(tf, settings)
            ),
            None,
        )
        if stale_tf is not None:
            self._mark_stale(pair, candles["1h"][-1].close)
            await self._note_stale_pair(pair)
            return self._record_decision(
                session,
                pair,
                now,
                Draft(
                    action="HOLD",
                    confidence=0.0,
                    reasoning=f"{stale_tf} data stale; holding",
                    signals=[],
                ),
                None,
                [],
                btc_regime=btc_regime,
                error=f"{stale_tf} stale",
            )

        self._stale_cycles.pop(pair, None)
        price = candles["1h"][-1].close
        self._update_price(session, pair, price, candles["1h"], now)
        signals = compute_pair_signals(
            pair,
            candles["1h"],
            candles["4h"],
            candles["1d"],
            btc_1h=cast("list[Candle] | None", context.get("btc_1h")),
            usdc_1h=cast("list[Candle] | None", context.get("usdc_1h")),
            settings=settings.market,
            now=now,
        )
        await self._check_pair_shock(
            session,
            pair,
            candles["1h"][-1],
            Decimal(str(signals.tf["1h"].atr14)) if signals.tf["1h"].atr14 else None,
            now,
        )

        pos = self.broker.positions.get(pair)
        prev_4h = compute_tf_values(candles["4h"][:-1], "4h") if len(candles["4h"]) > 1 else None
        decider = (
            self.openai_decider
            if self.openai_decider is not None and settings.model.decider == "openai"
            else self.decider
        )
        draft = await decider.decide(
            DecideContext(
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
                btc_regime=context.get("btc_regime"),  # type: ignore[arg-type]
                settings=settings,
                cycle=self.cycle,
                position=pos,
                context_candles={k: v for k, v in context.items() if isinstance(v, list)},
            )
        )
        return await self._execute_draft(
            session,
            pair,
            draft,
            signals,
            candles,
            price,
            now,
            btc_regime=btc_regime,
        )

    async def _fresh_1h(self, session: Session, pair: str, now: datetime) -> list[Candle]:
        """1h candles with the stale retry: up to 3 retries 10 s apart when
        the newest closed bar is missing, then the pair is marked stale."""
        for attempt in range(STALE_RETRIES + 1):
            candles = await self._fetch_closed(session, pair, "1h", now)
            if candles and candle_age_s(candles[-1], "1h", now) <= TF_SECONDS["1h"]:
                return candles
            if candles and candle_age_s(candles[-1], "1h", now) <= _stale_limit_s(
                "1h", self._settings()
            ):
                return candles
            if attempt < STALE_RETRIES:
                await self._sleep(STALE_RETRY_S)
        return candles

    # -------------------------------------------------------------- execute ==

    async def _execute_draft(
        self,
        session: Session,
        pair: str,
        draft: Draft,
        signals: PairSignals,
        candles: dict[str, list[Candle]],
        price: Decimal,
        now: datetime,
        *,
        btc_regime: Regime | None = None,
    ) -> Decision:
        if draft.action == "BUY" and self._settings().execution_mode == "auto":
            async with self._execution_lock:
                return await self._execute_draft_unlocked(
                    session,
                    pair,
                    draft,
                    signals,
                    candles,
                    price,
                    now,
                    btc_regime=btc_regime,
                )
        return await self._execute_draft_unlocked(
            session,
            pair,
            draft,
            signals,
            candles,
            price,
            now,
            btc_regime=btc_regime,
        )

    async def _execute_draft_unlocked(
        self,
        session: Session,
        pair: str,
        draft: Draft,
        signals: PairSignals,
        candles: dict[str, list[Candle]],
        price: Decimal,
        now: datetime,
        *,
        btc_regime: Regime | None = None,
    ) -> Decision:
        settings = self._settings()
        gates: list[Gate] = []
        status: Literal["executed", "pending", "approved", "vetoed", "expired", "none"] = "none"
        plan: Plan | None = None
        error: str | None = None
        action = draft.action
        decision_id = uuid.uuid4().hex[:16]

        if draft.action == "BUY":
            correlated = await self._correlated_open(session, pair, candles["1h"])
            ctx = BuyContext(
                pair=pair,
                now=now,
                entry=draft.entry or price,
                stop=draft.stop or Decimal("0"),
                target=draft.target or Decimal("0"),
                atr_1h=Decimal(str(signals.tf["1h"].atr14)) if signals.tf["1h"].atr14 else None,
                has_position=pair in self.broker.positions,
                last_stop_out_at=self._last_stop_out(session, pair),
                correlated_pairs=correlated,
                shock_brake_until=self.shock_brake_until,
                daily_loss_hit=(
                    self.broker.today_loss_pct(now) >= settings.risk.daily_loss_limit_pct
                ),
                risk_pct=self._risk_pct(session),
            )
            verdict = check_buy(ctx, self.broker, self.market_rules.get(pair), settings)
            gates = verdict.gates
            if verdict.outcome == "skip":
                action = "SKIP"
                error = verdict.skip_reason
                repo.add_activity(
                    session,
                    "skip",
                    f"SKIP {pair}: {verdict.skip_reason}",
                    pair=pair,
                    at=now,
                )
            else:
                plan = verdict.plan
                if settings.execution_mode == "auto":
                    await self._fill_buy(session, pair, plan, draft, price, now, decision_id)
                    status = "executed"
                else:
                    status = "pending"
        elif draft.action == "SELL":
            if pair in self.broker.positions:
                await self._close_position(session, pair, price, now, "model_sell")
                status = "executed"
        # A non-BUY decision on a held pair arms the invalidation re-eval
        # at the price the decider set (crossing it triggers one model call).
        pos = self.broker.positions.get(pair)
        if draft.action != "BUY" and pos is not None and draft.invalidation_price is not None:
            pos.invalidation_price = draft.invalidation_price
            repo.save_position_row(
                session,
                pos.to_model(),
                self.broker.account_id,
                pos.invalidation_price,
                pos.highest_close,
                pos.r_unit,
                pos.original_stop,
            )
        return self._record_decision(
            session,
            pair,
            now,
            draft,
            signals,
            gates,
            action=action,
            status=status,
            plan=plan,
            error=error,
            decision_id=decision_id,
            btc_regime=btc_regime,
        )

    async def _fill_buy(
        self,
        session: Session,
        pair: str,
        plan: Plan | None,
        draft: Draft,
        price: Decimal,
        now: datetime,
        decision_id: str,
    ) -> None:
        assert plan is not None
        pos = self.broker.buy(
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
        repo.add_exit_tick(session, self.broker.account_id, pair, price, "fill", at=now)
        repo.add_activity(
            session,
            "fill",
            f"BUY {pair} qty {plan.qty} @ {pos.entry}",
            pair=pair,
            at=now,
        )
        await self._notify("trade", f"Trade opened · {pair}", f"BUY {plan.qty} @ {pos.entry}")

    async def _close_position(
        self, session: Session, pair: str, price: Decimal, now: datetime, reason: str
    ) -> None:
        if price <= 0:
            return
        trade = self.broker.close(session, pair, price, now, reason)
        if trade is not None:
            repo.add_exit_tick(session, self.broker.account_id, pair, price, "fill", at=now)
            repo.add_activity(
                session,
                "exit",
                f"{reason} {pair} @ {price} realised {trade.realised_eur} EUR",
                pair=pair,
                at=now,
            )
            await self._notify(
                "trade",
                f"Trade closed · {pair}",
                f"{reason} @ {trade.exit}; realised {trade.realised_eur} EUR",
            )

    # --------------------------------------------------------------- watcher ==

    async def tick(self) -> None:
        """One 30-second watcher pass: prices, exits, kill, shock brake."""
        now = self.clock()
        session = self._sessions()
        try:
            settings = self._settings()
            await self._tick_shock_btc(session, now, settings)
            await self._tick_positions(session, now, settings)
            await self._tick_pending(session, now)
            await self._tick_kill(session, now, settings)
            await self._tick_risk_notifications(session, now, settings)
            self.broker.persist(session, now)
            session.commit()
        finally:
            session.close()

    async def _price(self, pair: str) -> Decimal:
        try:
            price = await self.client.ticker_price(pair)
        except MarketDataError:
            price = Decimal("0")
        if price <= 0:
            info = self.prices.get(pair)
            price = info.last if info else Decimal("0")
        return price

    async def _tick_positions(self, session: Session, now: datetime, settings: Settings) -> None:
        for pair, pos in list(self.broker.positions.items()):
            price = await self._price(pair)
            if price <= 0:
                continue
            self.broker.mark(pair, price, now)
            self._store_price(pair, price, stale=False)
            # Trailing needs the newest stored closed 1h close.
            last_1h = repo.load_candles(session, pair, "1h", 2)
            last_1h = drop_forming(last_1h, "1h", now)
            if last_1h:
                update_trail(pos, last_1h[-1].close)
            atr = self._atr_1h(session, pair, now)
            reason = check_exit(pos, price, atr, now, settings)
            if reason is not None:
                await self._close_position(session, pair, price, now, reason)
                continue
            repo.save_position_row(
                session,
                pos.to_model(),
                self.broker.account_id,
                pos.invalidation_price,
                pos.highest_close,
                pos.r_unit,
                pos.original_stop,
            )
            if self._near_level(pos, price):
                repo.add_exit_tick(session, self.broker.account_id, pair, price, "watch", at=now)
            if pos.invalidation_price is not None and price <= pos.invalidation_price:
                # One-shot: crossing triggers an immediate re-evaluation,
                # then the stored price is cleared until a decision sets a
                # new one.
                pos.invalidation_price = None
                repo.add_activity(
                    session,
                    "decision",
                    f"invalidation price crossed on {pair}; re-evaluating",
                    pair=pair,
                    at=now,
                )
                await self._decide_pair(session, pair, {}, now)

    def _near_level(self, pos: object, price: Decimal) -> bool:
        """Whether the watch price is within EXIT_TICK_PCT % of stop or target."""
        p = pos  # HeldPosition
        for level in (p.stop, p.target):  # type: ignore[attr-defined]
            if level > 0 and abs(price - level) / level * 100 <= EXIT_TICK_PCT:
                return True
        return False

    def _atr_1h(self, session: Session, pair: str, now: datetime) -> Decimal | None:
        candles = drop_forming(repo.load_candles(session, pair, "1h", 60), "1h", now)
        if len(candles) < 16:
            return None
        values = compute_tf_values(candles, "1h")
        return Decimal(str(values.atr14)) if values.atr14 else None

    async def _tick_pending(self, session: Session, now: datetime) -> None:
        """Expire drifted approvals and execute elapsed supervised veto windows."""
        settings = self._settings()
        for row in repo.pending_decisions(session, self.broker.account_id):
            if row.plan is None:
                continue
            plan = Plan.model_validate_json(row.plan)
            price = await self._price(row.pair)
            if price <= 0 or plan.entry <= 0:
                continue
            drift = abs(price - plan.entry) / plan.entry * 100
            if drift > settings.risk.pending_entry_drift_pct:
                row.status = "expired"
                self.pending.pop(row.id, None)
                repo.add_activity(
                    session,
                    "decision",
                    f"pending {row.pair} expired: price drifted {drift:.2f}%",
                    pair=row.pair,
                    at=now,
                )
            elif (
                settings.execution_mode == "supervised"
                and row.execute_at is not None
                and repo.aware_utc(row.execute_at) <= now
            ):
                async with self._execution_lock:
                    await self._approve_pending(session, row.id, now)
                    self.broker.persist(session, now)
                    session.commit()

    async def approve_pending(self, decision_id: str) -> Decision:
        async with self._execution_lock:
            session = self._sessions()
            try:
                decision = await self._approve_pending(session, decision_id, self.clock())
                self.broker.persist(session, self.clock())
                session.commit()
            finally:
                session.close()
        await self._push()
        return decision

    async def _approve_pending(
        self, session: Session, decision_id: str, now: datetime, price: Decimal | None = None
    ) -> Decision:
        row = repo.get_decision_row(session, decision_id)
        if row is not None:
            session.refresh(row)
        if row is None or row.account_id != self.broker.account_id or row.status != "pending":
            raise PendingRefusal("decision is not pending")
        if row.plan is None:
            raise PendingRefusal("pending decision has no plan")
        old_plan = Plan.model_validate_json(row.plan)
        price = price if price is not None else await self._price(row.pair)
        if price <= 0:
            raise PendingRefusal("no current price")
        settings = self._settings()
        drift = abs(price - old_plan.entry) / old_plan.entry * 100
        if drift > settings.risk.pending_entry_drift_pct:
            row.status = "expired"
            self.pending.pop(row.id, None)
            session.commit()
            raise PendingRefusal("pending decision expired because price moved too far")
        candles = drop_forming(repo.load_candles(session, row.pair, "1h", CORR_BARS), "1h", now)
        atr_1h = self._atr_1h(session, row.pair, now)
        correlated = await self._correlated_open(session, row.pair, candles)
        verdict = check_buy(
            BuyContext(
                pair=row.pair,
                now=now,
                entry=price,
                stop=old_plan.stop,
                target=old_plan.target,
                atr_1h=atr_1h,
                has_position=row.pair in self.broker.positions,
                last_stop_out_at=self._last_stop_out(session, row.pair),
                correlated_pairs=correlated,
                shock_brake_until=self.shock_brake_until,
                daily_loss_hit=self.broker.today_loss_pct(now)
                >= settings.risk.daily_loss_limit_pct,
                risk_pct=self._risk_pct(session),
            ),
            self.broker,
            self.market_rules.get(row.pair),
            settings,
        )
        row.gates = json.dumps([gate.model_dump(mode="json") for gate in verdict.gates])
        if verdict.outcome == "skip" or verdict.plan is None:
            row.action = "SKIP"
            row.status = "expired"
            row.error = verdict.skip_reason
            self.pending.pop(row.id, None)
            repo.add_activity(
                session, "skip", f"SKIP {row.pair}: {verdict.skip_reason}", pair=row.pair, at=now
            )
        else:
            draft = Draft(
                action="BUY",
                confidence=row.confidence,
                reasoning=row.reasoning,
                signals=[],
                invalidation=row.invalidation,
                invalidation_price=Decimal(row.invalidation_price)
                if row.invalidation_price
                else None,
            )
            await self._fill_buy(session, row.pair, verdict.plan, draft, price, now, row.id)
            row.plan = verdict.plan.model_dump_json()
            row.status = "approved"
            row.execute_at = now.replace(tzinfo=None)
            self.pending.pop(row.id, None)
        decision = decision_from_row(row)
        self.latest[row.pair] = decision
        return decision

    async def veto_pending(self, decision_id: str) -> Decision:
        async with self._execution_lock:
            session = self._sessions()
            try:
                row = repo.get_decision_row(session, decision_id)
                if (
                    row is None
                    or row.account_id != self.broker.account_id
                    or row.status != "pending"
                ):
                    raise PendingRefusal("decision is not pending")
                row.status = "vetoed"
                row.execute_at = self.clock().replace(tzinfo=None)
                self.pending.pop(row.id, None)
                repo.add_activity(
                    session,
                    "decision",
                    f"pending {row.pair} vetoed",
                    pair=row.pair,
                    at=self.clock(),
                )
                session.commit()
                decision = decision_from_row(row)
                self.latest[row.pair] = decision
            finally:
                session.close()
        await self._push()
        return decision

    async def _tick_risk_notifications(
        self, session: Session, now: datetime, settings: Settings
    ) -> None:
        daily_loss_hit = self.broker.today_loss_pct(now) >= settings.risk.daily_loss_limit_pct
        if daily_loss_hit and not self._daily_loss_notified:
            await self._notify(
                "daily_loss",
                "Daily loss limit reached",
                f"Today loss reached {self.broker.today_loss_pct(now):.2f}%",
            )
        self._daily_loss_notified = daily_loss_hit
        reasons = tuple(self._risk_state(session)[1])
        if reasons != self._notified_throttle:
            if reasons:
                await self._notify(
                    "risk_control",
                    "Risk throttle on",
                    f"Active reasons: {', '.join(reasons)}",
                )
            elif self._notified_throttle:
                await self._notify("risk_control", "Risk throttle lifted", "Full risk restored")
            self._notified_throttle = reasons

    async def _tick_kill(self, session: Session, now: datetime, settings: Settings) -> None:
        if self.status == "killed":
            return
        if self.broker.drawdown_pct() >= settings.risk.kill_drawdown_pct:
            await self._fire_kill(session, now)

    async def _fire_kill(self, session: Session, now: datetime) -> None:
        """Kill drawdown: flatten all, Killed state, alert. Exits keep being
        watched; no new trades are possible until acknowledged."""
        closed: list[str] = []
        for pair in list(self.broker.positions):
            price = await self._price(pair)
            if price <= 0:
                repo.add_activity(
                    session,
                    "error",
                    f"kill close skipped {pair}: no current price",
                    pair=pair,
                    at=now,
                )
                continue
            await self._close_position(session, pair, price, now, "kill")
            closed.append(pair)
        self.status = "killed"
        account = self.broker.account
        account.kill_at = now
        account.kill_peak = str(self.broker.peak_equity)
        account.kill_equity = str(self.broker.equity())
        account.kill_closed = json.dumps(closed)
        self.kill = KillInfo(
            at=now,
            peak=self.broker.peak_equity,
            equity=self.broker.equity(),
            drawdown_pct=self.broker.drawdown_pct(),
            closed=closed,
        )
        message = f"Drawdown hit; flattened {len(closed)} position(s)"
        repo.add_activity(session, "alert", f"KILL SWITCH: {message}", at=now)
        await self._notify("kill_switch", "Kill switch fired", message)
        await self._push()

    async def _tick_shock_btc(self, session: Session, now: datetime, settings: Settings) -> None:
        """BTC ± move over any 60-minute window on the 30 s ticker."""
        price = await self._price("BTC-EUR")
        if price > 0:
            self._btc_window.append((now, price))
            cutoff = now - timedelta(seconds=SHOCK_WINDOW_S)
            while self._btc_window and self._btc_window[0][0] < cutoff:
                self._btc_window.popleft()
            if len(self._btc_window) >= 2:
                prices = [p for _, p in self._btc_window]
                lo, hi = min(prices), max(prices)
                if lo > 0 and (hi - lo) / lo * 100 > settings.risk.shock_brake_btc_move_pct:
                    await self._trigger_shock(session, now, settings, "BTC move")

    async def _check_pair_shock(
        self,
        session: Session,
        pair: str,
        last_1h: Candle,
        atr_1h: Decimal | None,
        now: datetime,
    ) -> None:
        """Any active pair's last closed 1h candle range above 3 x ATR(1h)."""
        settings = self._settings()
        if atr_1h is None or atr_1h <= 0:
            return
        rng = last_1h.high - last_1h.low
        if rng > 3 * atr_1h:
            await self._trigger_shock(session, now, settings, f"{pair} candle range")

    async def _trigger_shock(
        self, session: Session, now: datetime, settings: Settings, source: str
    ) -> None:
        if self.shock_brake_until is not None and self.shock_brake_until > now:
            return
        until = now + timedelta(hours=float(settings.risk.shock_brake_hours))
        self.shock_brake_until = until
        message = f"{source}; entries paused until {until.isoformat()}"
        repo.add_activity(session, "alert", f"shock brake ON ({message})", at=now)
        await self._notify("risk_control", "Shock brake on", message)

    # ---------------------------------------------------------------- helpers

    def _risk_state(self, session: Session) -> tuple[Decimal, list[Literal["streak", "drawdown"]]]:
        """The risk % per trade right now plus active throttle reasons:
        throttled to 0.5 after 3 losses in a row or a drawdown beyond 4%
        from peak. Each trigger lifts on its own."""
        settings = self._settings()
        streak = repo.losing_streak(session, self.broker.account_id)
        dd = self.broker.drawdown_pct()
        if dd > settings.risk.throttle_drawdown_pct:
            self._throttle_dd = True
        elif dd <= settings.risk.throttle_lift_drawdown_pct:
            self._throttle_dd = False
        reasons: list[Literal["streak", "drawdown"]] = []
        if streak >= settings.risk.throttle_consecutive_losses:
            reasons.append("streak")
        if self._throttle_dd:
            reasons.append("drawdown")
        pct = (
            settings.risk.throttled_risk_per_trade_pct
            if reasons
            else settings.risk.risk_per_trade_pct
        )
        return pct, reasons

    def _risk_pct(self, session: Session) -> Decimal:
        return self._risk_state(session)[0]

    def _last_stop_out(self, session: Session, pair: str) -> datetime | None:
        trade = repo.last_exit_for_pair(session, self.broker.account_id, pair, "stop")
        return trade.closed_at if trade else None

    async def _correlated_open(
        self, session: Session, pair: str, candles_1h: list[Candle]
    ) -> list[str]:
        """Open positions whose 30-day 1h correlation with this pair exceeds
        the configured threshold."""
        settings = self._settings()
        out: list[str] = []
        for other in self.broker.positions:
            if other == pair:
                continue
            other_candles = drop_forming(
                repo.load_candles(session, other, "1h", CORR_BARS), "1h", self.clock()
            )
            corr = aligned_correlation(candles_1h, other_candles, CORR_BARS)
            if corr is not None and corr > float(settings.risk.correlated_threshold):
                out.append(other)
        return out

    def _expire_pending_for_pair(self, session: Session, pair: str, now: datetime) -> None:
        """A pending trade expires at that pair's next cycle."""
        for row in repo.pending_decisions(session, self.broker.account_id):
            if row.pair == pair:
                row.status = "expired"
                self.pending.pop(row.id, None)
                repo.add_activity(
                    session,
                    "decision",
                    f"pending {pair} expired at next cycle",
                    pair=pair,
                    at=now,
                )

    def _update_price(
        self, session: Session, pair: str, price: Decimal, candles: list[Candle], now: datetime
    ) -> None:
        chg = Decimal("0")
        if len(candles) >= 25 and candles[-25].close > 0:
            chg = (price / candles[-25].close - 1) * 100
        self._store_price(pair, price, chg, stale=False)
        self.broker.mark(pair, price, now)

    def _store_price(
        self, pair: str, last: Decimal, chg: Decimal = Decimal("0"), stale: bool = False
    ) -> None:
        old = self.prices.get(pair)
        self.prices[pair] = PriceInfo(
            last=last,
            chg_24h_pct=chg if chg else (old.chg_24h_pct if old else Decimal("0")),
            stale=stale,
        )

    async def _note_stale_pair(self, pair: str) -> None:
        count = self._stale_cycles.get(pair, 0) + 1
        self._stale_cycles[pair] = count
        if count == 2:
            await self._notify(
                "pair_stale",
                f"Pair stale · {pair}",
                "Market data was stale for 2 cycles in a row",
            )

    def _mark_stale(self, pair: str, last: Decimal) -> None:
        old = self.prices.get(pair)
        self.prices[pair] = PriceInfo(
            last=last,
            chg_24h_pct=old.chg_24h_pct if old else Decimal("0"),
            stale=True,
        )

    def _record_decision(
        self,
        session: Session,
        pair: str,
        now: datetime,
        draft: Draft,
        signals: PairSignals | None,
        gates: list[Gate],
        *,
        action: str | None = None,
        status: Literal["executed", "pending", "approved", "vetoed", "expired", "none"] = "none",
        plan: Plan | None = None,
        error: str | None = None,
        decision_id: str | None = None,
        btc_regime: Regime | None = None,
    ) -> Decision:
        settings = self._settings()
        regime = (
            signals.regime if signals else Regime(trend="sideways", volatility="normal", age_4h=0)
        )
        btc_regime = btc_regime or Regime(trend="sideways", volatility="normal", age_4h=0)
        final_action = action or draft.action
        decision = Decision(
            id=decision_id or uuid.uuid4().hex[:16],
            cycle=self.cycle,
            at=now,
            pair=pair,
            decider=cast("Literal['openai', 'rules']", draft.decider or self.decider.name),
            model=draft.model,
            prompt_version=draft.prompt_version,
            action=final_action,
            confidence=draft.confidence,
            reasoning=draft.reasoning,
            invalidation=draft.invalidation,
            signals=draft.signals,
            plan=plan,
            gates=gates,
            status=status,
            execute_at=None,
            error=error or draft.error,
            regime=regime,
            btc_regime=btc_regime,
            invalidation_price=draft.invalidation_price,
            risk_pct_used=self._risk_pct(session),
            account_id=self.broker.account_id,
            settings_version=self._settings_version(),
            prefilter=draft.prefilter,
        )
        if status == "pending":
            decision.execute_at = (
                now + timedelta(seconds=settings.veto_window_s)
                if settings.execution_mode == "supervised"
                else None
            )
            self.pending[decision.id] = decision
        repo.save_decision(
            session,
            decision,
            raw_brief=draft.raw_brief,
            raw_response=draft.raw_response,
        )
        if draft.llm_call_id:
            call_row = repo.get_llm_call(session, draft.llm_call_id)
            if call_row is not None:
                call_row.decision_id = decision.id
        self.latest[pair] = decision
        session.commit()
        return decision

    def risk_view(self, session: Session) -> RiskView:
        """The risk state the OpenAI decider puts in the brief."""
        settings = self._settings()
        _, reasons = self._risk_state(session)
        return RiskView(
            throttle_on=bool(reasons),
            correlated_slots_left=max(
                0, settings.risk.correlated_max_positions - len(self.broker.positions)
            ),
            shock_brake_on=(
                self.shock_brake_until is not None and self.shock_brake_until > self.clock()
            ),
        )

    def risk_budget(self) -> RiskBudget:
        """The risk meter for the snapshot, computed fresh each time."""
        settings = self._settings()
        equity = self.broker.equity()
        open_risk_pct = self.broker.open_risk() / equity * 100 if equity > 0 else Decimal("0")
        now = self.clock()
        today_loss = self.broker.today_loss_pct(now)
        dd = self.broker.drawdown_pct()
        # Full-size trades still fitting: min(open-risk room, cash room)
        # at a 3% stop distance.
        room_risk = equity * settings.risk.total_open_risk_pct / 100 - self.broker.open_risk()
        room_cash = self.broker.cash
        per_trade_risk = equity * settings.risk.risk_per_trade_pct / 100
        left = 0
        if per_trade_risk > 0:
            left = (
                int(min(room_risk / per_trade_risk, room_cash / (equity * Decimal("0.03"))))
                if equity > 0
                else 0
            )
        session = self._sessions()
        try:
            risk_pct_now, reasons = self._risk_state(session)
        finally:
            session.close()
        lifts = ", ".join(
            "next winning trade" if r == "streak" else "drawdown <= 2% from peak" for r in reasons
        )
        throttle = Throttle(reasons=reasons, lifts_when=lifts) if reasons else None
        correlated_used = len(self.broker.positions)
        return RiskBudget(
            open_risk_pct=open_risk_pct,
            open_risk_cap_pct=settings.risk.total_open_risk_pct,
            today_loss_pct=today_loss,
            daily_limit_pct=settings.risk.daily_loss_limit_pct,
            drawdown_pct=dd,
            kill_pct=settings.risk.kill_drawdown_pct,
            full_trades_left=max(0, left),
            risk_pct_now=risk_pct_now,
            throttle=throttle,
            shock_brake_until=self.shock_brake_until,
            correlated_slots_left=max(0, settings.risk.correlated_max_positions - correlated_used),
        )

    async def _notify(self, event: str, title: str, body: str) -> None:
        if self._notify_event is None:
            return
        try:
            await self._notify_event(event, title, body)
        except Exception:
            return

    async def _push(self) -> None:
        if self._broadcast is not None:
            await self._broadcast()


def decision_from_row(row: DecisionRow) -> Decision:
    return Decision(
        id=row.id,
        cycle=row.cycle,
        at=repo.aware_utc(row.at),
        pair=row.pair,
        decider=cast("Literal['openai', 'rules']", row.decider),
        model=row.model,
        prompt_version=row.prompt_version,
        action=cast("Literal['BUY', 'SELL', 'HOLD', 'SKIP']", row.action),
        confidence=row.confidence,
        reasoning=row.reasoning,
        invalidation=row.invalidation,
        signals=json.loads(row.signals or "[]"),
        plan=json.loads(row.plan) if row.plan else None,
        gates=json.loads(row.gates or "[]"),
        status=cast(
            "Literal['executed', 'pending', 'approved', 'vetoed', 'expired', 'none']", row.status
        ),
        execute_at=repo.aware_utc(row.execute_at) if row.execute_at else None,
        error=row.error,
        regime=json.loads(row.regime or "{}"),
        btc_regime=json.loads(row.btc_regime or "{}"),
        invalidation_price=Decimal(row.invalidation_price) if row.invalidation_price else None,
        risk_pct_used=Decimal(row.risk_pct_used),
        account_id=row.account_id,
        settings_version=row.settings_version,
        shadow_id=row.shadow_id,
        stand_in=row.stand_in,
        prefilter=row.prefilter,
    )


class PendingRefusal(RuntimeError):
    """Approval actions only apply to pending decisions."""


class KilledRefusal(RuntimeError):
    """Resume is refused while the agent is Killed."""
