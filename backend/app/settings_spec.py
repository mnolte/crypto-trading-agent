"""The settings screen's static description of every leaf in ``Settings``.

One place answers, per dotted key: which screen section shows it, its
allowed range, its unit, its choices and whether it is fixed. ``models.py``
builds its field validators from :data:`RANGES` and the cross-field rules
in :func:`cross_field_errors`, so the API, the preview and the screen all
read the same numbers — never a second copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import Settings

# section ids from design/screens/Settings.dc.html; None = accepted by the
# API but deliberately off the screen (the spec's five, plus schema_version).
SECTION_OFF = None

SECTIONS: dict[str, str | None] = {
    # Trading
    "active_markets": "markets",
    "execution_mode": "execution",
    "veto_window_s": "execution",
    "cycle_offset_s": "execution",
    "risk.pending_entry_drift_pct": "execution",
    "risk.time_stop_days": "exits",
    "risk.trailing_stop_enabled": "exits",
    "market.adx_threshold": "rules",
    "rules.": "rules",
    "risk.": "risk",
    "paper.taker_fee_pct": "broker",
    "paper.maker_fee_pct": "broker",
    "paper.slippage_pct": "broker",
    "paper.entry_orders": "broker",
    "paper.start_balance_eur": "paper",
    "model.": "model",
    "learning.briefing_time": "model",
    "learning.weekly_review": "model",
    "learning.ask_daily_limit": "model",
    "learning.insights_min_decisions": "model",
    "notifications.": "notify",
    "learning.plain_language": "display",
    "learning.quiet_line": "display",
    "ui.stop_near_atr": "display",
    "ui.log_watch_min_count": "display",
    "inspector.": "display",
    "data_source": "keys",
    "disabled_sources": "keys",
    # Accepted by the API, deliberately off the Settings screen.
    "market.deribit_requests_per_second": SECTION_OFF,
    "readiness.": "ready",
    "logging.console_level": "logs",
    "logging.keep_days.": "logs",
    "logging.max_db_mb": "logs",
    "timeouts.": "markets",
    "market.": "markets",
    # Records / Connections without settings rows
    "schema_version": SECTION_OFF,
    "logging.queue_size": SECTION_OFF,
    "logging.backtest_debug": SECTION_OFF,
    "logging.library_debug": SECTION_OFF,
    "storage.shutdown_grace_seconds": SECTION_OFF,
}

# Keys accepted by the API that have no row on the screen (spec: the five
# above; schema_version joins them).
OFF_SCREEN: frozenset[str] = frozenset(
    {
        "schema_version",
        "logging.queue_size",
        "logging.backtest_debug",
        "logging.library_debug",
        "storage.shutdown_grace_seconds",
        "market.deribit_requests_per_second",
    }
)

# Shown as locked text, never a control.
FIXED: frozenset[str] = frozenset({"market.stablecoin_bases"})

# Inclusive ranges, "min to max". Every numeric leaf the spec gives a range
# for lives here; models.py turns them into field validators.
RANGES: dict[str, tuple[str, str]] = {
    "risk.min_rr": ("1", "5"),
    "risk.max_cost_pct_of_risk": ("5", "100"),
    "risk.risk_per_trade_pct": ("0.1", "2"),
    "risk.throttled_risk_per_trade_pct": ("0.05", "2"),
    "risk.total_open_risk_pct": ("0.5", "10"),
    "risk.max_exposure_pct": ("10", "100"),
    "risk.min_trade_eur": ("5", "1000"),
    "risk.cooldown_after_stopout_hours": ("0", "72"),
    "risk.correlated_max_positions": ("1", "10"),
    "risk.correlated_threshold": ("0.5", "0.99"),
    "risk.shock_brake_btc_move_pct": ("1", "20"),
    "risk.shock_brake_hours": ("0.5", "24"),
    "risk.throttle_consecutive_losses": ("2", "10"),
    "risk.throttle_drawdown_pct": ("1", "20"),
    "risk.throttle_lift_drawdown_pct": ("0", "20"),
    "risk.daily_loss_limit_pct": ("0.5", "10"),
    "risk.kill_drawdown_pct": ("2", "30"),
    "risk.stop_min_atr": ("0.1", "10"),
    "risk.stop_max_atr": ("0.1", "10"),
    "risk.stop_buffer_atr": ("0", "1"),
    "risk.pending_entry_drift_pct": ("0.1", "5"),
    "risk.time_stop_days": ("1", "30"),
    "rules.prefilter_min_conds": ("1", "5"),
    "rules.buy_min_conds": ("1", "5"),
    "rules.buy_max_atr_above_ema20": ("0.5", "5"),
    "rules.stop_lookback_bars": ("2", "48"),
    "rules.stop_atr_pad": ("0", "1"),
    "rules.stop_max_atr": ("1", "10"),
    "rules.target_max_atr": ("1", "20"),
    "rules.target_rr": ("1", "10"),
    "market.adx_threshold": ("10", "50"),
    "market.min_avg_daily_volume_eur": ("100000", "100000000"),
    "market.max_avg_spread_pct": ("0.01", "2"),
    "market.min_daily_candles": ("30", "1000"),
    "market.eligibility_fail_days": ("1", "14"),
    "market.eligibility_beyond_pct": ("0", "100"),
    "market.eligibility_recover_days": ("1", "14"),
    "market.check_data_error_pct": ("1", "100"),
    "market.history_1h": ("100", "5000"),
    "market.history_4h": ("100", "5000"),
    "market.history_1d": ("100", "5000"),
    "market.structure_min_range_bars": ("3", "100"),
    "market.stale_grace_s": ("60", "7200"),
    "market.deribit_requests_per_second": ("0.1", "20"),
    "model.daily_call_budget": ("0", "20000"),
    "model.monthly_cost_cap_eur": ("0", "1000"),
    "paper.start_balance_eur": ("100", "1000000"),
    "paper.taker_fee_pct": ("0", "2"),
    "paper.maker_fee_pct": ("0", "2"),
    "paper.slippage_pct": ("0", "2"),
    "readiness.min_closed_trades": ("10", "1000"),
    "readiness.min_days_running": ("7", "365"),
    "readiness.max_drawdown_pct": ("1", "30"),
    "readiness.calibration_gap_pts": ("1", "50"),
    "veto_window_s": ("10", "600"),
    "cycle_offset_s": ("0", "600"),
    "learning.ask_daily_limit": ("0", "1000"),
    "learning.insights_min_decisions": ("5", "500"),
    "ui.stop_near_atr": ("0.5", "5"),
    "ui.log_watch_min_count": ("1", "1000"),
    "inspector.slow_call_seconds": ("1", "120"),
    "inspector.page_size": ("10", "100"),
    "inspector.attention.act_in_a_row_cycles": ("2", "20"),
    "inspector.attention.act_429_per_day": ("1", "100"),
    "inspector.attention.act_invalid_count": ("1", "20"),
    "inspector.attention.act_invalid_window": ("5", "100"),
    "inspector.attention.act_number_check_count": ("1", "10"),
    "inspector.attention.act_number_check_window": ("2", "20"),
    "inspector.attention.watch_within_cycles": ("1", "20"),
    "inspector.attention.watch_slow_per_day": ("1", "100"),
    "inspector.attention.watch_claim_share_pct": ("1", "100"),
    "inspector.attention.watch_claim_window": ("5", "100"),
    "logging.max_db_mb": ("100", "100000"),
    "notifications.smtp.port": ("1", "65535"),
}

# logging.keep_days.* share one range; timeouts.* share another.
KEEP_DAYS_RANGE = ("1", "3650")
TIMEOUT_RANGE = ("1", "300")

# The unit shown next to the control (the design's wording stays in the
# frontend section files; this is the machine-readable hint).
UNITS: dict[str, str] = {
    "active_markets": "pairs",
    "execution_mode": "",
    "veto_window_s": "s",
    "cycle_offset_s": "s",
    "risk.min_rr": "x",
    "risk.max_cost_pct_of_risk": "%",
    "risk.risk_per_trade_pct": "%",
    "risk.throttled_risk_per_trade_pct": "%",
    "risk.total_open_risk_pct": "%",
    "risk.max_exposure_pct": "%",
    "risk.min_trade_eur": "€",
    "risk.cooldown_after_stopout_hours": "h",
    "risk.correlated_max_positions": "positions",
    "risk.correlated_threshold": "",
    "risk.shock_brake_btc_move_pct": "%",
    "risk.shock_brake_hours": "h",
    "risk.throttle_consecutive_losses": "losses",
    "risk.throttle_drawdown_pct": "%",
    "risk.throttle_lift_drawdown_pct": "%",
    "risk.daily_loss_limit_pct": "%",
    "risk.kill_drawdown_pct": "%",
    "risk.stop_min_atr": "ATR",
    "risk.stop_max_atr": "ATR",
    "risk.stop_buffer_atr": "ATR",
    "risk.pending_entry_drift_pct": "%",
    "risk.time_stop_days": "days",
    "risk.trailing_stop_enabled": "",
    "rules.prefilter_min_conds": "conditions",
    "rules.buy_min_conds": "conditions",
    "rules.buy_max_atr_above_ema20": "ATR",
    "rules.stop_lookback_bars": "bars",
    "rules.stop_atr_pad": "ATR",
    "rules.stop_max_atr": "ATR",
    "rules.target_max_atr": "ATR",
    "rules.target_rr": "x",
    "market.adx_threshold": "",
    "market.min_avg_daily_volume_eur": "€",
    "market.max_avg_spread_pct": "%",
    "market.min_daily_candles": "days",
    "market.eligibility_fail_days": "days",
    "market.eligibility_beyond_pct": "%",
    "market.eligibility_recover_days": "days",
    "market.check_data_error_pct": "%",
    "market.history_1h": "bars",
    "market.history_4h": "bars",
    "market.history_1d": "bars",
    "market.structure_min_range_bars": "bars",
    "market.stale_grace_s": "s",
    "market.deribit_requests_per_second": "req/s",
    "market.stablecoin_bases": "",
    "model.decider": "",
    "model.model_id": "",
    "model.prompt_version": "",
    "model.daily_call_budget": "calls",
    "model.rules_prefilter": "",
    "model.monthly_cost_cap_eur": "€",
    "paper.start_balance_eur": "€",
    "paper.taker_fee_pct": "%",
    "paper.maker_fee_pct": "%",
    "paper.slippage_pct": "%",
    "paper.entry_orders": "",
    "notifications.browser_enabled": "",
    "notifications.email_enabled": "",
    "notifications.smtp.host": "",
    "notifications.smtp.port": "",
    "notifications.smtp.tls": "",
    "notifications.smtp.username": "",
    "notifications.smtp.sender": "",
    "notifications.smtp.recipient": "",
    "learning.plain_language": "",
    "learning.quiet_line": "",
    "learning.briefing_time": "Amsterdam",
    "learning.weekly_review": "",
    "learning.ask_daily_limit": "a day",
    "learning.insights_min_decisions": "decisions",
    "readiness.min_closed_trades": "trades",
    "readiness.min_days_running": "days",
    "readiness.max_drawdown_pct": "%",
    "readiness.calibration_gap_pts": "pts",
    "ui.stop_near_atr": "ATR",
    "ui.log_watch_min_count": "times",
    "inspector.slow_call_seconds": "s",
    "inspector.page_size": "calls",
    "inspector.attention.act_in_a_row_cycles": "cycles",
    "inspector.attention.act_429_per_day": "a day",
    "inspector.attention.act_invalid_count": "calls",
    "inspector.attention.act_invalid_window": "calls",
    "inspector.attention.act_number_check_count": "texts",
    "inspector.attention.act_number_check_window": "texts",
    "inspector.attention.watch_within_cycles": "cycles",
    "inspector.attention.watch_slow_per_day": "a day",
    "inspector.attention.watch_claim_share_pct": "%",
    "inspector.attention.watch_claim_window": "calls",
    "logging.console_level": "",
    "logging.keep_days.": "days",
    "logging.max_db_mb": "MB",
    "storage.shutdown_grace_seconds": "s",
    "timeouts.": "s",
    "schema_version": "",
    "data_source": "",
    "disabled_sources": "",
}

# Static choice lists; ``model.prompt_version`` is filled per request from
# the prompt files on disk.
CHOICES: dict[str, list[str]] = {
    "execution_mode": ["auto", "supervised", "manual"],
    "data_source": ["bitvavo", "sim"],
    "model.decider": ["openai", "rules"],
    "paper.entry_orders": ["market", "limit"],
    "learning.weekly_review": ["mon_08", "sun_20", "off"],
    "logging.console_level": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
}

# Which money base a percentage-of-X leaf converts to in the preview:
#   equity     — % of today's equity
#   day_start  — % of the day's start equity
#   peak_level — the equity level left after the % drawdown (kill level)
#   peak_drop  — the € amount a % drawdown from the peak represents
#   max_cost   — the € of max costs on one trade at full risk
EQUIVALENTS: dict[str, str] = {
    "risk.risk_per_trade_pct": "equity",
    "risk.throttled_risk_per_trade_pct": "equity",
    "risk.total_open_risk_pct": "equity",
    "risk.max_exposure_pct": "equity",
    "risk.daily_loss_limit_pct": "day_start",
    "risk.kill_drawdown_pct": "peak_level",
    "risk.throttle_drawdown_pct": "peak_drop",
    "risk.throttle_lift_drawdown_pct": "peak_drop",
    "readiness.max_drawdown_pct": "peak_drop",
    "risk.max_cost_pct_of_risk": "max_cost",
}


def section_for(key: str) -> str | None:
    """The screen section a dotted leaf belongs to (None = off screen)."""
    if key in SECTIONS:
        return SECTIONS[key]
    for prefix, section in SECTIONS.items():
        if prefix.endswith(".") and key.startswith(prefix):
            return section
    return SECTION_OFF


def unit_for(key: str) -> str | None:
    if key in UNITS:
        return UNITS[key]
    for prefix, unit in UNITS.items():
        if prefix.endswith(".") and key.startswith(prefix):
            return unit
    return None


def range_for(key: str) -> tuple[str, str] | None:
    """Inclusive "min to max" for a leaf; keep_days/timeouts share ranges."""
    if key in RANGES:
        return RANGES[key]
    if key.startswith("logging.keep_days."):
        return KEEP_DAYS_RANGE
    if key.startswith("timeouts."):
        return TIMEOUT_RANGE
    return None


def flatten_doc(doc: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested dicts become ``"a.b"`` leaf paths; lists stay leaf values."""
    out: dict[str, Any] = {}
    for key, value in doc.items():
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            out.update(flatten_doc(value, path))
        else:
            out[path] = value
    return out


