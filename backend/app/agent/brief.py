"""The per-pair market brief sent to the model, plus its input checks.

The brief is the model's entire view of the world: one pair's indicator
reads on 1h/4h/1d, its structure levels, the market-wide BTC/euro context,
liquidity, the position in this pair (or null), this pair's last three
closed trades, the previous decision for this pair, account-level risk
figures, a summary of other held pairs (correlations and time held only —
never their prices or P&L) and the limits from settings.

Every read is computed here from the same ``signals`` module the engine
uses — the model never sees raw candles or does maths. Values that are
missing or NaN become ``null``; the input checks below refuse to send a
brief with a missing indicator field, stale data, an implausible price or
a value that could only have leaked from another pair.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app import repo
from app.agent.broker.paper import HeldPosition
from app.agent.market.base import TF_SECONDS, Candle
from app.agent.signals import (
    PairSignals,
    TfValues,
    adx_read,
    atr_rank_read,
    compute_tf_values,
    ema,
    ema_cross_read,
    obv_read,
    price_rel_read,
    rsi_read,
    volume_read,
)
from app.db import AMSTERDAM
from app.models import CheckOut, Regime, Settings, Tf

# A price in the brief more than 50% away from the pair's own price can only
# have come from another pair — the cross-pair-leak check.
MAX_LEVEL_DISTANCE_PCT = 50.0

# The model's entry may sit at most 0.5% from the current price.
MAX_ENTRY_DISTANCE_PCT = 0.5

TF_KEYS: tuple[Tf, ...] = ("1h", "4h", "1d")

# Indicator fields each timeframe block carries.
TF_FIELDS: tuple[str, ...] = (
    "rsi",
    "ema20_vs_50",
    "vwap_dist",
    "obv_trend",
    "adx",
    "atr_pct",
    "atr_rank",
    "vol_x_avg",
)
TF_FIELDS_1D_EXTRA: tuple[str, ...] = ("ema200_dist",)


@dataclass
class BriefInputs:
    """Everything a brief needs beyond the pair's own signals.

    ``context_candles`` keys: btc_1h/btc_4h/btc_1d, usdc_1h, eth_4h.
    ``pair_trades`` holds (closed_at, realised_r) for the pair's last three
    closed trades, newest first. ``held_portfolio`` holds
    (pair, corr_30d, hours_held) for the *other* open positions.
    """

    pair: str
    now: datetime
    price: Decimal
    signals: PairSignals
    holding: bool
    settings: Settings
    session: Session
    source: str
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    context_candles: dict[str, list[Candle]] = field(default_factory=dict)
    btc_regime: Regime | None = None
    position: HeldPosition | None = None
    liquidity: dict[str, float | None] = field(default_factory=dict)
    pair_trades: list[tuple[datetime, Decimal]] = field(default_factory=list)
    hours_since_exit: float | None = None
    previous_decision: tuple[str, str | None] | None = None
    account: dict[str, float] = field(default_factory=dict)
    held_portfolio: list[tuple[str, float | None, float]] = field(default_factory=list)
    correlated_slots_left: int = 0
    risk_throttle: bool = False
    shock_brake: bool = False


@dataclass
class BriefMeta:
    """Facts the checks need that aren't visible in the brief JSON."""

    pair: str
    now: datetime
    price: float
    atr_1h: float | None
    holding: bool
    regime: Regime
    btc_regime: Regime | None
    bars: dict[str, int]
    data_age_s: dict[str, float]
    stale_tfs: list[str]
    support: list[float]
    resistance: list[float]


def _clean(value: float | None, digits: int = 4) -> float | None:
    """Round for the brief; None and NaN both become null."""
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _item(value: float | None, read: str | None, digits: int = 4) -> dict[str, Any]:
    return {"value": _clean(value, digits), "read": read if value is not None else "neutral"}


def _read_or_neutral(value: float | None, read: str | None) -> str:
    return read if value is not None and read is not None else "neutral"


def _tf_block(tf: str, v: TfValues, adx_threshold: float) -> dict[str, Any]:
    """One timeframe's reads — the exact field names the prompt describes,
    using the same read functions as the engine's own signal list."""
    ema_gap = (v.ema20 - v.ema50) / v.ema50 * 100 if v.ema20 is not None and v.ema50 else None
    atr_rank = atr_rank_read(v.atr_percentile)
    block: dict[str, Any] = {
        "rsi": _item(v.rsi14, rsi_read(v.rsi14)),
        "ema20_vs_50": _item(ema_gap, ema_cross_read(v.ema20, v.ema50), digits=3),
        "vwap_dist": _item(v.price_vs_vwap_pct, price_rel_read(v.price_vs_vwap_pct)),
        "obv_trend": _item(v.obv_trend, obv_read(v.obv_trend)),
        "adx": _item(v.adx14, adx_read(v.adx14, adx_threshold)),
        "atr_pct": _item(v.atr_pct, atr_rank),
        "atr_rank": _item(v.atr_percentile, atr_rank, digits=1),
        "vol_x_avg": _item(v.volume_ratio, volume_read(v.volume_ratio), digits=2),
    }
    if tf == "1d":
        block["ema200_dist"] = _item(v.price_vs_ema200_pct, price_rel_read(v.price_vs_ema200_pct))
    return block


