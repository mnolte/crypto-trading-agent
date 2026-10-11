"""Screen endpoints for the PR-4 GUI: candles, decisions, indicators,
performance, trades, activity, flatten preview, prompts, exports and the
danger-zone delete.

Every value the screens show is computed here (or in the engine/signals
modules); the frontend only formats. Money stays ``Decimal``, indicator
maths stays ``float``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, cast

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent.broker.paper import PaperBroker
from app.agent.market.base import TF_SECONDS, Candle, MarketDataError, drop_forming
from app.agent.signals import (
    Levels,
    TfValues,
    adx,
    atr,
    compute_pair_signals,
    compute_regime,
    compute_tf_values,
    ema,
    obv,
    reads_for_tf,
    rolling_vwap,
    rsi,
    trend_labels_4h,
)
from app.api.deps import AppState, get_state, require_token
from app.api.routes import broadcast_snapshot
from app.db import AMSTERDAM, AccountRow, DecisionRow, MarketRow, PositionRow, TradeRow, utcnow
from app.models import (
    ActivityListOut,
    ActivityOut,
    CandleFeed,
    CandlePoint,
    ChartMarker,
    Decision,
    DecisionDetail,
    DecisionListOut,
    DeleteDataRequest,
    DrawdownPoint,
    EquityPoint,
    FlattenPreview,
    HealthOut,
    IndicatorCell,
    IndicatorRowOut,
    IndicatorsOut,
    LevelOut,
    LiquidityOut,
    PerformanceOut,
    PerfTotals,
    PerPairPerf,
    PositionLines,
    PromptInfo,
    SeriesPoint,
    Tf,
    TradeListOut,
    TradeOut,
    TrendPoint,
)

router = APIRouter()

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
DECISION_MARK_ACTIONS = {"BUY", "SELL", "SKIP"}  # HOLD markers would crowd the chart

TF_LIMITS: dict[Tf, int] = {"1h": 800, "4h": 300, "1d": 300}


def decision_out(row: DecisionRow) -> Decision:
    signals = json.loads(row.signals or "[]")
    gates = json.loads(row.gates or "[]")
    return Decision(
        id=row.id,
        cycle=row.cycle,
        at=repo.aware_utc(row.at),
        pair=row.pair,
        decider=row.decider,
        model=row.model,
        prompt_version=row.prompt_version,
        action=row.action,
        confidence=row.confidence,
        reasoning=row.reasoning,
        invalidation=row.invalidation,
        signals=signals,
        plan=json.loads(row.plan) if row.plan else None,
        gates=gates,
        status=row.status,
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


def _session(state: AppState) -> Session:
    return state.session_factory()


def _candle_points(candles: list[Candle]) -> list[CandlePoint]:
    return [
        CandlePoint(
            time=c.open_time,
            open=float(c.open),
            high=float(c.high),
            low=float(c.low),
            close=float(c.close),
            volume=float(c.volume),
        )
        for c in candles
    ]


def _trade_markers(session: Session, pair: str, account_id: int | None) -> list[ChartMarker]:
    conds = [TradeRow.pair == pair]
    if account_id is not None:
        conds.append(TradeRow.account_id == account_id)
    rows = session.scalars(select(TradeRow).where(*conds)).all()
    markers = [
        ChartMarker(
            time=repo.aware_utc(t.opened_at), kind="buy", label="B", decision_id=t.decision_id
        )
        for t in rows
    ]
    markers += [
        ChartMarker(time=repo.aware_utc(t.closed_at), kind="sell", label="S", decision_id=None)
        for t in rows
    ]
    return markers


# ------------------------------------------------------------------- candles


@router.get("/api/candles/{pair}", response_model=CandleFeed)
def get_candles(
    request: Request,
    pair: str,
    tf: Tf = "1h",
    bars: Annotated[int, Query(ge=1, le=800)] = 200,
) -> CandleFeed:
    """Dashboard chart feed: stored closed candles, trade markers and the
    open position's entry/stop/target lines."""
    state = get_state(request)
    session = _session(state)
    try:
        engine = state.agent_engine
        now = engine.clock() if engine else utcnow()
        candles = drop_forming(repo.load_candles(session, pair, tf, bars), tf, now)
        account_id = state.broker.account_id if state.broker else None
        markers = _trade_markers(session, pair, account_id)
        markers = [m for m in markers if candles and m.time >= candles[0].open_time]
        position = None
        if engine is not None and pair in engine.broker.positions:
            pos = engine.broker.positions[pair]
            position = PositionLines(entry=pos.entry, stop=pos.stop, target=pos.target)
        return CandleFeed(
            pair=pair,
            tf=tf,
            candles=_candle_points(candles),
            markers=markers,
            position=position,
        )
    finally:
        session.close()


# ----------------------------------------------------------------- decisions


