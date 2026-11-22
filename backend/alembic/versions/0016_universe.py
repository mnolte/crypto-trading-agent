"""Universe screen storage (item 026).

markets gains name, last_price/price_at, vs_btc_30d_pct, corr_btc_30d,
eligible_since and failing_since; volume_30d_eur and spread_pct become
Decimals stored as text (SQLite has no decimal type — a REAL column
would round). market_checks and sync_runs are new.

The markets rebuild copies the float columns through Decimal(str(v)):
volume quantized to 0.01, spread to 0.0001, half to even.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dec(value: object, places: str) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_EVEN))


def upgrade() -> None:
    op.create_table(
        "market_checks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("thresholds", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("funnel", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("kept", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("held_back", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("failures", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # markets: SQLite cannot ALTER a column's type, so the table is
    # rebuilt. volume_30d_eur / spread_pct move from REAL to TEXT.
    op.create_table(
        "markets_new",
        sa.Column("market", sa.String(), primary_key=True),
        sa.Column("base", sa.String(), nullable=False, server_default=""),
        sa.Column("quote", sa.String(), nullable=False, server_default="EUR"),
        sa.Column("status", sa.String(), nullable=False, server_default=""),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_rule", sa.String(), nullable=True),
        sa.Column("volume_30d_eur", sa.String(), nullable=True),
        sa.Column("spread_pct", sa.String(), nullable=True),
        sa.Column("daily_candles", sa.Integer(), nullable=True),
        sa.Column("below_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("within_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("last_price", sa.String(), nullable=True),
        sa.Column("price_at", sa.DateTime(), nullable=True),
        sa.Column("vs_btc_30d_pct", sa.String(), nullable=True),
        sa.Column("corr_btc_30d", sa.String(), nullable=True),
        sa.Column("eligible_since", sa.DateTime(), nullable=True),
        sa.Column("failing_since", sa.DateTime(), nullable=True),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT market, base, quote, status, eligible, failed_rule, "
            "volume_30d_eur, spread_pct, daily_candles, below_streak, "
            "within_streak, checked_at, active, activated_at FROM markets"
        )
    ).all()
    for r in rows:
        conn.execute(
            sa.text(
                "INSERT INTO markets_new (market, base, quote, status, name, "
                "eligible, failed_rule, volume_30d_eur, spread_pct, "
                "daily_candles, below_streak, within_streak, checked_at, "
                "active, activated_at, last_price, price_at, "
                "vs_btc_30d_pct, corr_btc_30d, eligible_since, failing_since) "
                "VALUES (:market, :base, :quote, :status, NULL, :eligible, "
                ":failed_rule, :volume, :spread, :candles, :below, :within, "
                ":checked_at, :active, :activated_at, "
                "NULL, NULL, NULL, NULL, NULL, NULL)"
            ),
            {
                "market": r.market,
                "base": r.base,
                "quote": r.quote,
                "status": r.status,
                "eligible": r.eligible,
                "failed_rule": r.failed_rule,
                "volume": _dec(r.volume_30d_eur, "0.01"),
                "spread": _dec(r.spread_pct, "0.0001"),
                "candles": r.daily_candles,
                "below": r.below_streak,
                "within": r.within_streak,
                "checked_at": r.checked_at,
                "active": r.active,
                "activated_at": r.activated_at,
            },
        )
    op.drop_table("markets")
    op.rename_table("markets_new", "markets")


def downgrade() -> None:
    # The float columns cannot be recovered exactly from the text ones;
    # downgrade restores the pre-026 shape with CAST back to REAL.
    op.create_table(
        "markets_old",
        sa.Column("market", sa.String(), primary_key=True),
        sa.Column("base", sa.String(), nullable=False, server_default=""),
        sa.Column("quote", sa.String(), nullable=False, server_default="EUR"),
        sa.Column("status", sa.String(), nullable=False, server_default=""),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_rule", sa.String(), nullable=True),
        sa.Column("volume_30d_eur", sa.Float(), nullable=True),
        sa.Column("spread_pct", sa.Float(), nullable=True),
        sa.Column("daily_candles", sa.Integer(), nullable=True),
        sa.Column("below_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("within_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO markets_old (market, base, quote, status, eligible, "
            "failed_rule, volume_30d_eur, spread_pct, daily_candles, "
            "below_streak, within_streak, checked_at, active, activated_at) "
            "SELECT market, base, quote, status, eligible, failed_rule, "
            "CAST(volume_30d_eur AS REAL), CAST(spread_pct AS REAL), "
            "daily_candles, below_streak, within_streak, checked_at, "
            "active, activated_at FROM markets"
        )
    )
    op.drop_table("markets")
    op.rename_table("markets_old", "markets")
    op.drop_table("sync_runs")
    op.drop_table("market_checks")
