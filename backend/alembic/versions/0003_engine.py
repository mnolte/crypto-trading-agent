"""Paper engine: accounts, positions, trades, decisions, activity,
equity snapshots and exit ticks.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("start_balance", sa.String(), nullable=False),
        sa.Column("cash", sa.String(), nullable=False),
        sa.Column("peak_equity", sa.String(), nullable=False),
        sa.Column("day_start_equity", sa.String(), nullable=False),
        sa.Column("day_start_date", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("kill_at", sa.DateTime(), nullable=True),
        sa.Column("kill_peak", sa.String(), nullable=True),
        sa.Column("kill_equity", sa.String(), nullable=True),
        sa.Column("kill_closed", sa.String(), nullable=True),
    )
    op.create_table(
        "positions",
        sa.Column("pair", sa.String(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("entry", sa.String(), nullable=False),
        sa.Column("stop", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("last", sa.String(), nullable=False),
        sa.Column("invalidation_price", sa.String(), nullable=True),
        sa.Column("highest_close", sa.String(), nullable=True),
        sa.Column("r_unit", sa.String(), nullable=False),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("entry", sa.String(), nullable=False),
        sa.Column("exit", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=False),
        sa.Column("fee_eur", sa.String(), nullable=False),
        sa.Column("realised_eur", sa.String(), nullable=False),
        sa.Column("realised_r", sa.String(), nullable=False),
        sa.Column("exit_reason", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
    )
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("decider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.String(), nullable=False),
        sa.Column("invalidation", sa.String(), nullable=True),
        sa.Column("signals", sa.String(), nullable=False),
        sa.Column("plan", sa.String(), nullable=True),
        sa.Column("gates", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("execute_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("regime", sa.String(), nullable=False),
        sa.Column("btc_regime", sa.String(), nullable=False),
        sa.Column("invalidation_price", sa.String(), nullable=True),
        sa.Column("risk_pct_used", sa.String(), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("shadow_id", sa.String(), nullable=True),
        sa.Column("stand_in", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("raw_brief", sa.String(), nullable=True),
        sa.Column("raw_response", sa.String(), nullable=True),
    )
    op.create_table(
        "activity",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("pair", sa.String(), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=True),
    )
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("equity", sa.String(), nullable=False),
        sa.Column("cash", sa.String(), nullable=False),
    )
    op.create_table(
        "exit_ticks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("price", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("exit_ticks")
    op.drop_table("equity_snapshots")
    op.drop_table("activity")
    op.drop_table("decisions")
    op.drop_table("trades")
    op.drop_table("positions")
    op.drop_table("accounts")
