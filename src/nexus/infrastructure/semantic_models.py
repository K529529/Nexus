"""Alembic-owned semantic tables, separate from checkpoint persistence."""

from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from nexus.infrastructure.persistence.models import Base


class SemanticIndexRow(Base):
    __tablename__ = "repository_semantic_indexes"
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repositories.id"),
        primary_key=True,
    )
    embedding_provider: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(Text)
    embedding_dimension: Mapped[int]
    index_version: Mapped[str] = mapped_column(Text)
    chunk_strategy_version: Mapped[str] = mapped_column(Text)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SemanticChunkRow(Base):
    __tablename__ = "semantic_code_chunks"
    __table_args__ = (
        CheckConstraint("start_line >= 1", name="ck_semantic_start_line"),
        CheckConstraint("end_line >= start_line", name="ck_semantic_end_line"),
        UniqueConstraint(
            "repository_id", "file_path", "start_line", "end_line", name="uq_semantic_file_window"
        ),
        Index("ix_semantic_repository", "repository_id"),
        Index("ix_semantic_repository_file", "repository_id", "file_path"),
        Index("ix_semantic_repository_hash", "repository_id", "content_hash"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    repository_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repositories.id"),
    )
    file_path: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int]
    end_line: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    file_hash: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(VECTOR())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
