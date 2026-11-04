"""The errors table retires to errors_legacy.

Errors now live in logs.db (log_problems); the old table is renamed,
not dropped, and its rows are imported once at startup.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("errors", "errors_legacy")


def downgrade() -> None:
    op.rename_table("errors_legacy", "errors")
