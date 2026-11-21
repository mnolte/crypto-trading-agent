"""Settings versions (item 025).

Every settings save becomes a numbered version with its reason and
source; settings_history rows gain the version they belong to.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings_versions",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("changes", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column("settings_history", sa.Column("version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("settings_history", "version")
    op.drop_table("settings_versions")
