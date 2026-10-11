"""Pydantic models: decisions, positions, settings and the snapshot.

Every model here mirrors the "Data model and API" section of docs/SPEC.md.
Money is a Decimal everywhere; it serialises to JSON as a string and the
frontend never does arithmetic on it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Money travels as a string in JSON: Decimals are exact and the frontend only
# formats them.
MoneyDecimal = Annotated[Decimal, PlainSerializer(lambda v: format(v, "f"), return_type=str)]

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
    status: Literal["executed", "pending", "approved", "vetoed", "expired", "none"]
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

    min_rr: Decimal = Decimal("2.0")
    max_cost_pct_of_risk: Decimal = Decimal("30")
    risk_per_trade_pct: Decimal = Decimal("1.0")
    throttled_risk_per_trade_pct: Decimal = Decimal("0.5")
    total_open_risk_pct: Decimal = Decimal("2.5")
    max_exposure_pct: Decimal = Decimal("100")
    min_trade_eur: Decimal = Decimal("10")
    cooldown_after_stopout_hours: Decimal = Decimal("6")
    correlated_max_positions: int = 2
    correlated_threshold: Decimal = Decimal("0.8")
    shock_brake_btc_move_pct: Decimal = Decimal("4")
    shock_brake_hours: Decimal = Decimal("2")
    throttle_consecutive_losses: int = 3
    throttle_drawdown_pct: Decimal = Decimal("4")
    throttle_lift_drawdown_pct: Decimal = Decimal("2")
    daily_loss_limit_pct: Decimal = Decimal("3")
    kill_drawdown_pct: Decimal = Decimal("8")
    stop_min_atr: Decimal = Decimal("0.5")
    stop_max_atr: Decimal = Decimal("5")
    pending_entry_drift_pct: Decimal = Decimal("0.5")
    time_stop_days: Decimal = Decimal("5")
    trailing_stop_enabled: bool = False


class RulesSettings(BaseModel):
    """Rules-decider thresholds — tunable without code changes; every Rules
    decision stores the settings version it used."""

    model_config = ConfigDict(extra="ignore")

    prefilter_min_conds: int = 3  # of C2..C6, plus C1 and a position or setup
    buy_min_conds: int = 4  # of C2..C6 for a Rules BUY
    buy_max_atr_above_ema20: Decimal = Decimal("1.5")
    stop_lookback_bars: int = 6
    stop_atr_pad: Decimal = Decimal("0.2")
    stop_max_atr: Decimal = Decimal("5")
    target_max_atr: Decimal = Decimal("6")
    target_rr: Decimal = Decimal("2.5")


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decider: Literal["openai", "rules"] = "openai"
    model_id: str = "gpt-4o-mini"
    prompt_version: str = "decision.v1"
    daily_call_budget: int = 1000
    rules_prefilter: bool = True
    monthly_cost_cap_eur: Decimal = Decimal("10")


class PaperSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start_balance_eur: Decimal = Decimal("10000")
    fee_pct: Decimal = Decimal("0.25")
    slippage_pct: Decimal = Decimal("0.05")


class MarketSettings(BaseModel):
    """Market-data and eligibility thresholds — all tunable, never literals
    in logic. The shared ADX threshold is used by the reads, the regime,
    Rules condition C6 and Insights."""

    model_config = ConfigDict(extra="ignore")

    adx_threshold: Decimal = Decimal("25")
    min_avg_daily_volume_eur: Decimal = Decimal("1000000")
    max_avg_spread_pct: Decimal = Decimal("0.15")
    min_daily_candles: int = 300
    eligibility_fail_days: int = 3
    eligibility_beyond_pct: Decimal = Decimal("20")
    eligibility_recover_days: int = 3
    history_1h: int = 800
    history_4h: int = 300
    history_1d: int = 300
    structure_min_range_bars: int = 12
    stale_grace_s: int = 900  # freshness: close + interval + grace (1h → 75 min)
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
    port: int = 587
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
    ask_daily_limit: int = 100
    insights_min_decisions: int = 30


class ReadinessSettings(BaseModel):
    """The live-readiness thresholds editable in Settings; the other
    criteria (average R, profit factor, beats BTC, no kill, no errors) use
    the spec defaults and live in app.agent.readiness."""

    model_config = ConfigDict(extra="ignore")

    min_closed_trades: int = 100
    min_days_running: int = 30
    max_drawdown_pct: Decimal = Decimal("6")
    calibration_gap_pts: Decimal = Decimal("10")


class Settings(BaseModel):
    """The one validated JSON document stored in the settings table.

    Unknown keys are dropped on load; new fields added later take their default.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    active_markets: list[str] = DEFAULT_MARKETS.copy()
    execution_mode: ExecMode = "auto"
    veto_window_s: int = 60
    cycle_offset_s: int = 45
    data_source: Literal["bitvavo", "sim"] = "bitvavo"
    risk: RiskSettings = RiskSettings()
    rules: RulesSettings = RulesSettings()
    model: ModelSettings = ModelSettings()
    paper: PaperSettings = PaperSettings()
    market: MarketSettings = MarketSettings()
    notifications: NotificationSettings = NotificationSettings()
    learning: LearningSettings = LearningSettings()
    readiness: ReadinessSettings = ReadinessSettings()
    # Source ids the user disabled on the Sources page; the registry keeps
    # the runtime status.
    disabled_sources: list[str] = []


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


class LlmCallListOut(BaseModel):
    items: list[LlmCallListItem]


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


class SettingsHistoryItem(BaseModel):
    at: datetime
    key: str
    old: str | None
    new: str | None


class SettingsHistoryOut(BaseModel):
    items: list[SettingsHistoryItem]
