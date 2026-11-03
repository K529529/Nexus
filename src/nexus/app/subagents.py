"""In-process tasks around Conversation; every participant uses the same Agent Loop."""

from __future__ import annotations

import asyncio
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

    async def event(self, kind: str, data: Json) -> None:
        assert self.root.events is not None
        await self.root.events(kind, data)
        self.changed.set()

    def owner(self, ident: str) -> Conversation:
        if ident == "root":
            return self.root
        conversation = self.agents[ident].conversation
        assert conversation is not None
        return conversation

    def children(self, parent: str) -> list[Agent]:
        return [a for a in self.agents.values() if a.parent == parent]

    async def spawn(self, parent: str, args: Json) -> Json:
        config = self.root.config.multi_agent
        if not config.enabled or parent != "root":
            return {"error": "delegation_disabled"}
        role = args.get("role")
        if role not in config.roles:
            return {"error": "invalid_role"}
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
        agent = Agent(uuid4().hex, parent, role, args["task"], args["scope"], 1,
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
            mcp_servers={},
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
            child.gateway.approver = owner.gateway.approver
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
        if not any(a.result is not None and not a.delivered for a in children):
            self.changed.clear()
            try:
                async with asyncio.timeout(max(0, min(seconds, 60))):
                    await self.changed.wait()
            except TimeoutError:
                pass
        values = []
        for agent in children:
            values.append(agent.public())
            if agent.task is not None and agent.task.done() and not agent.delivered:
                await self.event("agent_result_delivered", {
                    "agent_id": agent.id, "parent_id": parent,
                })
                agent.delivered = True
        return {"agents": values}

    async def finish(self, parent: str) -> None:
        children = self.children(parent)
        for agent in children:
            if agent.task is not None and not agent.task.done():
                agent.state = "cancelling"
                if agent.conversation is not None:
                    agent.task.cancel()
        for agent in children:
            if agent.task is not None:
                await agent.task
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

        return {
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
        }


def role_prompt(role: str) -> str:
    return (Path(__file__).with_name("roles") / f"{role}.md").read_text(encoding="utf-8")
