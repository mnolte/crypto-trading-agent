from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app import repo
from app.sources import SourceRegistry

log = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
DERIBIT_BASE = "https://www.deribit.com/api/v2"
SYMBOLS: tuple[Literal["BTC", "ETH"], ...] = ("BTC", "ETH")
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ContextPoint:
    source: Literal["okx", "deribit"]
    metric: str
    symbol: Literal["BTC", "ETH"]
    ts: datetime
    value: float
    meta: dict[str, object] = field(default_factory=dict)

    def row(self, fetched_at: datetime) -> dict[str, object]:
        return {
            "source": self.source,
            "metric": self.metric,
            "symbol": self.symbol,
            "ts": self.ts.astimezone(UTC).replace(tzinfo=None),
            "value": self.value,
            "meta": json.dumps(self.meta, separators=(",", ":"), sort_keys=True),
            "fetched_at": fetched_at.astimezone(UTC).replace(tzinfo=None),
        }


class ProviderError(RuntimeError):
    pass


def from_ms(value: str | int | float) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def parse_okx_funding_history(payload: object, symbol: Literal["BTC", "ETH"]) -> list[ContextPoint]:
    data = _okx_data(payload)
    return [
        ContextPoint(
            "okx",
            "funding_rate_settled",
            symbol,
            from_ms(item["fundingTime"]),
            float(item["realizedRate"] or item["fundingRate"]),
        )
        for item in data
        if isinstance(item, dict)
        and item.get("fundingTime")
        and (item.get("realizedRate") or item.get("fundingRate"))
    ]


def parse_okx_current(payload: object, symbol: Literal["BTC", "ETH"]) -> list[ContextPoint]:
    data = _okx_data(payload)
    if not data or not isinstance(data[0], dict):
        return []
    item = data[0]
    if not item.get("ts") or item.get("fundingRate") in (None, ""):
        return []
    return [
        ContextPoint(
            "okx",
            "funding_rate_current",
            symbol,
            hour(from_ms(item["ts"])),
            float(item["fundingRate"]),
        )
    ]


def parse_okx_open_interest(
    payload: object, symbol: Literal["BTC", "ETH"], *, history: bool
) -> list[ContextPoint]:
    data = _okx_data(payload)
    if history:
        return [
            ContextPoint("okx", "open_interest_usd", symbol, from_ms(item[0]), float(item[3]))
            for item in data
            if isinstance(item, list) and len(item) >= 4
        ]
    if not data or not isinstance(data[0], dict):
        return []
    item = data[0]
    if not item.get("ts") or item.get("oiUsd") in (None, ""):
        return []
    return [
        ContextPoint(
            "okx", "open_interest_usd", symbol, hour(from_ms(item["ts"])), float(item["oiUsd"])
        )
    ]


def parse_okx_ratio(payload: object, symbol: Literal["BTC", "ETH"]) -> list[ContextPoint]:
    return [
        ContextPoint("okx", "long_short_account_ratio", symbol, from_ms(item[0]), float(item[1]))
        for item in _okx_data(payload)
        if isinstance(item, list) and len(item) >= 2
    ]


def parse_dvol(payload: object, symbol: Literal["BTC", "ETH"]) -> list[ContextPoint]:
    result = _deribit_result(payload)
    data = result.get("data", []) if isinstance(result, dict) else []
    return [
        ContextPoint("deribit", "dvol", symbol, from_ms(item[0]), float(item[4]))
        for item in data
        if isinstance(item, list) and len(item) >= 5
    ]


def choose_expiry(instruments: list[dict[str, Any]], now: datetime) -> int | None:
    expiries = {
        int(item["expiration_timestamp"])
        for item in instruments
        if item.get("expiration_timestamp")
        and from_ms(item["expiration_timestamp"]) >= now + timedelta(days=7)
    }
    target = now + timedelta(days=30)
    return min(expiries, key=lambda value: abs(from_ms(value) - target), default=None)


def interpolate_iv(points: list[tuple[float, float]], target: float) -> float | None:
    points = sorted(points)
    for delta, iv in points:
        if delta == target:
            return iv
    for (left_delta, left_iv), (right_delta, right_iv) in itertools.pairwise(points):
        if left_delta <= target <= right_delta and right_delta != left_delta:
            weight = (target - left_delta) / (right_delta - left_delta)
            return left_iv + weight * (right_iv - left_iv)
    return None


