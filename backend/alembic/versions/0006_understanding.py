"""Understanding and safety: entry pauses, briefings, settings history, alerts.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("last_kill_at", sa.DateTime(), nullable=True))
    op.create_table(
        "entry_pauses",
        sa.Column("pair", sa.String(), primary_key=True),
        sa.Column("until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "briefings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("for_date", sa.String(), nullable=False),
        sa.Column("facts", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("llm_call_id", sa.String(), nullable=True),
    )
    op.create_table(
        "settings_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("old", sa.String(), nullable=True),
        sa.Column("new", sa.String(), nullable=True),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("repeats", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("settings_history")
    op.drop_table("briefings")
    op.drop_table("entry_pauses")
    op.drop_column("accounts", "last_kill_at")
