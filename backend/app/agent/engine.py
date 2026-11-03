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
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.agent.broker.paper import BrokerState, HeldPosition, PaperBroker
from app.agent.deciders.base import DecideContext, Decider, Draft, RiskView
from app.agent.limit import buy_fills, post_only_limit, sell_fills
from app.agent.market.base import (
    Candle,
    MarketClient,
    MarketDataError,
    MarketInfo,
    drop_forming,
)
from app.agent.market.universe import entries_allowed, warming_up
from app.agent.risk import BuyContext, check_buy, check_exit, update_trail
from app.agent.signals import (
    PairSignals,
    aligned_correlation,
    compute_pair_signals,
    compute_tf_values,
    decision_signals,
)
from app.db import AccountRow, DecisionRow, PaperOrderRow, utcnow
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
SHOCK_RANGE_ATR_MULT = Decimal("3")  # spec-fixed: candle range over 3 x ATR(1h)
EXIT_TICK_PCT = Decimal("0.5")  # watcher prices within 0.5% of stop/target
CORR_BARS = 720
# Cached prices older than this are not trusted for exits or fills.
PRICE_FALLBACK_S = 300

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
Notifier = Callable[[str, str, str], Awaitable[None]]

log = logging.getLogger(__name__)


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
        self._price_fresh_at: dict[str, datetime] = {}
        self._watch_errors = 0
        self._cycle_errors = 0
        # PR 8: set from main.py after construction — drives the shadow
        # strategies on the same cycle snapshot and watcher tick.
        self.shadows: Any = None
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
            self._recover_startup(session)
            session.commit()
        finally:
            session.close()
        await self.refresh_prices()
        self._stop.clear()
        self.tasks = [
            asyncio.create_task(self._scheduler_loop()),
            asyncio.create_task(self._watcher_loop()),
        ]

    def _recover_startup(self, session: Session) -> None:
        """The restart-recovery half of ``start``: kill state, expired
        approvals and pre-restart limit orders."""
        account = self.broker.account
        # Cycle numbering is monotonic across restarts — decisions, equity
        # snapshots and llm calls key on it (the inspector's cycle column,
        # the quiet line's latest cycle, What-if's pair-and-cycle lookup).
        self.cycle = repo.latest_persisted_cycle(session, self.broker.account_id)
        # The shock brake and the drawdown throttle survive restarts like
        # the kill state does.
        if account.shock_brake_until is not None:
            until = repo.aware_utc(account.shock_brake_until)
            self.shock_brake_until = until if until > self.clock() else None
        self._throttle_dd = bool(account.throttle_dd)
        # The latest decision per pair also survives: rebuilt from the
        # stored rows so the dashboard isn't blank until the next cycle.
        latest_pairs = dict.fromkeys(
            [*self._settings().active_markets, *self.broker.positions]
        )
        for pair, row in repo.latest_decisions(
            session, self.broker.account_id, list(latest_pairs)
        ).items():
            self.latest[pair] = decision_from_row(row)
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
        # Open limit entries from before a restart are cancelled as
        # unfilled — the same rule as pending approvals. Resting target
        # orders stay on the book; they belong to open positions.
        cancelled = self._cancel_entry_orders(
            session, self.clock(), "restart", all_accounts=True
        )
        if cancelled:
            repo.add_activity(
                session,
                "system",
                f"{cancelled} open limit order(s) cancelled on restart",
            )

    async def refresh_prices(self) -> None:
        """One price pass over the active markets and held pairs.

        Spec restart behaviour: the backend refreshes prices at startup so
        the GUI shows a live price and 24h change per pair badge while the
        agent waits in Paused. Never raises: a failing source must not stop
        startup.
        """
        try:
            session = self._sessions()
            try:
                settings = self._settings()
                now = self.clock()
                pairs = dict.fromkeys([*settings.active_markets, *self.broker.positions])
                for pair in pairs:
                    try:
                        candles = await self._fetch_closed(session, pair, "1h", now)
                    except Exception:
                        # Stored candles still give the GUI a last price.
                        log.exception("startup price refresh failed for %s", pair)
                        session.rollback()
                        candles = drop_forming(
                            repo.load_candles(session, pair, "1h", TF_LIMITS["1h"]),
                            "1h",
                            now,
                        )
                    if not candles:
                        continue
                    if candle_age_s(candles[-1], "1h", now) > _stale_limit_s("1h", settings):
                        self._mark_stale(pair, candles[-1].close)
                    else:
                        self._update_price(session, pair, candles[-1].close, candles, now)
            finally:
                session.close()
        except Exception:
            log.exception("startup price refresh failed")

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
            self.next_run_at = None

    def resume(self) -> None:
        """Resume cycles; refused while Killed until the kill is acknowledged."""
        if self.status == "killed":
            raise KilledRefusal("agent is Killed; acknowledge the kill first")
        if self.status in ("paused", "error"):
            self.status = "running"
            self.next_run_at = self._next_due(self.clock())

    async def run_now(self) -> bool:
        """Start a cycle now if none is running. Refused while Killed: no
        new trades are possible until the kill is acknowledged."""
        if self.status == "killed":
            raise KilledRefusal("agent is Killed; acknowledge the kill first")
        if self.cycle_in_progress:
            return False
        await self.run_cycle()
        return True

    def _capture_execution_state(
        self,
    ) -> tuple[BrokerState, dict[str, Decision], dict[str, Decision]]:
        """Broker plus the in-memory decision indexes — everything that must
        survive a failed commit unchanged."""
        return (self.broker.capture_state(), dict(self.pending), dict(self.latest))

    def _restore_execution_state(
        self,
        state: tuple[BrokerState, dict[str, Decision], dict[str, Decision]],
    ) -> None:
        broker_state, pending, latest = state
        self.broker.restore_state(broker_state)
        self.pending = pending
        self.latest = latest

    async def flatten(self) -> list[str]:
        """Close every position at market and pause the agent."""
        async with self._execution_lock:
            session = self._sessions()
            state = self._capture_execution_state()
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
                # Stop & flatten pauses the agent: open limit entries on
                # the book can no longer be managed, so they cancel now.
                self._cancel_entry_orders(session, now, "stop & flatten")
                self.pause()
                self.broker.persist(session, now)
                repo.add_activity(
                    session, "exit", f"Stop & flatten: closed {len(closed)} position(s)"
                )
                session.commit()
            except Exception:
                session.rollback()
                self._restore_execution_state(state)
                raise
            finally:
                session.close()
        await self._push()
        return closed

    async def close_position(self, pair: str) -> bool:
        """Close one open position at market (the dashboard Close button)."""
        async with self._execution_lock:
            if pair not in self.broker.positions:
                return False
            price = await self._price(pair)
            if price <= 0:
                raise MarketDataError(f"no current price for {pair}")
            session = self._sessions()
            state = self._capture_execution_state()
            try:
                await self._close_position(session, pair, price, self.clock(), "manual")
                self.broker.persist(session, self.clock())
                session.commit()
            except Exception:
                session.rollback()
                self._restore_execution_state(state)
                raise
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
            self.next_run_at = None
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
        self._price_fresh_at.clear()
        self._watch_errors = 0
        self._cycle_errors = 0
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
            try:
                await self.run_cycle()
            except Exception as exc:
                # A crashed cycle must never kill the scheduler.
                await self._note_cycle_error(exc)

    async def _note_cycle_error(self, exc: Exception) -> None:
        """Best-effort error record for a crashed cycle; never raises.
        Repeated cycle crashes move the agent into the Error state — the
        pill shows it and cycles stop until Resume."""
        log.exception("cycle %d failed", self.cycle)
        self._cycle_errors += 1
        if self._cycle_errors >= 3 and self.status != "killed":
            self.status = "error"
            self.next_run_at = None
        try:
            session = self._sessions()
            try:
                repo.add_activity(
                    session,
                    "error",
                    f"cycle {self.cycle} failed: {exc}",
                    at=self.clock(),
                )
                session.commit()
            finally:
                session.close()
        except Exception:
            log.exception("recording the cycle error failed too")

    async def _watcher_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                self._watch_errors = 0
            except Exception as exc:
                # The watcher must never die on a bad tick — but failures
                # are logged and recorded, never silent: exits depend on
                # this loop.
                self._watch_errors += 1
                log.exception("watcher tick failed")
                if self._watch_errors == 1 or self._watch_errors % 10 == 0:
                    await self._note_watch_error(exc)
            await self._sleep(WATCH_S)

    async def _note_watch_error(self, exc: Exception) -> None:
        """Best-effort error record for a failed watcher tick; never raises."""
        try:
            session = self._sessions()
            try:
                repo.add_activity(
                    session,
                    "error",
                    f"exit watcher tick failed ({self._watch_errors} in a row): {exc}",
                    at=self.clock(),
                )
                session.commit()
            finally:
                session.close()
        except Exception:
            log.exception("recording the watcher error failed too")

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
                # A held position keeps its cycle on a deactivated or
                # failing market — the model still manages it until it
                # closes; "one position per pair" blocks any new entry.
                for pair in self.broker.positions:
                    if pair not in pairs:
                        pairs.append(pair)
                sem = asyncio.Semaphore(MAX_PARALLEL_PAIRS)

                async def guarded(pair: str) -> None:
                    async with sem:
                        pair_session = self._sessions()
                        try:
                            await self._decide_pair(pair_session, pair, context, now)
                        except Exception as exc:
                            await self._record_pair_failure(pair_session, pair, exc)
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
                session.commit()
                if self.shadows is not None:
                    await self.shadows.run_cycle(now, cycle_no)
                self.broker.persist(session, now)
                session.commit()
                # A clean cycle clears the crash streak and the Error state.
                self._cycle_errors = 0
                if self.status == "error":
                    self.status = "running"
            finally:
                session.close()
                self.cycle_in_progress = False
                self.next_run_at = (
                    self._next_due(self.clock() + timedelta(seconds=1))
                    if self.status == "running"
                    else None
                )
            await self._push()

    async def _record_pair_failure(self, session: Session, pair: str, exc: Exception) -> None:
        """A crashed pair still ends as a recorded HOLD — never a trade,
        never silent — and the other pairs' work is untouched."""
        log.exception("cycle %d: deciding %s failed", self.cycle, pair)
        try:
            session.rollback()
            now = self.clock()
            decision_id = uuid.uuid4().hex[:16]
            repo.add_activity(
                session,
                "error",
                f"cycle failed for {pair}: {exc}",
                pair=pair,
                decision_id=decision_id,
                at=now,
            )
            self._record_decision(
                session,
                pair,
                now,
                Draft(
                    action="HOLD",
                    confidence=0.0,
                    reasoning=f"cycle error: {exc}",
                    signals=[],
                ),
                None,
                [],
                error=f"cycle error: {exc}",
                decision_id=decision_id,
            )
        except Exception:
            session.rollback()

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
        # An unfilled limit entry dies at its pair's next cycle.
        self._cancel_entry_orders(session, now, "next cycle", pair=pair)
        btc_regime = cast("Regime | None", context.get("btc_regime"))
        if btc_regime is None:
            # The invalidation re-evaluation passes an empty context —
            # rebuild BTC's regime from stored candles so the brief keeps
            # its regime.btc block.
            btc_regime = self._stored_btc_regime(session, settings, now)

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
        pause = repo.active_entry_pause(session, pair, now)
        entries_open = entries_allowed(session, pair)
        prev_4h = compute_tf_values(candles["4h"][:-1], "4h") if len(candles["4h"]) > 1 else None
        decider = (
            self.openai_decider
            if self.openai_decider is not None and settings.model.decider == "openai"
            else self.decider
        )
        session.commit()
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
                entries_paused=pause is not None or not entries_open,
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
        the newest expected closed bar is missing (not yet published), then
        whatever we have — the freshness gate decides below."""
        candles: list[Candle] = []
        for attempt in range(STALE_RETRIES + 1):
            candles = await self._fetch_closed(session, pair, "1h", self.clock())
            if candles:
                # The newest bar that should be closed opened one interval
                # before the current hour boundary.
                boundary = self.clock().replace(minute=0, second=0, microsecond=0)
                if candles[-1].open_time >= boundary - timedelta(seconds=TF_SECONDS["1h"]):
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
        if draft.action == "SELL" or (
            draft.action == "BUY" and self._settings().execution_mode == "auto"
        ):
            # Snapshot inside the lock: a concurrent fill committed while we
            # waited must survive our rollback.
            async with self._execution_lock:
                state = self._capture_execution_state()
                try:
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
                except Exception:
                    session.rollback()
                    self._restore_execution_state(state)
                    raise
        # No awaits until commit — broker mutations stay atomic here.
        state = self._capture_execution_state()
        try:
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
        except Exception:
            session.rollback()
            self._restore_execution_state(state)
            raise

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
        status: Literal[
            "executed", "pending", "approved", "vetoed", "expired", "unfilled", "none"
        ] = "none"
        plan: Plan | None = None
        error: str | None = None
        action = draft.action
        decision_id = uuid.uuid4().hex[:16]

        if draft.action == "BUY":
            limit_price: Decimal | None = None
            if settings.execution_mode == "auto" and settings.paper.entry_orders == "limit":
                # Post-only placement: the decision's entry, moved to the
                # best bid when it would cross the book. Gates, sizing and
                # R/R all run at the limit price that will be placed.
                limit_price = await self._post_only_price(pair, draft.entry or price)
                if limit_price is None:
                    action = "SKIP"
                    error = "order book unavailable"
                    repo.add_activity(
                        session,
                        "skip",
                        f"SKIP {pair}: order book unavailable",
                        pair=pair,
                        at=now,
                    )
            if action != "SKIP":
                ctx = await self._buy_ctx(
                    session,
                    pair,
                    now,
                    entry=limit_price if limit_price is not None else (draft.entry or price),
                    stop=draft.stop or Decimal("0"),
                    target=draft.target or Decimal("0"),
                    atr_1h=(
                        Decimal(str(signals.tf["1h"].atr14)) if signals.tf["1h"].atr14 else None
                    ),
                    candles_1h=candles["1h"],
                    settings=settings,
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
                        if limit_price is not None:
                            self._place_entry_order(session, pair, decision_id, verdict.plan, now)
                            status = "none"
                        else:
                            await self._fill_buy(
                                session, pair, plan, draft, now, decision_id
                            )
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
        now: datetime,
        decision_id: str,
    ) -> None:
        # Fill at the plan's entry: the broker applies the same slippage
        # step the gates sized on, so the real fill equals fill_entry —
        # not a candle close up to 0.5% away when the model named an entry.
        assert plan is not None
        pos = self.broker.buy(
            session,
            pair,
            plan.entry,
            plan.qty,
            plan.stop,
            plan.target,
            decision_id,
            now,
            invalidation_price=draft.invalidation_price,
        )
        repo.add_exit_tick(session, self.broker.account_id, pair, pos.entry, "fill", at=now)
        repo.add_activity(
            session,
            "fill",
            f"BUY {pair} qty {plan.qty} @ {pos.entry}",
            pair=pair,
            at=now,
        )
        await self._notify("trade", f"Trade opened · {pair}", f"BUY {plan.qty} @ {pos.entry}")

    # ------------------------------------------------------ limit orders ==

    async def _post_only_price(self, pair: str, entry: Decimal) -> Decimal | None:
        """The limit a post-only buy rests at — the decision's entry, or
        the best bid when the entry would cross the book. None when the
        book can't be read: any failure means no order is placed."""
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
        self, session: Session, pair: str, decision_id: str, plan: Plan | None, now: datetime
    ) -> PaperOrderRow:
        """Rest a post-only entry on the book until the pair's next cycle."""
        assert plan is not None
        order = repo.add_paper_order(
            session,
            PaperOrderRow(
                decision_id=decision_id,
                account_id=self.broker.account_id,
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
            f"BUY {pair} limit {plan.fill_entry} placed (post-only)",
            pair=pair,
            decision_id=decision_id,
            at=now,
        )
        return order

    def _place_target_order(
        self, session: Session, pair: str, pos: HeldPosition, now: datetime
    ) -> PaperOrderRow:
        """Rest the position's target as a limit sell on the book."""
        return repo.add_paper_order(
            session,
            PaperOrderRow(
                decision_id=pos.decision_id,
                account_id=self.broker.account_id,
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
        self, session: Session, order: PaperOrderRow, now: datetime, reason: str
    ) -> None:
        """Cancel one resting order; an entry leg's decision goes to
        ``unfilled`` and is tracked like a BUY that was not taken."""
        repo.cancel_paper_order(session, order, now, reason)
        repo.add_activity(
            session,
            "order",
            f"{order.pair} limit {order.limit_price} cancelled: {reason}",
            pair=order.pair,
            decision_id=order.decision_id,
            at=now,
        )
        if order.leg != "entry":
            return
        row = repo.get_decision_row(session, order.decision_id)
        if row is not None and row.status in ("none", "approved"):
            row.status = "unfilled"
            if row.account_id == self.broker.account_id:
                self.latest[row.pair] = decision_from_row(row)

    def _cancel_entry_orders(
        self,
        session: Session,
        now: datetime,
        reason: str,
        *,
        pair: str | None = None,
        all_accounts: bool = False,
    ) -> int:
        """Cancel open entry orders — at a pair's cycle, on a blocking
        gate, or at restart (``all_accounts`` covers the shadows' books)."""
        count = 0
        for order in repo.open_paper_orders(
            session,
            None if all_accounts else self.broker.account_id,
            pair,
            leg="entry",
        ):
            self._cancel_order(session, order, now, reason)
            count += 1
        return count

    def _entry_block_reason(
        self, session: Session, pair: str, now: datetime, settings: Settings
    ) -> str | None:
        """A gate that started blocking entries cancels a resting order at
        once: kill switch, shock brake, entry pause, daily loss limit."""
        if self.status == "killed":
            return "kill switch"
        if self.shock_brake_until is not None and self.shock_brake_until > now:
            return "shock brake"
        if repo.active_entry_pause(session, pair, now) is not None:
            return "entry pause"
        if self.broker.today_loss_pct(now) >= settings.risk.daily_loss_limit_pct:
            return "daily loss limit"
        return None

    async def _buy_ctx(
        self,
        session: Session,
        pair: str,
        now: datetime,
        *,
        entry: Decimal,
        stop: Decimal,
        target: Decimal,
        atr_1h: Decimal | None,
        candles_1h: list[Candle],
        settings: Settings,
    ) -> BuyContext:
        """One pair's gate context — shared by the cycle, the approval
        path and the fill-time recheck of a resting limit order."""
        correlated = await self._correlated_open(session, pair, candles_1h)
        pause = repo.active_entry_pause(session, pair, now)
        return BuyContext(
            pair=pair,
            now=now,
            entry=entry,
            stop=stop,
            target=target,
            atr_1h=atr_1h,
            has_position=pair in self.broker.positions,
            last_stop_out_at=self._last_stop_out(session, pair),
            correlated_pairs=correlated,
            shock_brake_until=self.shock_brake_until,
            daily_loss_hit=(self.broker.today_loss_pct(now) >= settings.risk.daily_loss_limit_pct),
            risk_pct=self._risk_pct(session),
            entry_paused_until=(
                repo.aware_utc(pause.until) if pause is not None and pause.until else None
            ),
            entry_paused_indefinite=pause is not None and pause.until is None,
            entries_allowed=entries_allowed(session, pair),
        )

    async def _fill_entry_order(
        self,
        session: Session,
        order: PaperOrderRow,
        price: Decimal,
        now: datetime,
        settings: Settings,
    ) -> bool:
        """The market traded through the limit: recheck every gate at the
        fill — sizing and risk are computed again — then open at the exact
        limit price, or cancel the order when a gate now fails."""
        row = repo.get_decision_row(session, order.decision_id)
        if row is None or row.plan is None:
            self._cancel_order(session, order, now, "missing plan")
            return False
        plan = Plan.model_validate_json(row.plan)
        candles = drop_forming(repo.load_candles(session, order.pair, "1h", CORR_BARS), "1h", now)
        # The order was placed post-only — the recheck runs in limit mode
        # (exact fill, maker fee) even if the account has since switched.
        gate_settings = settings.model_copy(
            update={"paper": settings.paper.model_copy(update={"entry_orders": "limit"})}
        )
        verdict = check_buy(
            await self._buy_ctx(
                session,
                order.pair,
                now,
                entry=Decimal(order.limit_price),
                stop=plan.stop,
                target=plan.target,
                atr_1h=self._atr_1h(session, order.pair, now),
                candles_1h=candles,
                settings=gate_settings,
            ),
            self.broker,
            self.market_rules.get(order.pair),
            gate_settings,
        )
        if verdict.outcome == "skip" or verdict.plan is None:
            self._cancel_order(session, order, now, verdict.skip_reason or "gate")
            return False
        fill_plan = verdict.plan
        pos = self.broker.buy(
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
        # The new position's target rests on the book from the fill tick.
        self._place_target_order(session, order.pair, pos, now)
        row.plan = fill_plan.model_dump_json()
        row.status = "executed"
        row.execute_at = now.replace(tzinfo=None)
        repo.add_exit_tick(session, self.broker.account_id, order.pair, price, "fill", at=now)
        repo.add_activity(
            session,
            "fill",
            f"BUY {order.pair} qty {fill_plan.qty} @ {pos.entry} (limit)",
            pair=order.pair,
            decision_id=order.decision_id,
            at=now,
        )
        await self._notify(
            "trade",
            f"Trade opened · {order.pair}",
            f"BUY {fill_plan.qty} @ {pos.entry}",
        )
        self.latest[order.pair] = decision_from_row(row)
        return True

    async def _tick_entry_orders(
        self, session: Session, now: datetime, settings: Settings
    ) -> set[str]:
        """Drive open limit entries on the 30 s ticker: cancel at once when
        a gate starts blocking entries, fill on a trade-through. Returns
        the pairs whose entry filled this tick — a newly filled position's
        exits are evaluated from the next tick on."""
        filled: set[str] = set()
        orders = repo.open_paper_orders(session, self.broker.account_id, leg="entry")
        if settings.paper.entry_orders != "limit":
            # Resting limit entries only exist in limit mode — when the
            # account switches to market they can no longer be managed.
            for order in orders:
                self._cancel_order(session, order, now, "switched to market")
            return filled
        for order in orders:
            price, fresh = await self._priced(order.pair)
            if price <= 0:
                continue
            self._store_price(order.pair, price, stale=not fresh)
            if order.min_price is None or price < Decimal(order.min_price):
                order.min_price = str(price)
            block = self._entry_block_reason(session, order.pair, now, settings)
            if block is not None or (
                order.expires_at is not None and repo.aware_utc(order.expires_at) <= now
            ):
                self._cancel_order(session, order, now, block or "next cycle")
                continue
            if not buy_fills(price, Decimal(order.limit_price), self._tick_size(order.pair)):
                continue
            if await self._fill_entry_order(session, order, price, now, settings):
                filled.add(order.pair)
        return filled

    async def _close_position(
        self,
        session: Session,
        pair: str,
        price: Decimal,
        now: datetime,
        reason: str,
        *,
        order_type: Literal["market", "limit"] = "market",
        filled_order: PaperOrderRow | None = None,
    ) -> None:
        if price <= 0:
            return
        # Any resting orders on the pair resolve with the exit: the target
        # order that filled is marked filled, every other one is cancelled.
        for order in repo.open_paper_orders(session, self.broker.account_id, pair):
            if filled_order is not None and order.id == filled_order.id:
                repo.fill_paper_order(session, order, now)
            else:
                repo.cancel_paper_order(session, order, now, f"exit: {reason}")
        trade = self.broker.close(session, pair, price, now, reason, order_type=order_type)
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
        async with self._execution_lock:
            re_eval = await self._tick_locked()
        # Invalidation re-evaluations run AFTER the lock is released:
        # _decide_pair -> _execute_draft re-acquires it for SELL/auto BUY
        # and asyncio.Lock is not reentrant, so running them inside the
        # tick deadlocked the whole engine.
        for pair in re_eval:
            session = self._sessions()
            try:
                await self._decide_pair(session, pair, {}, self.clock())
            except Exception as exc:
                await self._record_pair_failure(session, pair, exc)
            finally:
                session.close()
        # Fills, exits and kills land between cycles — the snapshot push
        # keeps connected clients live instead of waiting for a cycle end.
        await self._push()

    async def _tick_locked(self) -> list[str]:
        now = self.clock()
        session = self._sessions()
        state = self._capture_execution_state()
        re_eval: list[str] = []
        try:
            settings = self._settings()
            await self._tick_shock_btc(session, now, settings)
            filled = await self._tick_entry_orders(session, now, settings)
            await self._tick_positions(session, now, settings, filled, re_eval)
            if self.shadows is not None:
                await self.shadows.tick(now)
            await self._tick_pending(session, now)
            await self._tick_kill(session, now, settings)
            await self._tick_risk_notifications(session, now, settings)
            self.broker.persist(session, now)
            session.commit()
        except Exception:
            session.rollback()
            self._restore_execution_state(state)
            raise
        finally:
            session.close()
        return re_eval

    async def _priced(self, pair: str) -> tuple[Decimal, bool]:
        """(price, fresh) — the ticker price, or the last cached price when
        the fetch fails but is no older than ``PRICE_FALLBACK_S``.

        The fallback keeps exits working through a brief blip; beyond the
        bound a pair reports 0 so no exit, kill or fill ever fires on data
        old enough to be wrong."""
        try:
            price = await self.client.ticker_price(pair)
        except MarketDataError:
            price = Decimal("0")
        if price > 0:
            self._price_fresh_at[pair] = self.clock()
            return price, True
        info = self.prices.get(pair)
        if info is None or info.last <= 0:
            return Decimal("0"), False
        fresh_at = self._price_fresh_at.get(pair)
        if (
            fresh_at is not None
            and (self.clock() - fresh_at).total_seconds() <= PRICE_FALLBACK_S
        ):
            return info.last, False
        return Decimal("0"), False

    async def _price(self, pair: str) -> Decimal:
        return (await self._priced(pair))[0]

    async def _tick_positions(
        self,
        session: Session,
        now: datetime,
        settings: Settings,
        filled: set[str] | None = None,
        re_eval: list[str] | None = None,
    ) -> None:
        filled = filled or set()
        re_eval = re_eval if re_eval is not None else []
        for pair, pos in list(self.broker.positions.items()):
            if pair in filled:
                # A limit entry filled this tick is never stopped or
                # targetted out in the same check — exits start next tick.
                continue
            price, fresh = await self._priced(pair)
            if price <= 0:
                continue
            self.broker.mark(pair, price, now)
            self._store_price(pair, price, stale=not fresh)
            # Trailing needs the newest stored closed 1h close.
            last_1h = repo.load_candles(session, pair, "1h", 2)
            last_1h = drop_forming(last_1h, "1h", now)
            if last_1h:
                update_trail(pos, last_1h[-1].close)
            atr = self._atr_1h(session, pair, now)
            # The target's style belongs to the position: a maker-filled
            # entry rests its target as a limit sell, a taker-filled one
            # keeps the market touch — flipping the entry-orders setting
            # never retroactively changes an open position's exit.
            limit_targets = pos.entry_maker
            target_order = next(
                (
                    o
                    for o in repo.open_paper_orders(
                        session, self.broker.account_id, pair, leg="target"
                    )
                ),
                None,
            )
            if not limit_targets and target_order is not None:
                self._cancel_order(session, target_order, now, "market position")
                target_order = None
            reason = check_exit(pos, price, atr, now, settings, target_is_limit=limit_targets)
            if reason is not None:
                # Stops, time stops and every forced exit stay market.
                await self._close_position(session, pair, price, now, reason)
                continue
            if limit_targets:
                if sell_fills(price, pos.target, self._tick_size(pair)):
                    # The market traded through the resting target: it
                    # fills at the exact limit price (maker fee, no slip).
                    await self._close_position(
                        session,
                        pair,
                        pos.target,
                        now,
                        "target",
                        order_type="limit",
                        filled_order=target_order,
                    )
                    continue
                if target_order is None:
                    target_order = self._place_target_order(session, pair, pos, now)
            if pos.invalidation_price is not None and price <= pos.invalidation_price:
                # One-shot: crossing queues an immediate re-evaluation
                # (run after the lock is released, see tick), then the
                # stored price is cleared until a decision sets a new one.
                pos.invalidation_price = None
                re_eval.append(pair)
                repo.add_activity(
                    session,
                    "decision",
                    f"invalidation price crossed on {pair}; re-evaluating",
                    pair=pair,
                    at=now,
                )
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

    def _stored_btc_regime(
        self, session: Session, settings: Settings, now: datetime
    ) -> Regime | None:
        """BTC's regime from stored candles — the fallback when no cycle
        context was passed (the invalidation re-evaluation path)."""
        btc_1h = drop_forming(
            repo.load_candles(session, "BTC-EUR", "1h", TF_LIMITS["1h"]), "1h", now
        )
        btc_4h = drop_forming(
            repo.load_candles(session, "BTC-EUR", "4h", TF_LIMITS["4h"]), "4h", now
        )
        btc_1d = drop_forming(
            repo.load_candles(session, "BTC-EUR", "1d", TF_LIMITS["1d"]), "1d", now
        )
        if not (btc_1h and btc_4h and btc_1d):
            return None
        return compute_pair_signals(
            "BTC-EUR", btc_1h, btc_4h, btc_1d, settings=settings.market, now=now
        ).regime

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
                await self._approve_pending(session, row.id, now)

    async def approve_pending(self, decision_id: str) -> Decision:
        async with self._execution_lock:
            session = self._sessions()
            state = self._capture_execution_state()
            try:
                decision = await self._approve_pending(session, decision_id, self.clock())
                self.broker.persist(session, self.clock())
                session.commit()
            except Exception:
                session.rollback()
                self._restore_execution_state(state)
                raise
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
        # Approving a limit-mode decision places the post-only order at the
        # decision's entry (post-only adjusted); gates rerun at the limit.
        limit_price: Decimal | None = None
        if settings.paper.entry_orders == "limit":
            limit_price = await self._post_only_price(row.pair, old_plan.entry)
            if limit_price is None:
                row.status = "expired"
                row.error = "order book unavailable"
                self.pending.pop(row.id, None)
                session.commit()
                raise PendingRefusal("no order book to place the limit order")
        candles = drop_forming(repo.load_candles(session, row.pair, "1h", CORR_BARS), "1h", now)
        atr_1h = self._atr_1h(session, row.pair, now)
        verdict = check_buy(
            await self._buy_ctx(
                session,
                row.pair,
                now,
                entry=limit_price if limit_price is not None else price,
                stop=old_plan.stop,
                target=old_plan.target,
                atr_1h=atr_1h,
                candles_1h=candles,
                settings=settings,
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
            if limit_price is not None:
                self._place_entry_order(session, row.pair, row.id, verdict.plan, now)
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
                await self._fill_buy(session, row.pair, verdict.plan, draft, now, row.id)
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
            state = self._capture_execution_state()
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
            except Exception:
                session.rollback()
                self._restore_execution_state(state)
                raise
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
        # A gate that blocks entries cancels resting limit orders at once.
        self._cancel_entry_orders(session, now, "kill switch")
        self.status = "killed"
        self.next_run_at = None
        account = self.broker.account
        account.kill_at = now
        account.last_kill_at = now
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
        price, fresh = await self._priced("BTC-EUR")
        if price > 0 and fresh:
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
        if rng > SHOCK_RANGE_ATR_MULT * atr_1h:
            await self._trigger_shock(session, now, settings, f"{pair} candle range")

    async def _trigger_shock(
        self, session: Session, now: datetime, settings: Settings, source: str
    ) -> None:
        if self.shock_brake_until is not None and self.shock_brake_until > now:
            return
        until = now + timedelta(hours=float(settings.risk.shock_brake_hours))
        self.shock_brake_until = until
        # Persist on the account row so a restart cannot silently lift the
        # brake; staged here so the brake lands with this session's commit.
        self.broker.account.shock_brake_until = until
        session.merge(self.broker.account)
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
        # Persisted on the account row so a restart keeps the hysteresis.
        if bool(self.broker.account.throttle_dd) != self._throttle_dd:
            self.broker.account.throttle_dd = self._throttle_dd
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
        status: Literal[
            "executed", "pending", "approved", "vetoed", "expired", "unfilled", "none"
        ] = "none",
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
            signals=decision_signals(draft.signals, signals),
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
        limited_by: Literal["risk", "cash"] = "risk"
        if per_trade_risk > 0 and equity > 0:
            by_risk = room_risk / per_trade_risk
            by_cash = room_cash / (equity * Decimal("0.03"))
            left = int(min(by_risk, by_cash))
            limited_by = "cash" if by_cash < by_risk else "risk"
        session = self._sessions()
        try:
            risk_pct_now, reasons = self._risk_state(session)
            correlated_detail = self._correlated_detail(session, settings)
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
            room_limited_by=limited_by,
            risk_pct_now=risk_pct_now,
            throttle=throttle,
            shock_brake_until=self.shock_brake_until,
            correlated_slots_left=max(0, settings.risk.correlated_max_positions - correlated_used),
            correlated_detail=correlated_detail,
        )

    def _correlated_detail(self, session: Session, settings: Settings) -> str | None:
        """The strongest 30-day correlation between two open positions —
        e.g. "BTC + ETH 0.86" — or None when fewer than two are open."""
        pairs = list(self.broker.positions)
        if len(pairs) < 2:
            return None
        series = {
            pair: drop_forming(
                repo.load_candles(session, pair, "1h", CORR_BARS), "1h", self.clock()
            )
            for pair in pairs
        }
        best: tuple[str, str, float] | None = None
        for i, a in enumerate(pairs):
            for b in pairs[i + 1 :]:
                corr = aligned_correlation(series[a], series[b], CORR_BARS)
                if corr is not None and (best is None or corr > best[2]):
                    best = (a, b, corr)
        if best is None:
            return None
        base_a = best[0].split("-")[0]
        base_b = best[1].split("-")[0]
        return f"{base_a} + {base_b} {best[2]:.2f}"

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
            "Literal['executed', 'pending', 'approved', 'vetoed', 'expired', 'unfilled', 'none']",
            row.status,
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
