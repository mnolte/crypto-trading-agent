"""Pydantic models: decisions, positions, settings and the snapshot.

Every model here mirrors the "Data model and API" section of docs/SPEC.md.
Money is a Decimal everywhere; it serialises to JSON as a string and the
frontend never does arithmetic on it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from app.settings_spec import KEEP_DAYS_RANGE, RANGES, TIMEOUT_RANGE, risk_errors, rules_errors

# Money travels as a string in JSON: Decimals are exact and the frontend only
# formats them.
MoneyDecimal = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"), return_type=str)]


def _range(lo: str, hi: str) -> AfterValidator:
    """An inclusive bounds check; the 422 message reads "0.1 to 2"."""

    def check(value: Decimal | int | float) -> Decimal | int | float:
        if not Decimal(lo) <= Decimal(str(value)) <= Decimal(hi):
            raise ValueError(f"must be {lo} to {hi}")
        return value

    return AfterValidator(check)


def _keep_days_ok(value: dict[str, int]) -> dict[str, int]:
    lo, hi = (Decimal(bound) for bound in KEEP_DAYS_RANGE)
    for days in value.values():
        if not lo <= Decimal(days) <= hi:
            raise ValueError(f"must be {KEEP_DAYS_RANGE[0]} to {KEEP_DAYS_RANGE[1]}")
    return value


Action = Literal["BUY", "SELL", "HOLD", "SKIP"]
ExecMode = Literal["auto", "supervised", "manual"]
Tf = Literal["1h", "4h", "1d"]
SignalRead = Literal[
    "bullish", "bearish", "neutral", "trending", "ranging", "low", "normal", "high"
]


class Signal(BaseModel):
    name: str
    tf: Tf
    read: SignalRead


class Gate(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    value: str
    limit: str | None = None  # the threshold the gate measured against


class Plan(BaseModel):
    entry: MoneyDecimal  # signal price the decider proposed
    fill_entry: MoneyDecimal  # expected fill: entry plus entry slippage
    stop: MoneyDecimal
    target: MoneyDecimal
    qty: MoneyDecimal
    notional: MoneyDecimal
    rr: MoneyDecimal


class Regime(BaseModel):
    trend: Literal["up", "down", "sideways"]
    volatility: Literal["low", "normal", "high"]
    age_4h: int


class Decision(BaseModel):
    id: str
    cycle: int
    at: datetime
    pair: str
    decider: Literal["openai", "rules"]
    model: str | None = None
    prompt_version: str | None = None
    action: Action
    confidence: float
    reasoning: str
    invalidation: str | None = None
    signals: list[Signal]
    plan: Plan | None = None
    gates: list[Gate]
    status: Literal["executed", "pending", "approved", "vetoed", "expired", "unfilled", "none"]
    execute_at: datetime | None = None
    error: str | None = None
    regime: Regime
    btc_regime: Regime
    invalidation_price: MoneyDecimal | None = None
    risk_pct_used: MoneyDecimal  # 1.0 normally, 0.5 when throttled
    account_id: int
    settings_version: int
    shadow_id: str | None = None  # None = live
    stand_in: bool = False  # What-if only: Rules answered for a missing model call
    prefilter: bool = True  # False = the Rules pre-filter skipped the model call


class Position(BaseModel):
    pair: str
    qty: MoneyDecimal
    entry: MoneyDecimal
    stop: MoneyDecimal
    target: MoneyDecimal
    opened_at: datetime
    decision_id: str
    last: MoneyDecimal
    unrealised: MoneyDecimal
    # Dashboard bar extras (None until computed for the snapshot).
    stop_dist_pct: MoneyDecimal | None = None  # last price above the stop, %
    stop_dist_atr: MoneyDecimal | None = None  # same distance in ATR(1h)
    pnl_r: MoneyDecimal | None = None  # unrealised P&L in R
    stop_above_entry: bool = False  # stop at or above the entry
    stop_near: bool = False  # amber: closer than settings.ui.stop_near_atr


class PriceInfo(BaseModel):
    last: MoneyDecimal
    chg_24h_pct: MoneyDecimal
    stale: bool


class ActivityItem(BaseModel):
    at: datetime
    kind: str
    pair: str | None
    text: str
    decision_id: str | None


class Usage(BaseModel):
    calls_today: int
    daily_budget: int
    cost_today_eur: MoneyDecimal
    cost_month_eur: MoneyDecimal
    cap_eur: MoneyDecimal


class EntryPause(BaseModel):
    pair: str  # "*" = all coins
    until: datetime | None


class Throttle(BaseModel):
    reasons: list[Literal["streak", "drawdown"]]
    lifts_when: str


class RiskBudget(BaseModel):
    open_risk_pct: MoneyDecimal
    open_risk_cap_pct: MoneyDecimal
    today_loss_pct: MoneyDecimal
    daily_limit_pct: MoneyDecimal
    drawdown_pct: MoneyDecimal
    kill_pct: MoneyDecimal
    full_trades_left: int  # min(open-risk room, cash room), at a 3% stop
    room_limited_by: Literal["risk", "cash"]  # which room bound full_trades_left
    risk_pct_now: MoneyDecimal  # 1.0, or 0.5 while throttled
    throttle: Throttle | None
    shock_brake_until: datetime | None
    correlated_slots_left: int
    # e.g. "BTC + ETH 0.86" — the strongest correlated pair among open
    # positions; None when nothing is open.
    correlated_detail: str | None = None


class KillInfo(BaseModel):
    at: datetime
    peak: MoneyDecimal
    equity: MoneyDecimal
    drawdown_pct: MoneyDecimal
    closed: list[str]  # pairs closed by the kill


class NeedsItem(BaseModel):
    """One thing only the user can resolve, with its single action."""

    kind: str  # "kill"|"approvals"|"errors"|"source"|"email"|"market"|"cost_cap"
    title: str
    detail: str | None = None
    action_label: str
    action_target: str  # frontend route
    since: datetime | None = None


class HealthItem(BaseModel):
    """One health-line reading: Bitvavo, stream, clock, key, collector, email."""

    key: str  # "bitvavo"|"stream"|"clock"|"openai"|"collector"|"email"
    label: str
    state: Literal["ok", "warn", "down", "off", "unknown"]
    value: str | None = None


class Needs(BaseModel):
    items: list[NeedsItem] = []
    health: list[HealthItem] = []


class Snapshot(BaseModel):
    status: Literal["running", "paused", "killed", "error"]
    mode: Literal["paper"]
    source: Literal["bitvavo", "sim"]
    cycle: int
    next_run_at: datetime | None
    cycle_in_progress: bool
    equity: MoneyDecimal
    cash: MoneyDecimal
    start_equity: MoneyDecimal
    today_pnl: MoneyDecimal
    today_pnl_pct: MoneyDecimal
    positions_unrealised: MoneyDecimal
    exposure_pct: float
    drawdown_pct: float
    prices: dict[str, PriceInfo]
    positions: list[Position]
    latest: dict[str, Decision]
    pending: list[Decision]
    activity: list[ActivityItem]
    usage: Usage
    account_id: int
    quiet: str
    risk_budget: RiskBudget
    paused: list[EntryPause]
    errors_24h: int
    errors_open: int
    kill: KillInfo | None
    today_trades: int = 0
    today_won: int = 0
    return_since_start_pct: float = 0.0
    needs: Needs = Needs()
    # BTC buy-and-hold return over the account's life — the "since start"
    # tile's comparison; None until BTC daily candles are stored.
    btc_return_since_start_pct: float | None = None
    account_started_at: datetime | None = None


# ---------------------------------------------------------------- settings --

DEFAULT_MARKETS = [
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "ADA-EUR",
    "LINK-EUR",
    "AVAX-EUR",
    "DOGE-EUR",
    "LTC-EUR",
]


class RiskSettings(BaseModel):
    """Every risk-gate threshold lives here, never as a literal in logic."""

    model_config = ConfigDict(extra="ignore")

    min_rr: Annotated[Decimal, _range(*RANGES["risk.min_rr"])] = Decimal("2.0")
    max_cost_pct_of_risk: Annotated[Decimal, _range(*RANGES["risk.max_cost_pct_of_risk"])] = (
        Decimal("30")
    )
    risk_per_trade_pct: Annotated[Decimal, _range(*RANGES["risk.risk_per_trade_pct"])] = Decimal(
        "1.0"
    )
    throttled_risk_per_trade_pct: Annotated[
        Decimal, _range(*RANGES["risk.throttled_risk_per_trade_pct"])
    ] = Decimal("0.5")
    total_open_risk_pct: Annotated[Decimal, _range(*RANGES["risk.total_open_risk_pct"])] = Decimal(
        "2.5"
    )
    max_exposure_pct: Annotated[Decimal, _range(*RANGES["risk.max_exposure_pct"])] = Decimal("100")
    min_trade_eur: Annotated[Decimal, _range(*RANGES["risk.min_trade_eur"])] = Decimal("10")
    cooldown_after_stopout_hours: Annotated[
        Decimal, _range(*RANGES["risk.cooldown_after_stopout_hours"])
    ] = Decimal("6")
    correlated_max_positions: Annotated[int, _range(*RANGES["risk.correlated_max_positions"])] = 2
    correlated_threshold: Annotated[Decimal, _range(*RANGES["risk.correlated_threshold"])] = (
        Decimal("0.8")
    )
    shock_brake_btc_move_pct: Annotated[
        Decimal, _range(*RANGES["risk.shock_brake_btc_move_pct"])
    ] = Decimal("4")
    shock_brake_hours: Annotated[Decimal, _range(*RANGES["risk.shock_brake_hours"])] = Decimal("2")
    throttle_consecutive_losses: Annotated[
        int, _range(*RANGES["risk.throttle_consecutive_losses"])
    ] = 3
    throttle_drawdown_pct: Annotated[Decimal, _range(*RANGES["risk.throttle_drawdown_pct"])] = (
        Decimal("4")
    )
    throttle_lift_drawdown_pct: Annotated[
        Decimal, _range(*RANGES["risk.throttle_lift_drawdown_pct"])
    ] = Decimal("2")
    daily_loss_limit_pct: Annotated[Decimal, _range(*RANGES["risk.daily_loss_limit_pct"])] = (
        Decimal("3")
    )
    kill_drawdown_pct: Annotated[Decimal, _range(*RANGES["risk.kill_drawdown_pct"])] = Decimal("8")
    stop_min_atr: Annotated[Decimal, _range(*RANGES["risk.stop_min_atr"])] = Decimal("0.5")
    stop_max_atr: Annotated[Decimal, _range(*RANGES["risk.stop_max_atr"])] = Decimal("5")
    stop_buffer_atr: Annotated[Decimal, _range(*RANGES["risk.stop_buffer_atr"])] = Decimal(
        "0.1"
    )  # stop sits this many ATR(1h) below the level
    pending_entry_drift_pct: Annotated[Decimal, _range(*RANGES["risk.pending_entry_drift_pct"])] = (
        Decimal("0.5")
    )
    time_stop_days: Annotated[Decimal, _range(*RANGES["risk.time_stop_days"])] = Decimal("5")
    trailing_stop_enabled: bool = False

    @model_validator(mode="after")
    def _cross_fields(self) -> RiskSettings:
        errors = risk_errors(self)
        if errors:
            key, message = next(iter(errors.items()))
            raise ValueError(f"{key}: {message}")
        return self


class RulesSettings(BaseModel):
    """Rules-decider thresholds — tunable without code changes; every Rules
    decision stores the settings version it used."""

    model_config = ConfigDict(extra="ignore")

    prefilter_min_conds: Annotated[int, _range(*RANGES["rules.prefilter_min_conds"])] = 3
    buy_min_conds: Annotated[int, _range(*RANGES["rules.buy_min_conds"])] = 4
    buy_max_atr_above_ema20: Annotated[
        Decimal, _range(*RANGES["rules.buy_max_atr_above_ema20"])
    ] = Decimal("1.5")
    stop_lookback_bars: Annotated[int, _range(*RANGES["rules.stop_lookback_bars"])] = 6
    stop_atr_pad: Annotated[Decimal, _range(*RANGES["rules.stop_atr_pad"])] = Decimal("0.2")
    stop_max_atr: Annotated[Decimal, _range(*RANGES["rules.stop_max_atr"])] = Decimal("5")
    target_max_atr: Annotated[Decimal, _range(*RANGES["rules.target_max_atr"])] = Decimal("6")
    target_rr: Annotated[Decimal, _range(*RANGES["rules.target_rr"])] = Decimal("2.5")

    @model_validator(mode="after")
    def _cross_fields(self) -> RulesSettings:
        errors = rules_errors(self)
        if errors:
            key, message = next(iter(errors.items()))
            raise ValueError(f"{key}: {message}")
        return self


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decider: Literal["openai", "rules"] = "openai"
    model_id: str = "gpt-4o-mini"
    prompt_version: str = "decision.v1"
    daily_call_budget: Annotated[int, _range(*RANGES["model.daily_call_budget"])] = 1000
    rules_prefilter: bool = True
    monthly_cost_cap_eur: Annotated[Decimal, _range(*RANGES["model.monthly_cost_cap_eur"])] = (
        Decimal("10")
    )


class PaperSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start_balance_eur: Annotated[Decimal, _range(*RANGES["paper.start_balance_eur"])] = Decimal(
        "10000"
    )
    taker_fee_pct: Annotated[Decimal, _range(*RANGES["paper.taker_fee_pct"])] = Decimal("0.25")
    maker_fee_pct: Annotated[Decimal, _range(*RANGES["paper.maker_fee_pct"])] = Decimal("0.15")
    slippage_pct: Annotated[Decimal, _range(*RANGES["paper.slippage_pct"])] = Decimal("0.05")
    # Market (default) or Limit (post-only): entries and targets pay the
    # maker fee and may not fill; stops and forced exits stay market.
    entry_orders: Literal["market", "limit"] = "market"


class MarketSettings(BaseModel):
    """Market-data and eligibility thresholds — all tunable, never literals
    in logic. The shared ADX threshold is used by the reads, the regime,
    Rules condition C6 and Insights."""

    model_config = ConfigDict(extra="ignore")

    adx_threshold: Annotated[Decimal, _range(*RANGES["market.adx_threshold"])] = Decimal("25")
    min_avg_daily_volume_eur: Annotated[
        Decimal, _range(*RANGES["market.min_avg_daily_volume_eur"])
    ] = Decimal("1000000")
    max_avg_spread_pct: Annotated[Decimal, _range(*RANGES["market.max_avg_spread_pct"])] = Decimal(
        "0.15"
    )
    min_daily_candles: Annotated[int, _range(*RANGES["market.min_daily_candles"])] = 300
    eligibility_fail_days: Annotated[int, _range(*RANGES["market.eligibility_fail_days"])] = 3
    eligibility_beyond_pct: Annotated[Decimal, _range(*RANGES["market.eligibility_beyond_pct"])] = (
        Decimal("20")
    )
    eligibility_recover_days: Annotated[int, _range(*RANGES["market.eligibility_recover_days"])] = 3
    check_data_error_pct: Annotated[Decimal, _range(*RANGES["market.check_data_error_pct"])] = (
        Decimal("50")
    )
    history_1h: Annotated[int, _range(*RANGES["market.history_1h"])] = 800
    history_4h: Annotated[int, _range(*RANGES["market.history_4h"])] = 300
    history_1d: Annotated[int, _range(*RANGES["market.history_1d"])] = 300
    structure_min_range_bars: Annotated[int, _range(*RANGES["market.structure_min_range_bars"])] = (
        12
    )
    stale_grace_s: Annotated[int, _range(*RANGES["market.stale_grace_s"])] = 900
    # Market-context collector: sustained Deribit public requests per second.
    # Deribit cut public/ticker to 5/s (burst 25) on 19 Nov 2026; 2/s keeps
    # the hourly ~216-request skew fetch well under the limit.
    deribit_requests_per_second: Annotated[
        float, _range(*RANGES["market.deribit_requests_per_second"])
    ] = 2.0
    stablecoin_bases: list[str] = [
        "USDC",
        "EURC",
        "USDT",
        "DAI",
        "FDUSD",
        "TUSD",
        "PYUSD",
        "EUR",
        "USD",
        "GBP",
    ]


class SmtpSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = ""
    port: Annotated[int, _range(*RANGES["notifications.smtp.port"])] = 587
    tls: bool = True
    username: str = ""
    sender: str = ""
    recipient: str = ""


class NotificationEventSettings(BaseModel):
    browser: bool
    email: bool


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    browser_enabled: bool = False
    email_enabled: bool = False
    smtp: SmtpSettings = SmtpSettings()
    events: dict[str, NotificationEventSettings] = {
        "kill_switch": NotificationEventSettings(browser=True, email=True),
        "daily_loss": NotificationEventSettings(browser=True, email=True),
        "pair_stale": NotificationEventSettings(browser=True, email=False),
        "source_failing": NotificationEventSettings(browser=True, email=False),
        "model_failing": NotificationEventSettings(browser=True, email=True),
        "trade": NotificationEventSettings(browser=False, email=False),
        "risk_control": NotificationEventSettings(browser=True, email=False),
        "review": NotificationEventSettings(browser=False, email=True),
    }


class LearningSettings(BaseModel):
    """Learning and explanations: plain-language mode, the quiet line and
    the scheduled model-written notes."""

    model_config = ConfigDict(extra="ignore")

    plain_language: bool = True
    quiet_line: bool = True
    briefing_time: str = Field(
        default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )  # HH:MM Europe/Amsterdam
    weekly_review: Literal["mon_08", "sun_20", "off"] = "mon_08"
    ask_daily_limit: Annotated[int, _range(*RANGES["learning.ask_daily_limit"])] = 100
    insights_min_decisions: Annotated[int, _range(*RANGES["learning.insights_min_decisions"])] = 30


class ReadinessSettings(BaseModel):
    """The live-readiness thresholds editable in Settings; the other
    criteria (average R, profit factor, beats BTC, no kill, no errors) use
    the spec defaults and live in app.agent.readiness."""

    model_config = ConfigDict(extra="ignore")

    min_closed_trades: Annotated[int, _range(*RANGES["readiness.min_closed_trades"])] = 100
    min_days_running: Annotated[int, _range(*RANGES["readiness.min_days_running"])] = 30
    max_drawdown_pct: Annotated[Decimal, _range(*RANGES["readiness.max_drawdown_pct"])] = Decimal(
        "6"
    )
    calibration_gap_pts: Annotated[Decimal, _range(*RANGES["readiness.calibration_gap_pts"])] = (
        Decimal("10")
    )


class UiSettings(BaseModel):
    """Display thresholds for the GUI — display rules live here, never as
    literals in logic."""

    model_config = ConfigDict(extra="ignore")

    stop_near_atr: Annotated[Decimal, _range(*RANGES["ui.stop_near_atr"])] = Decimal("1.5")
    log_watch_min_count: Annotated[int, _range(*RANGES["ui.log_watch_min_count"])] = 5


class InspectorAttentionSettings(BaseModel):
    """The thresholds for a call's stored attention level."""

    model_config = ConfigDict(extra="ignore")

    act_in_a_row_cycles: Annotated[
        int, _range(*RANGES["inspector.attention.act_in_a_row_cycles"])
    ] = 3
    act_429_per_day: Annotated[int, _range(*RANGES["inspector.attention.act_429_per_day"])] = 3
    act_invalid_count: Annotated[int, _range(*RANGES["inspector.attention.act_invalid_count"])] = 3
    act_invalid_window: Annotated[
        int, _range(*RANGES["inspector.attention.act_invalid_window"])
    ] = 20
    act_number_check_count: Annotated[
        int, _range(*RANGES["inspector.attention.act_number_check_count"])
    ] = 3
    act_number_check_window: Annotated[
        int, _range(*RANGES["inspector.attention.act_number_check_window"])
    ] = 5
    watch_within_cycles: Annotated[
        int, _range(*RANGES["inspector.attention.watch_within_cycles"])
    ] = 2
    watch_slow_per_day: Annotated[
        int, _range(*RANGES["inspector.attention.watch_slow_per_day"])
    ] = 3
    watch_claim_share_pct: Annotated[
        int, _range(*RANGES["inspector.attention.watch_claim_share_pct"])
    ] = 20
    watch_claim_window: Annotated[
        int, _range(*RANGES["inspector.attention.watch_claim_window"])
    ] = 20


