"""Role-separated evidence for the compactor; never changes main-agent messages."""

from __future__ import annotations

import json

from nexus.core.history import groups
from nexus.core.types import Json, Message, Session


def serialize_history(session: Session, messages: list[Message]) -> list[Json]:
    records: list[Json] = []
    for group in groups([m for m in messages if m.role != "system"]):
        message = group[0]
        record: Json = {
            "seq": message.seq,
            "run_id": message.run_id,
            "kind": "USER_TASK_INFORMATION" if message.role == "user" else "ASSISTANT_CLAIM",
            "text": message.content,
        }
        if message.tool_calls:
            record["tool_calls"] = [
                {"call_id": c.id, "name": c.name, "arguments_json": c.arguments_json}
                for c in message.tool_calls
            ]
        records.append(record)
        for call, result in zip(message.tool_calls, group[1:], strict=True):
            tool: Json = {
                "seq": result.seq,
                "run_id": result.run_id,
                "kind": "TOOL_EXECUTION_EVIDENCE",
                "call_seq": message.seq,
                "call_id": call.id,
                "tool_name": call.name,
            }
            try:
                body = json.loads(result.content)
            except ValueError:
                body = None
            if (
                isinstance(body, dict)
                and body.get("call_id") == call.id
                and type(body.get("ok")) is bool
                and isinstance(body.get("data"), dict)
            ):
                data = body["data"]
                tool["execution"] = {
                    "ok": body["ok"],
                    "error_code": body.get("error_code"),
                    "truncated": body.get("truncated"),
                    "exit_code": data.get("exit_code"),
                }
                # Preserve errors, stdout/stderr, diffs and truncation metadata in full.
                # No command-name guessing, test verdict inference or arbitrary clipping.
                tool["untrusted_payload"] = data
                if call.name == "apply_patch":
                    tool["reported_file_operations"] = (
                        [
                            {
                                k: f[k]
                                for k in ("path", "status", "before_hash", "after_hash")
                                if k in f
                            }
                            for f in data.get("files", [])
                            if isinstance(f, dict)
                        ]
                        if isinstance(data.get("files", []), list)
                        else []
                    )
            else:
                tool["execution"] = {"status": "UNKNOWN"}
                tool["untrusted_payload"] = result.content
            records.append(tool)
    return records


def runtime_outcomes(session: Session, messages: list[Message]) -> list[Json]:
    runs = dict.fromkeys(m.run_id for m in messages if m.run_id)
    return [
        {
            "run_id": run,
            "kind": "RUNTIME_LIFECYCLE_ONLY",
            "outcome": session.run_outcomes.get(run, {}).get("outcome", "unknown"),
            "reason": session.run_outcomes.get(run, {}).get("reason"),
            "covered_source_seqs": [m.seq for m in messages if m.run_id == run],
            "scope": "run outcome may cover messages outside this summarized prefix",
        }
        for run in runs
    ]
