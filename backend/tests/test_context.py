from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.context.collector import (
    ContextCollector,
    ContextPoint,
    ProviderError,
    calculate_skew,
    choose_expiry,
    parse_dvol,
    parse_okx_current,
    parse_okx_funding_history,
    parse_okx_open_interest,
    parse_okx_ratio,
)
from app.context.scheduler import context_loop, next_run
from app.db import MarketContextRow, make_engine, make_session_factory, run_migrations
from app.sources import SourceRegistry

NOW = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def test_endpoint_parsers() -> None:
    settled = parse_okx_funding_history(
        {"code": "0", "data": [{"fundingTime": "1000", "realizedRate": "0.01"}]},
        "BTC",
    )
    assert settled[0].metric == "funding_rate_settled" and settled[0].value == 0.01
    current = parse_okx_current(
        {"code": "0", "data": [{"ts": "1791971130123", "fundingRate": "0.02"}]},
        "BTC",
    )
    assert current[0].ts.minute == 0 and current[0].ts.second == 0
    oi = parse_okx_open_interest(
        {"code": "0", "data": [["1000", "1", "2", "3"]]}, "ETH", history=True
    )
    assert oi[0].value == 3
    ratio = parse_okx_ratio({"code": "0", "data": [["1000", "1.5"]]}, "BTC")
    assert ratio[0].value == 1.5
    dvol = parse_dvol({"result": {"data": [[1000, 1, 2, 0, 1.25]]}}, "ETH")
    assert dvol[0].value == 1.25


@pytest.mark.parametrize(
    "parser,payload,args",
    [
        (parse_okx_funding_history, {"code": "0", "data": []}, ("BTC",)),
        (parse_okx_current, {"code": "0", "data": []}, ("BTC",)),
        (parse_okx_ratio, {"code": "0", "data": []}, ("BTC",)),
        (parse_dvol, {"result": {"data": []}}, ("BTC",)),
    ],
)
def test_empty_responses(parser: object, payload: object, args: tuple[object, ...]) -> None:
    assert parser(payload, *args) == []  # type: ignore[operator]


def test_error_responses() -> None:
    with pytest.raises(ProviderError):
        parse_okx_ratio({"code": "500", "msg": "bad"}, "BTC")
    with pytest.raises(ProviderError):
        parse_dvol({"error": {"message": "bad"}}, "BTC")


def test_expiry_choice_skips_under_seven_days() -> None:
    instruments = [
        {"expiration_timestamp": int((NOW + timedelta(days=3)).timestamp() * 1000)},
        {"expiration_timestamp": int((NOW + timedelta(days=27)).timestamp() * 1000)},
        {"expiration_timestamp": int((NOW + timedelta(days=35)).timestamp() * 1000)},
    ]
    assert choose_expiry(instruments, NOW) == instruments[1]["expiration_timestamp"]


def test_skew_interpolation_and_atm() -> None:
    expiry = int((NOW + timedelta(days=30)).timestamp() * 1000)
    tickers = [
        {
            "instrument_name": "BTC-X-90000-P",
            "mark_iv": 60,
            "greeks": {"delta": -0.35},
            "underlying_price": 100000,
        },
        {
            "instrument_name": "BTC-X-100000-P",
            "mark_iv": 56,
            "greeks": {"delta": -0.15},
            "underlying_price": 100000,
        },
        {
            "instrument_name": "BTC-X-90000-C",
            "mark_iv": 50,
            "greeks": {"delta": 0.15},
            "underlying_price": 100000,
        },
        {
            "instrument_name": "BTC-X-100000-C",
            "mark_iv": 54,
            "greeks": {"delta": 0.35},
            "underlying_price": 100000,
        },
    ]
    point = calculate_skew(tickers, "BTC", expiry, NOW)
    assert point is not None
    assert point.value == pytest.approx(6)
    assert point.meta["put_iv_25d"] == pytest.approx(58)
    assert point.meta["call_iv_25d"] == pytest.approx(52)
    assert point.meta["atm_iv"] == pytest.approx(55)


