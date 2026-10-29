"""Append-only local sessions with durable tool boundaries and conservative recovery."""

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
from nexus.core.durable import sync_directory
from nexus.core.memory_snapshot import validate_snapshot
from nexus.core.plan import plan_data, validate_plan
from nexus.core.skills import active_skills, check_active, restore_skill
from nexus.core.types import (
    Json,
    Message,
    ModelError,
    PlanState,
    RuntimeEvent,
    Session,
    json_text,
)


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
    committed_plans: set[tuple[str, str]] = set()
    committed_skills: set[tuple[str, str]] = set()
    started_tool: tuple[str | None, object, object] | None = None
    try:
        for record in records[1:]:
            data = record["data"]
            run_id = record.get("run_id")
            if run_id is not None and (not isinstance(run_id, str) or not run_id):
                raise SessionError("Invalid run id")
            if record["kind"] == "run_started":
                if run_id != session.run_id:
                    session.plan = None
                    session.loaded_skills = ()
                    session.skills_run_id = run_id
                session.run_id = session.resume_run_id = run_id
                started_tool = None
            elif record["kind"] == "run_finished" and run_id == session.run_id:
                if run_id is not None:
                    session.run_outcomes[run_id] = {
                        "outcome": data.get("outcome", "unknown"),
                        "reason": data.get("reason"),
                        "handoff": data.get("handoff"),
                    }
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
            elif record["kind"] == "memory_snapshot":
                session.memory_snapshot = validate_snapshot(data)
            elif record["kind"] == "tool_started":
                started_tool = (run_id, data.get("call_id"), data.get("name"))
            elif record["kind"] == "tool_finished":
                started_tool = None
            elif record["kind"] == "skill_selected":
                if run_id is not None or set(data) != {"skill"}:
                    raise SessionError("Invalid skill_selected scope/fields")
                session.selected_skill = (
                    restore_skill(data["skill"]) if data["skill"] is not None else None
                )
                check_active(active_skills(session))
            elif record["kind"] == "skill_loaded":
                call_id = data.get("call_id")
                if (
                    not run_id
                    or run_id != session.run_id
                    or run_id != session.resume_run_id
                    or not isinstance(call_id, str)
                    or not call_id
                    or type(data.get("step")) is not int
                    or data["step"] < 1
                    or started_tool != (run_id, call_id, "load_skill")
                    or (run_id, call_id) in committed_skills
                ):
                    raise SessionError("Invalid skill_loaded run/call/step")
                pending_calls = []
                for message in session.messages:
                    if message.run_id != run_id:
                        continue
                    if message.tool_calls:
                        pending_calls = list(message.tool_calls)
                    elif message.role == "tool" and pending_calls:
                        pending_calls.pop(0)
                if (
                    not pending_calls
                    or pending_calls[0].id != call_id
                    or pending_calls[0].name != "load_skill"
                ):
                    raise SessionError("Invalid skill_loaded tool association")
                value = restore_skill(data["skill"])
                if json.loads(pending_calls[0].arguments_json) != {"name": value.name}:
                    raise SessionError("Invalid skill_loaded arguments")
                if value.name in {s.name for s in active_skills(session)}:
                    raise SessionError("Duplicate skill_loaded activation")
                check_active((*active_skills(session), value))
                session.loaded_skills = (*session.loaded_skills, value)
                session.skills_run_id = run_id
                committed_skills.add((run_id, call_id))
            elif record["kind"] == "plan_updated":
                call_id = data.get("call_id")
                if (
                    run_id is None
                    or run_id != session.run_id
                    or run_id != session.resume_run_id
                    or not isinstance(call_id, str)
                    or not call_id
                    or type(data.get("step")) is not int
                    or data["step"] < 1
                    or "explanation" not in data
                    or started_tool != (run_id, call_id, "update_plan")
                    or (run_id, call_id) in committed_plans
                ):
                    raise SessionError("Invalid plan_updated run/call/step")
                # Match the next unpaired call in the current sequential batch.
                pending = []
                for message in session.messages:
                    if message.run_id != run_id:
                        continue
                    if message.tool_calls:
                        pending = list(message.tool_calls)
                    elif message.role == "tool" and pending:
                        pending.pop(0)
                if not pending or pending[0].id != call_id or pending[0].name != "update_plan":
                    raise SessionError("Invalid plan_updated tool association")
                try:
                    items, _ = validate_plan(
                        {"plan": data.get("plan"), "explanation": data["explanation"]}
                    )
                except ValueError as exc:
                    raise SessionError(f"Invalid plan_updated: {exc}") from None
                if plan_data(items) != data["plan"]:
                    raise SessionError("Invalid plan_updated: plan is not normalized")
                session.plan = PlanState(run_id, items)
                committed_plans.add((run_id, call_id))
            elif record["kind"] == "context_compacted" and data.get("scope") in {"run", "session"}:
                if data.get("scope") == "run" and (run_id is None or run_id != session.run_id):
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
        # Windows byte-range locks also deny readers. Keep writer ownership off the JSONL.
        descriptor = os.open(path.with_suffix(path.suffix + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self._lock_stream = os.fdopen(descriptor, "r+b")
        try:
            _lock(self._lock_stream)
            flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0)
            descriptor = os.open(path, flags, 0o600)
            self.stream = os.fdopen(descriptor, "r+b")
        except BaseException:
            self._lock_stream.close()
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
                        "recovery_requires_commit": recovered_from is not None,
                    },
                )
            )
            for directory in (folder, folder.parent, folder.parent.parent):
                sync_directory(directory)
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
            if event.kind in {
                "session_created", "tool_checkpoint", "patch_diff", "patch_committed",
                "patch_manifest", "tool_recovery", "approval_resolved",
                "tail_recovery_completed",
            } or (event.kind == "message" and event.data.get("role") == "tool"):
                os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            self.failed = True
            raise SessionError("Session write/flush/fsync failed; recovery incomplete") from None
        self.seq += 1
        return self.seq

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            self._lock_stream.close()  # OS file locks release on close/process death.
        # Keep the lock file: unlinking it can let writers lock different file objects.


