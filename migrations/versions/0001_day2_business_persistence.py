"""Create Nexus-owned Day 2 business persistence.

Revision ID: day02_0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "day02_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("configuration_reference", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_path"),
    )
    op.create_table(
        "sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("configuration_reference", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_sessions_repository_last_active",
        "sessions",
        ["repository_id", "last_active_at"],
        unique=False,
    )
    op.create_table(
        "runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("changed_file_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("final_outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("graph_thread_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')",
            name="ck_runs_status_day2",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("graph_thread_id"),
    )
    op.create_index(
        "ix_runs_session_status_started",
        "runs",
        ["session_id", "status", "started_at"],
        unique=False,
    )
    op.create_table(
        "session_turns",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_session_turns_session_sequence",
        ),
    )


def downgrade() -> None:
    op.drop_table("session_turns")
    op.drop_index("ix_runs_session_status_started", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_sessions_repository_last_active", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("repositories")