def test_missing_deltas_returns_no_row(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    assert calculate_skew([], "BTC", int(NOW.timestamp() * 1000), NOW) is None


def _sessions(path: Path) -> tuple[Engine, sessionmaker[Session]]:
    run_migrations(path)
    engine = make_engine(path)
    return engine, make_session_factory(engine)


def test_upsert_same_row_twice_is_one(tmp_path: Path) -> None:
    engine, sessions = _sessions(tmp_path / "context.db")
    session = sessions()
    point = ContextPoint("okx", "open_interest_usd", "BTC", NOW, 123.0)
    try:
        repo.upsert_market_context(session, [point.row(NOW)])
        repo.upsert_market_context(session, [point.row(NOW + timedelta(minutes=1))])
        assert session.scalar(select(func.count()).select_from(MarketContextRow)) == 1
    finally:
        session.close()
        engine.dispose()


def test_percentile_90d_boundaries(tmp_path: Path) -> None:
    engine, sessions = _sessions(tmp_path / "percentile.db")
    session = sessions()
    try:
        short = [
            ContextPoint("okx", "m", "BTC", NOW - timedelta(days=i), float(i)) for i in range(30)
        ]
        repo.upsert_market_context(session, [point.row(NOW) for point in short])
        repo.upsert_market_context(session, [ContextPoint("okx", "m", "BTC", NOW, 15).row(NOW)])
        assert repo.percentile_90d(session, "m", "BTC", NOW) is None
        old = ContextPoint("okx", "m", "BTC", NOW - timedelta(days=30), 30)
        repo.upsert_market_context(session, [old.row(NOW)])
        assert repo.percentile_90d(session, "m", "BTC", NOW) == pytest.approx(50)
    finally:
        session.close()
        engine.dispose()


@respx.mock
async def test_disabled_source_makes_no_requests(tmp_path: Path) -> None:
    engine, sessions = _sessions(tmp_path / "disabled.db")
    collector = ContextCollector(
        sessions,
        SourceRegistry(["okx", "deribit"]),
        client=httpx.AsyncClient(),
        clock=lambda: NOW,
    )
    try:
        assert await collector.collect() == {}
        assert not respx.calls
    finally:
        await collector.client.aclose()
        engine.dispose()


@respx.mock
async def test_collect_twice_is_idempotent(tmp_path: Path) -> None:
    engine, sessions = _sessions(tmp_path / "idem.db")
    respx.get("https://www.okx.com/api/v5/public/funding-rate-history").mock(
        return_value=httpx.Response(
            200, json={"code": "0", "data": [{"fundingTime": "1000", "realizedRate": "0.01"}]}
        )
    )
    respx.get("https://www.okx.com/api/v5/public/funding-rate").mock(
        return_value=httpx.Response(
            200, json={"code": "0", "data": [{"ts": "1791971130123", "fundingRate": "0.02"}]}
        )
    )
    respx.get("https://www.okx.com/api/v5/public/open-interest").mock(
        return_value=httpx.Response(
            200, json={"code": "0", "data": [{"ts": "1791971130123", "oiUsd": "9"}]}
        )
    )
    respx.get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [["1000", "1", "2", "3"]]})
    )
    respx.get(
        "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"
    ).mock(return_value=httpx.Response(200, json={"code": "0", "data": [["1000", "1.5"]]}))
    respx.get("https://www.deribit.com/api/v2/public/get_volatility_index_data").mock(
        return_value=httpx.Response(
            200, json={"result": {"data": [[1000, 1, 2, 0, 1.25]], "continuation": None}}
        )
    )
    respx.get("https://www.deribit.com/api/v2/public/get_instruments").mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    collector = ContextCollector(
        sessions, SourceRegistry(), client=httpx.AsyncClient(), clock=lambda: NOW
    )
    session = sessions()
    try:
        await collector.collect()
        await collector.collect()
        count = session.scalar(select(func.count()).select_from(MarketContextRow))
        assert count == 5 * 2 + 1 * 2  # five OKX metrics + DVOL, per symbol
    finally:
        session.close()
        await collector.client.aclose()
        engine.dispose()


async def test_collector_timeout_does_not_block_other_task(tmp_path: Path) -> None:
    engine, sessions = _sessions(tmp_path / "timeout.db")
    registry = SourceRegistry(["deribit"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late")

    transport = httpx.MockTransport(handler)
    collector = ContextCollector(
        sessions,
        registry,
        client=httpx.AsyncClient(transport=transport),
        clock=lambda: NOW,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    trading_cycle_ran = False

    async def trading_cycle() -> None:
        nonlocal trading_cycle_ran
        await asyncio.sleep(0)
        trading_cycle_ran = True

    try:
        await asyncio.gather(collector.collect(), trading_cycle())
        assert trading_cycle_ran
        assert registry.status("okx").runtime.failures == 1
    finally:
        await collector.client.aclose()
        engine.dispose()


def test_schedule_minute_five_and_startup_catchup() -> None:
    assert next_run(NOW) == datetime(2026, 10, 14, 13, 5, tzinfo=UTC)

    class FakeCollector:
        def __init__(self) -> None:
            self.runs = 0

        async def collect(self) -> dict[str, int]:
            self.runs += 1
            return {}

        async def aclose(self) -> None:
            return None

    fake = FakeCollector()

    async def stop_after_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(context_loop(fake, clock=lambda: NOW, sleep=stop_after_sleep))  # type: ignore[arg-type]
    assert fake.runs == 1


def test_context_api_filters(client: TestClient, env_paths: Path) -> None:
    from app.config import get_env

    engine = make_engine(get_env().ta_db_path)
    session = make_session_factory(engine)()
    try:
        repo.upsert_market_context(
            session,
            [ContextPoint("okx", "open_interest_usd", "BTC", NOW, 10).row(NOW)],
        )
    finally:
        session.close()
        engine.dispose()
    body = client.get(
        "/api/context?metric=open_interest_usd&symbol=BTC&from=2026-10-14T00:00:00Z"
    ).json()
    assert len(body) == 1
    assert body[0]["meta"] == json.loads("{}")