def _sign_read(value: float | None) -> str | None:
    """Plain sign read for context moves: up is bullish, down bearish."""
    if value is None:
        return None
    if value > 0:
        return "bullish"
    if value < 0:
        return "bearish"
    return "neutral"


def _context_item(
    value: float | None,
    read: str | None,
    source: str,
    as_of: datetime | None,
) -> dict[str, Any]:
    return {
        "value": _clean(value),
        "read": _read_or_neutral(value, read),
        "source": source,
        "as_of": as_of.isoformat() if as_of else None,
    }


def _candles_end(candles: list[Candle], tf: Tf) -> datetime | None:
    if not candles:
        return None
    return candles[-1].close_time(tf)


def _ema_gap_read(candles: list[Candle], fast: int, slow: int) -> tuple[float | None, str | None]:
    """EMA fast-vs-slow % gap and the cross read on `candles`."""
    if len(candles) < slow + 50:
        return None, None
    closes = np.array([float(c.close) for c in candles])
    f = ema(closes, fast)[-1]
    s = ema(closes, slow)[-1]
    if f is None or s is None or s == 0 or np.isnan(f) or np.isnan(s):
        return None, None
    return float((f - s) / s * 100), ema_cross_read(float(f), float(s))


def _eth_btc_trend(eth_4h: list[Candle], btc_4h: list[Candle]) -> tuple[float | None, str | None]:
    """EMA 20 vs 50 of the ETH/BTC 4h ratio, aligned by open time."""
    if len(eth_4h) < 70 or len(btc_4h) < 70:
        return None, None
    btc_by_time = {c.open_time: c for c in btc_4h}
    ratio = np.array(
        [
            float(e.close) / float(b.close)
            for e in eth_4h
            if (b := btc_by_time.get(e.open_time)) is not None and b.close != 0
        ]
    )
    if len(ratio) < 70:
        return None, None
    f = ema(ratio, 20)[-1]
    s = ema(ratio, 50)[-1]
    if f is None or s is None or s == 0 or np.isnan(f) or np.isnan(s):
        return None, None
    return float((f - s) / s * 100), ema_cross_read(float(f), float(s))


def _structure_block(inp: BriefInputs) -> dict[str, Any]:
    levels = inp.signals.levels
    price = float(inp.price) if inp.price else None
    atr = inp.signals.tf["1h"].atr14

    def lvl(p: float) -> dict[str, Any]:
        out: dict[str, Any] = {"price": _clean(p)}
        if price:
            out["dist_pct"] = _clean((p / price - 1.0) * 100, digits=2)
        if price and atr:
            out["dist_atr"] = _clean((p - price) / atr, digits=2)
        return out

    return {
        "summary": "; ".join(inp.signals.structure) if inp.signals.structure else "none",
        "support": [lvl(p) for p in sorted(levels.support, reverse=True)[:5]],
        "resistance": [lvl(p) for p in sorted(levels.resistance, reverse=True)[:5]],
        "prev_day_high": lvl(levels.prev_day_high) if levels.prev_day_high is not None else None,
        "prev_day_low": lvl(levels.prev_day_low) if levels.prev_day_low is not None else None,
    }


def _position_block(inp: BriefInputs) -> dict[str, Any] | None:
    pos = inp.position
    if not inp.holding or pos is None:
        return None
    price = float(inp.price)
    entry = float(pos.entry)
    out: dict[str, Any] = {
        "qty": float(pos.qty),
        "entry": float(pos.entry),
        "stop": float(pos.stop),
        "target": float(pos.target),
        "hours_held": round((inp.now - pos.opened_at).total_seconds() / 3600, 1),
        "unrealised_pct": _clean((price / entry - 1.0) * 100, digits=2) if entry else None,
    }
    r_unit = float(pos.r_unit)
    if r_unit and r_unit > 0:
        since = [c for c in inp.candles.get("1h", []) if c.open_time >= pos.opened_at]
        if since:
            out["best_r"] = _clean((max(float(c.high) for c in since) - entry) / r_unit, 2)
            out["worst_r"] = _clean((min(float(c.low) for c in since) - entry) / r_unit, 2)
        else:
            out["best_r"] = None
            out["worst_r"] = None
    return out