@router.get("/api/decisions", response_model=DecisionListOut)
def get_decisions(
    request: Request,
    pair: str | None = None,
    outcome: Literal["all", "buy", "sell", "hold", "skip", "filled"] = "all",
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DecisionListOut:
    """Newest-first decision history; ``before`` pages backwards in time."""
    state = get_state(request)
    session = _session(state)
    try:
        action = outcome.upper() if outcome in ("buy", "sell", "hold", "skip") else None
        status = "executed" if outcome == "filled" else None
        account_id = state.broker.account_id if state.broker else None
        rows = repo.list_decision_rows(
            session,
            account_id=account_id,
            pair=pair,
            action=action,
            status=status,
            before=before,
            limit=limit,
        )
        return DecisionListOut(items=[decision_out(r) for r in rows])
    finally:
        session.close()


@router.get("/api/decisions/{decision_id}", response_model=DecisionDetail)
def get_decision(request: Request, decision_id: str) -> DecisionDetail:
    """One decision plus the raw stored call and prev/next ids for the
    drawer's stepper."""
    state = get_state(request)
    session = _session(state)
    try:
        row = repo.get_decision_row(session, decision_id)
        if row is None or (
            state.broker is not None and row.account_id != state.broker.account_id
        ):
            raise HTTPException(status_code=404, detail="unknown decision")
        prev_row = session.scalar(
            select(DecisionRow)
            .where(
                DecisionRow.account_id == row.account_id,
                (DecisionRow.at < row.at)
                | ((DecisionRow.at == row.at) & (DecisionRow.id < row.id)),
            )
            .order_by(DecisionRow.at.desc(), DecisionRow.id.desc())
            .limit(1)
        )
        next_row = session.scalar(
            select(DecisionRow)
            .where(
                DecisionRow.account_id == row.account_id,
                (DecisionRow.at > row.at)
                | ((DecisionRow.at == row.at) & (DecisionRow.id > row.id)),
            )
            .order_by(DecisionRow.at.asc(), DecisionRow.id.asc())
            .limit(1)
        )
        fill_price: Decimal | None = None
        if row.action == "BUY":
            # Only a real position or closed trade proves the BUY filled.
            pos = session.scalar(
                select(PositionRow).where(
                    PositionRow.account_id == row.account_id,
                    PositionRow.pair == row.pair,
                    PositionRow.decision_id == row.id,
                )
            )
            if pos is not None:
                fill_price = Decimal(pos.entry)
            else:
                trade = session.scalar(
                    select(TradeRow)
                    .where(
                        TradeRow.account_id == row.account_id,
                        TradeRow.pair == row.pair,
                        TradeRow.decision_id == row.id,
                    )
                    .order_by(TradeRow.id.desc())
                    .limit(1)
                )
                if trade is not None:
                    fill_price = Decimal(trade.entry)
        return DecisionDetail(
            decision=decision_out(row),
            raw_brief=row.raw_brief,
            raw_response=row.raw_response,
            prev_id=prev_row.id if prev_row else None,
            next_id=next_row.id if next_row else None,
            execution_mode=state.settings.execution_mode,
            fill_price=fill_price,
        )
    finally:
        session.close()


# ---------------------------------------------------------------- indicators


def _indicator_series(candles: list[Candle], tf: Tf) -> dict[str, list[SeriesPoint]]:
    """One point per candle for every overlay/pane series."""
    names = ["ema20", "ema50", "ema200", "vwap", "rsi14", "adx14", "atr_pct", "obv", "vol_avg"]
    if not candles:
        return {name: [] for name in names}
    high = np.array([float(c.high) for c in candles])
    low = np.array([float(c.low) for c in candles])
    close = np.array([float(c.close) for c in candles])
    volume = np.array([float(c.volume) for c in candles])
    adx14, _, _ = adx(high, low, close)
    atr14 = atr(high, low, close)
    arrays = {
        "ema20": ema(close, 20),
        "ema50": ema(close, 50),
        "ema200": ema(close, 200),
        "vwap": rolling_vwap(high, low, close, volume),
        "rsi14": rsi(close),
        "adx14": adx14,
        "atr_pct": atr14 / close * 100.0,
        "obv": obv(close, volume),
        "vol_avg": np.array(
            [
                float(np.mean(volume[max(0, i - 19) : i + 1])) if i >= 19 else np.nan
                for i in range(len(volume))
            ]
        ),
    }
    times = [c.open_time for c in candles]
    return {
        name: [
            SeriesPoint(time=t, value=None if np.isnan(v) else round(float(v), 6))
            for t, v in zip(times, arr, strict=True)
        ]
        for name, arr in arrays.items()
    }


# Glyphs used by the design files (kept as escape-based constants so the
# ambiguous-unicode lint rule stays quiet).
MINUS = "\u2212"  # minus sign
TIMES = "\u00d7"  # multiplication sign
PM = "\u00b1"  # plus-minus
DOT = "\u00b7"  # middle dot


def _f1(v: float | None) -> str | None:
    return None if v is None else f"{v:.1f}"


def _signed(v: float | None, dp: int = 2, suffix: str = "") -> str | None:
    """'+0.34%' / '-0.42' — always signed, typographic minus, as in the design."""
    if v is None:
        return None
    return f"{'+' if v >= 0 else MINUS}{abs(v):.{dp}f}{suffix}"


def _current_rows(tf_values: dict[str, TfValues], adx_threshold: float) -> list[IndicatorRowOut]:
    """The "Current values" table: one row per indicator, one cell per tf.
    Row order, cell text and reads mirror the design file's own definitions."""
    reads = {
        tf: {s.name: s.read for s in reads_for_tf(v, tf, adx_threshold)}  # type: ignore[arg-type]
        for tf, v in tf_values.items()
    }

    def gap(v: TfValues) -> float | None:
        if v.ema20 is None or not v.ema50:
            return None
        return (v.ema20 / v.ema50 - 1.0) * 100.0

    def adx_label(v: TfValues) -> str | None:
        if v.adx14 is None:
            return None
        return f"{v.adx14:.0f} {'trending' if v.adx14 >= adx_threshold else 'ranging'}"

    def atr_label(v: TfValues) -> str | None:
        if v.atr_pct is None:
            return None
        tail = f" {DOT} p{v.atr_percentile:.0f}" if v.atr_percentile is not None else ""
        return f"{v.atr_pct:.2f}%{tail}"

    def cells(
        fmt: Callable[[TfValues], str | None], read_name: str | None = None
    ) -> dict[str, IndicatorCell]:
        return {
            tf: IndicatorCell(
                text=fmt(v),
                read=reads[tf].get(read_name) if read_name else None,
            )
            for tf, v in tf_values.items()
        }

    return [
        IndicatorRowOut(
            name="RSI 14",
            threshold="RSI above 55 bullish, below 45 bearish",
            cells=cells(lambda v: _f1(v.rsi14), "RSI 14"),
        ),
        IndicatorRowOut(
            name="EMA 20 vs 50",
            threshold="EMA 20 vs 50 crossover",
            cells=cells(lambda v: _signed(gap(v), 2, "%"), "EMA 20 vs 50"),
        ),
        IndicatorRowOut(
            name="Price vs EMA 200",
            threshold=f"EMA 200 distance beyond {PM}0.1%",
            cells=cells(lambda v: _signed(v.price_vs_ema200_pct, 1, "%"), "Price vs EMA 200"),
        ),
        IndicatorRowOut(
            name="Price vs VWAP",
            threshold=f"VWAP distance beyond {PM}0.1%",
            cells=cells(lambda v: _signed(v.price_vs_vwap_pct, 2, "%"), "Price vs VWAP"),
        ),
        IndicatorRowOut(
            name="OBV trend",
            threshold=f"OBV trend beyond {PM}0.1",
            cells=cells(lambda v: _signed(v.obv_trend, 2), "OBV trend"),
        ),
        IndicatorRowOut(
            name="ADX 14",
            threshold=f"ADX {adx_threshold:g} or more trending",
            cells=cells(adx_label),
        ),
        IndicatorRowOut(
            name="ATR % of price",
            threshold="percentile over the tf lookback",
            cells=cells(atr_label),
        ),
        IndicatorRowOut(
            name=f"Volume {TIMES} avg",
            threshold="20-bar average",
            cells=cells(
                lambda v: None if v.volume_ratio is None else f"{v.volume_ratio:.2f}{TIMES}"
            ),
        ),
    ]


def _levels_out(
    levels: Levels,
    last_close: float | None,
    atr_1h: float | None,
) -> list[LevelOut]:
    """Support/resistance and prev-day levels with % and ATR distances."""
    out: list[LevelOut] = []

    def dist(price: float) -> tuple[float | None, float | None]:
        if not last_close:
            return None, None
        pct = (price / last_close - 1.0) * 100.0
        atr_d = (price - last_close) / atr_1h if atr_1h else None
        return pct, atr_d

    for price in sorted(levels.resistance, reverse=True):
        pct, atr_d = dist(price)
        out.append(LevelOut(kind="resistance", price=price, dist_pct=pct, dist_atr=atr_d))
    for price in sorted(levels.support, reverse=True):
        pct, atr_d = dist(price)
        out.append(LevelOut(kind="support", price=price, dist_pct=pct, dist_atr=atr_d))
    for kind, lvl_price in (
        ("prev_day_high", levels.prev_day_high),
        ("prev_day_low", levels.prev_day_low),
    ):
        if lvl_price is not None:
            pct, atr_d = dist(lvl_price)
            out.append(
                LevelOut(
                    kind=cast(Literal["prev_day_high", "prev_day_low"], kind),
                    price=lvl_price,
                    dist_pct=pct,
                    dist_atr=atr_d,
                )
            )
    return out


def _regime_band(
    display: list[Candle], candles_4h: list[Candle], candles_1d: list[Candle], threshold: float
) -> list[TrendPoint]:
    """Each displayed bar coloured by the confirmed 4h trend known when it opened."""
    labels = trend_labels_4h(candles_4h, candles_1d, threshold)
    if not labels:
        return []
    # (close time, label) per 4h bar — the label is known at its close.
    known: list[tuple[datetime, Literal["up", "down", "sideways"]]] = [
        (
            c.open_time + timedelta(seconds=TF_SECONDS["4h"]),
            cast(Literal["up", "down", "sideways"], lbl),
        )
        for c, lbl in zip(candles_4h, labels, strict=True)
    ]
    out: list[TrendPoint] = []
    j = -1
    for c in display:
        while j + 1 < len(known) and known[j + 1][0] <= c.open_time:
            j += 1
        label = known[j][1] if j >= 0 else "sideways"
        out.append(TrendPoint(time=c.open_time, value=label))
    return out


async def _liquidity(state: AppState, session: Session, pair: str) -> LiquidityOut:
    """Live order-book spread, ±0.5% depth and today's volume vs the 30d avg."""
    spread_pct: float | None = None
    depth: float | None = None
    try:
        book = await state.market_client.order_book(pair)
        if book.best_bid is None or book.best_ask is None:
            raise MarketDataError("empty order book")
        mid = (book.best_bid + book.best_ask) / 2
        if book.spread_pct is not None:
            spread_pct = float(book.spread_pct)
        band_low, band_high = mid * Decimal("0.995"), mid * Decimal("1.005")
        depth = float(
            sum(lvl.price * lvl.size for lvl in book.bids if lvl.price >= band_low)
            + sum(lvl.price * lvl.size for lvl in book.asks if lvl.price <= band_high)
        )
    except MarketDataError:
        pass
    vol24: Decimal | None = None
    try:
        tickers = await state.market_client.ticker_24h(pair)
        if tickers:
            vol24 = tickers[0].volume_quote
    except MarketDataError:
        pass
    row = repo.get_market_row(session, pair)
    avg = Decimal(str(row.volume_30d_eur)) if row and row.volume_30d_eur else None
    ratio = float(vol24 / avg) if vol24 is not None and avg else None
    return LiquidityOut(
        spread_pct=round(spread_pct, 4) if spread_pct is not None else None,
        depth_eur=round(depth, 0) if depth is not None else None,
        vol24h_vs_30d=round(ratio, 2) if ratio is not None else None,
    )


@router.get("/api/indicators/{pair}", response_model=IndicatorsOut)
async def get_indicators(
    request: Request,
    pair: str,
    tf: Tf = "1h",
    bars: Annotated[int, Query(ge=10, le=800)] = 300,
    at: str | None = None,
) -> IndicatorsOut:
    """The Indicators screen in one payload.

    ``at`` pins every value to a stored decision: the candles are sliced at
    the decision's timestamp so the numbers match what the decider saw.
    """
    state = get_state(request)
    session = _session(state)
    try:
        settings = state.settings
        threshold = float(settings.market.adx_threshold)
        engine = state.agent_engine
        now = engine.clock() if engine else utcnow()

        frozen_at: datetime | None = None
        frozen_cycle: int | None = None
        frozen_decision_id: str | None = None
        if at is not None:
            drow = repo.get_decision_row(session, at)
            if drow is None:
                # ``at`` also accepts an llm-call id: freeze at its linked
                # decision, or at the call's own moment when it produced none.
                call = repo.get_llm_call(session, at)
                if call is None:
                    raise HTTPException(status_code=404, detail="unknown decision")
                if call.pair != pair:
                    raise HTTPException(status_code=400, detail="call is for another pair")
                if call.decision_id:
                    drow = repo.get_decision_row(session, call.decision_id)
                else:
                    frozen_at = repo.aware_utc(call.at)
                    frozen_cycle = call.cycle
            if drow is not None:
                if drow.pair != pair:
                    raise HTTPException(status_code=400, detail="decision is for another pair")
                frozen_at = repo.aware_utc(drow.at)
                frozen_cycle = drow.cycle
                frozen_decision_id = drow.id

        ref = frozen_at or now
        candles_by_tf: dict[Tf, list[Candle]] = {}
        for t in ("1h", "4h", "1d"):
            raw = repo.load_candles(session, pair, t, TF_LIMITS[t], end=frozen_at)
            candles_by_tf[t] = drop_forming(raw, t, ref)

        ctx = compute_pair_signals(
            pair,
            candles_by_tf["1h"],
            candles_by_tf["4h"],
            candles_by_tf["1d"],
            btc_1h=drop_forming(
                repo.load_candles(session, "BTC-EUR", "1h", TF_LIMITS["1h"], end=frozen_at),
                "1h",
                ref,
            ),
            usdc_1h=drop_forming(
                repo.load_candles(session, "USDC-EUR", "1h", 48, end=frozen_at), "1h", ref
            ),
            settings=settings.market,
            now=ref,
        )

        display = candles_by_tf[tf][-bars:]
        series_all = _indicator_series(candles_by_tf[tf], tf)
        series = {name: pts[-len(display) :] for name, pts in series_all.items()}

        btc_4h = drop_forming(
            repo.load_candles(session, "BTC-EUR", "4h", TF_LIMITS["4h"], end=frozen_at), "4h", ref
        )
        btc_1d = drop_forming(
            repo.load_candles(session, "BTC-EUR", "1d", TF_LIMITS["1d"], end=frozen_at), "1d", ref
        )
        btc_1h = drop_forming(
            repo.load_candles(session, "BTC-EUR", "1h", TF_LIMITS["1h"], end=frozen_at), "1h", ref
        )
        btc_regime = (
            compute_regime(
                btc_4h,
                btc_1d,
                compute_tf_values(btc_1h, "1h").atr_percentile,
                threshold,
            )
            if btc_4h
            else None
        )

        # Data health is about the freshest data regardless of the pane: the
        # newest closed 1h candle against the 1h freshness limit.
        newest_age = (
            int(
                (
                    ref - (candles_by_tf["1h"][-1].open_time + timedelta(seconds=TF_SECONDS["1h"]))
                ).total_seconds()
            )
            if candles_by_tf["1h"]
            else None
        )
        stale_limit = TF_SECONDS["1h"] + settings.market.stale_grace_s
        missing = 0
        if len(display) >= 2:
            span = (display[-1].open_time - display[0].open_time).total_seconds()
            missing = max(0, int(span // TF_SECONDS[tf]) + 1 - len(display))

        account_id = state.broker.account_id if state.broker else None
        markers = _trade_markers(session, pair, account_id)
        if display:
            lo, hi = display[0].open_time, display[-1].open_time + timedelta(seconds=TF_SECONDS[tf])
            markers = [m for m in markers if lo <= m.time <= hi]
            drows = repo.list_decision_rows(
                session,
                account_id=account_id,
                pair=pair,
                after=lo,
                before=hi + timedelta(microseconds=1),
                limit=None,
            )
            for d in drows:
                d_at = repo.aware_utc(d.at)
                if d.action not in DECISION_MARK_ACTIONS:
                    continue
                markers.append(
                    ChartMarker(
                        time=d_at,
                        kind="decision",
                        action=d.action,
                        label="D",
                        decision_id=d.id,
                    )
                )
        markers.sort(key=lambda m: m.time)

        liquidity = LiquidityOut(spread_pct=None, depth_eur=None, vol24h_vs_30d=None)
        if frozen_at is None:
            liquidity = await _liquidity(state, session, pair)

        return IndicatorsOut(
            pair=pair,
            tf=tf,
            frozen=frozen_at is not None,
            frozen_at=frozen_at,
            frozen_decision_id=frozen_decision_id,
            frozen_cycle=frozen_cycle,
            candles=_candle_points(display),
            series=series,
            regime_band=_regime_band(display, candles_by_tf["4h"], candles_by_tf["1d"], threshold),
            regime=ctx.regime,
            btc_regime=btc_regime,
            current=_current_rows(ctx.tf, threshold),
            levels=_levels_out(ctx.levels, ctx.tf["1h"].close, ctx.tf["1h"].atr14),
            structure=ctx.structure,
            context=ctx.context.model_dump(),
            liquidity=liquidity,
            health=HealthOut(
                bars_loaded=len(display),
                bars_requested=bars,
                newest_closed_age_s=newest_age,
                stale_limit_s=stale_limit,
                stale=newest_age is not None and newest_age > stale_limit,
                missing_bars=missing,
                source="Simulated market"
                if settings.data_source == "sim"
                else "Bitvavo public API",
            ),
            markers=markers,
            adx_threshold=threshold,
        )
    finally:
        session.close()


@router.get("/api/indicators/{pair}/export.csv")
def export_indicators(
    request: Request,
    pair: str,
    tf: Tf = "1h",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: datetime | None = None,
) -> Response:
    """CSV of candles plus every indicator series for the window."""
    state = get_state(request)
    session = _session(state)
    try:
        candles = drop_forming(repo.load_candles(session, pair, tf, 20000, end=end), tf, utcnow())
        if start is not None:
            candles = [c for c in candles if c.open_time >= start]
        series = _indicator_series(candles, tf)
        names = list(series)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "open", "high", "low", "close", "volume", *names])
        for i, c in enumerate(candles):
            w.writerow(
                [
                    c.open_time.isoformat(),
                    str(c.open),
                    str(c.high),
                    str(c.low),
                    str(c.close),
                    str(c.volume),
                    *[
                        "" if series[n][i].value is None else f"{series[n][i].value:.6f}"
                        for n in names
                    ],
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{pair}-{tf}-indicators.csv"'},
        )
    finally:
        session.close()


# ---------------------------------------------------------------- performance


def _in_market_seconds(
    intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> float:
    """Union of open-position intervals inside [start, end], in seconds."""
    clipped = sorted((max(s, start), min(e, end)) for s, e in intervals if e > start and s < end)
    total = 0.0
    cur_s: datetime | None = None
    cur_e: datetime | None = None
    for s, e in clipped:
        if s >= e:
            continue
        if cur_s is None or cur_e is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    if cur_s is not None and cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


def _perf_totals(
    equity: list[EquityPoint],
    trades: list[TradeRow],
    positions: int,
    btc_return: float | None,
    in_market_s: float,
    window_s: float,
) -> PerfTotals:
    ret = None
    if len(equity) >= 2 and Decimal(equity[0].equity) != 0:
        ret = float((Decimal(equity[-1].equity) / Decimal(equity[0].equity) - 1) * 100)
    max_dd = None
    if equity:
        peak = Decimal(equity[0].equity)
        dds = [Decimal("0")]
        for p in equity:
            e = Decimal(p.equity)
            peak = max(peak, e)
            if peak > 0:
                dds.append((e / peak - 1) * 100)
        max_dd = float(min(dds))
    wins = sum(1 for t in trades if Decimal(t.realised_eur) > 0)
    gross_win = sum(Decimal(t.realised_eur) for t in trades if Decimal(t.realised_eur) > 0)
    gross_loss = -sum(Decimal(t.realised_eur) for t in trades if Decimal(t.realised_eur) < 0)
    rs = [float(t.realised_r) for t in trades]
    vs_btc = ret - btc_return if ret is not None and btc_return is not None else None
    return PerfTotals(
        return_pct=round(ret, 2) if ret is not None else None,
        benchmark_return_pct=round(btc_return, 2) if btc_return is not None else None,
        vs_btc_pct=round(vs_btc, 2) if vs_btc is not None else None,
        max_drawdown_pct=round(max_dd, 2) if max_dd is not None else None,
        trades=len(trades),
        open_now=positions,
        win_rate_pct=round(wins / len(trades) * 100, 1) if trades else None,
        wins=wins,
        avg_r=round(sum(rs) / len(rs), 2) if rs else None,
        profit_factor=(round(float(gross_win / gross_loss), 2) if gross_loss > 0 else None),
        fees_eur=sum(Decimal(t.fee_eur) for t in trades),
        time_in_market_pct=round(in_market_s / window_s * 100, 1) if window_s > 0 else None,
    )


@router.get("/api/performance", response_model=PerformanceOut)
def get_performance(
    request: Request,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: datetime | None = None,
) -> PerformanceOut:
    """Equity curve, BTC benchmark, drawdown, totals and per-pair stats for
    the current account over ``start..end``. ``start`` defaults to the
    account's creation ("Since start")."""
    state = get_state(request)
    session = _session(state)
    try:
        engine = state.agent_engine
        now = engine.clock() if engine else utcnow()
        end = end or now
        broker = state.broker
        account_id = broker.account_id if broker else None
        if start is None:
            account_row = session.get(AccountRow, account_id) if account_id is not None else None
            start = (
                repo.aware_utc(account_row.started_at)
                if account_row is not None
                else end - timedelta(days=30)
            )

        rows = (
            repo.list_equity_snapshots(session, account_id, start, end)
            if account_id is not None
            else []
        )
        equity = [EquityPoint(at=repo.aware_utc(r.at), equity=Decimal(r.equity)) for r in rows]
        account = session.get(AccountRow, account_id) if account_id is not None else None
        if account is not None and not equity:
            # No cycles in the window yet: a flat line at the account's balance.
            equity = [
                EquityPoint(
                    at=max(repo.aware_utc(account.started_at), start),
                    equity=Decimal(account.start_balance),
                ),
                EquityPoint(
                    at=end,
                    equity=Decimal(broker.equity()) if broker else Decimal(account.start_balance),
                ),
            ]
        elif broker is not None and equity and equity[-1].at < end - timedelta(seconds=1):
            equity.append(EquityPoint(at=end, equity=broker.equity()))

        # BTC buy-and-hold benchmark, normalised to the first equity value.
        benchmark: list[SeriesPoint] = []
        btc_return = None
        btc = repo.load_candles(session, "BTC-EUR", "1h", 10000, end=end)
        btc = [c for c in btc if c.open_time <= end]
        if equity and btc:
            anchor_close = next(
                (float(c.close) for c in btc if c.open_time >= start), float(btc[0].close)
            )
            closes = [(c.open_time, float(c.close)) for c in btc if c.open_time >= start]
            if closes and anchor_close > 0:
                e0 = Decimal(equity[0].equity)
                j = 0
                for p in equity:
                    while j + 1 < len(closes) and closes[j + 1][0] <= p.at:
                        j += 1
                    if closes[j][0] <= p.at:
                        value = e0 * Decimal(str(closes[j][1] / anchor_close))
                        benchmark.append(SeriesPoint(time=p.at, value=float(value)))
                btc_return = (closes[-1][1] / anchor_close - 1) * 100

        trades = (
            repo.list_trade_rows(session, account_id, start=start, end=end, limit=5000)
            if account_id is not None
            else []
        )
        positions = len(broker.positions) if broker else 0

        # Time in market: share of the window with at least one open position.
        # Intervals come from every trade overlapping the window plus the
        # positions still open; overlapping intervals are merged.
        intervals: list[tuple[datetime, datetime]] = [
            (max(repo.aware_utc(p.opened_at), start), end)
            for p in (broker.positions.values() if broker else [])
        ]
        if account_id is not None:
            intervals += [
                (repo.aware_utc(t.opened_at), repo.aware_utc(t.closed_at))
                for t in repo.list_trade_rows(session, account_id, start=start, limit=5000)
                if repo.aware_utc(t.opened_at) <= end
            ]
        in_market_s = _in_market_seconds(intervals, start, end)
        window_s = max(1.0, (end - start).total_seconds())

        dds: list[DrawdownPoint] = []
        if equity:
            peak = Decimal(equity[0].equity)
            for p in equity:
                e = Decimal(p.equity)
                peak = max(peak, e)
                pct = float((e / peak - 1) * 100) if peak > 0 else 0.0
                dds.append(DrawdownPoint(at=p.at, pct=round(pct, 3)))

        by_pair: dict[str, list[TradeRow]] = {}
        for t in trades:
            by_pair.setdefault(t.pair, []).append(t)
        per_pair = [
            PerPairPerf(
                pair=pair,
                trades=len(ts),
                realised_eur=sum(Decimal(t.realised_eur) for t in ts),
                avg_r=round(sum(float(t.realised_r) for t in ts) / len(ts), 2),
                fees_eur=sum(Decimal(t.fee_eur) for t in ts),
            )
            for pair, ts in sorted(by_pair.items())
        ]

        return PerformanceOut(
            start=start,
            end=end,
            equity=equity,
            benchmark=benchmark,
            drawdown=dds,
            kill_line_pct=float(state.settings.risk.kill_drawdown_pct),
            totals=_perf_totals(equity, trades, positions, btc_return, in_market_s, window_s),
            per_pair=per_pair,
        )
    finally:
        session.close()


# --------------------------------------------------------------------- trades


def _trade_out(t: TradeRow) -> TradeOut:
    return TradeOut(
        id=t.id,
        pair=t.pair,
        qty=Decimal(t.qty),
        entry=Decimal(t.entry),
        exit=Decimal(t.exit),
        opened_at=repo.aware_utc(t.opened_at),
        closed_at=repo.aware_utc(t.closed_at),
        fee_eur=Decimal(t.fee_eur),
        realised_eur=Decimal(t.realised_eur),
        realised_r=float(t.realised_r),
        exit_reason=t.exit_reason,
        decision_id=t.decision_id,
    )


@router.get("/api/trades", response_model=TradeListOut)
def get_trades(
    request: Request,
    pair: str | None = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> TradeListOut:
    state = get_state(request)
    session = _session(state)
    try:
        account_id = state.broker.account_id if state.broker else -1
        rows = repo.list_trade_rows(
            session, account_id, pair=pair, start=start, end=end, limit=limit
        )
        return TradeListOut(items=[_trade_out(t) for t in rows])
    finally:
        session.close()


# ------------------------------------------------------------------- activity


@router.get("/api/activity", response_model=ActivityListOut)
def get_activity(
    request: Request,
    pair: str | None = None,
    kind: str | None = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ActivityListOut:
    """Newest-first activity log; ``before`` is the row id of the last item
    from the previous page."""
    state = get_state(request)
    session = _session(state)
    try:
        rows = repo.list_activity(session, pair=pair, kind=kind, before=before, limit=limit)
        return ActivityListOut(
            items=[
                ActivityOut(
                    id=r.id,
                    at=repo.aware_utc(r.at),
                    kind=r.kind,
                    pair=r.pair,
                    text=r.text,
                    decision_id=r.decision_id,
                )
                for r in rows
            ],
            next_before=rows[-1].id if len(rows) == limit else None,
        )
    finally:
        session.close()


# ----------------------------------------------------------------- agent bits


@router.get("/api/agent/flatten-preview", response_model=FlattenPreview)
def flatten_preview(request: Request) -> FlattenPreview:
    """What Stop & flatten would close right now: count, estimated exit fees
    and the live unrealised P&L."""
    state = get_state(request)
    engine = state.agent_engine
    if engine is None:
        return FlattenPreview(
            count=0, pairs=[], est_fees_eur=Decimal("0"), unrealised_eur=Decimal("0")
        )
    positions = list(engine.broker.positions.values())
    fee_pct = state.settings.paper.fee_pct / Decimal("100")
    return FlattenPreview(
        count=len(positions),
        pairs=sorted(p.pair for p in positions),
        est_fees_eur=sum(p.qty * p.last * fee_pct for p in positions),
        unrealised_eur=sum(p.unrealised for p in positions),
    )


@router.get("/api/prompts", response_model=list[PromptInfo])
def get_prompts() -> list[PromptInfo]:
    """Every prompt version on disk with the model parameters from its header
    (the Settings select)."""
    out: list[PromptInfo] = []
    for path in sorted(PROMPTS_DIR.glob("decision.v*.md")):
        head = path.read_text()[:1200]
        vid = re.search(r"prompt_version:\s*(\S+)", head)
        temp = re.search(r"temperature:\s*(\S+)", head)
        tokens = re.search(r"max_output_tokens:\s*(\d+)", head)
        if vid is None:
            continue
        out.append(
            PromptInfo(
                id=vid.group(1),
                model="gpt-4o-mini",
                temperature=float(temp.group(1)) if temp else 0.0,
                max_output_tokens=int(tokens.group(1)) if tokens else 0,
            )
        )
    return out


# -------------------------------------------------------------------- exports


@router.get("/api/export/trades.csv")
def export_trades(
    request: Request,
    pair: str | None = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: datetime | None = None,
) -> Response:
    """Every closed trade (all accounts), newest first."""
    state = get_state(request)
    session = _session(state)
    try:
        conds = []
        if pair:
            conds.append(TradeRow.pair == pair)
        if start:
            conds.append(TradeRow.closed_at >= repo._utc_naive(start))
        if end:
            conds.append(TradeRow.closed_at <= repo._utc_naive(end))
        rows = session.scalars(
            select(TradeRow).where(*conds).order_by(TradeRow.closed_at.desc())
        ).all()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "id",
                "account_id",
                "pair",
                "qty",
                "entry",
                "exit",
                "opened_at",
                "closed_at",
                "fee_eur",
                "realised_eur",
                "realised_r",
                "exit_reason",
                "decision_id",
            ]
        )
        for t in rows:
            w.writerow(
                [
                    t.id,
                    t.account_id,
                    t.pair,
                    t.qty,
                    t.entry,
                    t.exit,
                    t.opened_at.isoformat(),
                    t.closed_at.isoformat(),
                    t.fee_eur,
                    t.realised_eur,
                    t.realised_r,
                    t.exit_reason,
                    t.decision_id,
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="trades.csv"'},
        )
    finally:
        session.close()


@router.get("/api/export/decisions.csv")
def export_decisions(request: Request) -> Response:
    """Every stored decision (all accounts), newest first."""
    state = get_state(request)
    session = _session(state)
    try:
        rows = session.scalars(
            select(DecisionRow).order_by(DecisionRow.at.desc(), DecisionRow.id.desc())
        ).all()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "id",
                "cycle",
                "at",
                "account_id",
                "pair",
                "decider",
                "model",
                "prompt_version",
                "action",
                "confidence",
                "reasoning",
                "invalidation",
                "invalidation_price",
                "status",
                "error",
                "risk_pct_used",
                "settings_version",
            ]
        )
        for d in rows:
            w.writerow(
                [
                    d.id,
                    d.cycle,
                    d.at.isoformat(),
                    d.account_id,
                    d.pair,
                    d.decider,
                    d.model or "",
                    d.prompt_version or "",
                    d.action,
                    d.confidence,
                    d.reasoning,
                    d.invalidation or "",
                    d.invalidation_price or "",
                    d.status,
                    d.error or "",
                    d.risk_pct_used,
                    d.settings_version,
                ]
            )
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="decisions.csv"'},
        )
    finally:
        session.close()


# -------------------------------------------------------------- danger zone


@router.delete("/api/data", dependencies=[Depends(require_token)])
async def delete_all_data(request: Request, body: DeleteDataRequest) -> dict[str, bool]:
    """Wipe every table except settings (API keys live outside the database),
    then open a fresh paper account. The agent is paused afterwards."""
    state = get_state(request)
    engine = state.agent_engine
    if engine is not None:
        engine.pause()
    session = _session(state)
    try:
        settings = repo.load_settings(session)
        repo.delete_all_data(session)
        account = repo.current_account(
            session,
            settings.paper.start_balance_eur,
            day_start_date=utcnow().astimezone(AMSTERDAM).date().isoformat(),
        )
        for market in settings.active_markets:
            row = session.get(MarketRow, market)
            if row is None:
                row = MarketRow(market=market, eligible=True)
            row.active = True
            row.activated_at = utcnow()
            repo.upsert_market_row(session, row)
        repo.add_activity(session, "system", "All data deleted; fresh paper account created")
        session.commit()
        if engine is not None:
            engine.reset_runtime(account, settings.paper)
            state.broker = engine.broker
        elif state.broker is not None:
            state.broker = PaperBroker(account, settings.paper)
    finally:
        session.close()
    await broadcast_snapshot(state)
    return {"ok": True}
