"""Execution modes, outcomes and immutable trade risk.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions", sa.Column("initial_stop", sa.String(), nullable=False, server_default="0")
    )
    op.add_column(
        "trades", sa.Column("initial_stop", sa.String(), nullable=False, server_default="0")
    )
    op.add_column("trades", sa.Column("r_unit", sa.String(), nullable=False, server_default="0"))
    op.create_table(
        "errors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("first_at", sa.DateTime(), nullable=False),
        sa.Column("last_at", sa.DateTime(), nullable=False),
        sa.Column("pair", sa.String(), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("affected_decision_or_fill", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.String(), nullable=True),
    )
    op.create_table(
        "outcome_failures",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column("horizon", sa.Integer(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(), nullable=False),
        sa.Column("last_at", sa.DateTime(), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_table(
        "outcomes",
        sa.Column("decision_id", sa.String(), primary_key=True),
        sa.Column("horizon", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("reference_at", sa.DateTime(), nullable=False),
        sa.Column("reference_price", sa.String(), nullable=False),
        sa.Column("forward_return_pct", sa.String(), nullable=False),
        sa.Column("best_move_pct", sa.String(), nullable=False),
        sa.Column("worst_move_pct", sa.String(), nullable=False),
        sa.Column("best_move_r", sa.String(), nullable=True),
        sa.Column("worst_move_r", sa.String(), nullable=True),
        sa.Column("first_touch", sa.String(), nullable=True),
        sa.Column("invalidated", sa.Boolean(), nullable=False),
        sa.Column("invalidation_followup_pct", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("realised_r", sa.String(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("method_version", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("outcomes")
    op.drop_table("outcome_failures")
    op.drop_table("errors")
    op.drop_column("trades", "r_unit")
    op.drop_column("trades", "initial_stop")
    op.drop_column("positions", "initial_stop")
