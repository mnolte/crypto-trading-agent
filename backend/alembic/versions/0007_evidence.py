"""Evidence: shadow accounts, What-if scenarios and Ask conversations.

The positions table's primary key widens to (pair, account_id) so a shadow
strategy can hold the same pair as the live account on its own paper
account. ``exit_ticks`` already store the account id.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot change a primary key in place: rebuild the table.
    op.create_table(
        "positions_new",
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("entry", sa.String(), nullable=False),
        sa.Column("stop", sa.String(), nullable=False),
        sa.Column("initial_stop", sa.String(), nullable=False, server_default="0"),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("last", sa.String(), nullable=False),
        sa.Column("invalidation_price", sa.String(), nullable=True),
        sa.Column("highest_close", sa.String(), nullable=True),
        sa.Column("r_unit", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("pair", "account_id"),
    )
    op.execute(
        "INSERT INTO positions_new (pair, account_id, qty, entry, stop,"
        " initial_stop, target, opened_at, decision_id, last,"
        " invalidation_price, highest_close, r_unit) SELECT pair,"
        " account_id, qty, entry, stop, initial_stop, target, opened_at,"
        " decision_id, last, invalidation_price, highest_close, r_unit FROM"
        " positions"
    )
    op.drop_table("positions")
    op.rename_table("positions_new", "positions")

    op.create_table(
        "shadows",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("overrides", sa.String(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("start_equity", sa.String(), nullable=False),
        sa.Column("daily_call_limit", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("pauses", sa.String(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "whatif_scenarios",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("spec", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("saved", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(), nullable=True),
    )
    op.create_table(
        "ask_conversations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("messages", sa.String(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_table("ask_conversations")
    op.drop_table("whatif_scenarios")
    op.drop_table("shadows")
    op.create_table(
        "positions_new",
        sa.Column("pair", sa.String(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.String(), nullable=False),
        sa.Column("entry", sa.String(), nullable=False),
        sa.Column("stop", sa.String(), nullable=False),
        sa.Column("initial_stop", sa.String(), nullable=False, server_default="0"),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=False),
        sa.Column("last", sa.String(), nullable=False),
        sa.Column("invalidation_price", sa.String(), nullable=True),
        sa.Column("highest_close", sa.String(), nullable=True),
        sa.Column("r_unit", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO positions_new SELECT pair, account_id, qty, entry, stop,"
        " initial_stop, target, opened_at, decision_id, last,"
        " invalidation_price, highest_close, r_unit FROM positions WHERE"
        " account_id = (SELECT id FROM accounts WHERE active = 1)"
    )
    op.drop_table("positions")
    op.rename_table("positions_new", "positions")
