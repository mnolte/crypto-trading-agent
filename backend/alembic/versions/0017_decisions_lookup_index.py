"""Index for the latest-decision-per-pair lookups (item 034).

``repo.latest_decisions`` and the Universe screen's per-market status ask
for the newest decision of each pair; without an index every lookup scans
the whole ``decisions`` table, which stalled ``GET /api/universe`` for
minutes on the event loop.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_decisions_account_pair_at",
        "decisions",
        ["account_id", "pair", "at"],
    )


def downgrade() -> None:
    op.drop_index("ix_decisions_account_pair_at", table_name="decisions")
