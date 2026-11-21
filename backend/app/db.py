"""Database engine, the settings table, migrations and the single-engine lock.

The backend takes an exclusive lock file next to the database at startup, so
two engine instances can never trade the same account.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo

from alembic.config import Config
from sqlalchemy import UniqueConstraint, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from alembic import command
from app.config import BACKEND_DIR

ALEMBIC_DIR = BACKEND_DIR / "alembic"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


class Base(DeclarativeBase):
    pass


class SettingsRow(Base):
    """The settings document: one row, one validated JSON blob."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=1)
    doc: Mapped[str]
    updated_at: Mapped[datetime]


class CandleRow(Base):
    """One closed candle per market and timeframe.

    Prices and volume are stored as their decimal strings so stored candles
    stay exact; they only become floats inside indicator maths.
    """

    __tablename__ = "candles"

    market: Mapped[str] = mapped_column(primary_key=True)
    tf: Mapped[str] = mapped_column(primary_key=True)
    open_time_ms: Mapped[int] = mapped_column(primary_key=True)
    open: Mapped[str]
    high: Mapped[str]
    low: Mapped[str]
    close: Mapped[str]
    volume: Mapped[str]


class MarketRow(Base):
    """Eligibility and activation state for one Bitvavo EUR market.

    The streak counters implement the anti-flapping buffer: a market fails
    only after ``eligibility_fail_days`` consecutive daily checks below a
    threshold (or at once when 20% beyond it), and needs
    ``eligibility_recover_days`` consecutive passing checks to return.
    """

    __tablename__ = "markets"

    market: Mapped[str] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(default="")
    quote: Mapped[str] = mapped_column(default="EUR")
    status: Mapped[str] = mapped_column(default="")
    eligible: Mapped[bool] = mapped_column(default=False)
    failed_rule: Mapped[str | None]
    volume_30d_eur: Mapped[float | None]
    spread_pct: Mapped[float | None]
    daily_candles: Mapped[int | None]
    below_streak: Mapped[int] = mapped_column(default=0)
    within_streak: Mapped[int] = mapped_column(default=0)
    checked_at: Mapped[datetime | None]
    active: Mapped[bool] = mapped_column(default=False)
    activated_at: Mapped[datetime | None]


class AccountRow(Base):
    """One paper account. A reset starts a new row; the unacknowledged kill
    fields persist the Killed state across restarts."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    started_at: Mapped[datetime]
    start_balance: Mapped[str]
    cash: Mapped[str]
    peak_equity: Mapped[str]
    day_start_equity: Mapped[str]
    day_start_date: Mapped[str]  # ISO date in Europe/Amsterdam
    active: Mapped[bool] = mapped_column(default=True)
    kill_at: Mapped[datetime | None]
    kill_peak: Mapped[str | None]
    kill_equity: Mapped[str | None]
    kill_closed: Mapped[str | None]  # JSON list of pairs flattened by the kill
    # The last kill-switch event on this account; never cleared by
    # acknowledge (the readiness check needs the 30-day history). A new
    # account row starts without one.
    last_kill_at: Mapped[datetime | None] = mapped_column(default=None)
    # The shock brake's expiry. Everything the agent knows survives a
    # restart — without this column a restart silently lifted the brake.
    shock_brake_until: Mapped[datetime | None] = mapped_column(default=None)
    # The drawdown leg of the risk throttle's hysteresis (>4% arms it,
    # <=2% releases it) — same restart-survival rule as the shock brake.
    throttle_dd: Mapped[bool] = mapped_column(default=False)


class PositionRow(Base):
    """One open paper position per pair per paper account.

    The primary key is (pair, account_id): a shadow strategy's account may
    hold the same pair the live account holds.
    """

    __tablename__ = "positions"

    pair: Mapped[str] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(primary_key=True)
    qty: Mapped[str]
    entry: Mapped[str]
    stop: Mapped[str]
    initial_stop: Mapped[str] = mapped_column(default="0")
    target: Mapped[str]
    opened_at: Mapped[datetime]
    decision_id: Mapped[str]
    last: Mapped[str]
    invalidation_price: Mapped[str | None]
    highest_close: Mapped[str | None]  # highest 1h close since entry (trailing)
    r_unit: Mapped[str]  # entry - stop at fill: the R multiple for exits
    entry_maker: Mapped[bool] = mapped_column(default=False)  # filled via a limit order


class TradeRow(Base):
    """One closed paper trade, tagged with the account it belonged to."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int]
    pair: Mapped[str]
    qty: Mapped[str]
    entry: Mapped[str]
    exit: Mapped[str]
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime]
    fee_eur: Mapped[str]  # both sides
    realised_eur: Mapped[str]
    realised_r: Mapped[str]
    initial_stop: Mapped[str] = mapped_column(default="0")
    r_unit: Mapped[str] = mapped_column(default="0")
    exit_reason: Mapped[str]
    decision_id: Mapped[str]


