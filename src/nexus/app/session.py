"""Append-only local sessions. Recovery restores messages, never executions."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from nexus import __version__
from nexus.core.context import ContextBuilder
from nexus.core.types import Json, Message, ModelError, RuntimeEvent, Session, ToolResult, json_text


class SessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def workspace_path(workspace: Path) -> str:
    return os.path.normcase(str(workspace.resolve()))


def session_directory(workspace: Path, home: Path | None = None) -> Path:
    key = hashlib.sha256(workspace_path(workspace).encode()).hexdigest()[:24]
    return (home or Path.home() / ".nexus") / "sessions" / key


def _lock(stream: BinaryIO) -> None:
    stream.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SessionError("Session already has a writer; close it before resuming.") from None


def read_records(stream: BinaryIO) -> tuple[list[Json], bool]:
    stream.seek(0)
    lines = stream.readlines()
    records: list[Json] = []
    truncated = False
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            if index == len(lines) - 1:
                truncated = True
                break
            raise SessionError(f"Corrupt session record at line {index + 1}") from None
        if not line.endswith(b"\n"):
            truncated = True
            break
        if (
            not isinstance(row, dict)
            or type(row.get("schema_version")) is not int
            or row.get("schema_version") != 1
            or type(row.get("seq")) is not int
            or row["seq"] != index + 1
            or not isinstance(row.get("data"), dict)
            or not isinstance(row.get("kind"), str)
        ):
            raise SessionError(f"Invalid schema/sequence at line {index + 1}")
        if records and row.get("session_id") != records[0].get("session_id"):
            raise SessionError("Session identity changed inside file")
        records.append(row)
    if not records or records[0]["kind"] != "session_created":
        raise SessionError("Missing valid session header")
    return records, truncated


def replay(records: list[Json], workspace: Path) -> Session:
    header = records[0]
    if header["data"].get("workspace") != workspace_path(workspace):
        raise SessionError("Session belongs to another workspace")
    session = Session(workspace.resolve(), header["session_id"])
    try:
        for record in records[1:]:
            data = record["data"]
            run_id = record.get("run_id")
            if run_id is not None and (not isinstance(run_id, str) or not run_id):
                raise SessionError("Invalid run id")
            if record["kind"] == "run_started":
                session.run_id = session.resume_run_id = run_id
            elif record["kind"] == "run_finished" and run_id == session.run_id:
                if data.get("outcome") == "completed":
                    session.resume_run_id = None
            elif record["kind"] == "message":
                if not isinstance(data.get("content", ""), str):
                    raise SessionError("Invalid message content")
                if record.get("protocol_data") is not None and not isinstance(
                    record["protocol_data"], dict
                ):
                    raise SessionError("Invalid private continuation data")
                session.messages.append(
                    Message.from_data(
                        data,
                        seq=record["seq"],
                        protocol_data=record.get("protocol_data"),
                        # Older recovery records omitted the envelope run id.
                        run_id=run_id or session.run_id,
                    )
                )
            elif record["kind"] == "instructions":
                session.messages = [Message("system", data["content"], seq=record["seq"])] + [
                    m for m in session.messages if m.role != "system"
                ]
            elif record["kind"] == "context_compacted" and data.get("scope") == "run":
                if run_id is None or run_id != session.run_id:
                    raise SessionError("Invalid compaction run")
                ContextBuilder().restore_compaction(session, run_id, data, record["seq"])
            # Legacy unscoped compactions remain audit-only: their summaries may mix runs.
    except (KeyError, TypeError, ValueError, ModelError):
        raise SessionError("Invalid message/run record") from None
    # Validate complete groups; only an unfinished final batch may lack results.
    expected: list[str] = []
    expected_run: str | None = None
    for message in session.messages:
        if message.role not in {"system", "user", "assistant", "tool"}:
            raise SessionError("Invalid message role")
        if message.role == "tool":
            if (
                not expected
                or message.tool_call_id != expected.pop(0)
                or message.run_id != expected_run
            ):
                raise SessionError("Invalid tool result pairing")
        else:
            if expected:
                raise SessionError("Unpaired tool call before subsequent conversation")
            if message.tool_calls:
                expected = [call.id for call in message.tool_calls]
                expected_run = message.run_id
                if (
                    message.role != "assistant"
                    or not all(expected)
                    or len(set(expected)) != len(expected)
                ):
                    raise SessionError("Invalid tool call group")
    return session


class SessionLog:
    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = path
        self.seq = 0
        self.failed = False
        flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0)
        descriptor = os.open(path, flags, 0o600)
        self.stream = os.fdopen(descriptor, "r+b")
        try:
            _lock(self.stream)
        except BaseException:
            self.stream.close()
            raise

    @classmethod
    def create(
        cls,
        session: Session,
        title: str,
        model: Json,
        home: Path | None = None,
        *,
        recovered_from: str | None = None,
    ) -> SessionLog:
        folder = session_directory(session.workspace, home)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        writer = cls(folder / f"{session.session_id}.jsonl", create=True)
        try:
            writer.append(
                RuntimeEvent(
                    "session_created",
                    now(),
                    session.session_id,
                    None,
                    {
                        "workspace": workspace_path(session.workspace),
                        "title": title[:80],
                        "nexus_version": __version__,
                        "model": model,
                        "recovered_from": recovered_from,
                    },
                )
            )
        except BaseException:
            writer.close()
            raise
        return writer

    def append(self, event: RuntimeEvent, protocol_data: Json | None = None) -> int:
        if self.failed:
            raise SessionError("Session writer failed; no further side effects may be dispatched")
        row = {"schema_version": 1, "seq": self.seq + 1, **asdict(event)}
        if protocol_data is not None:
            if event.kind != "message":
                raise SessionError("Private continuation data belongs only to a message")
            row["protocol_data"] = protocol_data
        try:
            self.stream.seek(0, os.SEEK_END)
            self.stream.write((json_text(row) + "\n").encode("utf-8"))
            self.stream.flush()
        except (OSError, ValueError):
            self.failed = True
            raise SessionError("Session write/flush failed; recovery may be incomplete") from None
        self.seq += 1
        return self.seq

    def close(self) -> None:
        self.stream.close()  # OS file locks release on close/process death.


def resume_session(
    path: Path, workspace: Path, home: Path | None = None
) -> tuple[Session, SessionLog, list[str]]:
    writer = SessionLog(path)
    warnings: list[str] = []
    try:
        records, truncated = read_records(writer.stream)
        session = replay(records, workspace)
        writer.seq = records[-1]["seq"]
        if truncated:
            writer.close()
            prior = session.session_id
            fresh = Session(
                workspace.resolve(),
                messages=session.messages,
                run_id=session.run_id,
                resume_run_id=session.resume_run_id,
                compactions=session.compactions,
            )
            writer = SessionLog.create(
                fresh,
                records[0]["data"]["title"],
                records[0]["data"].get("model", {}),
                home,
                recovered_from=prior,
            )
            session = fresh
            # Copy every valid event, preserving seq and run boundaries. Only the
            # session identity/header change; references still address the same seq.
            for record in records[1:]:
                writer.append(
                    RuntimeEvent(
                        record["kind"],
                        record["timestamp"],
                        session.session_id,
                        record.get("run_id"),
                        record["data"],
                    ),
                    record.get("protocol_data"),
                )
            warnings.append("Truncated tail recovered into a new session; original file preserved.")
        pending: list[str] = []
        pending_run: str | None = None
        for message in session.messages:
            if message.tool_calls:
                pending = [call.id for call in message.tool_calls]
                pending_run = message.run_id
            elif message.role == "tool" and pending:
                pending.pop(0)
        for call_id in pending:
            message = ToolResult(
                call_id,
                False,
                {
                    "detail": "Interrupted; effects unknown. Inspect actual state before retry.",
                },
                "interrupted_unknown",
            ).message()
            message.run_id = pending_run
            message.seq = writer.append(
                RuntimeEvent("message", now(), session.session_id, pending_run, message.public())
            )
            session.messages.append(message)
        if pending:
            warnings.append("Interrupted tool calls recorded as unknown; no calls were replayed.")
        if records[-1]["kind"] != "run_finished":
            writer.append(
                RuntimeEvent(
                    "warning",
                    now(),
                    session.session_id,
                    None,
                    {"detail": "Resumed interrupted/idle session; awaiting input."},
                )
            )
        return session, writer, warnings
    except BaseException:
        writer.close()
        raise


def list_sessions(workspace: Path, home: Path | None = None) -> list[Json]:
    items: list[Json] = []
    for path in session_directory(workspace, home).glob("*.jsonl"):
        try:
            with path.open("rb") as stream:
                records, truncated = read_records(stream)
            header = records[0]["data"]
            if header.get("workspace") != workspace_path(workspace):
                continue
            last_run = next(
                (
                    r
                    for r in reversed(records)
                    if r["kind"]
                    in {
                        "run_started",
                        "run_finished",
                    }
                ),
                None,
            )
            status = "interrupted"
            if not truncated and last_run and last_run["kind"] == "run_finished":
                status = last_run["data"]["outcome"]
            items.append(
                {
                    "path": str(path),
                    "title": header["title"],
                    "project": workspace.name,
                    "updated": records[-1]["timestamp"],
                    "status": status,
                }
            )
        except (SessionError, OSError, KeyError):
            # Keep damaged/busy files visible; selection gives the exact diagnostic.
            items.append(
                {
                    "path": str(path),
                    "title": path.stem,
                    "project": workspace.name,
                    "updated": "",
                    "status": "unreadable/busy",
                }
            )
    return sorted(items, key=lambda item: item["updated"], reverse=True)
