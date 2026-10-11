"""Daily call budgets and the monthly euro cost cap.

The daily budget counts every call that hit the wire for the trading
decisions, invalidation re-evaluations and replays (the ``decision`` and
``replay`` features); Ask, briefings and shadows get their own budgets in
later PRs. The monthly cap sums the estimated EUR cost over the current
Europe/Amsterdam month; at 100% decisions fall back to Rules until the 1st.

Prices come from ``config/models.yaml`` (USD per million tokens); an
unknown model reports its cost as unknown — never guessed. USD is
converted to EUR at the latest stored USDC-EUR daily close; USDC tracks
USD tightly enough for an estimate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from sqlalchemy.orm import Session

from app import repo
from app.config import BACKEND_DIR
from app.db import AMSTERDAM

MODEL_PRICES_PATH = BACKEND_DIR / "config" / "models.yaml"

# Features that share the daily call budget in v0.1.
BUDGET_FEATURES = ("decision", "replay")


class ModelPrices:
    """USD prices per million tokens, loaded once from models.yaml."""

    def __init__(self, path: Path = MODEL_PRICES_PATH) -> None:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        self._models: dict[str, dict[str, Decimal]] = {
            name: {
                "input": Decimal(str(prices["input_per_mtok_usd"])),
                "output": Decimal(str(prices["output_per_mtok_usd"])),
            }
            for name, prices in (data.get("models") or {}).items()
        }

    def price(self, model: str) -> tuple[Decimal, Decimal] | None:
        """(input, output) USD per million tokens, or None when unknown."""
        entry = self._models.get(model)
        if entry is None:
            return None
        return entry["input"], entry["output"]


def amsterdam_day_start(now: datetime) -> datetime:
    """Midnight Europe/Amsterdam for ``now``, returned in UTC."""
    ams = now.astimezone(AMSTERDAM)
    return ams.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def amsterdam_month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[first of month, first of next month) in UTC, on the Amsterdam clock."""
    ams = now.astimezone(AMSTERDAM)
    start = ams.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = start.replace(year=year, month=month)
    return start.astimezone(UTC), end.astimezone(UTC)


def calls_today(session: Session, account_id: int, now: datetime) -> int:
    """Model calls sent today (Amsterdam day) against the daily budget."""
    return repo.llm_calls_since(session, account_id, amsterdam_day_start(now), BUDGET_FEATURES)


def month_spend_eur(session: Session, account_id: int, now: datetime) -> Decimal:
    start, end = amsterdam_month_bounds(now)
    return repo.llm_cost_between(session, account_id, start, end)


def cap_state(spent_eur: Decimal, cap_eur: Decimal) -> Literal["ok", "warn", "cap"]:
    """warn from 80% of the monthly cap; cap at 100% — decisions go to Rules."""
    if cap_eur <= 0:
        return "cap"
    if spent_eur >= cap_eur:
        return "cap"
    if spent_eur >= cap_eur * Decimal("0.8"):
        return "warn"
    return "ok"


def usdc_eur_rate(session: Session, now: datetime) -> Decimal | None:
    """USD→EUR via the latest stored USDC-EUR daily close (None if unknown)."""
    candles = repo.load_candles(session, "USDC-EUR", "1d", 7, end=now)
    if not candles:
        candles = repo.load_candles(session, "USDC-EUR", "1d", 7)
    return candles[-1].close if candles else None


def cost_estimates(
    prices: ModelPrices,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    usd_eur: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    """(cost_usd, cost_eur) — None when the model's price is unknown."""
    price = prices.price(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None, None
    usd = (Decimal(input_tokens) * price[0] + Decimal(output_tokens) * price[1]) / Decimal(
        "1000000"
    )
    eur = (usd * usd_eur).quantize(Decimal("0.0001")) if usd_eur is not None else None
    return usd.quantize(Decimal("0.0001")), eur