def build_brief(inp: BriefInputs) -> tuple[dict[str, Any], BriefMeta]:
    """Assemble the brief JSON for one pair and the meta the checks need."""
    s = inp.settings
    adx_threshold = float(s.market.adx_threshold)
    candles = inp.candles
    ctx = inp.context_candles

    data_age: dict[str, float] = {}
    bars: dict[str, int] = {}
    stale: list[str] = []
    for tf in TF_KEYS:
        tf_candles = candles.get(tf, [])
        bars[tf] = len(tf_candles)
        end = _candles_end(tf_candles, tf)
        if end is not None:
            age = (inp.now - end).total_seconds()
            data_age[tf] = round(age, 1)
            if age > TF_SECONDS[tf] + s.market.stale_grace_s:
                stale.append(tf)

    tf_block = {tf: _tf_block(tf, inp.signals.tf[tf], adx_threshold) for tf in TF_KEYS}

    # ---- market context: BTC and euro moves only, never other held pairs'
    # prices. Everything carries source + as-of; missing inputs stay null.
    btc_1h, btc_4h, btc_1d = ctx.get("btc_1h", []), ctx.get("btc_4h", []), ctx.get("btc_1d", [])
    btc_source_time = _candles_end(btc_4h, "4h") or _candles_end(btc_1h, "1h")

    btc_4h_gap, btc_4h_read = _ema_gap_read(btc_4h, 20, 50)
    btc_1d_gap, btc_1d_read = _ema_gap_read(btc_1d, 50, 200)

    btc_chg: float | None = None
    if len(btc_1h) >= 25:
        then = float(btc_1h[-25].close)
        now_c = float(btc_1h[-1].close)
        if then:
            btc_chg = (now_c / then - 1.0) * 100

    btc_atr_rank = compute_tf_values(btc_1h, "1h").atr_percentile if btc_1h else None

    eth_gap, eth_read = _eth_btc_trend(ctx.get("eth_4h", []), btc_4h)

    mctx: dict[str, Any] = {
        "btc_trend_4h": _context_item(btc_4h_gap, btc_4h_read, inp.source, btc_source_time),
        "btc_trend_1d": _context_item(
            btc_1d_gap, btc_1d_read, inp.source, _candles_end(btc_1d, "1d")
        ),
        "btc_chg_24h_pct": _context_item(
            btc_chg, _sign_read(btc_chg), inp.source, _candles_end(btc_1h, "1h")
        ),
        "btc_atr_rank_1h": _context_item(
            btc_atr_rank, atr_rank_read(btc_atr_rank), inp.source, _candles_end(btc_1h, "1h")
        ),
        "eth_btc_trend": _context_item(eth_gap, eth_read, inp.source, _candles_end(btc_4h, "4h")),
        "eur_usd_1h_pct": _context_item(
            inp.signals.context.eur_usd_1h_pct,
            _sign_read(inp.signals.context.eur_usd_1h_pct),
            inp.source,
            inp.now,
        ),
        "eur_usd_24h_pct": _context_item(
            inp.signals.context.eur_usd_24h_pct,
            _sign_read(inp.signals.context.eur_usd_24h_pct),
            inp.source,
            inp.now,
        ),
    }
    if inp.pair != "BTC-EUR":
        corr = inp.signals.context.corr_btc_30d
        corr_read = (
            "high" if corr is not None and corr >= float(s.risk.correlated_threshold) else "normal"
        )
        mctx["rs_btc_24h_pct"] = _context_item(
            inp.signals.context.rs_btc_24h_pct,
            _sign_read(inp.signals.context.rs_btc_24h_pct),
            inp.source,
            btc_source_time,
        )
        mctx["rs_btc_7d_pct"] = _context_item(
            inp.signals.context.rs_btc_7d_pct,
            _sign_read(inp.signals.context.rs_btc_7d_pct),
            inp.source,
            btc_source_time,
        )
        mctx["corr_btc_30d"] = _context_item(corr, corr_read, inp.source, btc_source_time)

    # ---- regime: BTC's brief gets one block, other pairs get pair + BTC.
    regime: dict[str, Any] = {"pair": inp.signals.regime.model_dump()}
    if inp.pair != "BTC-EUR" and inp.btc_regime is not None:
        regime["btc"] = inp.btc_regime.model_dump()

    ams = inp.now.astimezone(AMSTERDAM)
    brief: dict[str, Any] = {
        "pair": inp.pair,
        "now": inp.now.isoformat(),
        "price": float(inp.price),
        "tf": tf_block,
        "structure": _structure_block(inp),
        "market_context": mctx,
        "liquidity": {
            "spread_pct": _clean(inp.liquidity.get("spread_pct")),
            "depth_eur": _clean(inp.liquidity.get("depth_eur"), digits=0),
            "vol24h_vs_30d": _clean(inp.liquidity.get("vol24h_vs_30d"), digits=2),
        },
        "position": _position_block(inp),
        "pair_history": {
            "trades": [
                {"closed_at": t.isoformat(), "result_r": float(r)} for t, r in inp.pair_trades[:3]
            ],
            "hours_since_exit": _clean(inp.hours_since_exit, digits=1),
        },
        "previous_decision": (
            {"action": inp.previous_decision[0], "invalidation": inp.previous_decision[1]}
            if inp.previous_decision
            else None
        ),
        "account": {
            "exposure_pct": _clean(inp.account.get("exposure_pct"), digits=2),
            "today_pnl_pct": _clean(inp.account.get("today_pnl_pct"), digits=2),
            "drawdown_pct": _clean(inp.account.get("drawdown_pct"), digits=2),
        },
        "portfolio": {
            "held": [
                {"pair": p, "corr_30d": _clean(corr, digits=2), "hours_held": round(h, 1)}
                for p, corr, h in inp.held_portfolio
            ],
            "correlated_slots_left": inp.correlated_slots_left,
            "risk_throttle": inp.risk_throttle,
            "shock_brake": inp.shock_brake,
        },
        "regime": regime,
        "limits": {
            "min_rr": float(s.risk.min_rr),
            "min_stop_atr": float(s.risk.stop_min_atr),
            "max_stop_atr": float(s.risk.stop_max_atr),
            # The smallest stop distance that passes the "Costs vs risk"
            # gate: round-trip costs must stay under max_cost_pct_of_risk
            # of the stop, so the floor is cost_pct / (max_cost / 100).
            "min_stop_pct": float(
                (s.paper.fee_pct * 2 + s.paper.slippage_pct * 2) * 100 / s.risk.max_cost_pct_of_risk
            )
            if s.risk.max_cost_pct_of_risk > 0
            else None,
            "max_age_s": {tf: TF_SECONDS[tf] + s.market.stale_grace_s for tf in TF_KEYS},
        },
        "time": {"hour": ams.hour, "weekday": ams.strftime("%A")},
        "data_age_s": data_age,
    }

    meta = BriefMeta(
        pair=inp.pair,
        now=inp.now,
        price=float(inp.price),
        atr_1h=inp.signals.tf["1h"].atr14,
        holding=inp.holding,
        regime=inp.signals.regime,
        btc_regime=inp.btc_regime,
        bars=bars,
        data_age_s=data_age,
        stale_tfs=stale,
        support=[float(p) for p in inp.signals.levels.support],
        resistance=[float(p) for p in inp.signals.levels.resistance],
    )
    return brief, meta


