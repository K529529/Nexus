"""Private SQLAlchemy rows for the business schema through Day 3."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RepositoryRow(Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    configuration_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_repository_last_active", "repository_id", "last_active_at"),
    )

    session_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    repository_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("repositories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    configuration_reference: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'INTERRUPTED', 'COMPLETED', 'FAILED')",
            name="ck_runs_status_day2",
        ),
        Index("ix_runs_session_status_started", "session_id", "status", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    task: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    model_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    changed_file_refs: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    final_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    graph_thread_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class SessionTurnRow(Base):
    __tablename__ = "session_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_session_turns_session_sequence"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_payload: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "risk_level IN ('SAFE', 'WRITE', 'DANGEROUS')",
            name="ck_approvals_risk_level_day3",
        ),
        CheckConstraint(
            "decision IN ('PENDING', 'APPROVED', 'DENIED')",
            name="ck_approvals_decision_day3",
        ),
        CheckConstraint(
            "(decision = 'PENDING' AND decided_at IS NULL) OR "
            "(decision IN ('APPROVED', 'DENIED') AND decided_at IS NOT NULL)",
            name="ck_approvals_decided_at_day3",
        ),
        Index("ix_approvals_run_created", "run_id", "created_at", "approval_id"),
    )

    approval_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_or_command_summary: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