def calculate_skew(
    tickers: list[dict[str, Any]], symbol: Literal["BTC", "ETH"], expiry_ms: int, ts: datetime
) -> ContextPoint | None:
    puts: list[tuple[float, float]] = []
    calls: list[tuple[float, float]] = []
    strikes: dict[float, list[float]] = {}
    underlying: float | None = None
    for item in tickers:
        greeks = item.get("greeks")
        iv = item.get("mark_iv")
        name = str(item.get("instrument_name", ""))
        if not isinstance(greeks, dict) or iv is None:
            continue
        try:
            delta = float(greeks["delta"])
            strike = float(name.split("-")[-2])
            iv_value = float(iv)
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if name.endswith("-P"):
            puts.append((delta, iv_value))
        elif name.endswith("-C"):
            calls.append((delta, iv_value))
        strikes.setdefault(strike, []).append(iv_value)
        if item.get("underlying_price") is not None:
            underlying = float(item["underlying_price"])
    put_iv = interpolate_iv(puts, -0.25)
    call_iv = interpolate_iv(calls, 0.25)
    if put_iv is None or call_iv is None or underlying is None or not strikes:
        return None
    atm_strike = min(strikes, key=lambda strike: abs(strike - underlying))
    atm_iv = sum(strikes[atm_strike]) / len(strikes[atm_strike])
    expiry = from_ms(expiry_ms)
    return ContextPoint(
        "deribit",
        "skew_25d",
        symbol,
        hour(ts),
        put_iv - call_iv,
        {
            "put_iv_25d": put_iv,
            "call_iv_25d": call_iv,
            "atm_iv": atm_iv,
            "expiry": expiry.isoformat(),
        },
    )


