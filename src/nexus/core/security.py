"""Deterministic tool dispatch. Approval never changes the execution environment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Literal, Protocol
from uuid import uuid4

from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Session,
    Tool,
    ToolCall,
    ToolCancelled,
    ToolResult,
    json_text,
)

Mode = Literal["local", "read-only", "workspace-write"]


@dataclass(frozen=True)
class SecurityPolicy:
    mode: Mode = "local"
    writable_roots: tuple[str, ...] = ()
    ask_tools: tuple[str, ...] = ()
    allow_host_memory: bool = False
    allow_trusted_skills: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"local", "read-only", "workspace-write"}:
            raise ValueError("Unknown execution mode")


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments_digest: str
    capability_scope: str
    expires_at: float
    arguments_preview: str = ""


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    choice: Literal["allow_once", "deny", "cancel"]


Approver = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]


class RestrictedBackend(Protocol):
    async def execute(
        self,
        name: str,
        arguments: Json,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult: ...


def digest(arguments: Json) -> str:
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class SecurityGateway:
    def __init__(
        self,
        policy: SecurityPolicy | None = None,
        *,
        backend: RestrictedBackend | None = None,
        approver: Approver | None = None,
        approval_timeout: float = 120,
    ) -> None:
        self.policy, self.backend, self.approver = policy or SecurityPolicy(), backend, approver
        self.approval_timeout = approval_timeout

    def route(self, tool: Tool, args: Json) -> tuple[str, str, str]:
        """Return decision, backend, capability. Names cannot confer trust."""
        cap = tool.capability
        if self.policy.mode == "local":
            return ("ask" if tool.spec.name in self.policy.ask_tools else "allow", "local", cap)
        if cap == "internal":
            route = "host-internal"
        elif cap == "skill-read" and self.policy.allow_trusted_skills:
            route = "host-skills"
        elif cap.startswith("memory-"):
            if cap == "memory-scratchpad":
                cap = "memory-read" if args.get("action") == "list" else "memory-write"
            if (
                not self.policy.allow_host_memory
                or cap == "memory-search"
                or (self.policy.mode == "read-only" and cap != "memory-read")
            ):
                return "deny", "none", cap
            return "ask", "host-memory", cap
        elif cap in {"shell", "patch"}:
            if cap == "patch" and self.policy.mode == "read-only":
                return "deny", "none", cap
            route = "restricted"
        else:
            return "deny", "none", cap
        return ("ask" if tool.spec.name in self.policy.ask_tools else "allow", route, cap)

    async def execute(
        self,
        call: ToolCall,
        registry: dict[str, Tool],
        session: Session,
        context: ExecutionContext,
        emit: Emit,
    ) -> ToolResult:
        def failed(code: str) -> ToolResult:
            return ToolResult(context.call_id, False, {"execution_state": "not_executed"}, code)

        try:
            args = json.loads(call.arguments_json)
            if not isinstance(args, dict):
                raise ValueError("object required")
            argument_digest = digest(args)
        except (ValueError, TypeError):
            await emit(
                "tool_policy",
                {
                    "call_id": call.id,
                    "decision": "deny",
                    "reason": "invalid_arguments",
                    "mode": self.policy.mode,
                },
            )
            return failed("invalid_arguments")
        tool = registry.get(call.name)
        decision, backend, cap = self.route(tool, args) if tool else ("deny", "none", "unknown")
        scope = json_text(
            {
                "capability": cap,
                "backend": backend,
                "mode": self.policy.mode,
                "workspace": str(context.workspace),
                "writable_roots": self.policy.writable_roots,
            }
        )
        facts = {
            "call_id": call.id,
            "tool_name": call.name,
            "mode": self.policy.mode,
            "backend": backend,
            "capability_scope": scope,
            "arguments_digest": argument_digest,
        }
        await emit("tool_policy", {**facts, "decision": decision})
        if tool is None:
            return failed("unknown_tool")
        if decision == "deny":
            return failed("policy_denied")
        if decision == "ask":
            request = ApprovalRequest(
                uuid4().hex,
                session.session_id,
                session.run_id or "",
                call.id,
                call.name,
                argument_digest,
                scope,
                time.monotonic() + self.approval_timeout,
                json_text(args),
            )
            # Arguments already exist in the tool-call record. Do not duplicate (or
            # truncate a credential across) an additional audit field.
            approval_facts = {
                key: value for key, value in asdict(request).items() if key != "arguments_preview"
            }
            await emit("approval_requested", approval_facts)
            choice = "unavailable"
            try:
                if self.approver is not None:
                    async with asyncio.timeout(self.approval_timeout):
                        answer = await self.approver(request)
                    choice = answer.choice if answer.request_id == request.request_id else "invalid"
                    if time.monotonic() >= request.expires_at:
                        choice = "expired"
                    # A suspended UI cannot authorize a mutated call or changed run.
                    if (
                        session.session_id != request.session_id
                        or session.run_id != request.run_id
                        or call.id != request.call_id
                        or call.name != request.tool_name
                        or digest(json.loads(call.arguments_json)) != argument_digest
                    ):
                        choice = "invalid"
            except TimeoutError:
                choice = "expired"
            except asyncio.CancelledError:
                await emit("approval_resolved", {**approval_facts, "choice": "cancel"})
                raise ToolCancelled(failed("approval_cancelled")) from None
            except Exception:
                choice = "unavailable"
            # Await durable sink before any side effect. No approval cache survives this call.
            await emit("approval_resolved", {**approval_facts, "choice": choice})
            if choice == "cancel":
                raise ToolCancelled(failed("approval_cancelled"))
            if choice != "allow_once":
                return failed("approval_" + ("denied" if choice == "deny" else choice))
        await emit("tool_execution", {**facts, "state": "dispatching"})
        try:
            if backend == "restricted":
                value = (
                    await self.backend.execute(call.name, args, context, emit)
                    if self.backend is not None
                    else failed("backend_unavailable")
                )
            else:
                value = await tool.execute(args, context, emit)
        except ToolCancelled as exc:
            await emit(
                "tool_execution",
                {**facts, "state": "cancelled", "error_code": exc.result.error_code},
            )
            raise
        except BaseException:
            await emit("tool_execution", {**facts, "state": "interrupted_unknown"})
            raise
        await emit(
            "tool_execution",
            {**facts, "state": "finished", "ok": value.ok, "error_code": value.error_code},
        )
        return value