class DecisionRow(Base):
    """One decision per pair per cycle — BUY, SELL, HOLD and SKIP alike."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(primary_key=True)
    cycle: Mapped[int]
    at: Mapped[datetime]
    account_id: Mapped[int]
    pair: Mapped[str]
    decider: Mapped[str]
    model: Mapped[str | None]
    prompt_version: Mapped[str | None]
    action: Mapped[str]
    confidence: Mapped[float]
    reasoning: Mapped[str]
    invalidation: Mapped[str | None]
    signals: Mapped[str]  # JSON list[Signal]
    plan: Mapped[str | None]  # JSON Plan when one was sized
    gates: Mapped[str]  # JSON list[Gate]
    status: Mapped[str]
    execute_at: Mapped[datetime | None]
    error: Mapped[str | None]
    regime: Mapped[str]  # JSON Regime
    btc_regime: Mapped[str]  # JSON Regime
    invalidation_price: Mapped[str | None]
    risk_pct_used: Mapped[str]
    settings_version: Mapped[int]
    shadow_id: Mapped[str | None]
    stand_in: Mapped[bool] = mapped_column(default=False)
    raw_brief: Mapped[str | None]  # filled by the OpenAI layer (PR 5)
    raw_response: Mapped[str | None]
    # False means the Rules pre-filter skipped the pair without a model call.
    prefilter: Mapped[bool] = mapped_column(default=True)


class LlmCallRow(Base):
    """One model call (or a blocked one): the inspector's unit of audit.

    ``request_json``/``response_raw`` store the exact bytes sent and
    returned; ``attempts`` is a JSON list of per-try results. An
    ``input_blocked`` row means an input check failed before anything was
    transmitted — ``request_json`` then holds the request that would have
    been sent. ``feature`` scopes the cost: "decision" for live calls,
    "briefing", "ask", "replay" and "shadow:<id>" for the Evidence layer.
    """

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    cycle: Mapped[int | None]
    account_id: Mapped[int]
    pair: Mapped[str]
    decision_id: Mapped[str | None]  # the decision this call produced
    feature: Mapped[str]  # "decision" | "replay"
    replay_of: Mapped[str | None]  # original call for replays
    model: Mapped[str]
    prompt_version: Mapped[str]
    status: Mapped[str]  # "ok" | "invalid_output" | "timeout" | "api_error" | "input_blocked"
    attempts: Mapped[str]  # JSON list of {at, outcome, error, http_status, latency_ms}
    latency_ms: Mapped[int | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[str | None]  # Decimal; NULL = unknown model price
    cost_eur: Mapped[str | None]
    request_id: Mapped[str | None]  # OpenAI's x-request-id
    request_json: Mapped[str | None]  # exact request body sent (bytes as text)
    response_raw: Mapped[str | None]  # exact raw response body
    parsed: Mapped[str | None]  # JSON parsed model output
    input_checks: Mapped[str]  # JSON list[Check]
    output_checks: Mapped[str]  # JSON list[Check]
    verdict: Mapped[str | None]  # "correct" | "wrong" | None
    verdict_note: Mapped[str | None]
    verdict_at: Mapped[datetime | None]


class OutcomeRow(Base):
    __tablename__ = "outcomes"

    decision_id: Mapped[str] = mapped_column(primary_key=True)
    horizon: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int]
    pair: Mapped[str]
    action: Mapped[str]
    reference_at: Mapped[datetime]
    reference_price: Mapped[str]
    forward_return_pct: Mapped[str]
    best_move_pct: Mapped[str]
    worst_move_pct: Mapped[str]
    best_move_r: Mapped[str | None]
    worst_move_r: Mapped[str | None]
    first_touch: Mapped[str | None]
    invalidated: Mapped[bool]
    invalidation_followup_pct: Mapped[str | None]
    label: Mapped[str]
    realised_r: Mapped[str | None]
    computed_at: Mapped[datetime]
    method_version: Mapped[str]


class OutcomeFailureRow(Base):
    __tablename__ = "outcome_failures"

    decision_id: Mapped[str] = mapped_column(primary_key=True)
    horizon: Mapped[int] = mapped_column(primary_key=True)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str]
    last_at: Mapped[datetime]
    terminal: Mapped[bool] = mapped_column(default=False)


class ErrorRow(Base):
    """The retired errors table — renamed errors_legacy by migration
    0013; rows are imported once into logs.db as problems."""

    __tablename__ = "errors_legacy"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    first_at: Mapped[datetime]
    last_at: Mapped[datetime]
    pair: Mapped[str | None]
    text: Mapped[str]
    affected_decision_or_fill: Mapped[bool] = mapped_column(default=False)
    resolved_at: Mapped[datetime | None]
    resolution_note: Mapped[str | None]


class ActivityRow(Base):
    """The activity log: cycle starts, decisions, skips, fills, exits, errors."""

    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    kind: Mapped[str]
    pair: Mapped[str | None]
    text: Mapped[str]
    decision_id: Mapped[str | None]


class EquitySnapshotRow(Base):
    """One equity reading per cycle for the performance curve."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int]
    at: Mapped[datetime]
    cycle: Mapped[int]
    equity: Mapped[str]
    cash: Mapped[str]


