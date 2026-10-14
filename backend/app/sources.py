"""The source registry: one descriptor per external data source.

Each source declares an id, name, purpose, key requirement (none, optional
or required), a freshness limit and a rate limit. The Settings page and
``GET /api/sources`` are built from these descriptors, so adding a source
needs no new UI.

Runtime status lives here in memory: enabled flags persist in the settings
document (``disabled_sources``); last success, failures and rate-limit
usage are tracked per run. A required key that is missing disables the
source with a clear message — it never fails cycles.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx

from app.config import get_secret

KeyRequirement = Literal["none", "optional", "required"]

OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    purpose: str
    key: KeyRequirement
    freshness_limit_s: int | None
    rate_limit: str | None


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        id="bitvavo",
        name="Bitvavo",
        purpose="Market data: candles, order books and ticker prices",
        key="none",
        freshness_limit_s=120,
        rate_limit="1000 weight per minute per IP",
    ),
    SourceSpec(
        id="openai",
        name="OpenAI",
        purpose="Decision layer, Ask, briefings and replays",
        key="required",
        freshness_limit_s=None,
        rate_limit="per-minute token limit",
    ),
    SourceSpec(
        id="okx",
        name="OKX market data",
        purpose="BTC/ETH funding, open interest and long/short positioning",
        key="none",
        freshness_limit_s=3600,
        rate_limit="public endpoint limits",
    ),
    SourceSpec(
        id="deribit",
        name="Deribit options data",
        purpose="BTC/ETH DVOL and 25-delta options skew",
        key="none",
        freshness_limit_s=3600,
        rate_limit="public endpoint limits",
    ),
)

_SPECS = {s.id: s for s in SOURCES}


def get_spec(source_id: str) -> SourceSpec | None:
    return _SPECS.get(source_id)


@dataclass
class SourceRuntime:
    last_success_at: datetime | None = None
    last_error: str | None = None
    failures: int = 0
    rate_limit_remaining: int | None = None
    rate_limit_limit: int | None = None
    rate_limit_reset_at: datetime | None = None


@dataclass
class SourceStatus:
    spec: SourceSpec
    enabled: bool
    disabled_reason: str | None
    key_set: bool
    key_last4: str | None
    runtime: SourceRuntime = field(default_factory=SourceRuntime)


class SourceRegistry:
    """Descriptors plus per-run status for every data source."""

    def __init__(self, disabled: list[str] | None = None) -> None:
        self._disabled = set(disabled or [])
        self._runtime = {s.id: SourceRuntime() for s in SOURCES}
        self.on_failure_threshold: Callable[[str, str], None] | None = None
        self.on_recovered: Callable[[str], None] | None = None

    def set_disabled(self, disabled: list[str]) -> None:
        self._disabled = set(disabled)

    def status(self, source_id: str) -> SourceStatus:
        spec = _SPECS[source_id]
        key = get_secret(source_id)
        enabled = True
        reason: str | None = None
        if spec.id in self._disabled:
            enabled, reason = False, "disabled in settings"
        elif spec.key == "required" and key is None:
            enabled, reason = False, "API key missing"
        return SourceStatus(
            spec=spec,
            enabled=enabled,
            disabled_reason=reason,
            key_set=key is not None,
            key_last4=key[-4:] if key else None,
            runtime=self._runtime[spec.id],
        )

    def statuses(self) -> list[SourceStatus]:
        return [self.status(s.id) for s in SOURCES]

    def record(
        self,
        source_id: str,
        error: BaseException | None,
        *,
        at: datetime | None = None,
        rate_limit_remaining: int | None = None,
        rate_limit_limit: int | None = None,
        rate_limit_reset_at: datetime | None = None,
    ) -> None:
        rt = self._runtime[source_id]
        if error is None:
            recovered = rt.failures > 0
            rt.last_success_at = at or datetime.now().astimezone()
            rt.last_error = None
            rt.failures = 0
            if recovered and self.on_recovered is not None:
                self.on_recovered(source_id)
        else:
            rt.last_error = str(error)
            rt.failures += 1
            if rt.failures == 3 and self.on_failure_threshold is not None:
                self.on_failure_threshold(source_id, rt.last_error)
        if rate_limit_remaining is not None:
            rt.rate_limit_remaining = rate_limit_remaining
        if rate_limit_limit is not None:
            rt.rate_limit_limit = rate_limit_limit
        if rate_limit_reset_at is not None:
            rt.rate_limit_reset_at = rate_limit_reset_at


async def test_source_key(source_id: str) -> tuple[bool, str | None]:
    """One minimal request with the stored key. Returns (ok, error).

    Bitvavo needs no key — the ping hits the public /time endpoint. OpenAI
    gets a bare GET /models with the key, the cheapest authenticated call.
    """
    spec = get_spec(source_id)
    if spec is None:
        return False, "unknown source"
    key = get_secret(source_id)
    if spec.key == "required" and key is None:
        return False, "no key set"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if source_id == "openai":
                response = await client.get(
                    f"{OPENAI_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
            elif source_id == "bitvavo":
                response = await client.get("https://api.bitvavo.com/v2/time")
            elif source_id == "okx":
                response = await client.get("https://www.okx.com/api/v5/public/time")
            elif source_id == "deribit":
                response = await client.get("https://www.deribit.com/api/v2/public/test")
            else:
                return False, "no test for this source"
        if response.status_code == 200:
            return True, None
        detail = _provider_error(response)
        return False, detail
    except httpx.HTTPError as exc:
        return False, str(exc)


def _provider_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if err:
                return str(err)
    except ValueError:
        pass
    return f"HTTP {response.status_code}"
