from __future__ import annotations

import asyncio
import itertools
import json
import logging
import shutil
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.context.collector import (
    WEBSOCKET_MAX_QUEUE,
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
from app.db import MarketContextRow, make_engine, make_session_factory
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


def _sessions(path: Path, template: Path) -> tuple[Engine, sessionmaker[Session]]:
    shutil.copyfile(template, path)
    engine = make_engine(path)
    return engine, make_session_factory(engine)


def test_upsert_same_row_twice_is_one(tmp_path: Path, migrated_db: Path) -> None:
    engine, sessions = _sessions(tmp_path / "context.db", migrated_db)
    session = sessions()
    point = ContextPoint("okx", "open_interest_usd", "BTC", NOW, 123.0)
    try:
        repo.upsert_market_context(session, [point.row(NOW)])
        repo.upsert_market_context(session, [point.row(NOW + timedelta(minutes=1))])
        assert session.scalar(select(func.count()).select_from(MarketContextRow)) == 1
    finally:
        session.close()
        engine.dispose()


def test_percentile_90d_boundaries(tmp_path: Path, migrated_db: Path) -> None:
    engine, sessions = _sessions(tmp_path / "percentile.db", migrated_db)
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
async def test_disabled_source_makes_no_requests(tmp_path: Path, migrated_db: Path) -> None:
    engine, sessions = _sessions(tmp_path / "disabled.db", migrated_db)
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
async def test_collect_twice_is_idempotent(tmp_path: Path, migrated_db: Path) -> None:
    engine, sessions = _sessions(tmp_path / "idem.db", migrated_db)
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


async def test_collector_timeout_does_not_block_other_task(
    tmp_path: Path, migrated_db: Path
) -> None:
    engine, sessions = _sessions(tmp_path / "timeout.db", migrated_db)
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


async def test_collector_first_error_names_exception_class(
    tmp_path: Path, migrated_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    engine, sessions = _sessions(tmp_path / "errtext.db", migrated_db)

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    collector = ContextCollector(
        sessions,
        SourceRegistry(["okx"]),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
        sleep=lambda _seconds: asyncio.sleep(0),
    )
    try:
        await collector.collect()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "market context deribit" in w and "first error: ReadTimeout" in w for w in warnings
        )
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


SKEW_EXPIRY_MS = int((NOW + timedelta(days=30)).timestamp() * 1000)
SKEW_INSTRUMENTS = [
    {"instrument_name": name, "expiration_timestamp": SKEW_EXPIRY_MS}
    for name in (
        "BTC-13NOV26-90000-P",
        "BTC-13NOV26-100000-P",
        "BTC-13NOV26-90000-C",
        "BTC-13NOV26-100000-C",
    )
]
SKEW_TICKERS = {
    "BTC-13NOV26-90000-P": (-0.35, 60.0),
    "BTC-13NOV26-100000-P": (-0.15, 56.0),
    "BTC-13NOV26-90000-C": (0.15, 50.0),
    "BTC-13NOV26-100000-C": (0.35, 54.0),
}


class _FakePacer:
    """Deterministic monotonic clock: sleeps advance it and are recorded."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _deribit_handler(
    calls: list[tuple[str, float]],
    pacer: _FakePacer,
    *,
    instruments: Callable[[httpx.Request, int], httpx.Response] | None = None,
) -> Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]:
    instrument_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal instrument_calls
        path = request.url.path
        calls.append((path, pacer.now))
        if path.endswith("/public/get_volatility_index_data"):
            return httpx.Response(
                200,
                json={"result": {"data": [[1000, 1, 2, 0, 1.25]], "continuation": None}},
            )
        if path.endswith("/public/get_instruments"):
            instrument_calls += 1
            if instruments is not None:
                return instruments(request, instrument_calls)
            return httpx.Response(200, json={"result": SKEW_INSTRUMENTS})
        if path.endswith("/public/ticker"):
            return httpx.Response(500, json={"error": {"message": "unexpected ticker poll"}})
        return httpx.Response(404)

    return handler


class _FakeDeribitWebSocket:
    def __init__(self, *, subscribe_error: bool = False, drop: set[str] | None = None) -> None:
        self.subscribe_error = subscribe_error
        self.drop = drop or set()
        self.requests: list[dict[str, Any]] = []
        self.messages: list[str] = []

    async def __aenter__(self) -> _FakeDeribitWebSocket:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, message: str) -> None:
        request = json.loads(message)
        self.requests.append(request)
        channels = request["params"]["channels"]
        if self.subscribe_error:
            self.messages.append(
                json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": "subscribe failed"}})
            )
            return
        self.messages.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": channels}))
        for channel in channels:
            name = channel.removeprefix("incremental_ticker.")
            if name in self.drop:
                continue
            delta, iv = SKEW_TICKERS[name]
            self.messages.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "subscription",
                        "params": {
                            "channel": channel,
                            "data": {
                                "type": "snapshot",
                                "instrument_name": name,
                                "mark_iv": iv,
                                "greeks": {"delta": delta},
                                "underlying_price": 100000,
                            },
                        },
                    }
                )
            )

    async def recv(self) -> str:
        if not self.messages:
            raise TimeoutError("no more fake websocket messages")
        return self.messages.pop(0)


class _FakeDeribitWebSocketFactory:
    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        subscribe_error: bool = False,
        drop: set[str] | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.subscribe_error = subscribe_error
        self.drop = drop
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sockets: list[_FakeDeribitWebSocket] = []

    def __call__(self, uri: str, **kwargs: Any) -> _FakeDeribitWebSocket:
        self.calls.append((uri, kwargs))
        if self.connect_error is not None:
            raise self.connect_error
        socket = _FakeDeribitWebSocket(subscribe_error=self.subscribe_error, drop=self.drop)
        self.sockets.append(socket)
        return socket


def _websocket_collector(
    sessions: sessionmaker[Session],
    registry: SourceRegistry,
    handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
    pacer: _FakePacer,
    websocket: _FakeDeribitWebSocketFactory,
) -> ContextCollector:
    return ContextCollector(
        sessions,
        registry,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
        sleep=pacer.sleep,
        monotonic=pacer.monotonic,
        websocket=websocket,  # type: ignore[arg-type]
    )


async def test_deribit_websocket_snapshots_produce_skew_without_ticker_rest(
    tmp_path: Path, migrated_db: Path
) -> None:
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []
    handler = _deribit_handler(calls, pacer)
    engine, sessions = _sessions(tmp_path / "ws-skew.db", migrated_db)
    registry = SourceRegistry(["okx"])
    websocket = _FakeDeribitWebSocketFactory()
    collector = _websocket_collector(sessions, registry, handler, pacer, websocket)
    session = sessions()
    try:
        counts = await collector.collect()
        assert counts["skew_25d"] == 2  # BTC and ETH
        values = session.scalars(
            select(MarketContextRow.value).where(MarketContextRow.metric == "skew_25d")
        ).all()
        assert len(values) == 2
        assert all(value == pytest.approx(6.0) for value in values)
        assert not any(path.endswith("/public/ticker") for path, _at in calls)
        assert len(websocket.sockets) == 2  # one batched subscription per symbol
        assert all(
            len(socket.requests[0]["params"]["channels"]) == 4 for socket in websocket.sockets
        )
        assert all(
            channel.startswith("incremental_ticker.")
            for socket in websocket.sockets
            for channel in socket.requests[0]["params"]["channels"]
        )
        assert websocket.calls[0][1]["max_queue"] == WEBSOCKET_MAX_QUEUE
        assert registry.status("deribit").runtime.failures == 0
    finally:
        session.close()
        await collector.client.aclose()
        engine.dispose()


async def test_deribit_rest_429_then_200_recovers_skew(tmp_path: Path, migrated_db: Path) -> None:
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []

    def instruments(_request: httpx.Request, call: int) -> httpx.Response:
        if call == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"result": SKEW_INSTRUMENTS})

    handler = _deribit_handler(calls, pacer, instruments=instruments)
    engine, sessions = _sessions(tmp_path / "retry.db", migrated_db)
    registry = SourceRegistry(["okx"])
    websocket = _FakeDeribitWebSocketFactory()
    collector = _websocket_collector(sessions, registry, handler, pacer, websocket)
    try:
        counts = await collector.collect()
        assert counts["skew_25d"] == 2
        assert 2.0 in pacer.sleeps  # Retry-After honoured
        assert sum(path.endswith("/public/get_instruments") for path, _at in calls) == 3
        assert registry.status("deribit").runtime.failures == 0
    finally:
        await collector.client.aclose()
        engine.dispose()


async def test_deribit_requests_respect_min_interval(tmp_path: Path, migrated_db: Path) -> None:
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []
    handler = _deribit_handler(calls, pacer)
    engine, sessions = _sessions(tmp_path / "pace.db", migrated_db)
    websocket = _FakeDeribitWebSocketFactory()
    collector = _websocket_collector(
        sessions,
        SourceRegistry(["okx"]),
        handler,
        pacer,
        websocket,
    )
    try:
        counts = await collector.collect()
        assert counts["skew_25d"] == 2
        assert len(calls) == 4  # DVOL and instruments x BTC and ETH; no ticker polling
        # Default 2 requests/second -> 0.5 s between REST request starts.
        assert all(
            later - earlier >= 0.5 - 1e-6
            for (_path, earlier), (_later_path, later) in itertools.pairwise(calls)
        )
    finally:
        await collector.client.aclose()
        engine.dispose()


async def test_deribit_persistent_429_records_error_without_skew(
    tmp_path: Path, migrated_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []

    def instruments(_request: httpx.Request, _call: int) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "too_many_requests"}})

    handler = _deribit_handler(calls, pacer, instruments=instruments)
    engine, sessions = _sessions(tmp_path / "limited.db", migrated_db)
    registry = SourceRegistry(["okx"])
    websocket = _FakeDeribitWebSocketFactory()
    collector = _websocket_collector(sessions, registry, handler, pacer, websocket)
    try:
        counts = await collector.collect()
        assert "skew_25d" not in counts
        runtime = registry.status("deribit").runtime
        assert runtime.failures == 1
        assert "429" in (runtime.last_error or "")
        # One summary warning per collect; per-request detail stays at debug.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert sum("market context deribit" in w for w in warnings) == 1
        assert not any("market context request failed" in w for w in warnings)
        instrument_calls = sum(path.endswith("/public/get_instruments") for path, _at in calls)
        assert instrument_calls == 2 * 5  # both symbols exhaust the 5-try budget
        assert not websocket.calls
    finally:
        await collector.client.aclose()
        engine.dispose()


async def test_deribit_websocket_connect_failure_records_error_without_skew(
    tmp_path: Path, migrated_db: Path
) -> None:
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []
    handler = _deribit_handler(calls, pacer)
    engine, sessions = _sessions(tmp_path / "ws-connect-error.db", migrated_db)
    registry = SourceRegistry(["okx"])
    websocket = _FakeDeribitWebSocketFactory(connect_error=OSError("connect failed"))
    collector = _websocket_collector(sessions, registry, handler, pacer, websocket)
    try:
        counts = await collector.collect()
        assert "skew_25d" not in counts
        runtime = registry.status("deribit").runtime
        assert runtime.failures == 1
        assert "connect failed" in (runtime.last_error or "")
        assert len(websocket.calls) == 2
    finally:
        await collector.client.aclose()
        engine.dispose()


async def test_deribit_websocket_subscribe_failure_records_error_without_skew(
    tmp_path: Path, migrated_db: Path
) -> None:
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []
    handler = _deribit_handler(calls, pacer)
    engine, sessions = _sessions(tmp_path / "ws-subscribe-error.db", migrated_db)
    registry = SourceRegistry(["okx"])
    websocket = _FakeDeribitWebSocketFactory(subscribe_error=True)
    collector = _websocket_collector(sessions, registry, handler, pacer, websocket)
    try:
        counts = await collector.collect()
        assert "skew_25d" not in counts
        runtime = registry.status("deribit").runtime
        assert runtime.failures == 1
        assert "subscribe failed" in (runtime.last_error or "")
        assert len(websocket.sockets) == 2
    finally:
        await collector.client.aclose()
        engine.dispose()


async def test_deribit_websocket_missing_snapshot_records_error_without_skew(
    tmp_path: Path, migrated_db: Path
) -> None:
    pacer = _FakePacer()
    calls: list[tuple[str, float]] = []
    handler = _deribit_handler(calls, pacer)
    engine, sessions = _sessions(tmp_path / "ws-missing-snapshot.db", migrated_db)
    registry = SourceRegistry(["okx"])
    websocket = _FakeDeribitWebSocketFactory(drop={"BTC-13NOV26-90000-P"})
    collector = _websocket_collector(sessions, registry, handler, pacer, websocket)
    try:
        counts = await collector.collect()
        assert "skew_25d" not in counts
        runtime = registry.status("deribit").runtime
        assert runtime.failures == 1
        assert "snapshots incomplete" in (runtime.last_error or "")
        assert len(websocket.sockets) == 2
    finally:
        await collector.client.aclose()
        engine.dispose()


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
