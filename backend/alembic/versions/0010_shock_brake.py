"""Persist the shock brake so a restart cannot silently lift it.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("shock_brake_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "shock_brake_until")
