"""Risk gates, sizing and the always-on exit rules.

Every BUY passes every gate here before it can execute; a failed gate turns
it into SKIP with the gate named. Sizing is done here, never by a decider.
Every threshold comes from ``Settings`` — nothing is a literal in logic.

Money is ``Decimal`` everywhere. ATR distances arrive as Decimals converted
from the float indicator maths at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from app.agent.broker.paper import HeldPosition, PaperBroker
from app.agent.market.base import MarketInfo
from app.models import Gate, Plan, Settings

HUNDRED = Decimal("100")
ZERO = Decimal("0")


@dataclass
class BuyVerdict:
    """The gate layer's answer for one proposed BUY."""

    outcome: str  # "fill" | "skip"
    gates: list[Gate]
    plan: Plan | None = None
    skip_reason: str | None = None


@dataclass
class BuyContext:
    """Everything a BUY gate can look at, gathered by the engine."""

    pair: str
    now: datetime
    entry: Decimal
    stop: Decimal
    target: Decimal
    atr_1h: Decimal | None
    has_position: bool
    last_stop_out_at: datetime | None
    correlated_pairs: list[str] = field(default_factory=list)
    shock_brake_until: datetime | None = None
    daily_loss_hit: bool = False
    risk_pct: Decimal = Decimal("1")  # 1.0 normal, 0.5 while throttled


def _gate(name: str, status: str, value: str) -> Gate:
    return Gate(name=name, status=status, value=value)


def _fmt(d: Decimal, places: int = 2) -> str:
    return str(d.quantize(Decimal(1).scaleb(-places)))