def risk_errors(risk: Any) -> dict[str, str]:
    """The risk section's cross-field rules, keyed by the leaf that must
    move ("risk." prefix included)."""
    errors: dict[str, str] = {}
    # (bounded key, its value, bound key, its value, strict?)
    pairs = [
        (
            "risk.throttled_risk_per_trade_pct",
            risk.throttled_risk_per_trade_pct,
            "risk.risk_per_trade_pct",
            risk.risk_per_trade_pct,
            False,
        ),
        (
            "risk.risk_per_trade_pct",
            risk.risk_per_trade_pct,
            "risk.total_open_risk_pct",
            risk.total_open_risk_pct,
            False,
        ),
        ("risk.stop_min_atr", risk.stop_min_atr, "risk.stop_max_atr", risk.stop_max_atr, True),
        (
            "risk.throttle_lift_drawdown_pct",
            risk.throttle_lift_drawdown_pct,
            "risk.throttle_drawdown_pct",
            risk.throttle_drawdown_pct,
            True,
        ),
        (
            "risk.throttle_drawdown_pct",
            risk.throttle_drawdown_pct,
            "risk.kill_drawdown_pct",
            risk.kill_drawdown_pct,
            True,
        ),
        (
            "risk.daily_loss_limit_pct",
            risk.daily_loss_limit_pct,
            "risk.kill_drawdown_pct",
            risk.kill_drawdown_pct,
            True,
        ),
    ]
    for lo_key, lo, hi_key, hi, strict in pairs:
        bad = lo >= hi if strict else lo > hi
        if bad:
            errors[lo_key] = f"must be {'<' if strict else '<='} {hi_key}"
    return errors


def rules_errors(rules: Any) -> dict[str, str]:
    """The entry-rules cross-field rule."""
    if rules.prefilter_min_conds > rules.buy_min_conds:
        return {"rules.prefilter_min_conds": "must be <= rules.buy_min_conds"}
    return {}


def cross_field_errors(settings: Settings) -> dict[str, str]:
    """Every cross-field rule at once — the preview's per-field error map."""
    return {**risk_errors(settings.risk), **rules_errors(settings.rules)}
