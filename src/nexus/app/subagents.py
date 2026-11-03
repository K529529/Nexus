"""In-process tasks around Conversation; every participant uses the same Agent Loop."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from rich.cells import cell_len

from nexus.app.agent_view import AgentUsage, combined_usage, usage_label
from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Message,
    RunResult,
    RuntimeEvent,
    Tool,
    ToolResult,
    ToolSpec,
    Usage,
    json_text,
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
Supply task_name: a short, descriptive task title in the user's language.
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
    log_path: str = ""
    source_call_id: str = ""
    source_run_id: str | None = None
    model_settings: Json | None = None
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    accepting: bool = False
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task_name: str = ""
    task_run_id: str | None = None
    run_ids: set[str] = field(default_factory=set)

    def public(self) -> Json:
        return {
            "agent_id": self.id, "parent_id": self.parent, "role": self.role,
            "assignment": self.assignment, "scope": self.scope, "depth": self.depth,
            "model": self.model, "reasoning_effort": self.reasoning_effort,
            "state": self.state, "operation": self.operation,
            "elapsed_ms": int((time.monotonic() - self.started) * 1000)
            if self.task is not None and not self.task.done() else self.elapsed_ms,
            "result": asdict(self.result) if self.result is not None else None,
            "log_path": self.log_path, "call_id": self.source_call_id,
            "source_run_id": self.source_run_id, "model_settings": self.model_settings,
            "execution_id": self.execution_id,
            "task_name": self.task_name, "task_run_id": self.task_run_id,
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
        self.usage: dict[str, AgentUsage] = {"root": AgentUsage()}
        self.current_run_id: str | None = None

    def observe_root(self, event: RuntimeEvent) -> None:
        self.usage["root"].observe(event)
        if event.kind == "run_started":
            self.current_run_id = event.run_id

    def short_task_name(self, text: str) -> str:
        from nexus.app.events import redact
        from nexus.app.tui import terminal_text
        clean = terminal_text(str(redact(text, self.root.secret_values())))
        goal = re.search(r"(?:^|[\n;；])\s*(?:[-*#]\s*)?(?:任务名称|任务|目标|goal|task|title)"
                         r"\s*[:：]\s*([^\n;；]+)", clean, re.I)
        first = goal[1] if goal else next((line.strip() for line in clean.splitlines()
                                          if line.strip()), "未命名任务")
        name = " ".join(first.split())
        if cell_len(name) > 24:
            while cell_len(name) > 23:
                name = name[:-1]
            name += "…"
        return name

    def task_run(self, agent: Agent) -> str | None:
        # Old logs store the immediate parent's run. Walk to the root delegation.
        seen: set[str] = set()
        while agent.task_run_id is None and agent.parent in self.agents:
            if agent.id in seen:
                return None
            seen.add(agent.id)
            agent = self.agents[agent.parent]
        return agent.task_run_id or (agent.source_run_id if agent.parent == "root" else None)

    def has_unresolved_effects(self, agent: Agent) -> bool:
        return any(value.get("agent_id") == agent.id or value.get("run_id") in agent.run_ids
                   for value in self.root.session.unresolved_effects.values())

    def needs_attention(self, agent: Agent) -> bool:
        return (agent.state not in {"completed", "aborted"} or agent.accepting
                or agent.task is not None and not agent.task.done()
                or agent.result is not None and not agent.delivered
                or self.has_unresolved_effects(agent)
                or bool(agent.conversation and agent.conversation.inbox.guides()))

    def current_agents(self) -> set[str]:
        current = self.root.session.run_id or self.current_run_id
        visible = {a.id for a in self.agents.values()
                   if (current is not None and self.task_run(a) == current)
                   or self.needs_attention(a)}
        # Keep ancestors as context for an older descendant that still needs work.
        for ident in list(visible):
            parent = self.agents[ident].parent
            while parent in self.agents and parent not in visible:
                visible.add(parent)
                parent = self.agents[parent].parent
        return visible

    async def event(self, kind: str, data: Json) -> None:
        assert self.root.events is not None
        await self.root.events(kind, data)
        parent = data.get("parent_id", "root")
        if parent != "root" and parent in self.agents:
            owner = self.agents[parent].conversation
            if owner is not None and owner.events is not None and not owner.closed:
                await owner.events(kind, data)
        self.changed.set()
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

    async def spawn(self, parent: str, args: Json, call_id: str = "") -> Json:
        config = self.root.config.multi_agent
        if not self.can_delegate(parent):
            return {"error": "delegation_disabled"}
        owner = self.owner(parent)
        if isinstance(owner.registry_source, dict):
            return {"error": "delegation_requires_session_registry_factory"}
        prior = next((a for a in self.children(parent) if call_id
                      and a.source_call_id == call_id and a.source_run_id == owner.session.run_id),
                     None)
        if prior is not None:
            return {"agent_id": prior.id, "state": prior.state, "already_created": True}
        role = args.get("role")
        if role not in config.roles:
            return {"error": "invalid_role"}
        if parent != "root" and role not in ROLE_CHILDREN[self.agents[parent].role]:
            return {"error": "role_delegation_denied"}
        if any(not isinstance(args.get(k), str) or not args[k].strip() for k in ("task", "scope")):
            return {"error": "task_and_scope_required"}
        if "task_name" in args and (not isinstance(args["task_name"], str)
                                    or not args["task_name"].strip()):
            return {"error": "invalid_task_name"}
        if self.occupied() >= config.max_agents:
            return {"error": "capacity_full", "max_agents": config.max_agents}
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
        agent.log_path = f"{self.root.session.session_id}/subagents/{agent.id}.jsonl"
        agent.source_call_id, agent.source_run_id = call_id, owner.session.run_id
        agent.task_name = self.short_task_name(args.get("task_name") or agent.assignment)
        agent.task_run_id = ((self.root.session.run_id or self.current_run_id) if parent == "root"
                             else self.task_run(self.agents[parent]))
        agent.model_settings = asdict(model)
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
            tracing=replace(owner.config.tracing, enabled=False),
        )
        agent.task = asyncio.create_task(self.execute(agent, child_config))
        return {"agent_id": agent.id, "state": agent.state}

    async def execute(self, agent: Agent, config: Config, *, resume: Path | None = None,
                      continuation: str | None = None) -> None:
        from nexus.app.bootstrap import Conversation
        owner = self.owner(agent.parent)
        child: Conversation | None = None
        result: RunResult | None = None
        try:
            if agent.state == "cancelling":
                raise asyncio.CancelledError
            async def consume(event: RuntimeEvent) -> None:
                if event.run_id is not None:
                    agent.run_ids.add(event.run_id)
                self.usage.setdefault(agent.id, AgentUsage()).observe(event)
                if event.kind == "tool_started":
                    agent.operation = str(event.data.get("name", ""))
                elif event.kind == "model_started":
                    agent.operation = "model"
                elif event.kind == "approval_requested":
                    agent.operation = "awaiting approval"
                await self.root.consumer(RuntimeEvent(
                    "subagent_event", event.timestamp, event.session_id, event.run_id,
                    {"agent_id": agent.id, "kind": event.kind, "data": event.data},
                ))

            child = Conversation(
                owner.workspace, config, consume, home=owner.home,
                registry=owner.registry_source, environment_display=owner.environment_display,
                skill_directory=owner.catalog.root if owner.catalog else None,
                coordinator=self, agent_id=agent.id,
                resume=resume,
            )
            agent.conversation = child
            self.root.session.unresolved_effects.update(child.session.unresolved_effects)
            child.session.unresolved_effects = self.root.session.unresolved_effects
            if resume is not None and child.writer is not None:
                assert self.root.writer is not None
                agent.log_path = child.writer.path.relative_to(
                    self.root.writer.path.parent,
                ).as_posix()
            from nexus.core.security import ApprovalDecision, ApprovalRequest

            async def approve(request: ApprovalRequest) -> ApprovalDecision:
                approver = self.root.gateway.approver
                if approver is None:
                    return ApprovalDecision(request.request_id, "deny")
                return await approver(request)

            child.gateway.approver = approve
            child.gateway.approval_lock = self.approval_lock
            child.ensure_events(agent.assignment)
            assert child.events is not None
            agent.state = "running"
            agent.accepting = True
            await self.event("agent_state", agent.public())
            text = continuation or agent.assignment + "\n\nAssigned scope: " + agent.scope
            while True:
                await child.events("agent_execution_started", {
                    "agent_id": agent.id, "parent_id": agent.parent,
                    "execution_id": agent.execution_id,
                })
                result = await child.turn(text)
                # The same boundary serializes acceptance with the decision to finish.
                # A message already committing holds this lock until it reaches Inbox.
                async with agent.input_lock:
                    if agent.state == "cancelling" or (
                        agent.task is not None and agent.task.cancelling()
                    ):
                        agent.accepting = False
                        if result.outcome == "completed":
                            result = replace(result, outcome="aborted", reason="user_abort")
                        break
                    if result.outcome != "completed" or not child.inbox.guides():
                        agent.accepting = False
                        break
                    child.session.resume_run_id = child.session.run_id
                    agent.execution_id = uuid4().hex
                    text = "Continue the task using pending guidance."
                # Preserve the agent/history/run, but each further core execution has
                # distinct trace identity. Never publish an intermediate final result.
                await self.event("agent_state", agent.public())
        except asyncio.CancelledError:
            result = RunResult("aborted", reason="user_abort")
        except Exception as exc:
            result = RunResult("failed", reason=f"runtime_error:{type(exc).__name__}")
        finally:
            agent.accepting = False
            try:
                if child is not None:
                    try:
                        async with agent.input_lock:
                            if child.events is not None and result is not None:
                                await child.events("agent_execution_finished", {
                                    "execution_id": agent.execution_id, "result": asdict(result),
                                })
                    finally:
                        await child.close()
                if result is not None and (
                    "cleanup_incomplete" in (result.reason or "")
                ):
                    agent.state = "cleanup_failed"
                else:
                    agent.state = result.outcome if result else "failed"
            except Exception:
                agent.state = "cleanup_failed"
            agent.result = result
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
        return {"agents": values, "guidance_pending": bool(inbox.guides())}

    async def message(self, parent: str, target: str, text: str) -> Json:
        agent = self.agents.get(target)
        if agent is None or agent.parent != parent:
            return {"error": "not_direct_child"}
        async with agent.input_lock:
            child = agent.conversation
            if child is None or child.closed or not agent.accepting or agent.state == "cancelling":
                return {"error": "agent_not_accepting"}
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
            agent.accepting = False
            agent.state = "cancelling"
            await self.event("agent_state", agent.public())
            if agent.conversation is not None and not cancelling:
                agent.task.cancel()
            await asyncio.shield(agent.task)
        return agent.public()

    async def prepare(self, ident: str) -> None:
        owner = self.owner(ident)
        await self.deliver_results(ident)
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

    def occupied(self) -> int:
        return sum(a.state in {"starting", "running", "waiting_decision", "cancelling",
                               "cleanup_failed", "recovery_failed"}
                   or a.task is not None and not a.task.done() for a in self.agents.values())

    def resolve(self, value: str) -> str | None:
        if value == "root":
            return value
        matches = [ident for ident in self.agents if ident.startswith(value)]
        return matches[0] if len(matches) == 1 else None

    def panel(self, selected: str = "root", *, history: bool = False, compact: bool = True) -> str:
        from nexus.app.events import redact
        if not self.agents:
            return ""
        current = self.current_agents()
        archived = self.agents.keys() - current
        visible = archived if history else current
        heading = "历史" if history else "当前任务 / 待处理"
        lines = [f"Async Agents · {heading} · {self.occupied()}/"
                 f"{self.root.config.multi_agent.max_agents} active",
                 "会话累计（主+全部子代理）: "
                 + usage_label(combined_usage(list(self.usage.values())))]
        def priority(agent: Agent) -> int:
            # Put live branches before retained failures and settled results so
            # a bounded terminal does not bury today's workers under old alerts.
            active = (agent.state in {"starting", "running", "waiting_decision", "cancelling"}
                      or agent.task is not None and not agent.task.done())
            own = 0 if active else 1 if self.needs_attention(agent) else 2
            return min([own, *(priority(child) for child in self.children(agent.id))])
        def walk(parent: str) -> None:
            children = sorted(self.children(parent), key=priority)
            for agent in children:
                if agent.id not in visible:
                    walk(agent.id)
                    continue
                usage = self.usage.get(agent.id, AgentUsage()).public()
                mark = "!" if agent.state in {"waiting_decision", "failed", "cleanup_failed",
                                             "recovery_failed", "interrupted"} else (
                                                 "✓" if agent.state == "completed" else "●")
                elapsed = agent.public()["elapsed_ms"] / 1000
                target = "→ " if agent.id == selected else ""
                name = agent.task_name or self.short_task_name(agent.assignment)
                status = agent.state + (
                    " / unresolved" if self.has_unresolved_effects(agent) else "")
                if agent.result is not None and not agent.delivered:
                    status += " / 待交付"
                current_run = self.root.session.run_id or self.current_run_id
                if not history and self.task_run(agent) not in {None, current_run}:
                    status += " [跨轮]"
                lines.append(f"{'  ' * (agent.depth - 1)}├ {mark} {target}{name} · "
                             f"{agent.role} {agent.id[:8]} · {status}")
                if not compact:
                    lines.append(f"{'  ' * (agent.depth - 1)}│   "
                                 f"{agent.operation or 'idle'} · {elapsed:.1f}s · "
                                 f"该代理累计: {usage_label(usage)} · fresh · "
                                 f"任务 {(self.task_run(agent) or 'unknown')[:8]}")
                walk(agent.id)
        walk("root")
        if not visible:
            lines.append("  暂无历史代理。" if history else "  当前任务暂无子代理。")
        if not history and archived:
            lines.append(f"历史 {len(archived)} 个 · /agents history")
        return str(redact("\n".join(lines), self.root.secret_values()))

    async def user_message(self, target: str, text: str) -> Json:
        agent = self.agents.get(target)
        if agent is None:
            return {"error": "unknown_agent"}
        result = await self.message(agent.parent, target, text)
        if "error" not in result:
            parent = self.owner(agent.parent)
            assert parent.events is not None
            await parent.inbox.receive(
                f"[User guided child {target}]\n{text}", "guide", parent.events,
            )
        return result

    def execution_metadata(self, ident: str) -> Json:
        agent = self.agents.get(ident)
        return {"agent_id": ident, "parent_agent_id": agent.parent if agent else None,
                "root_session_id": self.root.session.session_id,
                "root_run_id": self.root.session.run_id,
                "root_execution_id": self.root.session.execution_id,
                "parent_execution_id": self.owner(agent.parent).session.execution_id
                if agent else None,
                "delegation_call_id": agent.source_call_id if agent else None}

    def details(self, target: str) -> str:
        from nexus.app.session import read_records
        agent = self.agents[target]
        lines = [json_text({**agent.public(),
                            "usage": self.usage.get(target, AgentUsage()).public()})]
        path = self.log_file(agent)
        if path.exists():
            with path.open("rb") as stream:
                rows, _ = read_records(stream)
            for row in rows[-60:]:
                if row["kind"] in {"message", "warning", "tool_finished", "approval_requested"}:
                    lines.append(row["kind"] + ": " + json_text(row["data"]))
        return "\n".join(lines)

    def result_in_history(self, ident: str, agent: Agent) -> bool:
        return self.result_in_messages(self.owner(ident).session.messages, agent)

    @staticmethod
    def result_in_messages(messages: list[Message], agent: Agent) -> bool:
        for message in messages:
            if message.input_id == f"agent-result:{agent.id}:{agent.execution_id}":
                return True
            if message.role != "tool":
                continue
            try:
                data = json.loads(message.content).get("data", {})
            except (ValueError, AttributeError):
                continue
            agents = data.get("agents", []) if isinstance(data, dict) else []
            if any(a.get("agent_id") == agent.id and a.get("execution_id") == agent.execution_id
                   and a.get("result") is not None
                   and a.get("state") in {"completed", "failed", "aborted", "limited"}
                   for a in agents if isinstance(a, dict)):
                return True
        return False

    async def deliver_results(self, parent: str) -> bool:
        from nexus.core.agent import append_message

        owner = self.owner(parent)
        assert owner.events is not None
        injected = False
        for agent in self.children(parent):
            if agent.delivered or agent.result is None or agent.state in {
                "starting", "running", "waiting_decision", "cancelling", "interrupted",
            } or agent.task is not None and not agent.task.done():
                continue
            if not self.result_in_history(parent, agent):
                await append_message(owner.session, Message(
                    "user", "[Subagent result]\n" + json_text(agent.public()),
                    input_id=f"agent-result:{agent.id}:{agent.execution_id}",
                ), owner.events)
                injected = True
            agent.delivered = True
            await self.event("agent_result_delivered", {"agent_id": agent.id, "parent_id": parent,
                                                       "execution_id": agent.execution_id})
        return injected

    async def before_final(self, parent: str) -> bool:
        await self.finish(parent)
        return await self.deliver_results(parent)

    def log_file(self, agent: Agent) -> Path:
        assert self.root.writer is not None
        base = self.root.writer.path.parent.resolve()
        path = (base / agent.log_path).resolve()
        relative = path.relative_to(base)
        if len(relative.parts) != 3 or relative.parts[1] != "subagents" or path.suffix != ".jsonl":
            raise ValueError("Invalid subagent log path")
        return path

    def restore(self) -> None:
        from nexus.app.session import now, read_records, resume_session

        assert self.root.writer is not None
        restored_messages = {"root": self.root.session.messages}
        with self.root.writer.path.open("rb") as stream:
            rows, _ = read_records(stream)
        for row in rows:
            self.observe_root(RuntimeEvent(
                row["kind"], row["timestamp"], row["session_id"], row.get("run_id"), row["data"],
            ))
        for row in rows:
            data = row["data"]
            if row["kind"] in {"agent_created", "agent_state"}:
                ident = data["agent_id"]
                self.agents[ident] = Agent(
                    ident, data["parent_id"], data["role"], data["assignment"], data["scope"],
                    data["depth"], data["model"], data.get("reasoning_effort"),
                    state="cleanup_failed" if data.get("state") == "cleanup_failed"
                    else "interrupted", elapsed_ms=data.get("elapsed_ms", 0),
                    log_path=data.get("log_path", f"{row['session_id']}/subagents/{ident}.jsonl"),
                    source_call_id=data.get("call_id", ""), source_run_id=data.get("source_run_id"),
                    model_settings=data.get("model_settings"),
                    execution_id=data.get("execution_id", "legacy"),
                    task_name=data.get("task_name") or self.short_task_name(data["assignment"]),
                    task_run_id=data.get("task_run_id"),
                )
        for agent in self.agents.values():
            if agent.task_run_id is None:
                agent.task_run_id = self.task_run(agent)
            # Receipt events are diagnostic. Only the parent's committed message
            # proves delivery (including a crash between message and receipt).
            agent.delivered = False
            writer = None
            try:
                path = self.log_file(agent)
                if not path.exists():
                    continue  # Creation was saved before the child first ran.
                session, writer, _ = resume_session(path, self.root.workspace, self.root.home,
                                                    directory=path.parent)
                restored_messages[agent.id] = session.messages
                agent.log_path = writer.path.relative_to(self.root.writer.path.parent).as_posix()
                self.root.session.unresolved_effects.update(session.unresolved_effects)
                with writer.path.open("rb") as stream:
                    child_rows, _ = read_records(stream)
                ledger = self.usage.setdefault(agent.id, AgentUsage())
                for row in child_rows:
                    if row.get("run_id") is not None:
                        agent.run_ids.add(row["run_id"])
                    ledger.observe(RuntimeEvent(row["kind"], row["timestamp"], row["session_id"],
                                                row.get("run_id"), row["data"]))
                marker = next((r["seq"] for r in reversed(child_rows)
                               if r["kind"] == "agent_execution_started"
                               and r["data"].get("execution_id") == agent.execution_id),
                              0 if agent.execution_id == "legacy" else None)
                settled = next((r for r in reversed(child_rows)
                                if r["kind"] == "agent_execution_finished"
                                and r["data"].get("execution_id") == agent.execution_id), None)
                if marker is None and settled is None:
                    continue
                last_start = max((r["seq"] for r in child_rows if r["kind"] == "run_started"
                                  and r["seq"] > (marker or 0)), default=len(child_rows) + 1)
                finished = next((r for r in reversed(child_rows) if r["kind"] == "run_finished"
                                 and r["seq"] > last_start), None)
                if settled is not None:
                    finished = {"data": settled["data"]["result"]}
                elif finished is not None:
                    from nexus.core.inbox import Inbox
                    pending = Inbox()
                    pending.restore(child_rows)
                    if pending.guides():
                        finished = None  # Core finished, but accepted guidance remains unfinished.
                if finished is not None:
                    data = finished["data"]
                    agent.result = RunResult(**{**data, "usage": Usage(**data["usage"])})
                    if agent.state != "cleanup_failed":
                        agent.state = agent.result.outcome
                    if "cleanup_incomplete" in (agent.result.reason or ""):
                        agent.state = "cleanup_failed"
            except (OSError, ValueError, RuntimeError, KeyError):
                agent.state = "recovery_failed"
                self.root.session.unresolved_effects[f"agent:{agent.id}"] = {
                    "name": "unknown", "agent_id": agent.id,
                }
            finally:
                if writer is not None:
                    writer.close()
                self.root.writer.append(RuntimeEvent(
                    "agent_state", now(), self.root.session.session_id, None, agent.public(),
                ))
        for agent in self.agents.values():
            # Reconstruct delivery from committed parent messages, never from
            # receipt events. The default panel can then archive settled history
            # immediately on resume, including nested children.
            agent.delivered = self.result_in_messages(
                restored_messages.get(agent.parent, []), agent,
            )

    async def resume(self, parent: str, target: str, text: str) -> Json:
        from nexus.app.config import parse_model

        agent = self.agents.get(target)
        if agent is None or agent.parent != parent:
            return {"error": "not_direct_child"}
        if (not self.can_delegate(parent)
                or self.occupied() >= self.root.config.multi_agent.max_agents):
            return {"error": "delegation_disabled_or_full"}
        if agent.state not in {"interrupted", "failed", "aborted", "limited"}:
            return {"error": "agent_not_resumable"}
        if parent != "root" and agent.role not in ROLE_CHILDREN[self.agents[parent].role]:
            return {"error": "role_delegation_denied"}
        owner = self.owner(parent)
        selected = parse_model({"selected": agent.model_settings}, "selected")
        if isinstance(owner.registry_source, dict):
            return {"error": "delegation_requires_session_registry_factory"}
        role = self.root.config.multi_agent.roles[agent.role]
        config = replace(owner.config, model=selected,
                         tracing=replace(owner.config.tracing, enabled=False),
                         limits=replace(owner.config.limits, context_window=selected.context_window,
                                        max_output_tokens=selected.max_output_tokens),
                         mcp_servers={s: v for s, v in owner.config.mcp_servers.items()
                                      if s in role.mcp_servers})
        path = self.log_file(agent)
        agent.state, agent.result, agent.delivered = "starting", None, False
        agent.accepting = False
        agent.execution_id = uuid4().hex
        agent.task_run_id = ((self.root.session.run_id or self.current_run_id) if parent == "root"
                             else self.task_run(self.agents[parent]))
        agent.started = time.monotonic()
        await self.event("agent_state", agent.public())
        agent.task = asyncio.create_task(self.execute(agent, config,
            resume=path if path.exists() else None, continuation=text))
        return {"agent_id": target, "state": "starting"}

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
                agent.accepting = False
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
            value = await self.spawn(parent, args, context.call_id)
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

        async def resume(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            value = await self.resume(parent, args["agent_id"], args["message"])
            return ToolResult(context.call_id, "error" not in value, value, value.get("error"))

        tools = {
            "spawn_agent": Tool(ToolSpec("spawn_agent", "Delegate a fresh asynchronous task. "
                "Specify background, goal, deliverables, constraints and exclusive file scope.", {
                    "type": "object", "properties": {
                        "role": {"type": "string", "enum": ["scout", "worker", "reviewer"]},
                        "task": {"type": "string"}, "scope": {"type": "string"},
                        "task_name": {"type": "string", "description":
                            "Short descriptive title in the user's language, e.g. 测试覆盖调查."},
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
            "resume_agent": Tool(ToolSpec("resume_agent", "Explicitly continue an interrupted "
                "direct child from its own history, under current permissions; no task replay.", {
                    "type": "object", "properties": {"agent_id": {"type": "string"},
                                                       "message": {"type": "string"}},
                    "required": ["agent_id", "message"], "additionalProperties": False,
                }), resume, "internal"),
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
