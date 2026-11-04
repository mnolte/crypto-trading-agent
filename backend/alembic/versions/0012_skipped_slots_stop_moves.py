"""Skipped cycle slots and stop moves for the cycles card.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skipped_slots",
        sa.Column("slot_at", sa.DateTime(), primary_key=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "stop_moves",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("old_stop", sa.String(), nullable=False),
        sa.Column("new_stop", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stop_moves")
    op.drop_table("skipped_slots")