class InspectorSettings(BaseModel):
    """Inspector display and attention thresholds."""

    model_config = ConfigDict(extra="ignore")

    slow_call_seconds: Annotated[int, _range(*RANGES["inspector.slow_call_seconds"])] = 15
    page_size: Annotated[int, _range(*RANGES["inspector.page_size"])] = 50
    attention: InspectorAttentionSettings = InspectorAttentionSettings()


class LoggingSettings(BaseModel):
    """The log store's knobs — every value the spec makes configurable."""

    model_config = ConfigDict(extra="ignore")

    console_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    queue_size: int = 20000
    # Days kept per level: debug 7, info 30, warning 180, error/critical 365.
    keep_days: Annotated[dict[str, int], AfterValidator(_keep_days_ok)] = {
        "DEBUG": 7,
        "INFO": 30,
        "WARNING": 180,
        "ERROR": 365,
        "CRITICAL": 365,
    }
    max_db_mb: Annotated[int, _range(*RANGES["logging.max_db_mb"])] = 2000
    backtest_debug: bool = False  # backtest children log INFO+ unless this is on
    library_debug: bool = False  # library loggers otherwise land at WARNING+


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shutdown_grace_seconds: int = 60


class TimeoutSettings(BaseModel):
    """One configured timeout per external call — never a literal at the
    call site."""

    model_config = ConfigDict(extra="ignore")

    bitvavo_rest_s: Annotated[float, _range(*TIMEOUT_RANGE)] = 10
    stream_read_s: Annotated[float, _range(*TIMEOUT_RANGE)] = 60
    openai_s: Annotated[float, _range(*TIMEOUT_RANGE)] = 30
    collector_s: Annotated[float, _range(*TIMEOUT_RANGE)] = 10
    smtp_s: Annotated[float, _range(*TIMEOUT_RANGE)] = 15
    keyring_s: Annotated[float, _range(*TIMEOUT_RANGE)] = 5


