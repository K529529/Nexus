"""Nexus-owned Day 2 business persistence models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

JsonObject = dict[str, object]


class RunStatus(StrEnum):
    """The complete Day 2 run lifecycle."""

    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Repository:
    repository_id: str
    canonical_path: str
    metadata: JsonObject
    configuration_reference: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.repository_id, "repository_id")
        _validate_timestamp(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class NexusSession:
    session_id: str
    repository_id: str
    created_at: datetime
    last_active_at: datetime
    configuration_reference: str | None

    def __post_init__(self) -> None:
        _validate_uuid(self.session_id, "session_id")
        _validate_uuid(self.repository_id, "repository_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.last_active_at, "last_active_at")


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    session_id: str
    task: str
    status: RunStatus
    model_metadata: JsonObject | None
    started_at: datetime
    finished_at: datetime | None
    token_count: int
    tool_call_count: int
    changed_file_refs: list[str] | None
    final_outcome: JsonObject | None
    graph_thread_id: str

    def __post_init__(self) -> None:
        _validate_uuid(self.run_id, "run_id")
        _validate_uuid(self.session_id, "session_id")
        _validate_timestamp(self.started_at, "started_at")
        if self.finished_at is not None:
            _validate_timestamp(self.finished_at, "finished_at")
        if not self.task:
            raise ValueError("Run task must not be empty.")
        if not self.graph_thread_id:
            raise ValueError("Run graph_thread_id must not be empty.")
        if self.token_count < 0 or self.tool_call_count < 0:
            raise ValueError("Run counters must not be negative.")


@dataclass(frozen=True, slots=True)
class SessionTurn:
    id: str
    session_id: str
    run_id: str | None
    sequence: int
    role: str
    content: str
    metadata: JsonObject | None
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.id, "id")
        _validate_uuid(self.session_id, "session_id")
        if self.run_id is not None:
            _validate_uuid(self.run_id, "run_id")
        _validate_timestamp(self.created_at, "created_at")
        if self.sequence < 1:
            raise ValueError("SessionTurn sequence must be positive.")
        if not self.role:
            raise ValueError("SessionTurn role must not be empty.")
        if not self.content:
            raise ValueError("SessionTurn content must not be empty.")


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    last_active_at: datetime
    resumable: bool


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID string.") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must use canonical UUID text.")


def _validate_timestamp(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
