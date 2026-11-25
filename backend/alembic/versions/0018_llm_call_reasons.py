"""Inspector status, reasons and attention on llm_calls (item 043).

Existing rows are backfilled with the same classification rule new rows
use; their attention is ``none`` because the level is deliberately frozen
at write time and a backfill must not turn old history into new work.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-25
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import sqlalchemy as sa

from alembic import op
from app.agent.reasons import classify

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(conn: sa.Connection) -> set[str]:
    return {row[1] for row in conn.execute(sa.text("PRAGMA table_info(llm_calls)"))}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _columns(conn)
    if "outcome_status" not in cols:
        op.add_column(
            "llm_calls",
            sa.Column("outcome_status", sa.String(), nullable=False, server_default="success"),
        )
    if "reasons" not in cols:
        op.add_column(
            "llm_calls",
            sa.Column("reasons", sa.Text(), nullable=False, server_default="[]"),
        )
    if "attention" not in cols:
        op.add_column(
            "llm_calls",
            sa.Column("attention", sa.String(), nullable=False, server_default="none"),
        )

    # Backfill only rows still carrying the default empty classification;
    # running the revision body again therefore changes nothing.
    decisions = {
        str(r.id): (str(r.action), str(r.error) if r.error is not None else None)
        for r in conn.execute(sa.text("SELECT id, action, error FROM decisions"))
    }
    rows = conn.execute(
        sa.text(
            "SELECT id, at, cycle, account_id, pair, decision_id, feature, replay_of, "
            "model, prompt_version, status, attempts, latency_ms, input_tokens, "
            "output_tokens, cost_usd, cost_eur, request_id, request_json, response_raw, "
            "parsed, input_checks, output_checks, verdict, verdict_note, verdict_at "
            "FROM llm_calls WHERE reasons = '[]'"
        )
    ).all()
    for r in rows:
        final_action: str | None = None
        change_detail: str | None = None
        if r.decision_id and str(r.decision_id) in decisions:
            final_action, change_detail = decisions[str(r.decision_id)]
        elif r.status == "input_blocked":
            final_action = "HOLD"
        data = {name: getattr(r, name) for name in r._fields}
        data.update({"outcome_status": "success", "reasons": "[]", "attention": "none"})
        row = cast(Any, SimpleNamespace(**data))
        outcome, reasons = classify(row, final_action, 15, change_detail=change_detail)
        conn.execute(
            sa.text(
                "UPDATE llm_calls SET outcome_status = :outcome, reasons = :reasons, "
                "attention = 'none' WHERE id = :id"
            ),
            {
                "outcome": outcome,
                "reasons": json.dumps(reasons, separators=(",", ":"), ensure_ascii=False),
                "id": r.id,
            },
        )

    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_llm_calls_account_at_id "
            "ON llm_calls (account_id, at, id)"
        )
    )
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_llm_calls_outcome_at ON llm_calls (outcome_status, at)"
        )
    )
    conn.execute(
        sa.text("CREATE INDEX IF NOT EXISTS ix_llm_calls_feature_at ON llm_calls (feature, at)")
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_feature_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_outcome_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_account_at_id", table_name="llm_calls")
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_column("attention")
        batch.drop_column("reasons")
        batch.drop_column("outcome_status")