class Settings(BaseModel):
    """The one validated JSON document stored in the settings table.

    Unknown keys are dropped on load; new fields added later take their default.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    active_markets: list[str] = DEFAULT_MARKETS.copy()
    execution_mode: ExecMode = "auto"
    veto_window_s: Annotated[int, _range(*RANGES["veto_window_s"])] = 60
    cycle_offset_s: Annotated[int, _range(*RANGES["cycle_offset_s"])] = 45
    data_source: Literal["bitvavo", "sim"] = "bitvavo"
    risk: RiskSettings = RiskSettings()
    rules: RulesSettings = RulesSettings()
    model: ModelSettings = ModelSettings()
    paper: PaperSettings = PaperSettings()
    market: MarketSettings = MarketSettings()
    notifications: NotificationSettings = NotificationSettings()
    learning: LearningSettings = LearningSettings()
    readiness: ReadinessSettings = ReadinessSettings()
    ui: UiSettings = UiSettings()
    inspector: InspectorSettings = InspectorSettings()
    logging: LoggingSettings = LoggingSettings()
    storage: StorageSettings = StorageSettings()
    timeouts: TimeoutSettings = TimeoutSettings()
    # Source ids the user disabled on the Sources page; the registry keeps
    # the runtime status.
    disabled_sources: list[str] = []


# ------------------------------------------------------ settings sections --


class SettingsSaveIn(BaseModel):
    """PUT /api/settings body: the whole draft, a required reason and the
    "it is me" flag for saves that loosen something."""

    settings: Settings
    reason: str
    confirm_looser: bool = False


class SettingsSaveOut(BaseModel):
    version: int
    settings: Settings


class SettingsMetaItem(BaseModel):
    """One leaf's description for the sectioned Settings screen."""

    section: str | None  # None = accepted by the API, off the screen
    default: object  # the Settings() default, JSON value
    min: str | None = None
    max: str | None = None
    step: str | None = None
    unit: str | None = None
    choices: list[str] | None = None
    direction: str | None = None  # which way is looser; None = uncovered
    fixed: bool = False


