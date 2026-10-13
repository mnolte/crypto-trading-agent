"""Supported markets: the eligibility rules, the 3-day anti-flap buffer,
activation refusal and the warming-up state."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.agent.market.base import Candle, MarketInfo, OrderBook, Ticker24h
from app.agent.market.universe import (
    IneligibleMarketError,
    MarketMetrics,
    activate_market,
    apply_check,
    backfill_market,
    deactivate_market,
    entries_allowed,
    failed_rules,
    run_eligibility_check,
    warming_up,
)
from app.config import get_env
from app.db import MarketRow, make_engine, make_session_factory, run_migrations
from app.models import MarketSettings
from app.repo import candle_counts, get_market_row

NOW = datetime(2026, 10, 1, tzinfo=UTC)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


class FakeMarket:
    """A canned ``MarketClient`` for eligibility tests."""

    def __init__(
        self,
        markets: list[MarketInfo],
        tickers: dict[str, Ticker24h],
        daily: dict[str, list[Candle]],
        hourly: dict[str, list[Candle]] | None = None,
    ) -> None:
        self._markets = markets
        self._tickers = tickers
        self._daily = daily
        self._hourly = hourly or {}

    async def aclose(self) -> None:
        pass

    async def markets(self) -> list[MarketInfo]:
        return self._markets

    async def ticker_24h(self, market: str | None = None) -> list[Ticker24h]:
        if market:
            return [self._tickers[market]]
        return list(self._tickers.values())

    async def candles(
        self,
        market: str,
        interval: str,
        limit: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        bars = self._hourly.get(market, []) if interval != "1d" else self._daily.get(market, [])
        if interval == "4h":
            bars = bars[::4]
        return bars[-limit:]

    async def order_book(self, market: str, depth: int = 50) -> OrderBook:
        return OrderBook(market=market, bids=[], asks=[])

    async def ticker_price(self, market: str) -> Decimal:
        return Decimal("1")


def _candles(
    n: int, *, price: float = 100.0, volume: float = 100.0, step: timedelta = DAY
) -> list[Candle]:
    end = NOW
    return [
        Candle(
            open_time=end - (n - i) * step,
            open=f"{price}",
            high=f"{price * 1.01}",
            low=f"{price * 0.99}",
            close=f"{price}",
            volume=f"{volume}",
        )
        for i in range(n)
    ]


def _info(market: str, base: str, status: str = "trading", quote: str = "EUR") -> MarketInfo:
    return MarketInfo(market=market, base=base, quote=quote, status=status)


def _ticker(market: str, bid: float = 99.95, ask: float = 100.05) -> Ticker24h:
    return Ticker24h(
        market=market, last=Decimal("100"), bid=Decimal(str(bid)), ask=Decimal(str(ask))
    )


def _metrics(**kw: object) -> MarketMetrics:
    base = {
        "market": "X-EUR",
        "base": "X",
        "quote": "EUR",
        "status": "trading",
        "is_stablecoin": False,
        "volume_30d_eur": 2_000_000.0,
        "spread_pct": 0.10,
        "daily_candles": 320,
    }
    base.update(kw)
    return MarketMetrics(**base)  # type: ignore[arg-type]


@pytest.fixture
def session(env_paths: Path) -> Iterator[Session]:
    run_migrations(get_env().ta_db_path)
    engine = make_engine(get_env().ta_db_path)
    s = make_session_factory(engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# --------------------------------------------------------------- rule checks


def test_failed_rules_each_boundary() -> None:
    cfg = MarketSettings()
    assert failed_rules(_metrics(), cfg) == []
    assert failed_rules(_metrics(volume_30d_eur=999_999.0), cfg) == ["volume"]
    assert failed_rules(_metrics(volume_30d_eur=1_000_000.0), cfg) == []
    assert failed_rules(_metrics(spread_pct=0.16), cfg) == ["spread"]
    assert failed_rules(_metrics(spread_pct=0.15), cfg) == []
    assert failed_rules(_metrics(daily_candles=299), cfg) == ["history"]
    assert failed_rules(_metrics(is_stablecoin=True), cfg) == ["type"]
    assert failed_rules(_metrics(status="halted"), cfg) == ["status"]
    # First failed rule in spec order is what the market gets tagged with.
    assert failed_rules(_metrics(volume_30d_eur=1.0, status="halted", is_stablecoin=True), cfg) == [
        "volume",
        "type",
        "status",
    ]


# ------------------------------------------------------------- the buffer ---


def _row(eligible: bool = True) -> MarketRow:
    return MarketRow(market="X-EUR", eligible=eligible)


def test_one_day_below_stays_eligible() -> None:
    cfg = MarketSettings()
    row = _row(eligible=True)
    m = _metrics(volume_30d_eur=900_000.0)  # below, not 20% beyond
    assert apply_check(row, m, cfg, NOW, is_first_check=False) is None
    assert row.eligible and row.below_streak == 1
    assert apply_check(row, m, cfg, NOW + DAY, is_first_check=False) is None
    assert row.eligible and row.below_streak == 2
    assert apply_check(row, m, cfg, NOW + 2 * DAY, is_first_check=False) == "failed"
    assert not row.eligible and row.failed_rule == "volume"


def test_instant_fail_when_20pct_beyond() -> None:
    cfg = MarketSettings()
    row = _row(eligible=True)
    m = _metrics(volume_30d_eur=700_000.0)  # < 0.8M → beyond
    assert apply_check(row, m, cfg, NOW, is_first_check=False) == "failed"
    assert not row.eligible


def test_categorical_fail_is_instant() -> None:
    cfg = MarketSettings()
    row = _row(eligible=True)
    assert apply_check(row, _metrics(status="halted"), cfg, NOW, is_first_check=False) == "failed"


def test_streak_resets_on_pass() -> None:
    cfg = MarketSettings()
    row = _row(eligible=True)
    bad = _metrics(spread_pct=0.16)
    apply_check(row, bad, cfg, NOW, is_first_check=False)
    apply_check(row, bad, cfg, NOW + DAY, is_first_check=False)
    apply_check(row, _metrics(), cfg, NOW + 2 * DAY, is_first_check=False)
    assert row.below_streak == 0
    apply_check(row, bad, cfg, NOW + 3 * DAY, is_first_check=False)
    assert row.eligible  # back to day one of the streak


def test_failed_market_needs_three_days_back() -> None:
    cfg = MarketSettings()
    row = _row(eligible=False)
    row.failed_rule = "volume"
    good = _metrics()
    assert apply_check(row, good, cfg, NOW, is_first_check=False) is None
    assert apply_check(row, good, cfg, NOW + DAY, is_first_check=False) is None
    assert apply_check(row, good, cfg, NOW + 2 * DAY, is_first_check=False) == "recovered"
    assert row.eligible and row.failed_rule is None


def test_failed_market_regresses_on_fail() -> None:
    cfg = MarketSettings()
    row = _row(eligible=False)
    apply_check(row, _metrics(), cfg, NOW, is_first_check=False)
    apply_check(row, _metrics(), cfg, NOW + DAY, is_first_check=False)
    apply_check(row, _metrics(spread_pct=0.5), cfg, NOW + 2 * DAY, is_first_check=False)
    assert row.within_streak == 0 and not row.eligible


def test_first_check_decides_immediately() -> None:
    cfg = MarketSettings()
    good_row = _row(eligible=False)
    apply_check(good_row, _metrics(), cfg, NOW, is_first_check=True)
    assert good_row.eligible
    bad_row = _row(eligible=True)
    apply_check(bad_row, _metrics(volume_30d_eur=10.0), cfg, NOW, is_first_check=True)
    assert not bad_row.eligible and bad_row.failed_rule == "volume"


# ------------------------------------------------------------- full check --


async def test_run_eligibility_check_over_eur_markets(session: Session) -> None:
    cfg = MarketSettings()
    infos = [
        _info("GOOD-EUR", "GOOD"),
        _info("THIN-EUR", "THIN"),
        _info("USDC-EUR", "USDC"),
        _info("BTC-USDT", "BTC", quote="USDT"),  # not a EUR market: skipped entirely
    ]
    tickers = {i.market: _ticker(i.market) for i in infos}
    daily = {
        "GOOD-EUR": _candles(320, price=100.0, volume=15_000.0),  # 1.5M/day
        "THIN-EUR": _candles(320, price=100.0, volume=500.0),  # 50k/day
        "USDC-EUR": _candles(320, price=0.92, volume=5_000_000.0),
    }
    client = FakeMarket(infos, tickers, daily)
    rows, transitions = await run_eligibility_check(client, session, cfg, NOW)
    by_market = {r.market: r for r in rows}
    assert "BTC-USDT" not in by_market
    assert by_market["GOOD-EUR"].eligible
    assert not by_market["THIN-EUR"].eligible
    assert by_market["THIN-EUR"].failed_rule == "volume"
    assert not by_market["USDC-EUR"].eligible
    assert by_market["USDC-EUR"].failed_rule == "type"
    # First check: states recorded, no flapping transitions to alert on.
    assert transitions == {}
    # Rows persisted.
    assert get_market_row(session, "GOOD-EUR") is not None


async def test_history_check_counts_closed_bars_only(session: Session) -> None:
    """The exchange's `limit` includes the still-forming daily bar, which we
    drop — a market with exactly `min_daily_candles` closed bars must pass."""
    cfg = MarketSettings()
    forming = Candle(
        open_time=NOW,
        open="100",
        high="101",
        low="99",
        close="100",
        volume="15000",
    )
    daily = [*_candles(cfg.min_daily_candles, price=100.0, volume=15_000.0), forming]
    client = FakeMarket(
        [_info("EDGE-EUR", "EDGE")],
        {"EDGE-EUR": _ticker("EDGE-EUR")},
        {"EDGE-EUR": daily},
    )
    rows, _ = await run_eligibility_check(client, session, cfg, NOW)
    assert rows[0].eligible
    assert rows[0].daily_candles == cfg.min_daily_candles


# ------------------------------------------------------- activate/backfill -


async def test_backfill_then_not_warming(session: Session) -> None:
    cfg = MarketSettings()
    hourly = _candles(1300, step=HOUR)  # → 325 4h bars, past the 300-bar target
    daily = _candles(320, step=DAY)
    client = FakeMarket(
        [_info("GOOD-EUR", "GOOD")],
        {"GOOD-EUR": _ticker("GOOD-EUR")},
        {"GOOD-EUR": daily},
        {"GOOD-EUR": hourly},
    )
    session.add(MarketRow(market="GOOD-EUR", eligible=True, active=False))
    session.commit()
    row = await activate_market(client, session, "GOOD-EUR", cfg, NOW)
    assert row.active
    counts = candle_counts(session, "GOOD-EUR")
    assert counts["1h"] >= cfg.history_1h
    assert counts["1d"] >= cfg.history_1d
    assert not warming_up(session, "GOOD-EUR", cfg)


async def test_warming_until_history_loaded(session: Session) -> None:
    cfg = MarketSettings()
    session.add(MarketRow(market="GOOD-EUR", eligible=True, active=True))
    session.commit()
    assert warming_up(session, "GOOD-EUR", cfg)
    client = FakeMarket([], {}, {}, {"GOOD-EUR": _candles(900, step=HOUR)})
    await backfill_market(client, session, "GOOD-EUR", cfg, NOW)
    # 4h and 1d still thin (the fake serves the same list sliced), so the
    # market is only done once every timeframe reached its target.
    counts = candle_counts(session, "GOOD-EUR")
    done = (
        counts.get("1h", 0) >= cfg.history_1h
        and counts.get("4h", 0) >= cfg.history_4h
        and counts.get("1d", 0) >= cfg.history_1d
    )
    assert warming_up(session, "GOOD-EUR", cfg) == (not done)


async def test_activation_refused_when_ineligible(session: Session) -> None:
    cfg = MarketSettings()
    session.add(MarketRow(market="THIN-EUR", eligible=False, failed_rule="volume"))
    session.commit()
    client = FakeMarket([], {}, {})
    with pytest.raises(IneligibleMarketError) as exc:
        await activate_market(client, session, "THIN-EUR", cfg, NOW)
    assert exc.value.failed_rule == "volume"


async def test_activation_unchecked_market_gets_checked(session: Session) -> None:
    cfg = MarketSettings()
    client = FakeMarket(
        [_info("NEW-EUR", "NEW")],
        {"NEW-EUR": _ticker("NEW-EUR")},
        {"NEW-EUR": _candles(320, price=100.0, volume=15_000.0)},
        {"NEW-EUR": _candles(900, step=HOUR)},
    )
    row = await activate_market(client, session, "NEW-EUR", cfg, NOW)
    assert row.active and row.eligible


async def test_deactivate(session: Session) -> None:
    session.add(MarketRow(market="GOOD-EUR", eligible=True, active=True))
    session.commit()
    row = deactivate_market(session, "GOOD-EUR")
    assert row is not None and not row.active


# -------------------------------------------------------- entries allowed --


def test_entries_allowed(session: Session) -> None:
    """New entries need an active, eligible market. A missing row means
    the check never ran and stays tradeable; a failing or inactive row
    takes no new entries."""
    assert entries_allowed(session, "X-EUR")
    session.add(MarketRow(market="OK-EUR", active=True, eligible=True))
    session.add(MarketRow(market="FAIL-EUR", active=True, eligible=False, failed_rule="volume"))
    session.add(MarketRow(market="OFF-EUR", active=False, eligible=True))
    session.add(MarketRow(market="DEAD-EUR", active=False, eligible=False))
    session.commit()
    assert entries_allowed(session, "OK-EUR")
    assert not entries_allowed(session, "FAIL-EUR")
    assert not entries_allowed(session, "OFF-EUR")
    assert not entries_allowed(session, "DEAD-EUR")
