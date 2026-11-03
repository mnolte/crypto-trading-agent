"""Limit entries: paper_orders, position entry leg, taker/maker fees.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename_fee(direction: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Keep the configured fee value across the taker/maker split."""
    paper = doc.get("paper")
    if not isinstance(paper, dict):
        return doc
    if direction == "up":
        if "fee_pct" in paper and "taker_fee_pct" not in paper:
            paper["taker_fee_pct"] = paper.pop("fee_pct")
        else:
            paper.pop("fee_pct", None)
    else:
        if "taker_fee_pct" in paper and "fee_pct" not in paper:
            paper["fee_pct"] = paper.pop("taker_fee_pct")
        else:
            paper.pop("taker_fee_pct", None)
        paper.pop("maker_fee_pct", None)
        paper.pop("entry_orders", None)
    return doc


def _migrate_settings(direction: str) -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, doc FROM settings")).all()
    for row_id, doc in rows:
        parsed = _rename_fee(direction, cast("dict[str, Any]", json.loads(cast(str, doc))))
        conn.execute(
            sa.text("UPDATE settings SET doc = :doc WHERE id = :id"),
            {"doc": json.dumps(parsed), "id": row_id},
        )


def upgrade() -> None:
    _migrate_settings("up")
    op.add_column(
        "positions",
        sa.Column("entry_maker", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_table(
        "paper_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("leg", sa.String(), nullable=False),
        sa.Column("limit_price", sa.String(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("placed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("filled_at", sa.DateTime(), nullable=True),
        sa.Column("fill_price", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_reason", sa.String(), nullable=True),
        sa.Column("min_price", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("paper_orders")
    op.drop_column("positions", "entry_maker")
    _migrate_settings("down")
