"""Create Nexus-owned Day 3 approval persistence.

Revision ID: day03_0002
Revises: day02_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "day03_0002"
down_revision: str | None = "day02_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("approval_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("resource_or_command_summary", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "risk_level IN ('SAFE', 'WRITE', 'DANGEROUS')",
            name="ck_approvals_risk_level_day3",
        ),
        sa.CheckConstraint(
            "decision IN ('PENDING', 'APPROVED', 'DENIED')",
            name="ck_approvals_decision_day3",
        ),
        sa.CheckConstraint(
            "(decision = 'PENDING' AND decided_at IS NULL) OR "
            "(decision IN ('APPROVED', 'DENIED') AND decided_at IS NOT NULL)",
            name="ck_approvals_decided_at_day3",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index(
        "ix_approvals_run_created",
        "approvals",
        ["run_id", "created_at", "approval_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_run_created", table_name="approvals")
    op.drop_table("approvals")