def check_buy(
    ctx: BuyContext, broker: PaperBroker, market: MarketInfo | None, settings: Settings
) -> BuyVerdict:
    """Run every gate in spec order; on the first failure the BUY is SKIP."""
    risk = settings.risk
    paper = settings.paper
    gates: list[Gate] = []

    entry, stop, target = ctx.entry, ctx.stop, ctx.target
    equity = broker.equity()

    # The expected fill is one slippage step above the signal price. Every
    # gate and the sizing run on it so a BUY that only works at the signal
    # price — worse R/R, a fatter cost share, more real risk — is a SKIP.
    fill_entry = entry * (1 + paper.slippage_pct / HUNDRED)
    stop_dist = entry - stop
    fill_dist = fill_entry - stop

    # -- Min reward/risk -------------------------------------------------
    rr = (target - fill_entry) / fill_dist if fill_dist > 0 else ZERO
    if stop_dist <= 0 or fill_dist <= 0 or rr < risk.min_rr:
        gates.append(_gate("Min reward/risk", "fail", f"R/R {_fmt(rr)} < {_fmt(risk.min_rr)}"))
        return BuyVerdict("skip", gates, skip_reason="Min reward/risk")
    gates.append(_gate("Min reward/risk", "pass", f"R/R {_fmt(rr)}"))

    # -- Costs vs risk (the fee gate) ------------------------------------
    # Round-trip taker fees + slippage both ways must stay under
    # max_cost_pct_of_risk % of the stop distance.
    cost_pct = paper.fee_pct * 2 + paper.slippage_pct * 2
    stop_dist_pct = fill_dist / fill_entry * HUNDRED if fill_entry > 0 else ZERO
    if stop_dist_pct <= 0 or cost_pct > risk.max_cost_pct_of_risk / HUNDRED * stop_dist_pct:
        gates.append(
            _gate(
                "Costs vs risk",
                "fail",
                f"costs {_fmt(cost_pct)}% > {_fmt(risk.max_cost_pct_of_risk)}% "
                f"of {_fmt(stop_dist_pct)}% stop",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Costs vs risk")
    gates.append(
        _gate("Costs vs risk", "pass", f"costs {_fmt(cost_pct)}% of {_fmt(stop_dist_pct)}% stop")
    )

    # -- One position per pair --------------------------------------------
    if ctx.has_position or ctx.pair in broker.positions:
        gates.append(_gate("One position per pair", "fail", "already holding"))
        return BuyVerdict("skip", gates, skip_reason="One position per pair")
    gates.append(_gate("One position per pair", "pass", "flat"))

    # -- Cooldown after stop-out ------------------------------------------
    if ctx.last_stop_out_at is not None:
        until = utc(ctx.last_stop_out_at) + timedelta(
            hours=float(risk.cooldown_after_stopout_hours)
        )
        if ctx.now < until:
            gates.append(
                _gate(
                    "Cooldown after stop-out",
                    "fail",
                    f"until {until.isoformat()}",
                )
            )
            return BuyVerdict("skip", gates, skip_reason="Cooldown after stop-out")
    gates.append(_gate("Cooldown after stop-out", "pass", "clear"))

    # -- Correlated positions ---------------------------------------------
    if len(ctx.correlated_pairs) >= risk.correlated_max_positions:
        gates.append(
            _gate(
                "Correlated positions",
                "fail",
                f"corr > {_fmt(risk.correlated_threshold)} with {', '.join(ctx.correlated_pairs)}",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Correlated positions")
    gates.append(
        _gate(
            "Correlated positions",
            "pass",
            f"{len(ctx.correlated_pairs)} correlated",
        )
    )

    # -- Shock brake -------------------------------------------------------
    if ctx.shock_brake_until is not None and ctx.now < ctx.shock_brake_until:
        gates.append(
            _gate(
                "Shock brake",
                "fail",
                f"until {ctx.shock_brake_until.isoformat()}",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Shock brake")
    gates.append(_gate("Shock brake", "pass", "off"))

    # -- Daily loss limit --------------------------------------------------
    if ctx.daily_loss_hit:
        gates.append(
            _gate(
                "Daily loss limit",
                "fail",
                f"-{_fmt(risk.daily_loss_limit_pct)}% of day-start equity",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Daily loss limit")
    gates.append(_gate("Daily loss limit", "pass", "under limit"))

    # ------------------------------------------------------------------ sizing
    if fill_dist <= 0 or fill_entry <= 0:
        gates.append(_gate("Sizing", "fail", "no stop distance"))
        return BuyVerdict("skip", gates, skip_reason="Sizing")

    # Size on the slipped fill price so the real open risk, not the planned
    # one, stays inside every cap.
    qty = equity * ctx.risk_pct / HUNDRED / fill_dist
    gates.append(
        _gate(
            "Risk per trade",
            "pass",
            f"{_fmt(ctx.risk_pct)}% of equity = {_fmt(equity * ctx.risk_pct / HUNDRED)} EUR",
        )
    )

    # Total open risk: existing risk plus the new trade's risk.
    open_risk_cap = equity * risk.total_open_risk_pct / HUNDRED
    room = open_risk_cap - broker.open_risk()
    if room <= 0:
        gates.append(
            _gate(
                "Total open risk",
                "fail",
                f"{_fmt(risk.total_open_risk_pct)}% cap reached",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Total open risk")
    qty = min(qty, room / fill_dist)
    gates.append(_gate("Total open risk", "pass", f"room {_fmt(room)} EUR"))

    # Exposure cap (spot: no leverage).
    exposure_cap = equity * risk.max_exposure_pct / HUNDRED
    exposure_room = exposure_cap - broker.exposure()
    if exposure_room <= 0:
        gates.append(
            _gate(
                "Max total exposure",
                "fail",
                f"{_fmt(risk.max_exposure_pct)}% cap reached",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Max total exposure")
    qty = min(qty, exposure_room / fill_entry)
    gates.append(_gate("Max total exposure", "pass", f"room {_fmt(exposure_room)} EUR"))

    # Cash available incl. the buy-side fee.
    cash_room = broker.cash / (fill_entry * (1 + paper.fee_pct / HUNDRED))
    if cash_room <= 0:
        gates.append(_gate("Cash available incl. fees", "fail", f"cash {_fmt(broker.cash)} EUR"))
        return BuyVerdict("skip", gates, skip_reason="Cash available incl. fees")
    qty = min(qty, cash_room)
    gates.append(_gate("Cash available incl. fees", "pass", f"cash {_fmt(broker.cash)} EUR"))

    # Round down to the market's quantity precision.
    decimals = market.quantity_decimals if market and market.quantity_decimals is not None else 8
    step = Decimal(1).scaleb(-decimals)
    qty = qty.quantize(step, rounding=ROUND_DOWN)
    notional = qty * fill_entry

    if qty <= 0 or notional < risk.min_trade_eur:
        gates.append(
            _gate(
                "Sizing",
                "fail",
                f"notional {_fmt(notional)} EUR < {_fmt(risk.min_trade_eur)} EUR",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Sizing")
    if market and market.min_order_quote is not None and notional < market.min_order_quote:
        gates.append(
            _gate(
                "Sizing",
                "fail",
                f"notional {_fmt(notional)} EUR < market min {_fmt(market.min_order_quote)} EUR",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Sizing")
    if market and market.min_order_base is not None and qty < market.min_order_base:
        gates.append(
            _gate(
                "Sizing",
                "fail",
                f"qty {_fmt(qty, 6)} < market min {_fmt(market.min_order_base, 6)}",
            )
        )
        return BuyVerdict("skip", gates, skip_reason="Sizing")

    plan = Plan(
        entry=entry,
        fill_entry=fill_entry,
        stop=stop,
        target=target,
        qty=qty,
        notional=notional,
        rr=rr,
    )
    return BuyVerdict("fill", gates, plan=plan)


# ------------------------------------------------------------------ exits --


def check_exit(
    pos: HeldPosition,
    price: Decimal,
    atr_1h: Decimal | None,
    now: datetime,
    settings: Settings,
) -> str | None:
    """The exits the agent always takes — checked every 30 s on the ticker.

    Returns the exit reason or None. Breakeven and trailing mutate
    ``pos.stop`` in place and return None; the caller persists the row.
    """
    risk = settings.risk
    paper = settings.paper

    if price <= pos.stop:
        return "stop"
    if price >= pos.target:
        return "target"
    if utc(pos.opened_at) + timedelta(days=float(risk.time_stop_days)) <= now:
        return "time_stop"

    # Breakeven: at +1R the stop moves to entry plus round-trip fees.
    r_unit = pos.r_unit if pos.r_unit > 0 else pos.entry - pos.stop
    be_stop = pos.entry * (1 + (paper.fee_pct * 2) / HUNDRED)
    if r_unit > 0 and price >= pos.entry + r_unit and pos.stop < be_stop:
        pos.stop = be_stop

    # Trailing (setting, off by default): at +2R the stop follows the
    # highest 1h close since entry minus 2 x ATR(1h); never moves down.
    if (
        risk.trailing_stop_enabled
        and r_unit > 0
        and atr_1h is not None
        and atr_1h > 0
        and price >= pos.entry + 2 * r_unit
        and pos.highest_close is not None
    ):
        trail = pos.highest_close - 2 * atr_1h
        if trail > pos.stop:
            pos.stop = trail
    return None


def update_trail(pos: HeldPosition, close_1h: Decimal) -> None:
    """Track the highest 1h close since entry for the trailing stop."""
    if pos.highest_close is None or close_1h > pos.highest_close:
        pos.highest_close = close_1h


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