class SettingsMetaOut(BaseModel):
    version: int
    items: dict[str, SettingsMetaItem]


class SettingsChangeOut(BaseModel):
    """One leaf that differs between two documents, tagged tighter /
    looser / neutral by the directions map."""

    key: str
    old: object | None = None
    new: object | None = None
    direction: str  # "looser" | "tighter" | "neutral"


class SettingsPreviewSummary(BaseModel):
    """The "What these limits mean today" sentences, as data. Money is a
    decimal string; the frontend only formats it."""

    equity: MoneyDecimal
    day_start_equity: MoneyDecimal
    peak_equity: MoneyDecimal
    loss_at_stop_eur: MoneyDecimal  # one trade at full risk hitting its stop
    throttled_loss_eur: MoneyDecimal  # the same while the throttle is on
    positions_at_full_risk: int
    next_position_pct: MoneyDecimal  # what the next position gets, %
    daily_stop_eur: MoneyDecimal  # loss at the daily limit, from day start
    kill_level_eur: MoneyDecimal  # equity level that fires the kill switch


class SettingsPreviewOut(BaseModel):
    """POST /api/settings/preview: nothing is saved. ``equivalents`` maps a
    dotted key to its euro amount ("€83.30"); ``errors`` maps a dotted key
    to its range or cross-field message."""

    equivalents: dict[str, str]
    summary: SettingsPreviewSummary
    changes: list[SettingsChangeOut]
    errors: dict[str, str]


class SettingsHistoryVersionOut(BaseModel):
    version: int
    at: datetime
    reason: str
    source: str
    changes: list[SettingsChangeOut]


class SettingsHistoryRowOut(BaseModel):
    """A per-setting change from before settings versions (item 025)."""

    at: datetime
    key: str
    old: str | None
    new: str | None


class SettingsHistoryPageOut(BaseModel):
    versions: list[SettingsHistoryVersionOut]
    rows: list[SettingsHistoryRowOut]
    next_cursor: str | None = None


# ---------------------------------------------------------- screen payloads --
# Response shapes for the PR-4 screens. Everything the UI renders arrives
# computed; the frontend only formats.


