"""OpenAI decider: the llm_calls audit table and the decisions.prefilter
flag (False means the Rules pre-filter skipped the pair without a call).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("pair", sa.String(), nullable=False),
        sa.Column("decision_id", sa.String(), nullable=True),
        sa.Column("feature", sa.String(), nullable=False),
        sa.Column("replay_of", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.String(), nullable=False, server_default="[]"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.String(), nullable=True),
        sa.Column("cost_eur", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("request_json", sa.String(), nullable=True),
        sa.Column("response_raw", sa.String(), nullable=True),
        sa.Column("parsed", sa.String(), nullable=True),
        sa.Column("input_checks", sa.String(), nullable=False, server_default="[]"),
        sa.Column("output_checks", sa.String(), nullable=False, server_default="[]"),
        sa.Column("verdict", sa.String(), nullable=True),
        sa.Column("verdict_note", sa.String(), nullable=True),
        sa.Column("verdict_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "decisions",
        sa.Column("prefilter", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("decisions", "prefilter")
    op.drop_table("llm_calls")