class ExitTickRow(Base):
    """Prices at real fills and at watcher checks within 0.5% of a stop or
    target — What-if replays exits from these stored ticks."""

    __tablename__ = "exit_ticks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int]
    pair: Mapped[str]
    at: Mapped[datetime]
    price: Mapped[str]
    kind: Mapped[str]  # "fill" | "watch"


class PaperOrderRow(Base):
    """One simulated limit order: a post-only entry or a resting target.

    ``min_price`` tracks the lowest traded price seen while the order was
    open so a cancelled entry can show how close the fill came. Entry legs
    expire at the pair's next cycle (``expires_at``); target legs rest on
    the book until the position exits.
    """

    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    decision_id: Mapped[str]
    account_id: Mapped[int]
    pair: Mapped[str]
    side: Mapped[str]  # "buy" | "sell"
    leg: Mapped[str]  # "entry" | "target"
    limit_price: Mapped[str]
    qty: Mapped[str]
    placed_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(default="open")  # "open" | "filled" | "cancelled"
    filled_at: Mapped[datetime | None] = mapped_column(default=None)
    fill_price: Mapped[str | None] = mapped_column(default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(default=None)
    cancel_reason: Mapped[str | None] = mapped_column(default=None)
    min_price: Mapped[str | None] = mapped_column(default=None)


class EntryPauseRow(Base):
    """A pause on new entries for one pair — or ``"*"`` for every coin.

    ``until`` NULL means "until I resume"; a timestamp pauses until that
    moment and expires automatically. Open positions and SELL decisions are
    unaffected.
    """

    __tablename__ = "entry_pauses"

    pair: Mapped[str] = mapped_column(primary_key=True)
    until: Mapped[datetime | None]
    created_at: Mapped[datetime]


class BriefingRow(Base):
    """One daily briefing or weekly review: the code-computed facts, the
    text that was shown, how it was produced and the model call behind it."""

    __tablename__ = "briefings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    account_id: Mapped[int]
    type: Mapped[str]  # "daily" | "weekly"
    for_date: Mapped[str]  # Amsterdam date the briefing belongs to
    facts: Mapped[str]  # JSON: every figure the text was allowed to use
    text: Mapped[str]
    source: Mapped[str]  # "model" | "template"
    reason: Mapped[str | None]  # why the plain template was used
    llm_call_id: Mapped[str | None]


class SettingsHistoryRow(Base):
    """Every settings change: dotted leaf key, old and new JSON values.

    Decisions store the settings version they ran under; this table answers
    "what changed, and when" — the Readiness screen shows loosened
    thresholds with their date. From item 025 each row also carries the
    settings version it belongs to (NULL for changes from before versions).
    """

    __tablename__ = "settings_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    key: Mapped[str]
    old: Mapped[str | None]  # JSON-encoded leaf value (None = key absent)
    new: Mapped[str | None]
    version: Mapped[int | None] = mapped_column(default=None)


class SettingsVersionRow(Base):
    """One numbered settings version: the whole document, why it was saved
    and where the save came from (screen, promote, whatif, action,
    migration). ``changes`` is a JSON list of {key, old, new, direction}."""

    __tablename__ = "settings_versions"

    version: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    reason: Mapped[str]
    source: Mapped[str]
    doc: Mapped[str]  # the settings document as saved
    changes: Mapped[str] = mapped_column(default="[]")  # JSON list


class AlertRow(Base):
    """Sent notifications; the 30-minute repeat rule consults this across
    restarts so an alert is not re-sent after every boot."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    event: Mapped[str]
    channel: Mapped[str]  # "browser" | "email"
    title: Mapped[str]
    repeats: Mapped[int] = mapped_column(default=0)


class ShadowRow(Base):
    """One shadow strategy: a model + prompt + overrides on its own paper
    account. ``pauses`` is a JSON list of {"from", "to"} cost-cap pause
    windows (last "to" null while paused); Compare leaves them out."""

    __tablename__ = "shadows"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    model: Mapped[str]
    prompt_version: Mapped[str]
    overrides: Mapped[str]  # JSON dict of dotted settings paths -> values
    status: Mapped[str]  # "running" | "stopped"
    created_at: Mapped[datetime]
    stopped_at: Mapped[datetime | None]
    account_id: Mapped[int]  # its own paper account row (active=False)
    start_equity: Mapped[str]  # live account equity at creation
    daily_call_limit: Mapped[int] = mapped_column(default=300)
    pauses: Mapped[str] = mapped_column(default="[]")


class WhatifRow(Base):
    """One What-if run or saved scenario. ``spec`` holds {from, to,
    overrides}; ``result`` the computed curves, metrics and trade lists."""

    __tablename__ = "whatif_scenarios"

    id: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    name: Mapped[str | None]
    spec: Mapped[str]  # JSON WhatIfSpec
    status: Mapped[str]  # "queued" | "running" | "done" | "failed"
    progress: Mapped[float] = mapped_column(default=0.0)
    result: Mapped[str | None]  # JSON WhatIfResult
    saved: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None]


class AskConversationRow(Base):
    """One Ask conversation: ``messages`` is a JSON list of {role, text,
    at, citations, call_ids}. Stored locally; listed inside the panel."""

    __tablename__ = "ask_conversations"

    id: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    title: Mapped[str]
    messages: Mapped[str]  # JSON list[AskMessage]


class MarketContextRow(Base):
    __tablename__ = "market_context"
    __table_args__ = (UniqueConstraint("source", "metric", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str]
    metric: Mapped[str]
    symbol: Mapped[str]
    ts: Mapped[datetime]
    value: Mapped[float]
    meta: Mapped[str] = mapped_column(default="{}")
    fetched_at: Mapped[datetime]


class SkippedSlotRow(Base):
    """A scheduled quarter-hour slot the cycle did not run in, and why."""

    __tablename__ = "skipped_slots"

    slot_at: Mapped[datetime] = mapped_column(primary_key=True)  # the UTC quarter-hour boundary
    reason: Mapped[str]  # "asleep" | "stopped" | "paused"
    recorded_at: Mapped[datetime]


class StopMoveRow(Base):
    """Every time the exit watcher moves a position's stop (breakeven/trailing)."""

    __tablename__ = "stop_moves"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int]
    pair: Mapped[str]
    at: Mapped[datetime]
    old_stop: Mapped[str]
    new_stop: Mapped[str]
    reason: Mapped[str]  # "breakeven" | "trailing"


