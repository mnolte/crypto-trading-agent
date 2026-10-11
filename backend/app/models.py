"""Pydantic models: decisions, positions, settings and the snapshot.

Every model here mirrors the "Data model and API" section of docs/SPEC.md.
Money is a Decimal everywhere; it serialises to JSON as a string and the
frontend never does arithmetic on it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer

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
    risk_pct_now: MoneyDecimal  # 1.0, or 0.5 while throttled
    throttle: Throttle | None
    shock_brake_until: datetime | None
    correlated_slots_left: int


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
    # Source ids the user disabled on the Sources page; the registry keeps
    # the runtime status.
    disabled_sources: list[str] = []