def brief_json(brief: dict[str, Any]) -> str:
    """The brief serialized exactly as it goes into the request."""
    return json.dumps(brief, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------ checks --

# Bars needed before an indicator is meaningful: EMA n needs n+50 bars, the
# 14-period RSI/ATR/ADX need 42. The 1d block also carries EMA 200.
BARS_NEEDED: dict[str, int] = {"1h": 100, "4h": 100, "1d": 250}


def input_checks(brief: dict[str, Any], meta: BriefMeta) -> list[CheckOut]:
    """Validate the brief before it is sent. A failed check means the call
    is not sent and the cycle holds for this pair."""
    checks: list[CheckOut] = []

    # 1. Fields complete: every indicator field on every timeframe has a
    # value. A null means the data was missing or NaN upstream.
    missing: list[str] = []
    for tf in TF_KEYS:
        fields = list(TF_FIELDS) + (list(TF_FIELDS_1D_EXTRA) if tf == "1d" else [])
        for name in fields:
            item = brief["tf"].get(tf, {}).get(name)
            if not isinstance(item, dict) or item.get("value") is None:
                missing.append(f"{name} ({tf})")
    checks.append(
        CheckOut(
            name="Fields complete",
            status="fail" if missing else "pass",
            detail="Missing: " + ", ".join(missing) if missing else "all indicator fields present",
        )
    )

    # 2. Ranges valid: RSI inside 0..100, ATR positive, prices > 0.
    bad: list[str] = []
    for tf in TF_KEYS:
        rsi = brief["tf"][tf]["rsi"]["value"]
        if rsi is not None and not (0 <= rsi <= 100):
            bad.append(f"rsi ({tf}) = {rsi}")
        atr = brief["tf"][tf]["atr_pct"]["value"]
        if atr is not None and atr <= 0:
            bad.append(f"atr_pct ({tf}) = {atr}")
    price = brief.get("price")
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        bad.append("price")
    for lvl_list in brief["structure"]["support"] + brief["structure"]["resistance"]:
        p = lvl_list.get("price")
        if p is None or not math.isfinite(p) or p <= 0:
            bad.append("structure level")
    pos = brief.get("position")
    if pos is not None:
        for k in ("entry", "stop", "target"):
            v = pos.get(k)
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                bad.append(f"position.{k}")
    checks.append(
        CheckOut(
            name="Ranges valid",
            status="fail" if bad else "pass",
            detail="Invalid: " + ", ".join(bad) if bad else "RSI/ATR/price ranges sane",
        )
    )

    # 3. Fresh: each timeframe's data_age within the limit the brief states.
    over: list[str] = []
    limits = brief["limits"]["max_age_s"]
    for tf in TF_KEYS:
        age = meta.data_age_s.get(tf)
        if age is None:
            over.append(f"{tf}: no candles")
        elif age > limits[tf]:
            over.append(f"{tf}: {int(age)}s > {limits[tf]}s")
    checks.append(
        CheckOut(
            name="Fresh",
            status="fail" if over else "pass",
            detail="; ".join(over) if over else "all timeframes inside the limit",
        )
    )

    # 4. History: enough closed bars for the indicators the brief carries.
    short: list[str] = []
    for tf in TF_KEYS:
        need = BARS_NEEDED[tf]
        if meta.bars.get(tf, 0) < need:
            short.append(f"{tf}: {meta.bars.get(tf, 0)}/{need} bars")
    checks.append(
        CheckOut(
            name="History",
            status="fail" if short else "pass",
            detail="; ".join(short) if short else "enough closed bars for every indicator",
        )
    )

    # 5. No cross-pair leak: every price quoted stays within 50% of the
    # pair's own price; anything further could only be another pair's data.
    leaks: list[str] = []
    if meta.price is not None and math.isfinite(meta.price) and meta.price > 0:

        def _far(p: float | None) -> bool:
            return (
                p is not None
                and math.isfinite(p)
                and abs(p / meta.price - 1.0) * 100 > MAX_LEVEL_DISTANCE_PCT
            )

        for lvl in brief["structure"]["support"]:
            if _far(lvl.get("price")):
                leaks.append("support")
        for lvl in brief["structure"]["resistance"]:
            if _far(lvl.get("price")):
                leaks.append("resistance")
        for k in ("prev_day_high", "prev_day_low"):
            lvl = brief["structure"].get(k)
            if lvl and _far(lvl.get("price")):
                leaks.append(k)
        if pos is not None:
            for k in ("entry", "stop", "target"):
                if _far(pos.get(k)):
                    leaks.append(f"position.{k}")
    checks.append(
        CheckOut(
            name="No cross-pair leak",
            status="fail" if leaks else "pass",
            detail="Out-of-range: " + ", ".join(sorted(set(leaks)))
            if leaks
            else "every level within 50% of price",
        )
    )

    # 6. Market context warnings: missing BTC/euro context never blocks,
    # but the model is told it's absent.
    absent = [
        name
        for name, item in brief["market_context"].items()
        if not isinstance(item, dict) or item.get("value") is None
    ]
    checks.append(
        CheckOut(
            name="Market context",
            status="warn" if absent else "pass",
            detail="Missing: " + ", ".join(absent) if absent else "BTC and euro context present",
        )
    )
    return checks


# --------------------------------------------------------------- repo glue --


def pair_history(
    session: Session, account_id: int, pair: str, now: datetime
) -> tuple[list[tuple[datetime, Decimal]], float | None]:
    """The pair's last three closed trades plus hours since the last exit."""
    trades = repo.list_trade_rows(session, account_id, pair=pair, limit=3)
    out = [(repo.aware_utc(t.closed_at), Decimal(t.realised_r)) for t in trades]
    hours: float | None = None
    if out:
        hours = round((now - out[0][0]).total_seconds() / 3600, 1)
    return out, hours


def previous_decision(
    session: Session, account_id: int, pair: str
) -> tuple[str, str | None] | None:
    """The pair's previous decision — action and invalidation only."""
    rows = repo.list_decision_rows(session, account_id=account_id, pair=pair, limit=1)
    if not rows:
        return None
    return rows[0].action, rows[0].invalidation