class FillRow(Base):
    """One paper fill: the record behind every position open and close.

    ``reference_price`` is the price the fill was simulated from (the last
    trade for a market order, the watcher's crossing price for a stop, the
    limit for a limit fill) and ``slippage_eur`` its cost against it —
    both NULL on fills rebuilt from trades and positions, which never had
    them recorded. ``trade_id`` is set on the exit fill and, once the
    trade closes, on its entry fill too.
    """

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int]
    at: Mapped[datetime]
    pair: Mapped[str]
    side: Mapped[str]  # "buy" | "sell"
    qty: Mapped[str]
    reference_price: Mapped[str | None]
    fill_price: Mapped[str]
    order_type: Mapped[str]  # "market" | "limit"
    fee_eur: Mapped[str]
    fee_kind: Mapped[str]  # "maker" | "taker"
    slippage_eur: Mapped[str | None]
    # decision | stop | target | time_stop | model_sell | flatten | manual | kill
    reason: Mapped[str]
    decision_id: Mapped[str]
    trade_id: Mapped[int | None]
    paper_order_id: Mapped[int | None]
    source: Mapped[str]  # "live" | "rebuilt"


class MetaRow(Base):
    """Small key/value store for one-off bookkeeping (the fills rebuild)."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


BUSY_TIMEOUT_MS = 30000


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    # The app has many concurrent writers (parallel pairs, the watcher,
    # shadows, collectors). WAL lets reads and one write overlap; the busy
    # timeout makes writers wait on each other instead of failing at once.
    event.listen(
        engine,
        "connect",
        lambda dbapi_conn, _record: _apply_sqlite_pragmas(dbapi_conn),
    )
    return engine


def _apply_sqlite_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def run_migrations(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


class EngineAlreadyRunningError(RuntimeError):
    pass


class EngineLock:
    """Exclusive advisory lock on a file next to the database.

    Uses fcntl.flock so the lock is released automatically if the process dies
    (no stale pid files). Refuses to start while another instance holds it.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock_path = db_path.with_suffix(db_path.suffix + ".lock")
        self._fh: IO[str] | None = None
        self._preexisted = False

    @property
    def path(self) -> Path:
        return self._lock_path

    @property
    def preexisted(self) -> bool:
        """A leftover lock file means the last run did not shut down
        cleanly (a clean release deletes it)."""
        return self._preexisted

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._preexisted = self._lock_path.exists()
        fh = self._lock_path.open("w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            msg = (
                f"another trading-agent instance already holds the engine lock "
                f"at {self._lock_path}; refusing to start"
            )
            raise EngineAlreadyRunningError(msg) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def heartbeat(self) -> None:
        """A heartbeat job touches the file so the next start can tell a
        stale lock from a live one."""
        if self._fh is not None:
            try:
                os.utime(self._lock_path, None)
            except OSError:
                pass

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
            try:
                self._lock_path.unlink()
            except OSError:
                pass


def utcnow() -> datetime:
    return datetime.now(UTC)


AMSTERDAM = ZoneInfo("Europe/Amsterdam")