def resume_session(
    path: Path, workspace: Path, home: Path | None = None
) -> tuple[Session, SessionLog, list[str]]:
    writer = SessionLog(path)
    source_writer: SessionLog | None = None
    warnings: list[str] = []
    try:
        records, truncated = read_records(writer.stream)
        prior_source = records[0]["data"].get("recovered_from")
        if prior_source and not any(
            r["kind"] == "tail_recovery_completed"
            and r["data"].get("source_session_id") == prior_source for r in records
        ):
            # Legacy recovery copies have no executable PREPARED checkpoint.
            if records[0]["data"].get("recovery_requires_commit") or any(
                r["kind"] == "tool_checkpoint" for r in records
            ):
                raise SessionError("Incomplete recovery copy; inspect before continuing")
        if truncated:
            # Keep the original writer lock while choosing/creating its unique
            # successor. Reopening the preserved torn source must not fork a second
            # PREPARED execution after the first successor already completed it.
            successors = []
            folders = {path.parent, session_directory(workspace, home)}
            for folder in folders:
                for candidate in folder.glob("*.jsonl"):
                    if candidate.resolve() == path.resolve():
                        continue
                    try:
                        with candidate.open("rb") as stream:
                            header = json.loads(stream.readline())
                    except (OSError, ValueError):
                        continue
                    if (isinstance(header, dict)
                            and header.get("data", {}).get("recovered_from")
                            == records[0]["session_id"]):
                        successors.append(candidate)
            if len(successors) > 1:
                raise SessionError("Multiple recovery copies; inspect before continuing")
            if successors:
                writer.close()
                return resume_session(successors[0], workspace, home)
        session = replay(records, workspace)
        writer.seq = records[-1]["seq"]
        if truncated:
            source_writer = writer
            prior = session.session_id
            fresh = Session(
                workspace.resolve(),
                messages=session.messages,
                run_id=session.run_id,
                resume_run_id=session.resume_run_id,
                checkpoint=session.checkpoint,
                run_outcomes=session.run_outcomes,
                plan=session.plan,
                selected_skill=session.selected_skill,
                loaded_skills=session.loaded_skills,
                skills_run_id=session.skills_run_id,
                memory_snapshot=session.memory_snapshot,
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
            writer.append(RuntimeEvent(
                "tail_recovery_completed", now(), session.session_id, None,
                {"source_session_id": prior},
            ))
            warnings.append("Truncated tail recovered into a new session; original file preserved.")
        from nexus.app.tool_recovery import recover_tools

        warnings.extend(recover_tools(records, session, writer))
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
    finally:
        if source_writer is not None:
            source_writer.close()


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
            status = "idle" if last_run is None and not truncated else "interrupted"
            if not truncated and last_run and last_run["kind"] == "run_finished":
                status = last_run["data"]["outcome"]
            items.append(
                {
                    "path": str(path),
                    "title": next(
                        (
                            r["data"]["content"][:100]
                            for r in reversed(records)
                            if r["kind"] == "message" and r["data"].get("role") == "user"
                        ),
                        header["title"],
                    ),
                    "session_id": records[0]["session_id"],
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