def _okx_data(payload: object) -> list[Any]:
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise ProviderError(f"OKX error: {payload!r}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ProviderError("OKX response has no data list")
    return data


def _deribit_result(payload: object) -> Any:
    if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
        raise ProviderError(f"Deribit error: {payload!r}")
    return payload["result"]


class ContextCollector:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sources: SourceRegistry,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._sessions = session_factory
        self.sources = sources
        self.client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self.clock = clock
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(
        self,
        source: str,
        path: str,
        params: dict[str, str | int | float | bool | None],
    ) -> object:
        base = OKX_BASE if source == "okx" else DERIBIT_BASE
        for attempt in range(2):
            try:
                response = await self.client.get(f"{base}{path}", params=params, timeout=10.0)
                if response.status_code in RETRY_STATUSES and attempt == 0:
                    retry_after = response.headers.get("retry-after")
                    delay = (
                        min(float(retry_after), 30.0)
                        if retry_after
                        else 1.0
                        if response.status_code == 429
                        else 0.25
                    )
                    await self._sleep(delay)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.NetworkError, httpx.TimeoutException):
                if attempt == 0:
                    await self._sleep(0.25)
                    continue
                raise
        raise AssertionError("unreachable")

    async def _try(
        self,
        source: str,
        path: str,
        params: dict[str, str | int | float | bool | None],
        errors: list[Exception],
    ) -> object | None:
        try:
            return await self._get(source, path, params)
        except Exception as exc:  # noqa: BLE001 - boundary: one collector source
            errors.append(exc)
            log.warning("market context request failed %s %s: %s", source, path, exc)
            return None

    async def collect(self, *, days: int | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source, collect in (("okx", self._collect_okx), ("deribit", self._collect_deribit)):
            if not self.sources.status(source).enabled:
                continue
            try:
                points, error = await collect(days)
                session = self._sessions()
                try:
                    repo.upsert_market_context(
                        session, [point.row(self.clock()) for point in points]
                    )
                finally:
                    session.close()
                self.sources.record(source, error, at=self.clock())
                for point in points:
                    counts[point.metric] = counts.get(point.metric, 0) + 1
            except Exception as exc:
                self.sources.record(source, exc)
                log.exception("market context collection failed for %s", source)
        return counts

    async def _collect_okx(self, days: int | None) -> tuple[list[ContextPoint], Exception | None]:
        points: list[ContextPoint] = []
        errors: list[Exception] = []
        until = self.clock()
        for symbol in SYMBOLS:
            inst = f"{symbol}-USDT-SWAP"
            funding = await self._try(
                "okx",
                "/api/v5/public/funding-rate-history",
                {"instId": inst, "limit": "400"},
                errors,
            )
            if funding is not None:
                points += parse_okx_funding_history(funding, symbol)
            if days is None:
                current = await self._try(
                    "okx", "/api/v5/public/funding-rate", {"instId": inst}, errors
                )
                if current is not None:
                    points += parse_okx_current(current, symbol)
                spot_oi = await self._try(
                    "okx",
                    "/api/v5/public/open-interest",
                    {"instType": "SWAP", "instId": inst},
                    errors,
                )
                if spot_oi is not None:
                    points += parse_okx_open_interest(spot_oi, symbol, history=False)
            windows: list[tuple[datetime | None, datetime | None]] = [(None, None)]
            if days is not None:
                windows = []
                cursor = until - timedelta(days=days)
                while cursor < until:
                    chunk_end = min(cursor + timedelta(days=4), until)
                    windows.append((cursor, chunk_end))
                    cursor = chunk_end
            for window_start, window_end in windows:
                history_params: dict[str, str | int | float | bool | None] = {
                    "instId": inst,
                    "period": "1H",
                    "limit": "100",
                }
                if window_start is not None and window_end is not None:
                    history_params.update(
                        begin=str(int(window_start.timestamp() * 1000)),
                        end=str(int(window_end.timestamp() * 1000)),
                    )
                oi = await self._try(
                    "okx",
                    "/api/v5/rubik/stat/contracts/open-interest-history",
                    history_params,
                    errors,
                )
                if oi is not None:
                    points += parse_okx_open_interest(oi, symbol, history=True)
                ratio = await self._try(
                    "okx",
                    "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
                    history_params,
                    errors,
                )
                if ratio is not None:
                    points += parse_okx_ratio(ratio, symbol)
                if days is not None:
                    await self._sleep(0.1)
        if days is not None:
            cutoff = until - timedelta(days=days)
            points = [point for point in points if point.ts >= cutoff]
        return points, errors[0] if errors else None

    async def _collect_deribit(
        self, days: int | None
    ) -> tuple[list[ContextPoint], Exception | None]:
        now = self.clock()
        start = now - timedelta(days=days or 2)
        points: list[ContextPoint] = []
        errors: list[Exception] = []
        for symbol in SYMBOLS:
            end = now
            while True:
                payload = await self._try(
                    "deribit",
                    "/public/get_volatility_index_data",
                    {
                        "currency": symbol,
                        "start_timestamp": int(start.timestamp() * 1000),
                        "end_timestamp": int(end.timestamp() * 1000),
                        "resolution": "3600",
                    },
                    errors,
                )
                if payload is None:
                    break
                points += parse_dvol(payload, symbol)
                result = _deribit_result(payload)
                continuation = result.get("continuation") if isinstance(result, dict) else None
                if continuation is None:
                    break
                next_end = from_ms(continuation)
                if next_end >= end:
                    errors.append(ProviderError("Deribit DVOL continuation did not move backward"))
                    break
                if next_end <= start:
                    break
                end = next_end
            if days is not None:
                continue
            payload = await self._try(
                "deribit",
                "/public/get_instruments",
                {"currency": symbol, "kind": "option", "expired": "false"},
                errors,
            )
            if payload is None:
                continue
            result = _deribit_result(payload)
            instruments = result if isinstance(result, list) else []
            expiry = choose_expiry(instruments, now)
            if expiry is None:
                continue
            selected = [
                item for item in instruments if int(item.get("expiration_timestamp", 0)) == expiry
            ]
            semaphore = asyncio.Semaphore(4)

            async def ticker(
                name: str, limit: asyncio.Semaphore = semaphore
            ) -> dict[str, Any] | None:
                async with limit:
                    payload = await self._try(
                        "deribit",
                        "/public/ticker",
                        {"instrument_name": name},
                        errors,
                    )
                    if payload is None:
                        return None
                    result = _deribit_result(payload)
                    return result if isinstance(result, dict) else None

            tickers = [
                item
                for item in await asyncio.gather(
                    *(ticker(str(item["instrument_name"])) for item in selected)
                )
                if item is not None
            ]
            skew = calculate_skew(tickers, symbol, expiry, now)
            if skew is None:
                log.warning("no skew row for %s: missing deltas", symbol)
            else:
                points.append(skew)
        return points, errors[0] if errors else None
