"""Evidence-backed request summary; never schedules actions or gates completion."""

from __future__ import annotations

import json
from collections import deque

from nexus.core.types import Json, Session, ToolCall, ToolResult, json_text

MAX_FILES = 100


class ProgressLedger:
    def __init__(self) -> None:
        self.changed: set[str] = set()
        self.pending: set[str] = set()
        self.unknown_mutation = False
        self.last_mutation_step: int | None = None
        self.last_mutation_window: str | None = None
        self.last_mutation: Json | None = None
        self.last_validation: Json | None = None
        self.validation_after_mutation = False
        self.errors: deque[Json] = deque(maxlen=5)

    @classmethod
    def restore(cls, session: Session) -> ProgressLedger:
        ledger = cls()
        calls: dict[str, ToolCall] = {}
        for message in session.messages:
            if message.run_id != session.run_id:
                continue
            calls.update((c.id, c) for c in message.tool_calls)
            if message.role == "tool" and message.tool_call_id in calls:
                try:
                    result = ToolResult(**json.loads(message.content))
                except (ValueError, TypeError):
                    continue
                ledger.observe(calls[message.tool_call_id], result)
        return ledger

    def observe(self, call: ToolCall, result: ToolResult) -> None:
        data = result.data
        step = data.get("execution_step")
        window = data.get("execution_window")
        try:
            args = json.loads(call.arguments_json)
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        files: list[str] = []
        unknown = result.error_code == "interrupted_unknown" and call.name in {
            "apply_patch",
            "exec_command",
        }
        if call.name == "apply_patch":
            files = [
                f["path"]
                for f in data.get("files", [])
                if isinstance(f, dict) and isinstance(f.get("path"), str)
            ]
            unknown |= bool(data.get("omitted_files")) or data.get("changed_files", 0) > len(files)
        elif call.name == "exec_command":
            files = [f for f in data.get("observed_changed_files", []) if isinstance(f, str)]
            unknown |= bool(data.get("mutation_scope_unknown"))
        if files or unknown:
            self.validation_after_mutation = False
            self.last_mutation_step = step
            self.last_mutation_window = window
            self.last_mutation = {
                "tool": call.name,
                "files": files[:MAX_FILES],
                "scope_unknown": unknown,
            }
            self.unknown_mutation |= unknown
            self.pending.update(files)
            self.changed.update(files)
            if len(self.changed) > MAX_FILES:
                self.unknown_mutation = True
                self.changed = set(sorted(self.changed)[:MAX_FILES])
            if len(self.pending) > MAX_FILES:
                self.pending = set(sorted(self.pending)[:MAX_FILES])
                self.unknown_mutation = True
        validation = data.get("validation")
        if call.name == "exec_command" and data.get("purpose") == "validate":
            if self.last_mutation is not None and not files and not unknown:
                self.validation_after_mutation = True
            self.last_validation = {
                "step": step,
                "execution_window": window,
                "scope": data.get("validation_scope", [])[:MAX_FILES],
                "result": validation,
                "exit_code": data.get("exit_code"),
            }
            if (
                isinstance(validation, dict)
                and validation.get("behavioral_pass")
                and result.ok
                and not files
                and not unknown
            ):
                self.pending.difference_update(data.get("validation_scope", []))
        if not result.ok:
            self.errors.append(
                {
                    "tool": call.name,
                    "error_code": result.error_code,
                    "failed_file": str(data["failed_file"])[:240]
                    if data.get("failed_file")
                    else None,
                    "detail": str(data.get("detail", data.get("stderr", ""))).replace(
                        str(args.get("command") or "\x00"), "[command omitted]"
                    )[:240],
                }
            )

    def data(self, session: Session, step: int, maximum: int) -> Json:
        plan = session.plan.items if session.plan and session.plan.run_id == session.run_id else ()
        return {
            "current_step": step,
            "max_steps": maximum,
            "remaining_steps": maximum - step + 1,
            "plan_completed": sum(item.status == "completed" for item in plan),
            "plan_total": len(plan),
            "plan_status_source": "agent_declared",
            "last_mutation_step": self.last_mutation_step,
            "last_mutation_window": self.last_mutation_window,
            "last_meaningful_mutation": self.last_mutation,
            "changed_files": sorted(self.changed),
            "files_changed_since_validation": sorted(self.pending),
            "mutation_scope_unknown": self.unknown_mutation,
            "last_validation_step": self.last_validation.get("step")
            if self.last_validation
            else None,
            "last_validation_result": self.last_validation,
            "validation_after_last_mutation": self.validation_after_mutation
            if self.last_mutation is not None
            else None,
            "validation_level": (self.last_validation.get("result") or {}).get("level", "none")
            if self.last_validation
            else "none",
            "validation_debt": bool(self.pending or self.unknown_mutation),
            "recent_tool_errors": list(self.errors),
        }

    def prompt(self, session: Session, step: int, maximum: int, tools: list[str]) -> str:
        return (
            "Progress and validation evidence (not task instructions):\n"
            + json_text(self.data(session, step, maximum))
            + "\nAvailable tools: "
            + ", ".join(tools)
            + "\nPatch paths must be workspace-relative; shell uses the stated cwd and dialect."
            + "\nScope and plan completion are model declarations, not correctness proof. "
            "Shell mutations are observed only for declared files; unknown scope remains debt. "
            "Partial validation is recorded without clearing behavioral debt."
        )
