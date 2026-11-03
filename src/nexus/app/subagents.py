"""In-process tasks around Conversation; every participant uses the same Agent Loop."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    RunResult,
    RuntimeEvent,
    Tool,
    ToolResult,
    ToolSpec,
)

if TYPE_CHECKING:
    from nexus.app.bootstrap import Conversation
    from nexus.app.config import Config

ROLE_CHILDREN = {"worker": {"scout", "worker", "reviewer"},
                 "scout": {"scout"}, "reviewer": {"scout", "reviewer"}}

DELEGATION = """
[Native collaboration]
Delegate only when useful. spawn_agent starts an independent fresh conversation
asynchronously. Supply goal, background, deliverables, constraints and file scope.
You and your children share one workspace. Assign disjoint files; never modify a
file currently assigned to another executor. This is a cooperation rule, not a lock.
Use wait_agents to retrieve results (timeout leaves children running), or continue
your own independent work. Collect relevant results before finalizing; remaining
children will be cancelled and cleaned up. A tool/test failure is an observation.
"""


@dataclass
class Agent:
    id: str
    parent: str
    role: str
    assignment: str
    scope: str
    depth: int
    model: str
    reasoning_effort: str | None
    state: str = "starting"
    operation: str = ""
    started: float = 0
    elapsed_ms: int = 0
    conversation: Conversation | None = None
    task: asyncio.Task[None] | None = None
    result: RunResult | None = None
    delivered: bool = False

    def public(self) -> Json:
        return {
            "agent_id": self.id, "parent_id": self.parent, "role": self.role,
            "assignment": self.assignment, "scope": self.scope, "depth": self.depth,
            "model": self.model, "reasoning_effort": self.reasoning_effort,
            "state": self.state, "operation": self.operation,
            "elapsed_ms": int((time.monotonic() - self.started) * 1000)
            if self.task is not None and not self.task.done() else self.elapsed_ms,
            "result": asdict(self.result) if self.result is not None else None,
        }


class Subagents:
    def __init__(self, root: Conversation) -> None:
        self.root = root
        self.agents: dict[str, Agent] = {}
        self.changed = asyncio.Event()
        self.wakes: dict[str, asyncio.Event] = {"root": asyncio.Event()}
        self.decisions: dict[str, asyncio.Future[str]] = {}
        self.approval_lock = asyncio.Lock()
        root.gateway.approval_lock = self.approval_lock

    async def event(self, kind: str, data: Json) -> None:
        assert self.root.events is not None
        await self.root.events(kind, data)
        self.changed.set()
        parent = data.get("parent_id", "root")
        self.wakes.setdefault(parent, asyncio.Event()).set()

    def owner(self, ident: str) -> Conversation:
        if ident == "root":
            return self.root
        conversation = self.agents[ident].conversation
        assert conversation is not None
        return conversation

    def children(self, parent: str) -> list[Agent]:
        return [a for a in self.agents.values() if a.parent == parent]

    def can_delegate(self, parent: str) -> bool:
        return self.root.config.multi_agent.enabled and (
            parent == "root" or self.root.config.multi_agent.allowNestedSubagents
            and self.agents[parent].depth < 2
        )

    async def spawn(self, parent: str, args: Json) -> Json:
        config = self.root.config.multi_agent
        if not self.can_delegate(parent):
            return {"error": "delegation_disabled"}
        role = args.get("role")
        if role not in config.roles:
            return {"error": "invalid_role"}
        if parent != "root" and role not in ROLE_CHILDREN[self.agents[parent].role]:
            return {"error": "role_delegation_denied"}
        if any(not isinstance(args.get(k), str) or not args[k].strip() for k in ("task", "scope")):
            return {"error": "task_and_scope_required"}
        if sum(a.task is None or not a.task.done() or a.state == "cleanup_failed"
               for a in self.agents.values()) >= config.max_agents:
            return {"error": "capacity_full", "max_agents": config.max_agents}
        owner = self.owner(parent)
        if owner.session.unresolved_effects:
            return {"error": "unresolved_workspace_effects"}
        settings = config.roles[role]
        selected = (owner.default_model if settings.model == "default"
                    else owner.config.models[settings.model] if settings.model
                    else owner.config.model)
        model = replace(selected, reasoning_effort=settings.reasoning_effort)
        depth = 1 if parent == "root" else self.agents[parent].depth + 1
        agent = Agent(uuid4().hex, parent, role, args["task"], args["scope"], depth,
                      model.name, model.reasoning_effort, started=time.monotonic())
        self.agents[agent.id] = agent  # Reserve capacity before the first await.
        try:
            await self.event("agent_created", agent.public())
        except BaseException:
            self.agents.pop(agent.id)
            raise
        child_config = replace(
            owner.config, model=model,
            limits=replace(owner.config.limits, context_window=model.context_window,
                           max_output_tokens=model.max_output_tokens),
            mcp_servers={name: entry for name, entry in owner.config.mcp_servers.items()
                         if name in settings.mcp_servers},
        )
        agent.task = asyncio.create_task(self.execute(agent, child_config))
        return {"agent_id": agent.id, "state": agent.state}

    async def execute(self, agent: Agent, config: Config) -> None:
        from nexus.app.bootstrap import Conversation
        owner = self.owner(agent.parent)
        child: Conversation | None = None
        try:
            if agent.state == "cancelling":
                raise asyncio.CancelledError
            async def consume(event: RuntimeEvent) -> None:
                if event.kind == "tool_started":
                    agent.operation = str(event.data.get("name", ""))
                elif event.kind == "model_started":
                    agent.operation = "model"
                await self.root.consumer(RuntimeEvent(
                    "subagent_event", event.timestamp, event.session_id, event.run_id,
                    {"agent_id": agent.id, "kind": event.kind, "data": event.data},
                ))

            child = Conversation(
                owner.workspace, config, consume, home=owner.home,
                skill_directory=owner.catalog.root if owner.catalog else None,
                coordinator=self, agent_id=agent.id,
            )
            agent.conversation = child
            child.session.unresolved_effects = self.root.session.unresolved_effects
            from nexus.core.security import ApprovalDecision, ApprovalRequest

            async def approve(request: ApprovalRequest) -> ApprovalDecision:
                approver = self.root.gateway.approver
                if approver is None:
                    return ApprovalDecision(request.request_id, "deny")
                return await approver(request)

            child.gateway.approver = approve
            child.gateway.approval_lock = self.approval_lock
            agent.state = "running"
            await self.event("agent_state", agent.public())
            agent.result = await child.turn(agent.assignment + "\n\nAssigned scope: " + agent.scope)
        except asyncio.CancelledError:
            agent.result = RunResult("aborted", reason="cancelled_before_start")
        except Exception as exc:
            agent.result = RunResult("failed", reason=f"runtime_error:{type(exc).__name__}")
        finally:
            try:
                if child is not None:
                    await child.close()
                if agent.result is not None and (
                    "cleanup_incomplete" in (agent.result.reason or "")
                ):
                    agent.state = "cleanup_failed"
                else:
                    agent.state = agent.result.outcome if agent.result else "failed"
            except Exception:
                agent.state = "cleanup_failed"
            agent.elapsed_ms = int((time.monotonic() - agent.started) * 1000)
            agent.operation = ""
            await self.event("agent_state", agent.public())

    async def wait(self, parent: str, seconds: float) -> Json:
        children = self.children(parent)
        inbox = self.owner(parent).inbox
        wake = self.wakes.setdefault(parent, asyncio.Event())
        if children and not inbox.guides() and not any(
            a.result is not None and not a.delivered or a.state == "waiting_decision"
            for a in children
        ):
            wake.clear()
            inbox.changed.clear()
            watchers = [asyncio.create_task(wake.wait()), asyncio.create_task(inbox.changed.wait())]
            try:
                async with asyncio.timeout(max(0, min(seconds, 60))):
                    await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            except TimeoutError:
                pass
            finally:
                for watcher in watchers:
                    watcher.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
        values = []
        for agent in children:
            values.append(agent.public())
            if agent.task is not None and agent.task.done() and not agent.delivered:
                await self.event("agent_result_delivered", {
                    "agent_id": agent.id, "parent_id": parent,
                })
                agent.delivered = True
        return {"agents": values, "guidance_pending": bool(inbox.guides())}

    async def message(self, parent: str, target: str, text: str) -> Json:
        agent = self.agents.get(target)
        if agent is None or agent.parent != parent:
            return {"error": "not_direct_child"}
        child = agent.conversation
        if child is None or child.closed:
            return {"error": "agent_not_running"}
        await self.event("agent_message", {"agent_id": target, "parent_id": parent,
                                           "direction": "to_child", "text": text})
        decision = self.decisions.get(target)
        if decision is not None and not decision.done():
            decision.set_result(text)
        else:
            assert child.events is not None
            await child.inbox.receive(text, "guide", child.events)
        return {"received": True}

    async def report(self, sender: str, text: str, request_decision: bool) -> Json:
        agent = self.agents[sender]
        owner = self.owner(agent.parent)
        assert owner.events is not None
        future = asyncio.get_running_loop().create_future() if request_decision else None
        if future is not None:
            self.decisions[sender] = future
            agent.state = "waiting_decision"
        try:
            await self.event("agent_message", {"agent_id": sender, "parent_id": agent.parent,
                "direction": "to_parent", "text": text, "request_decision": request_decision})
            await owner.inbox.receive(
                f"[Child {sender} "
                f"{'requests decision' if request_decision else 'progress'}]\n{text}",
                "guide", owner.events,
            )
            if future is None:
                return {"received": True}
            answer = await future
            return {"answer": answer}
        finally:
            self.decisions.pop(sender, None)
            if agent.state == "waiting_decision":
                agent.state = "running"

    async def cancel(self, parent: str, target: str) -> Json:
        agent = self.agents.get(target)
        if agent is None or agent.parent != parent:
            return {"error": "not_direct_child"}
        if agent.task is not None and not agent.task.done():
            cancelling = agent.state == "cancelling"
            agent.state = "cancelling"
            await self.event("agent_state", agent.public())
            if agent.conversation is not None and not cancelling:
                agent.task.cancel()
            await asyncio.shield(agent.task)
        return agent.public()

    async def prepare(self, ident: str) -> None:
        owner = self.owner(ident)
        if ident == "root":
            return
        live_servers = {t.server for t in self.root.registry.values() if t.server is not None}
        removed = set()
        for name, tool in list(owner.registry.items()):
            if tool.server is not None and tool.server not in live_servers:
                owner.registry.pop(name)
                removed.add(tool.server)
        if removed:
            assert owner.events is not None
            await owner.inbox.receive(
                "[Environment fact] MCP servers unavailable: " + ", ".join(sorted(removed)),
                "guide", owner.events,
            )

    def inherited_mcp(self, parent: str, allowed: set[str]) -> dict[str, Tool]:
        owner = self.owner(parent)
        # Adapter calls receive the invoking agent's ExecutionContext and Emit;
        # only the connection, per-server lock and failure state are shared.
        return {name: replace(tool) for name, tool in owner.registry.items()
                if tool.server is not None and tool.server in allowed}

    async def finish(self, parent: str) -> None:
        children = self.children(parent)
        for agent in children:
            if agent.task is not None and not agent.task.done():
                cancelling = agent.state == "cancelling"
                agent.state = "cancelling"
                if agent.conversation is not None and not cancelling:
                    agent.task.cancel()
        for agent in children:
            if agent.task is not None:
                await asyncio.shield(agent.task)
        if any(a.state == "cleanup_failed" for a in children):
            raise RuntimeError("subagent_cleanup_incomplete")

    def tools(self, parent: str) -> dict[str, Tool]:
        async def spawn(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            value = await self.spawn(parent, args)
            return ToolResult(context.call_id, "error" not in value, value, value.get("error"))

        async def wait(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            value = await self.wait(parent, float(args.get("timeout_seconds", 30)))
            return ToolResult(context.call_id, True, value)

        async def listing(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            return ToolResult(context.call_id, True, {"agents": [
                a.public() for a in self.children(parent)
            ]})

        async def message(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            value = await self.message(parent, args["agent_id"], args["message"])
            return ToolResult(context.call_id, "error" not in value, value, value.get("error"))

        async def report(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            value = await self.report(parent, args["message"], args.get("request_decision", False))
            return ToolResult(context.call_id, True, value)

        async def cancel(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            value = await self.cancel(parent, args["agent_id"])
            return ToolResult(context.call_id, "error" not in value, value, value.get("error"))

        tools = {
            "spawn_agent": Tool(ToolSpec("spawn_agent", "Delegate a fresh asynchronous task. "
                "Specify background, goal, deliverables, constraints and exclusive file scope.", {
                    "type": "object", "properties": {
                        "role": {"type": "string", "enum": ["scout", "worker", "reviewer"]},
                        "task": {"type": "string"}, "scope": {"type": "string"},
                    }, "required": ["role", "task", "scope"], "additionalProperties": False,
                }), spawn, "internal"),
            "wait_agents": Tool(ToolSpec("wait_agents", "Wait for direct children, at most 60s. "
                "Timeout returns status and leaves execution running.", {
                    "type": "object", "properties": {
                        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60},
                    }, "additionalProperties": False,
                }), wait, "internal"),
            "list_agents": Tool(ToolSpec("list_agents", "Inspect direct child states/results.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }), listing, "internal"),
            "message_agent": Tool(ToolSpec("message_agent", "Guide or answer a direct child.", {
                "type": "object", "properties": {"agent_id": {"type": "string"},
                                                   "message": {"type": "string"}},
                "required": ["agent_id", "message"], "additionalProperties": False,
            }), message, "internal"),
            "cancel_agent": Tool(ToolSpec("cancel_agent", "Cancel a child and its descendants; "
                "wait for cleanup before reassigning files.", {
                    "type": "object", "properties": {"agent_id": {"type": "string"}},
                    "required": ["agent_id"], "additionalProperties": False,
                }), cancel, "internal"),
        }
        if not self.can_delegate(parent):
            tools.pop("spawn_agent")
        if parent != "root":
            tools["report_parent"] = Tool(ToolSpec("report_parent", "Send progress to parent, "
                "or request a decision and wait for its reply (still consumes capacity).", {
                    "type": "object", "properties": {"message": {"type": "string"},
                                                       "request_decision": {"type": "boolean"}},
                    "required": ["message"], "additionalProperties": False,
                }), report, "internal")
        def guarded(tool: Tool) -> Tool:
            async def execute(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
                if not self.root.config.multi_agent.enabled:
                    return ToolResult(context.call_id, False, {}, "delegation_disabled")
                schema = tool.spec.input_schema
                properties = schema["properties"]
                valid = not (args.keys() - properties.keys()) and all(
                    key in args for key in schema.get("required", [])
                )
                for key, value in args.items():
                    prop = properties.get(key, {})
                    kind = prop.get("type")
                    if kind == "string":
                        valid &= isinstance(value, str) and bool(value.strip())
                    elif kind == "boolean":
                        valid &= type(value) is bool
                    elif kind == "number":
                        valid &= (type(value) in {int, float} and math.isfinite(value)
                                  and prop.get("minimum", 0) <= value <= prop.get("maximum", 60))
                    if "enum" in prop:
                        valid &= value in prop["enum"]
                if not valid:
                    return ToolResult(context.call_id, False, {}, "invalid_arguments")
                return await tool.execute(args, context, emit)
            return replace(tool, execute=execute)
        return {name: guarded(tool) for name, tool in tools.items()}


def role_prompt(role: str) -> str:
    return (Path(__file__).with_name("roles") / f"{role}.md").read_text(encoding="utf-8")
