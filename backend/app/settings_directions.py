"""Which way is looser for every settings leaf — the one map.

``DIRECTIONS`` gives each covered dotted key its direction (which way is
looser) and a one-line reason. Covered keys are every leaf under ``risk``,
``rules``, ``model``, ``paper``, ``market``, ``readiness`` and
``notifications.events``, plus ``execution_mode``, ``veto_window_s``,
``cycle_offset_s`` and ``active_markets``. A covered key without an entry
counts as looser (fails closed), and a test fails until every covered key
has one.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

Direction = Literal["up", "down", "off", "longer", "toward_auto", "neutral"]
Tag = Literal["looser", "tighter", "neutral"]

# dotted key -> (direction, one-line reason shown when a save asks again)
DIRECTIONS: dict[str, tuple[Direction, str]] = {
    # ------------------------------------------------------------- risk --
    "risk.min_rr": ("down", "a lower minimum R/R lets weaker trades through"),
    "risk.max_cost_pct_of_risk": ("up", "costs may eat more of the stop distance"),
    "risk.risk_per_trade_pct": ("up", "more of the account on one trade"),
    "risk.throttled_risk_per_trade_pct": (
        "up",
        "the halved risk while throttled can be larger",
    ),
    "risk.total_open_risk_pct": ("up", "more total risk across open positions"),
    "risk.max_exposure_pct": ("up", "more of the account exposed at once"),
    "risk.min_trade_eur": ("down", "smaller trades are allowed"),
    "risk.cooldown_after_stopout_hours": ("down", "re-entry sooner after a stop-out"),
    "risk.correlated_max_positions": ("up", "more correlated positions at once"),
    "risk.correlated_threshold": (
        "up",
        "fewer pairs count as correlated, so more can stack",
    ),
    "risk.shock_brake_btc_move_pct": ("up", "the brake triggers only on bigger moves"),
    "risk.shock_brake_hours": ("down", "the shock brake releases sooner"),
    "risk.throttle_consecutive_losses": ("up", "the throttle waits for more losses"),
    "risk.throttle_drawdown_pct": ("up", "the throttle arms only on a deeper drawdown"),
    "risk.throttle_lift_drawdown_pct": ("up", "the throttle lifts sooner"),
    "risk.daily_loss_limit_pct": ("up", "more loss allowed in a day"),
    "risk.kill_drawdown_pct": ("up", "the kill switch fires later"),
    "risk.stop_min_atr": ("down", "lets stops sit tighter"),
    "risk.stop_max_atr": ("up", "a wider range for the stop distance"),
    "risk.stop_buffer_atr": ("neutral", "where the stop sits; size follows"),
    "risk.pending_entry_drift_pct": ("up", "entries tolerate more drift while waiting"),
    "risk.time_stop_days": ("neutral", "cuts winners and losers alike"),
    "risk.trailing_stop_enabled": ("neutral", "locks gains, may exit earlier"),
    # ------------------------------------------------------------ rules --
    "rules.prefilter_min_conds": ("down", "the pre-filter passes setups more easily"),
    "rules.buy_min_conds": ("down", "Rules BUYs need fewer conditions"),
    "rules.buy_max_atr_above_ema20": ("up", "entries allowed further above the EMA"),
    "rules.stop_lookback_bars": ("neutral", "where the stop level is looked up"),
    "rules.stop_atr_pad": ("neutral", "padding below the stop level"),
    "rules.stop_max_atr": ("up", "a wider stop range for Rules"),
    "rules.target_max_atr": ("neutral", "cap on the target distance"),
    "rules.target_rr": ("down", "targets may promise less reward"),
    # ------------------------------------------------------------ model --
    "model.decider": ("neutral", "who decides; always asks once more"),
    "model.model_id": ("neutral", "which model decides; always asks once more"),
    "model.prompt_version": ("neutral", "which prompt decides; always asks once more"),
    "model.daily_call_budget": ("up", "more model calls per day"),
    "model.rules_prefilter": ("off", "switched off, every pair reaches the model"),
    "model.monthly_cost_cap_eur": ("up", "more model spend per month"),
    # ------------------------------------------------------------- paper --
    "paper.start_balance_eur": ("neutral", "account size, not risk"),
    "paper.taker_fee_pct": ("down", "results look better than they are"),
    "paper.maker_fee_pct": ("down", "results look better than they are"),
    "paper.slippage_pct": ("down", "results look better than they are"),
    "paper.entry_orders": ("neutral", "market or limit entries"),
    # ------------------------------------------------------------ market --
    "market.adx_threshold": ("down", "weaker trends count as trending"),
    "market.min_avg_daily_volume_eur": ("down", "thinner markets are eligible"),
    "market.max_avg_spread_pct": ("up", "wider spreads are allowed"),
    "market.min_daily_candles": ("down", "less history is required"),
    "market.eligibility_fail_days": ("up", "markets stay active longer while failing"),
    "market.eligibility_beyond_pct": ("up", "markets further beyond still count"),
    "market.eligibility_recover_days": ("down", "markets pass again sooner"),
    "market.check_data_error_pct": (
        "neutral",
        "how much missing data counts as a data error, not a rule",
    ),
    "market.history_1h": ("neutral", "how much 1h history is kept"),
    "market.history_4h": ("neutral", "how much 4h history is kept"),
    "market.history_1d": ("neutral", "how much daily history is kept"),
    "market.structure_min_range_bars": ("neutral", "structure lookback"),
    "market.stale_grace_s": ("up", "staler data still counts as fresh"),
    "market.stablecoin_bases": ("down", "a longer list is tighter"),
    # --------------------------------------------------------- readiness --
    "readiness.min_closed_trades": ("down", "readiness reached with fewer trades"),
    "readiness.min_days_running": ("down", "readiness reached sooner"),
    "readiness.max_drawdown_pct": ("up", "a deeper drawdown still passes"),
    "readiness.calibration_gap_pts": ("up", "a wider calibration gap still passes"),
    # ----------------------------------------------------- notifications --
    "notifications.browser_enabled": ("off", "fewer alerts reach you"),
    "notifications.email_enabled": ("off", "fewer alerts reach you"),
    # ---------------------------------------------------------- top level --
    "execution_mode": ("toward_auto", "manual to supervised to auto is looser"),
    "veto_window_s": ("down", "a shorter window to veto"),
    "cycle_offset_s": ("neutral", "when in the hour the cycle starts"),
    "active_markets": ("longer", "more markets traded"),
}

# Sections whose every leaf must appear in DIRECTIONS (or match the
# notifications.events pattern); a missing entry counts as looser.
COVERED_PREFIXES = (
    "risk.",
    "rules.",
    "model.",
    "paper.",
    "market.",
    "readiness.",
    "notifications.events.",
)
COVERED_KEYS = frozenset({"execution_mode", "veto_window_s", "cycle_offset_s", "active_markets"})

# Changes that always ask once more, whatever their direction.
CONFIRM_ALWAYS_KEYS = frozenset({"model.decider", "model.model_id", "model.prompt_version"})
_EVENT_CHANNEL = re.compile(r"^notifications\.events\.[^.]+\.(browser|email)$")

_EXEC_RANK = {"manual": 0, "supervised": 1, "auto": 2}


def covered(key: str) -> bool:
    """Whether a missing DIRECTIONS entry for this key fails closed."""
    return key in COVERED_KEYS or any(key.startswith(p) for p in COVERED_PREFIXES)


def direction_for(key: str) -> tuple[Direction, str] | None:
    """The (direction, reason) for a leaf, or None when uncovered."""
    found = DIRECTIONS.get(key)
    if found is not None:
        return found
    if _EVENT_CHANNEL.match(key):
        return ("off", "a channel taken away from the event")
    if covered(key):
        # Fails closed: an unmapped covered key counts as looser.
        return ("up", "no direction mapped — treated as looser")
    return None


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _len(value: Any) -> int | None:
    return len(value) if isinstance(value, (list, tuple, dict, str)) else None


def tag_change(key: str, old: Any, new: Any) -> Tag:
    """Classify one leaf change as looser, tighter or neutral."""
    found = DIRECTIONS.get(key)
    if found is None and _EVENT_CHANNEL.match(key):
        kind: Direction = "off"
    elif found is None:
        # Uncovered keys are neutral; a covered key without a map entry
        # fails closed and counts as looser.
        return "looser" if covered(key) else "neutral"
    else:
        kind = found[0]
    if kind == "neutral":
        return "neutral"
    if kind == "up":
        a, b = _num(old), _num(new)
        if a is None or b is None:
            la, lb = _len(old), _len(new)
            if la is None or lb is None or la == lb:
                return "neutral"
            return "looser" if lb > la else "tighter"
        if b > a:
            return "looser"
        if b < a:
            return "tighter"
        return "neutral"
    if kind == "down":
        a, b = _num(old), _num(new)
        if a is None or b is None:
            la, lb = _len(old), _len(new)
            if la is None or lb is None or la == lb:
                return "neutral"
            return "looser" if lb < la else "tighter"
        if b < a:
            return "looser"
        if b > a:
            return "tighter"
        return "neutral"
    if kind == "off":
        return "looser" if not new else "tighter"
    if kind == "longer":
        la, lb = _len(old), _len(new)
        if la is None or lb is None or la == lb:
            return "neutral"
        return "looser" if lb > la else "tighter"
    if kind == "toward_auto":
        ea, eb = _EXEC_RANK.get(str(old)), _EXEC_RANK.get(str(new))
        if ea is None or eb is None:
            return "neutral"
        if eb > ea:
            return "looser"
        if eb < ea:
            return "tighter"
        return "neutral"
    return "neutral"


def confirm_always(key: str, old: Any, new: Any) -> bool:
    """Changes that ask once more whatever their direction: the live model
    or prompt, and a notification channel going off."""
    if key in CONFIRM_ALWAYS_KEYS:
        return True
    return bool(_EVENT_CHANNEL.match(key)) and not new