class CandlePoint(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class SeriesPoint(BaseModel):
    time: datetime
    value: float | None


class TrendPoint(BaseModel):
    time: datetime
    value: Literal["up", "down", "sideways"]


class ChartMarker(BaseModel):
    """A trade fill or decision stamped on the bar where it happened."""

    time: datetime
    kind: Literal["buy", "sell", "decision"]
    action: Action | None = None
    label: str
    decision_id: str | None = None


class PositionLines(BaseModel):
    entry: MoneyDecimal
    stop: MoneyDecimal
    target: MoneyDecimal


class CandleFeed(BaseModel):
    """Dashboard chart: candles plus trade markers and open-position lines."""

    pair: str
    tf: Tf
    candles: list[CandlePoint]
    markers: list[ChartMarker]
    position: PositionLines | None


class DecisionListOut(BaseModel):
    items: list[Decision]


class OutcomeOut(BaseModel):
    horizon: int
    reference_at: datetime
    reference_price: MoneyDecimal
    forward_return_pct: MoneyDecimal
    best_move_pct: MoneyDecimal
    worst_move_pct: MoneyDecimal
    best_move_r: MoneyDecimal | None
    worst_move_r: MoneyDecimal | None
    first_touch: str | None
    invalidated: bool
    invalidation_followup_pct: MoneyDecimal | None
    label: str
    realised_r: MoneyDecimal | None
    computed_at: datetime
    method_version: str


class OutcomeFailureOut(BaseModel):
    horizon: int
    reason: str


class PaperOrderOut(BaseModel):
    """One simulated limit order (entry or target leg)."""

    id: int
    decision_id: str
    pair: str
    side: Literal["buy", "sell"]
    leg: Literal["entry", "target"]
    limit_price: MoneyDecimal
    qty: MoneyDecimal
    placed_at: datetime
    expires_at: datetime | None = None
    status: Literal["open", "filled", "cancelled"]
    filled_at: datetime | None = None
    fill_price: MoneyDecimal | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    min_price: MoneyDecimal | None = None  # lowest trade price while open
    last_price: MoneyDecimal | None = None  # current market price (display only)
    limit_gap_pct: MoneyDecimal | None = None  # absolute distance from last price, %
    limit_gap_side: Literal["above", "below"] | None = None  # where last sits vs the limit


class DecisionDetail(BaseModel):
    decision: Decision
    raw_brief: str | None = None
    raw_response: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    execution_mode: ExecMode
    fill_price: MoneyDecimal | None = None
    outcomes: list[OutcomeOut] = []
    outcome_failures: list[OutcomeFailureOut] = []
    outcome_pending_until: datetime | None = None
    entry_order: PaperOrderOut | None = None


# ------------------------------------------------------- Positions & orders --


class StopHistoryItem(BaseModel):
    """One stop level on a position card: the initial stop at entry, then
    each breakeven/trailing move the watcher made."""

    kind: Literal["initial", "breakeven", "trailing"]
    stop: MoneyDecimal
    at: datetime


class LatestDecisionOut(BaseModel):
    """The pair's newest decision, for the card's "Latest decision" link."""

    id: str
    at: datetime
    action: str
    confidence: float
    limit_price: MoneyDecimal | None = None  # the entry order's limit, if any


class PositionCardOut(BaseModel):
    """One open position with every fact the card shows (016)."""

    pair: str
    qty: MoneyDecimal
    entry: MoneyDecimal
    stop: MoneyDecimal
    target: MoneyDecimal
    opened_at: datetime
    decision_id: str
    last: MoneyDecimal
    unrealised: MoneyDecimal
    stop_dist_pct: MoneyDecimal | None
    stop_dist_atr: MoneyDecimal | None
    target_dist_pct: MoneyDecimal | None
    target_dist_atr: MoneyDecimal | None
    pnl_r: MoneyDecimal | None
    stop_above_entry: bool
    stop_near: bool
    stop_hit_eur: MoneyDecimal  # qty x (stop - entry), before fees
    stop_hit_r: MoneyDecimal | None
    stop_hit_pct_equity: MoneyDecimal | None
    rr_at_entry: MoneyDecimal | None
    time_stop_at: datetime | None
    entry_order_type: Literal["market", "limit"]
    round_trip_fees_eur: MoneyDecimal  # entry fee + estimated exit fee
    stop_history: list[StopHistoryItem]
    latest_decision: LatestDecisionOut | None
    watcher_checked_at: datetime | None


class PositionsTotalsOut(BaseModel):
    count: int
    unrealised: MoneyDecimal
    stop_hit_total: MoneyDecimal  # sum of every position's stop-hit result
    open_risk_pct: Decimal  # broker.open_risk() as % of equity
    open_risk_cap_pct: Decimal
    exposure: MoneyDecimal
    exposure_pct: float
    cash: MoneyDecimal
    fees_today: MoneyDecimal
    fees_total: MoneyDecimal  # every fill of this account, all time
    closest_pair: str | None  # nearest stop in ATR(1h)
    closest_atr: Decimal | None


class ShadowRefOut(BaseModel):
    """One shadow strategy for the Live / Shadow switch — running or
    stopped, by name."""

    id: str
    name: str
    model: str
    prompt_version: str
    status: str  # "running" | "stopped"
    started_at: datetime  # its paper account's start
    entry_orders: str  # "market" | "limit" — its paper entry order type


class TodayItemOut(BaseModel):
    """One fill since 00:00 Amsterdam (live account only)."""

    at: datetime
    side: Literal["buy", "sell"]
    pair: str
    qty: MoneyDecimal
    price: MoneyDecimal
    reason: str
    decision_id: str
    trade_id: int | None
    still_open: bool = False  # a buy fill whose position is still open
    action: str | None = None  # the decision's action (entries)
    confidence: float | None = None
    rr: MoneyDecimal | None = None
    held_seconds: int | None = None  # exits: opened -> closed
    realised_eur: MoneyDecimal | None = None
    realised_r: MoneyDecimal | None = None
    fees_eur: MoneyDecimal | None = None  # the closed trade's both-side fee


class TodayOut(BaseModel):
    items: list[TodayItemOut]
    entries: int
    exits: int
    realised: MoneyDecimal
    fees: MoneyDecimal


class PaperAccountOut(BaseModel):
    """The live paper account card (live only)."""

    id: int
    started_at: datetime
    start_balance: MoneyDecimal
    cash: MoneyDecimal
    coins_value: MoneyDecimal  # equity - cash
    equity: MoneyDecimal
    realised_pnl: MoneyDecimal
    closed_trades: int
    fees_total: MoneyDecimal
    slippage_total: MoneyDecimal


class PositionsOut(BaseModel):
    """GET /api/positions: one account's positions, working orders and
    totals; ``today`` and ``paper_account`` only for account=live."""

    account: str  # "live" or the shadow id
    shadows: list[ShadowRefOut]
    totals: PositionsTotalsOut
    positions: list[PositionCardOut]
    orders: list[PaperOrderOut]
    today: TodayOut | None = None
    paper_account: PaperAccountOut | None = None


class FillOut(BaseModel):
    """One paper fill for the order history (live account only)."""

    id: int
    at: datetime
    pair: str
    side: Literal["buy", "sell"]
    qty: MoneyDecimal
    reference_price: MoneyDecimal | None
    fill_price: MoneyDecimal
    order_type: Literal["market", "limit"]
    fee_eur: MoneyDecimal
    fee_kind: Literal["maker", "taker"]
    slippage_eur: MoneyDecimal | None
    reason: str
    decision_id: str
    decision_action: str | None = None  # the decision's action, for the link label
    decision_at: datetime | None = None
    trade_id: int | None
    paper_order_id: int | None
    source: Literal["live", "rebuilt"]


class FillsPageOut(BaseModel):
    items: list[FillOut]
    next_cursor: str | None = None
    total: int = 0  # every fill matching the filters, all pages


class LevelOut(BaseModel):
    kind: Literal["support", "resistance", "prev_day_high", "prev_day_low"]
    price: float
    dist_pct: float | None  # signed distance from last price, %
    dist_atr: float | None  # same distance in 1h ATRs


class IndicatorCell(BaseModel):
    text: str | None
    read: SignalRead | None


class IndicatorRowOut(BaseModel):
    name: str
    threshold: str
    cells: dict[str, IndicatorCell]  # keyed "1h" | "4h" | "1d"


class LiquidityOut(BaseModel):
    spread_pct: float | None
    depth_eur: float | None
    vol24h_vs_30d: float | None


class HealthOut(BaseModel):
    bars_loaded: int
    bars_requested: int
    newest_closed_age_s: int | None
    stale_limit_s: int
    stale: bool
    missing_bars: int
    source: str


class IndicatorsOut(BaseModel):
    """The whole Indicators screen in one payload.

    ``frozen_*`` is set when the payload is pinned to a past decision
    (``?at=<decision id>``): every value is computed from candles that were
    closed at that moment.
    """

    pair: str
    tf: Tf
    frozen: bool
    frozen_at: datetime | None
    frozen_decision_id: str | None
    frozen_cycle: int | None
    candles: list[CandlePoint]
    series: dict[str, list[SeriesPoint]]
    regime_band: list[TrendPoint]
    regime: Regime
    btc_regime: Regime | None
    current: list[IndicatorRowOut]
    levels: list[LevelOut]
    structure: list[str]
    context: dict[str, float | None]
    liquidity: LiquidityOut
    health: HealthOut
    markers: list[ChartMarker]
    adx_threshold: float


class EquityPoint(BaseModel):
    at: datetime
    equity: MoneyDecimal


class DrawdownPoint(BaseModel):
    at: datetime
    pct: float


class PerfTotals(BaseModel):
    return_pct: float | None
    benchmark_return_pct: float | None
    vs_btc_pct: float | None
    max_drawdown_pct: float | None
    trades: int
    open_now: int
    win_rate_pct: float | None
    wins: int
    avg_r: float | None
    profit_factor: float | None
    fees_eur: MoneyDecimal
    time_in_market_pct: float | None


class PerPairPerf(BaseModel):
    pair: str
    trades: int
    realised_eur: MoneyDecimal
    avg_r: float | None
    fees_eur: MoneyDecimal


class PerformanceOut(BaseModel):
    start: datetime
    end: datetime
    equity: list[EquityPoint]
    benchmark: list[SeriesPoint]
    drawdown: list[DrawdownPoint]
    kill_line_pct: float
    totals: PerfTotals
    per_pair: list[PerPairPerf]


class TradeOut(BaseModel):
    id: int
    pair: str
    qty: MoneyDecimal
    entry: MoneyDecimal
    exit: MoneyDecimal
    opened_at: datetime
    closed_at: datetime
    fee_eur: MoneyDecimal
    realised_eur: MoneyDecimal
    realised_r: float
    exit_reason: str
    decision_id: str


class TradeListOut(BaseModel):
    items: list[TradeOut]


class ActivityOut(BaseModel):
    id: int
    at: datetime
    kind: str
    pair: str | None
    text: str
    decision_id: str | None


class ActivityListOut(BaseModel):
    items: list[ActivityOut]
    next_before: int | None


class FlattenPreview(BaseModel):
    count: int
    pairs: list[str]
    est_fees_eur: MoneyDecimal
    unrealised_eur: MoneyDecimal


class PromptInfo(BaseModel):
    id: str
    model: str
    temperature: float
    max_output_tokens: int


class ErrorOut(BaseModel):
    id: int
    first_at: datetime
    last_at: datetime
    pair: str | None
    text: str
    affected_decision_or_fill: bool
    resolved_at: datetime | None
    resolution_note: str | None


class ResolveErrorIn(BaseModel):
    note: str


class DeleteDataRequest(BaseModel):
    confirm: Literal["DELETE"]


# --------------------------------------------------- LLM inspector payloads --


class CheckOut(BaseModel):
    """One input or output check result shown on a call card."""

    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str = ""


class LlmCallAttemptOut(BaseModel):
    at: datetime | None
    outcome: str  # "sent" | "retried" | "failed"
    error: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None


class LlmCallListItem(BaseModel):
    """One call-history row; ``kind == "skipped"`` is a decision row where
    the Rules pre-filter passed nothing to the model."""

    kind: Literal["call", "skipped"]
    id: str  # call id, or decision id for skipped rows
    at: datetime
    pair: str
    feature: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    status: str | None = None  # "ok" | "invalid_output" | "timeout" | "api_error" | "input_blocked"
    action: Action | None = None  # what the model (or Rules, for skipped) answered
    final_action: Action | None = None  # the decision the engine recorded
    issues: int = 0  # warning + failed output checks
    verdict: str | None = None
    decision_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_eur: MoneyDecimal | None = None
    latency_ms: int | None = None
    source: str | None = None  # live | shadow | replay | text | ask
    outcome_status: str | None = None  # success | warning | error
    reasons: list[LlmCallReasonOut] = []
    attention: str | None = None  # none | watch | act
    outcome_label: str | None = None  # the decision outcome after 72h
    fold_key: str | None = None


class LlmCallReasonOut(BaseModel):
    code: str
    label: str
    detail: str | None = None


class LlmCallListOut(BaseModel):
    items: list[LlmCallListItem]
    next_cursor: str | None = None
    total: int = 0


class LlmCallTopReasonOut(BaseModel):
    code: str
    label: str
    detail: str | None = None
    count: int
    example: str | None = None
    attention: str = "none"


class LlmCallSummaryOut(BaseModel):
    calls: int
    cycles_without_call: int
    counts: dict[str, int]
    shares: dict[str, float]
    attention_counts: dict[str, int]
    cost_eur: dict[str, MoneyDecimal]
    latency_ms: dict[str, int | None]
    buckets: list[dict[str, object]]
    option_counts: dict[str, dict[str, int]]
    top_reasons: list[LlmCallTopReasonOut]


class LlmCallExplainedReasonOut(BaseModel):
    code: str
    label: str
    meaning: str
    likely_cause: str
    why_here: str
    what_to_do: str
    attention: str
    attention_why: str | None = None
    links: list[dict[str, str]] = []


class BriefSectionRow(BaseModel):
    """One label/value/read row in the inspector's brief sections."""

    label: str
    value: str
    read: str | None = None
    path: str


class BriefSection(BaseModel):
    title: str
    rows: list[BriefSectionRow]


class ReplayInfo(BaseModel):
    id: str
    at: datetime
    model: str
    prompt_version: str
    action: Action | None
    differs: bool  # parsed action differs from the original call's


class LlmCallDetail(BaseModel):
    """Everything stored for one call: the exact request and response, the
    checks, retries, cost and the decision it produced."""

    id: str
    at: datetime
    cycle: int | None
    pair: str
    decision_id: str | None
    feature: str
    replay_of: str | None
    model: str
    prompt_version: str
    status: str
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: MoneyDecimal | None
    cost_eur: MoneyDecimal | None
    request_id: str | None
    request_json: str | None  # exact request body sent
    response_raw: str | None  # exact response body returned
    system_prompt: str | None  # extracted from request_json
    brief: str | None  # the market brief, extracted from request_json
    output_schema: str | None  # the structured-output schema sent
    parsed: dict[str, object] | None
    input_checks: list[CheckOut]
    output_checks: list[CheckOut]
    attempts: list[LlmCallAttemptOut]
    replays: list[ReplayInfo]
    decision_action: Action | None = None
    verdict: str | None
    verdict_note: str | None
    verdict_at: datetime | None
    brief_upgraded: str | None = None  # e.g. "v1 → v2" on a replay whose brief was upgraded
    outcome_status: str = "success"
    reasons: list[LlmCallReasonOut] = []
    attention: str = "none"
    explained_reasons: list[LlmCallExplainedReasonOut] = []
    result_sentence: str = ""
    final_action_sentence: str = ""
    sections: list[BriefSection] = []
    prompt_hash_match: bool | None = None
    payload_cleared: bool = False
    newer_id: str | None = None
    older_id: str | None = None
    outside_filters: bool = False


class LlmCallReplayIn(BaseModel):
    model: str | None = None  # None keeps the original model
    prompt_version: str | None = None


class LlmCallVerdictIn(BaseModel):
    verdict: Literal["correct", "wrong"] | None
    note: str | None = None


class OutcomeScorecardRow(BaseModel):
    decider: str
    model: str | None
    prompt_version: str | None
    decisions: int
    buy_win_rate_pct: float | None
    average_r: float | None
    missed: int
    avoided: int


class OutcomeScorecardOut(BaseModel):
    items: list[OutcomeScorecardRow]


class CostsOut(BaseModel):
    """Estimated spend per feature for one month, against the cap."""

    month: str
    cap_eur: MoneyDecimal
    spent_eur: MoneyDecimal
    spent_usd: MoneyDecimal | None  # None when any call's price was unknown
    by_feature: dict[str, MoneyDecimal]


# ------------------------------------------------- understanding & safety --


class EntryPauseIn(BaseModel):
    """``until`` sets a timed pause; omitted/None pauses until resumed."""

    until: datetime | None = None


class GlossaryTermOut(BaseModel):
    term: str
    definition: str


class GlossaryOut(BaseModel):
    terms: list[GlossaryTermOut]
    plain_language: bool  # the current setting; tooltips render when on


class ReadinessRowOut(BaseModel):
    """One live-readiness criterion: what it checks, where we stand and
    what is needed, all formatted in code."""

    id: str
    name: str
    why: str
    value_text: str  # "38" / "+4.13% vs -2.10%" / "0 kills"
    target_text: str  # "≥ 100" / "ahead" / "0"
    progress: float  # 0..1 for the progress bar
    met: bool
    hint: str | None = None  # "≈ 7 Dec" / "needs better trades, not time"
    estimated_at: datetime | None = None  # count criteria at the current pace
    loosened: str | None = None  # "was 120, changed 10 Oct" when loosened


class ReadinessOut(BaseModel):
    met: int
    total: int
    status: str  # headline: "Not ready for real money yet" / all met
    summary: str  # why, in one or two sentences
    estimate: str | None  # "At the current pace, all criteria …"
    account_id: int
    account_started_at: datetime
    checked_at: datetime
    rows: list[ReadinessRowOut]


class BriefingOut(BaseModel):
    id: int
    at: datetime
    type: Literal["daily", "weekly"]
    facts: dict[str, object]
    text: str
    source: Literal["model", "template"]
    reason: str | None  # why the plain template was used
    llm_call_id: str | None


class BriefingListOut(BaseModel):
    items: list[BriefingOut]


# --------------------------------------------------------------- evidence --


class DeciderOption(BaseModel):
    """One selectable decider in the Insights filter."""

    id: str  # "openai" | "rules" | "shadow:<id>" | "all"
    label: str


class InsightSliceOut(BaseModel):
    """One row of the evidence table: a slice of decisions by the chosen
    dimension with its outcome metrics, all computed in the backend."""

    key: str
    label: str
    decisions: int
    buys: int
    buy_win_rate_pct: float | None
    avg_r: float | None
    avoided_pct: float | None  # of scored HOLD/SKIP outcomes
    missed_pct: float | None
    state: Literal["finding", "early"]  # slices under 10 are not returned


class CalibrationBucketOut(BaseModel):
    """One confidence band: average stated confidence vs actual win rate."""

    label: str  # "0.5-0.6" ... "0.8+"
    buys: int
    stated_pct: float | None
    actual_pct: float | None


class InsightFinding(BaseModel):
    """A card at the top of the Insights screen — a mechanically derived
    observation from finding-level slices only."""

    kind: Literal["overconfident", "underconfident", "weak_spot", "strength"]
    title: str
    text: str
    n: int
    whatif_hint: str | None = None  # e.g. "Try skipping weekends in What-if"


class InsightsOut(BaseModel):
    dimension: str
    start: datetime
    end: datetime
    decider: str
    prompt_version: str | None
    min_decisions: int
    slice_min: int  # slices under this are hidden, at/above show greyed
    total_decisions: int
    decisions_needed: int  # until the largest slice reaches the finding bar
    largest_slice_label: str | None  # biggest slice even when under slice_min
    largest_slice_decisions: int
    decider_options: list[DeciderOption]
    findings: list[InsightFinding]
    slices: list[InsightSliceOut]
    calibration: list[CalibrationBucketOut]
    calibration_gap_pts: float | None


# ----------------------------------------------------------------- what-if --


class WhatIfSpecIn(BaseModel):
    """The scenario definition: a stored window plus dotted settings
    overrides. ``no_weekend_entries`` is a scenario-only switch (the
    design's checkbox) — it is not a live setting."""

    start: datetime = Field(alias="from")
    end: datetime = Field(alias="to")
    overrides: dict[str, object] = {}
    name: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class WhatIfJobOut(BaseModel):
    id: str
    status: Literal["queued", "running", "done", "failed", "interrupted"]
    progress: float
    name: str | None
    saved: bool
    created_at: datetime
    updated_at: datetime
    spec: WhatIfSpecIn
    error: str | None
    result: WhatIfResult | None = None


class WhatIfMetric(BaseModel):
    label: str
    unit: Literal["pct", "eur", "r", "count"] = "count"
    actual: float | None
    scenario: float | None
    change: float | None  # scenario - actual (percentage points / R / EUR)


class WhatIfTrade(BaseModel):
    pair: str
    opened_at: datetime
    closed_at: datetime | None
    entry: MoneyDecimal
    exit: MoneyDecimal | None
    realised_r: MoneyDecimal | None
    realised_eur: MoneyDecimal | None
    reason: str  # why it appears: "blocked by X" for removals, "stand-in"…
    exit_reason: str | None
    stand_in: bool
    decision_id: str | None


class WhatIfResult(BaseModel):
    metrics: list[WhatIfMetric]
    equity: list[SeriesPoint]  # scenario equity curve
    actual: list[SeriesPoint]  # what actually happened
    removed: list[WhatIfTrade]
    added: list[WhatIfTrade]
    stand_ins: int
    trades: int  # scenario closed-trade count


class WhatIfSaveIn(BaseModel):
    name: str | None = None


class WhatIfApplyIn(BaseModel):
    confirm: Literal["APPLY"]


# ----------------------------------------------------------------- shadows --


class ShadowIn(BaseModel):
    name: str | None = None  # defaults to "Shadow A"/"Shadow B"
    model: str
    prompt_version: str
    overrides: dict[str, object] = {}
    daily_call_limit: int = 300


class ShadowPause(BaseModel):
    start: datetime
    end: datetime | None  # null while the pause is in force


class ShadowOut(BaseModel):
    id: str
    name: str
    model: str
    prompt_version: str
    overrides: dict[str, object]
    status: Literal["running", "stopped"]
    paused: bool  # inside a cost-cap pause window (no new decisions)
    pauses: list[ShadowPause]
    created_at: datetime
    stopped_at: datetime | None
    account_id: int
    start_equity: MoneyDecimal
    equity: MoneyDecimal | None
    return_pct: float | None  # since creation, pause windows excluded
    live_return_pct: float | None  # live return over the same stretches
    decisions: int
    daily_call_limit: int
    calls_today: int
    entry_orders: Literal["market", "limit"] = "market"
    orders_placed: int = 0  # limit entry orders placed while running
    orders_filled: int = 0
    missed_fill_avg_r: MoneyDecimal | None = None  # avg R unfilled entries would have made


class ShadowPromoteIn(BaseModel):
    confirm: Literal["PROMOTE"]


# --------------------------------------------------------------------- ask --


class AskCitation(BaseModel):
    """A record the answer used, as a link target."""

    kind: Literal["decision", "trade", "call"]
    id: str
    label: str
    href: str


class AskMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    at: datetime
    citations: list[AskCitation] = []
    call_ids: list[str] = []
    records_used: int = 0
    model: str | None = None
    cost_eur: MoneyDecimal | None = None
    source: Literal["model", "template"] = "model"


class AskContext(BaseModel):
    """The screen the panel floats over — e.g. an open decision."""

    page: str | None = None
    pair: str | None = None
    decision_id: str | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    context: AskContext | None = None


class AskOut(BaseModel):
    conversation_id: str
    answer: AskMessageOut
    questions_today: int
    daily_limit: int
    remaining: int


class AskConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: datetime
    turns: int


class AskConversationOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    messages: list[AskMessageOut]


class AskConversationListOut(BaseModel):
    items: list[AskConversationSummary]
    questions_today: int
    daily_limit: int


# -------------------------------------------------------- outcomes stats --


class OutcomeStatsRow(OutcomeScorecardRow):
    """The scorecard grouped by the requested keys (spec's stats view)."""

    shadow: str | None = None


class OutcomeStatsOut(BaseModel):
    items: list[OutcomeStatsRow]


# ------------------------------------------------------------ cycles card --


class CycleSlotChange(BaseModel):
    """Something that happened inside a quarter-hour slot."""

    kind: Literal["entry", "exit", "stop_move", "error"]
    pair: str | None = None
    detail: str  # short text for tooltips, e.g. "SOL-EUR 136.10 → 139.10 · breakeven"
    decision_id: str | None = None
    exit_gain: bool | None = None  # exits only


class CycleSlot(BaseModel):
    """One quarter-hour slot on the rolling timeline."""

    at: datetime  # UTC quarter-hour boundary
    state: Literal[
        "entry",
        "exit_gain",
        "exit_loss",
        "stop_moved",
        "error",
        "no_change",
        "missed",
        "paused",
        "next",
        "future",
    ]
    missed_reason: str | None = None  # "asleep" | "stopped" | "paused"
    cycle: int | None = None
    changes: list[CycleSlotChange] = []
    jobs: list[str] = []  # "eligibility" | "backup" | "briefing" | "weekly_review"


class CycleGroup(BaseModel):
    """One action+reason group inside a cycle's decisions."""

    label: str  # "BUY" | "SELL" | "HOLD" | "SKIP" | "NO CALL"
    reason: str | None = None  # gate name, "pre-filter", "positions kept", "model exit"
    pairs: list[str] = []
    count: int
    decision_id: str | None = None
    detail: str | None = None  # e.g. "0.0807 @ 61,240 · R/R 2.1"
    change: bool = False  # an executed entry or exit


class CycleDecisions(BaseModel):
    slot_at: datetime
    cycle: int | None = None
    groups: list[CycleGroup] = []
    changes: list[CycleSlotChange] = []
    decision_count: int = 0
    missed: bool = False


class MissedRange(BaseModel):
    from_at: datetime
    to_at: datetime
    reason: str | None = None
    count: int


class CyclesSummary(BaseModel):
    ran: int
    scheduled: int
    missed: list[MissedRange] = []
    errors: int = 0
    cycle_id: int = 0
    running_since: datetime | None = None


class CyclesRecent(BaseModel):
    slots: list[CycleSlot]
    summary: CyclesSummary
    decisions: list[CycleDecisions]
