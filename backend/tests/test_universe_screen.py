"""Item 026: the Universe screen backend — check funnel, bad-data
retention, BTC-relative metrics, statuses, sync runs and the API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.agent.market.base import Candle, MarketInfo, OrderBook, Ticker24h
from app.agent.market.sync import SyncRuns, backfill_retry_due
from app.agent.market.universe import (
    activate_market,
    run_eligibility_check,
)
from app.api.deps import AppState
from app.api.universe import MarketStatusOut, market_status
from app.db import (
    MarketCheckRow,
    MarketRow,
    SyncRunRow,
    make_engine,
    make_session_factory,
)
from app.models import MarketSettings, Tf
from app.repo import candle_counts

NOW = datetime(2026, 10, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def _info(market: str, base: str, status: str = "trading", quote: str = "EUR") -> MarketInfo:
    return MarketInfo(
        market=market,
        status=status,
        base=base,
        quote=quote,
        min_order_base=Decimal("0.001"),
        min_order_quote=Decimal("1"),
        tick_size=Decimal("0.01"),
        quantity_decimals=8,
    )


def _ticker(market: str, bid: str = "99.95", ask: str = "100.05") -> Ticker24h:
    return Ticker24h(
        market=market,
        last=Decimal("100"),
        open=Decimal("99"),
        high=Decimal("101"),
        low=Decimal("98"),
        volume=Decimal("1000"),
        volume_quote=Decimal("100000"),
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


def _daily(market: str, closes: list[str], volume: str = "2000000") -> list[Candle]:
    """Daily candles ending at NOW; ``closes`` oldest → newest."""
    out = []
    for i, close in enumerate(closes):
        out.append(
            Candle(
                market=market,
                interval="1d",
                open_time=NOW - DAY * (len(closes) - i),
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=Decimal(volume),
            )
        )
    return out


class FakeMarket:
    def __init__(
        self,
        markets: list[MarketInfo],
        tickers: dict[str, Ticker24h],
        daily: dict[str, list[Candle]],
        names: dict[str, str] | None = None,
        prices: dict[str, Decimal] | None = None,
    ) -> None:
        self._markets = markets
        self._tickers = tickers
        self._daily = daily
        self._names = names or {}
        self._prices = prices or {}
        self.rate_limit_reset_at = None

    async def aclose(self) -> None:
        pass

    async def markets(self) -> list[MarketInfo]:
        return self._markets

    async def assets(self) -> dict[str, str]:
        return self._names

    async def ticker_prices(self) -> dict[str, Decimal]:
        return self._prices

    async def ticker_24h(self, market: str | None = None) -> list[Ticker24h]:
        if market:
            return [self._tickers[market]]
        return list(self._tickers.values())

    async def candles(
        self,
        market: str,
        interval: Tf,
        limit: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        bars = self._daily.get(market, [])
        return bars[-limit:]

    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        return OrderBook(market=market, bids=[], asks=[])

    async def ticker_price(self, market: str, **_kw: Any) -> Decimal:
        return Decimal("1")


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    engine = make_engine(tmp_path / "t.db")
    from app.db import Base

    Base.metadata.create_all(engine)
    s = make_session_factory(engine)()
    yield s
    s.close()
    engine.dispose()


def _cfg(**kw: Any) -> MarketSettings:
    base = {
        "min_avg_daily_volume_eur": Decimal("1000000"),
        "max_avg_spread_pct": Decimal("0.15"),
        "min_daily_candles": 300,
        "eligibility_fail_days": 3,
        "eligibility_beyond_pct": Decimal("20"),
        "eligibility_recover_days": 3,
        "stablecoin_bases": ["USDC", "USDT"],
        "check_data_error_pct": Decimal("50"),
        "history_1h": 800,
        "history_4h": 300,
        "history_1d": 300,
    }
    base.update(kw)
    return MarketSettings(**base)


def _passing_market(
    market: str = "AAA-EUR", base: str = "AAA"
) -> tuple[MarketInfo, Ticker24h, list[Candle]]:
    closes = [str(100 + i * 0.1) for i in range(320)]
    return _info(market, base), _ticker(market), _daily(market, closes)


# ------------------------------------------------------------- the funnel --


@pytest.mark.asyncio
async def test_check_funnel_and_kept_held_back(session: Session) -> None:
    """236-style funnel: rules drop markets in order; the streak rule's
    kept/held-back markets land on the market_checks row."""
    infos = [
        _info("BTC-EUR", "BTC"),
        _info("AAA-EUR", "AAA"),
        _info("SHIB-EUR", "SHIB"),
        _info("USDC-EUR", "USDC"),  # stablecoin → dropped at type
        _info("HALT-EUR", "HALT", status="halted"),
        _info("THIN-EUR", "THIN"),  # no ticker → volume fails
        _info("USDT-EUR", "USDT", quote="EUR"),
        _info("XXX-USDT", "XXX", quote="USDT"),  # not EUR → never listed
    ]
    ticker_markets = (
        "BTC-EUR",
        "AAA-EUR",
        "SHIB-EUR",
        "HALT-EUR",
        "THIN-EUR",
        "USDC-EUR",
        "USDT-EUR",
    )
    tickers = {m: _ticker(m) for m in ticker_markets}
    daily = {
        m: _daily(m, [str(100 + i) for i in range(320)])
        for m in ("BTC-EUR", "AAA-EUR", "SHIB-EUR", "HALT-EUR", "USDC-EUR", "USDT-EUR")
    }
    # SHIB passes volume+spread but has only 100 daily candles → history.
    daily["SHIB-EUR"] = _daily("SHIB-EUR", [str(50 + i) for i in range(100)])
    # THIN has candles but no volume → fails volume anyway.
    client = FakeMarket(infos, tickers, daily)
    cfg = _cfg()

    rows, _ = await run_eligibility_check(client, session, cfg, NOW)

    check = session.scalar(select(MarketCheckRow).order_by(MarketCheckRow.id.desc()))
    assert check is not None and check.status == "ok"
    funnel = json.loads(check.funnel)
    by_rule = {s["rule"]: s for s in funnel}
    assert by_rule["listed"]["left"] == 7  # EUR markets only
    assert "USDC-EUR" in by_rule["type"]["dropped"]
    assert "USDT-EUR" in by_rule["type"]["dropped"]
    assert "HALT-EUR" in by_rule["status"]["dropped"]
    assert "THIN-EUR" in by_rule["volume"]["dropped"] or "THIN-EUR" in by_rule["spread"]["dropped"]
    assert "SHIB-EUR" in by_rule["history"]["dropped"]
    # BTC, AAA pass everything.
    assert by_rule["all"]["left"] == 2
    btc = next(r for r in rows if r.market == "BTC-EUR")
    aaa = next(r for r in rows if r.market == "AAA-EUR")
    assert btc.eligible and aaa.eligible
    assert btc.eligible_since == NOW
    shib = next(r for r in rows if r.market == "SHIB-EUR")
    assert not shib.eligible and shib.failed_rule == "history"


@pytest.mark.asyncio
async def test_data_error_keeps_previous_result(session: Session) -> None:
    """volume/spread missing for ≥ check_data_error_pct of previously
    eligible markets → failed run, nothing moves."""
    # Seed two eligible rows, then a check where their tickers are gone.
    session.add(MarketRow(market="AAA-EUR", eligible=True, eligible_since=NOW - DAY))
    session.add(MarketRow(market="BBB-EUR", eligible=True, eligible_since=NOW - DAY))
    session.commit()

    infos = [_info("AAA-EUR", "AAA"), _info("BBB-EUR", "BBB")]
    # No tickers at all → volume/spread missing for 100% of previously eligible.
    daily = {m: _daily(m, [str(100 + i) for i in range(320)]) for m in ("AAA-EUR", "BBB-EUR")}
    client = FakeMarket(infos, {}, daily)
    cfg = _cfg()

    rows, transitions = await run_eligibility_check(client, session, cfg, NOW)
    assert rows == [] and transitions == {}

    check = session.scalar(select(MarketCheckRow).order_by(MarketCheckRow.id.desc()))
    assert check is not None and check.status == "failed" and check.error
    for market in ("AAA-EUR", "BBB-EUR"):
        row = session.get(MarketRow, market)
        assert row is not None
        assert row.eligible and row.eligible_since == (NOW - DAY).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_below_threshold_data_error_is_not_an_error(session: Session) -> None:
    """49% missing with a 50% threshold is not a data error."""
    for i in range(10):
        session.add(MarketRow(market=f"M{i}-EUR", eligible=True))
    session.commit()
    infos = [_info(f"M{i}-EUR", f"M{i}") for i in range(10)]
    # 4 of 10 previously eligible miss volume/spread → 40% < 50%.
    tickers = {f"M{i}-EUR": _ticker(f"M{i}-EUR") for i in range(6)}
    daily = {
        f"M{i}-EUR": _daily(f"M{i}-EUR", [str(100 + j) for j in range(320)]) for i in range(10)
    }
    client = FakeMarket(infos, tickers, daily)
    rows, _ = await run_eligibility_check(client, session, _cfg(), NOW)
    check = session.scalar(select(MarketCheckRow).order_by(MarketCheckRow.id.desc()))
    assert check is not None and check.status == "ok"
    assert len(rows) == 10


# -------------------------------------------------------- BTC-relative --


@pytest.mark.asyncio
async def test_vs_btc_and_correlation(session: Session) -> None:
    """vs BTC = market 30d return - BTC-EUR's; corr = Pearson of the 30
    daily returns. BTC-EUR itself gets nulls."""
    btc_closes = [str(100 * (1.001**i) * (1 + 0.01 * ((i % 7) - 3))) for i in range(320)]
    # AAA tracks BTC's daily pattern with a slightly larger amplitude.
    aaa_closes = [str(50 * (1.0015**i) * (1 + 0.012 * ((i % 7) - 3))) for i in range(320)]
    infos = [_info("BTC-EUR", "BTC"), _info("AAA-EUR", "AAA")]
    tickers = {"BTC-EUR": _ticker("BTC-EUR"), "AAA-EUR": _ticker("AAA-EUR")}
    daily = {
        "BTC-EUR": _daily("BTC-EUR", btc_closes),
        "AAA-EUR": _daily("AAA-EUR", aaa_closes),
    }
    client = FakeMarket(infos, tickers, daily)
    await run_eligibility_check(client, session, _cfg(), NOW)

    btc = session.get(MarketRow, "BTC-EUR")
    aaa = session.get(MarketRow, "AAA-EUR")
    assert btc is not None and aaa is not None
    assert btc.vs_btc_30d_pct is None and btc.corr_btc_30d is None
    assert aaa.vs_btc_30d_pct is not None and aaa.corr_btc_30d is not None
    # AAA drifts up faster than BTC over the same 30 days.
    assert aaa.vs_btc_30d_pct is not None and aaa.vs_btc_30d_pct > Decimal("0")
    # Same daily pattern, larger amplitude → strongly correlated.
    assert aaa.corr_btc_30d is not None and aaa.corr_btc_30d > Decimal("0.9")


@pytest.mark.asyncio
async def test_vs_btc_null_without_31_closes(session: Session) -> None:
    infos = [_info("BTC-EUR", "BTC"), _info("AAA-EUR", "AAA")]
    tickers = {"BTC-EUR": _ticker("BTC-EUR"), "AAA-EUR": _ticker("AAA-EUR")}
    daily = {
        "BTC-EUR": _daily("BTC-EUR", [str(100 + i) for i in range(320)]),
        "AAA-EUR": _daily("AAA-EUR", [str(50 + i) for i in range(20)]),  # young market
    }
    client = FakeMarket(infos, tickers, daily)
    await run_eligibility_check(client, session, _cfg(), NOW)
    aaa = session.get(MarketRow, "AAA-EUR")
    assert aaa is not None
    assert aaa.vs_btc_30d_pct is None and aaa.corr_btc_30d is None


# ------------------------------------------------------------- statuses --


def _state(session: Session) -> AppState:
    """A minimal AppState for market_status."""

    class _S:
        account_id: int = 1
        settings: Any = type("X", (), {"market": _cfg()})()
        broker: Any = type("B", (), {"positions": {}})()
        agent_engine: Any = None
        session_factory: Any = None

    state = _S()
    state.session_factory = lambda: session
    return cast(AppState, state)


def test_market_status_kinds(session: Session) -> None:
    state = _state(session)
    cfg = _cfg()

    def status(row: MarketRow) -> MarketStatusOut:
        return market_status(session, state, row, cfg)

    # eligible, not active
    row = MarketRow(market="AAA-EUR", eligible=True, eligible_since=NOW)
    assert status(row).kind == "eligible"
    # recovering: fails empty, within_streak 1 of 3
    row = MarketRow(market="BBB-EUR", eligible=False, within_streak=1)
    assert status(row).kind == "recovering"
    # excluded
    row = MarketRow(
        market="CCC-EUR",
        eligible=False,
        failed_rule="volume",
        volume_30d_eur=Decimal("310000"),
    )
    out = status(row)
    assert out.kind == "excluded" and "volume" in out.line
    # active, plain
    row = MarketRow(market="DDD-EUR", eligible=True, active=True)
    assert status(row).kind == "active"
    # active, failing (lost eligibility)
    row = MarketRow(
        market="EEE-EUR",
        eligible=False,
        active=True,
        failed_rule="spread",
        spread_pct=Decimal("0.23"),
        failing_since=NOW - DAY,
    )
    out = status(row)
    assert out.kind == "failing" and "no new entries" in out.line
    # active, 2 of 3 fails
    row = MarketRow(
        market="FFF-EUR",
        eligible=True,
        active=True,
        below_streak=2,
        failed_rule="volume",
        volume_30d_eur=Decimal("900000"),
    )
    out = status(row)
    assert out.kind == "fails" and "2 of 3" in out.line
    # deactivated with a position → held_not_active
    from app.db import PositionRow

    session.add(
        PositionRow(
            pair="GGG-EUR",
            account_id=1,
            qty="1.5",
            entry="100",
            stop="90",
            target="120",
            opened_at=NOW,
            decision_id="d",
            last="100",
            r_unit="10",
        )
    )
    session.commit()
    row = MarketRow(market="GGG-EUR", eligible=False, active=False)
    out = status(row)
    assert out.kind == "held_not_active" and out.held


# ------------------------------------------------------------ sync runs --


def test_sync_run_lifecycle_and_retry_due(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "s.db")
    from app.db import Base

    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    syncs = SyncRuns(sf)
    run = syncs.begin("daily_check")
    run.progress(3, 10)
    run.fail("AAA-EUR", "boom", next_retry=NOW)
    run.finish()
    latest = syncs.latest("daily_check")
    assert latest is not None and latest.status == "partial"
    payload = SyncRuns.row_payload(latest)
    assert payload["failures"][0]["what"] == "AAA-EUR"

    # Backfill run with a failed timeframe → retry_due picks it up.
    br = syncs.begin("backfill:SOL-EUR", total=1400)
    br.fail("SOL-EUR", "429", next_retry=NOW - timedelta(minutes=1), tf="4h")
    br.progress(800, 1400)
    br.finish()
    with sf() as s:
        due = backfill_retry_due(s, NOW)
    assert due == {"SOL-EUR": {"4h"}}
    engine.dispose()


def test_sync_run_prunes_old_rows(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "p.db")
    from app.db import Base

    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    syncs = SyncRuns(sf)
    old = SyncRunRow(
        job="candles",
        started_at=(NOW - timedelta(days=40)).replace(tzinfo=None),
        status="ok",
        done=1,
        total=1,
        failures=None,
        updated_at=(NOW - timedelta(days=40)).replace(tzinfo=None),
    )
    with sf() as s:
        s.add(old)
        s.commit()
    syncs.begin("candles")
    with sf() as s:
        remaining = s.scalars(select(SyncRunRow)).all()
    cutoff = (NOW - timedelta(days=30)).replace(tzinfo=None)
    assert all(r.job != "candles" or r.started_at > cutoff for r in remaining)
    engine.dispose()


# -------------------------------------------------------- activation --


@pytest.mark.asyncio
async def test_activate_returns_before_backfill(session: Session) -> None:
    """on_active fires instead of awaiting the backfill."""
    info, ticker, daily = _passing_market()
    cfg = _cfg()
    fired: list[str] = []

    class SlowClient(FakeMarket):
        async def candles(
            self,
            market: str,
            interval: Tf,
            limit: int,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
        ) -> list[Candle]:
            import asyncio

            await asyncio.sleep(0.2)
            return await super().candles(market, interval, limit, start=start, end=end)

    slow = SlowClient([info], {info.market: ticker}, {info.market: daily})
    row = await activate_market(slow, session, info.market, cfg, NOW, on_active=fired.append)
    assert row.active and fired == [info.market]
    # The backfill was NOT awaited: 1h/4h/1d candles for the market are
    # absent (the daily screening candles are '1d' though — check 1h).
    assert candle_counts(session, info.market).get("1h", 0) == 0


# ------------------------------------------------------------ decimals --


def test_decimal_columns_roundtrip(session: Session) -> None:
    row = MarketRow(
        market="DEC-EUR",
        volume_30d_eur=Decimal("1234567.89"),
        spread_pct=Decimal("0.12345"),
        last_price=Decimal("65432.10"),
    )
    session.add(row)
    session.commit()
    got = session.get(MarketRow, "DEC-EUR")
    assert got is not None
    assert got.volume_30d_eur == Decimal("1234567.89")
    assert got.spread_pct == Decimal("0.12345")
    assert got.last_price == Decimal("65432.10")


# ------------------------------------------------------------------ api --


def test_universe_api_endpoints(sim_client: TestClient) -> None:
    token = sim_client.get("/api/session").json()["token"]
    headers = {"X-Local-Token": token}

    resp = sim_client.get("/api/universe")
    assert resp.status_code == 200
    body = resp.json()
    assert "check" in body and "tiles" in body and "calls" in body
    assert body["calls"]["per_market_per_day"] == 96
    assert isinstance(body["markets"], list)
    for m in body["markets"]:
        assert m["status"]["kind"] in {
            "warming_up",
            "failing",
            "fails",
            "paused",
            "held",
            "active",
            "eligible",
            "recovering",
            "excluded",
            "held_not_active",
        }

    resp = sim_client.get("/api/universe/check")
    assert resp.status_code == 200
    assert "latest" in resp.json() and "in_use" in resp.json()

    resp = sim_client.get("/api/universe/sync")
    assert resp.status_code == 200
    sync = resp.json()
    assert "jobs" in sync and "watcher" in sync

    # Unknown job → 404.
    resp = sim_client.post("/api/universe/sync/nope/retry", headers=headers)
    assert resp.status_code == 404
