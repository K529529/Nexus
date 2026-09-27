"""Frozen Day 4 coding loop implemented behind Nexus-owned boundaries."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import fields, replace
from hashlib import sha256
from typing import Any, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from nexus.application.diff_service import FinalDiffCollector, FinalDiffEvidence
from nexus.application.event_publisher import LiveRuntimeEventBridge
from nexus.application.execution_ledger import (
    RuntimeEventBuffer,
    ToolExecutionLedger,
)
from nexus.application.plan_approval_service import PlanApprovalService
from nexus.application.tool_runtime import ToolRuntime
from nexus.config.models import ApprovalMode
from nexus.domain.agent_decision import (
    AgentDecision,
    AgentDecisionKind,
    AgentDecisionRequest,
    AgentRuntimeFeedback,
    Observation,
    ToolAction,
)
from nexus.domain.agent_state import AgentState
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.exploration import ContextBuildRequest, ExplorationRequest
from nexus.domain.model import ModelMessage
from nexus.domain.persistence import SessionTurn
from nexus.domain.planning import (
    ChangedFile,
    ChangeKind,
    Plan,
    PlanApprovalResumeInput,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    TerminalStatus,
)
from nexus.domain.ports.agent_decision import AgentDecisionAdapter
from nexus.domain.ports.context import ContextManager
from nexus.domain.ports.graph_runtime import GraphRuntime
from nexus.domain.ports.planning import Planner, PlanningRequest, RepairPlanningRequest
from nexus.domain.ports.repository_context import ContextBuilder, RepositoryExplorer
from nexus.domain.ports.validation import ValidationPlanner, ValidationRunner
from nexus.domain.runtime_events import (
    AgentSemanticRetryStarted,
    AgentStepCompleted,
    AgentStepStarted,
    ApprovalActorCategory,
    ApprovalRequested,
    ApprovalResolved,
    ApprovalSubject,
    ChangedFileRecorded,
    ContextBuilt,
    ExecutionPhase,
    FinalResult,
    PhaseFinished,
    PhaseStarted,
    PlanCreated,
    PlanStepCompleted,
    RepairStarted,
    ReplanOccurred,
    RepositoryExplored,
    RuntimeStatus,
)
from nexus.domain.tooling import ApprovalDecision, RiskLevel, ToolInvocation, ToolResult
from nexus.domain.validation import ValidationStatus
from nexus.errors import ModelError, NexusError
from nexus.tools.native import _MAX_READ_LINES

_SAFE_PATCH_FAILURE_DETAILS = {
    ("INVALID_PATCH", "The patch exceeds the Day 4 size limit."): "patch exceeds size limit",
    ("INVALID_PATCH", "The patched file exceeds the Day 4 size limit."): (
        "patched file exceeds size limit"
    ),
    ("INVALID_PATCH", "The patch target is not supported UTF-8 text."): (
        "target is not supported UTF-8 text"
    ),
    ("INVALID_PATCH", "The patch target is not valid UTF-8 text."): (
        "target is not valid UTF-8 text"
    ),
    ("INVALID_PATCH", "The patch has no valid hunk."): "patch has no valid hunk",
    ("INVALID_PATCH", "Patch headers do not match the target."): (
        "patch headers do not match the target path"
    ),
    ("INVALID_PATCH", "Multi-file patches are not allowed."): (
        "patch must contain only one file"
    ),
    ("INVALID_PATCH", "The patch contains invalid hunk syntax."): (
        "unified-diff hunk syntax is invalid"
    ),
    ("INVALID_PATCH", "Invalid no-newline marker."): "no-newline marker is invalid",
    ("INVALID_PATCH", "Patch hunk body is invalid."): "unified-diff hunk body is invalid",
    ("INVALID_PATCH", "Patch hunk counts do not match."): (
        "unified-diff hunk header counts do not match the hunk body"
    ),
    ("INVALID_PATCH", "The patch has no hunk."): "patch has no hunk",
    ("INVALID_PATCH", "The patch is invalid."): "patch is invalid",
    ("PATCH_CONFLICT", "Patch hunk position is invalid."): "hunk position is invalid",
    ("PATCH_CONFLICT", "No-newline marker conflicts with source."): (
        "no-newline marker conflicts with current source"
    ),
    ("PATCH_CONFLICT", "Patch context does not match."): (
        "patch context does not match current source"
    ),
    ("PATCH_NO_CHANGES", "The patch does not change file content."): (
        "patch does not change file content"
    ),
}
_PATCH_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)


class Day4LangGraphRuntime:
    """Execute the approved Day 4 graph with durable Plan approval interrupts."""

    def __init__(
        self,
        *,
        explorer: RepositoryExplorer,
        context_builder: ContextBuilder,
        planner: Planner,
        agent: AgentDecisionAdapter,
        tool_runtime: ToolRuntime,
        validation_planner: ValidationPlanner,
        validation_runner: ValidationRunner,
        plan_approval_service: PlanApprovalService,
        diff_collector: FinalDiffCollector,
        event_buffer: RuntimeEventBuffer | LiveRuntimeEventBridge,
        ledger: ToolExecutionLedger,
        approval_mode: ApprovalMode,
        max_steps: int,
        max_repair_attempts: int,
        max_replans: int,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        legacy_runtime: GraphRuntime | None = None,
        context_manager: ContextManager | None = None,
        conversation_turns: Callable[[str], Awaitable[Sequence[SessionTurn]]] | None = None,
        model_call_id: Callable[[], str] | None = None,
        normalize_argv: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        self._explorer = explorer
        self._context_builder = context_builder
        self._planner = planner
        self._agent = agent
        self._tool_runtime = tool_runtime
        self._validation_planner = validation_planner
        self._validation_runner = validation_runner
        self._plan_approval_service = plan_approval_service
        self._diff_collector = diff_collector
        self._events = event_buffer
        self._ledger = ledger
        self._approval_mode = approval_mode
        self._max_steps = max_steps
        self._max_repair_attempts = max_repair_attempts
        self._max_replans = max_replans
        self._legacy_runtime = legacy_runtime
        self._context_manager = context_manager
        self._conversation_turns = conversation_turns
        self._model_call_id = model_call_id
        self._normalize_argv = normalize_argv or (lambda argv: list(argv))

        builder = StateGraph(AgentState)
        builder.add_node("initialize_run", self._initialize_run)
        builder.add_node(
            "explore_repository",
            self._profile_node(ExecutionPhase.REPOSITORY, self._explore_repository),
        )
        builder.add_node(
            "build_context", self._profile_node(ExecutionPhase.CONTEXT, self._build_context)
        )
        builder.add_node(
            "create_plan", self._profile_node(ExecutionPhase.PLANNING, self._create_plan)
        )
        builder.add_node(
            "approval_gate",
            self._approval_gate,
            destinations=("agent_step", "finalize_failed"),
        )
        builder.add_node(
            "agent_step",
            self._profile_node(ExecutionPhase.AGENT, self._agent_step),
            destinations=("agent_step", "execute_tool", "validate", "finalize_failed"),
        )
        builder.add_node(
            "execute_tool", self._profile_node(ExecutionPhase.AGENT, self._execute_tool)
        )
        builder.add_node(
            "observe",
            self._profile_node(ExecutionPhase.AGENT, self._observe),
            destinations=("agent_step", "create_plan", "finalize_failed"),
        )
        builder.add_node(
            "validate",
            self._profile_node(ExecutionPhase.VALIDATION, self._validate),
            destinations=("finalize", "repair_plan", "finalize_failed"),
        )
        builder.add_node(
            "repair_plan", self._profile_node(ExecutionPhase.REPAIR, self._repair_plan)
        )
        builder.add_node("finalize", self._finalize)
        builder.add_node("finalize_failed", self._finalize_failed)
        builder.add_edge(START, "initialize_run")
        builder.add_edge("initialize_run", "explore_repository")
        builder.add_edge("explore_repository", "build_context")
        builder.add_edge("build_context", "create_plan")
        builder.add_edge("create_plan", "approval_gate")
        builder.add_edge("execute_tool", "observe")
        builder.add_edge("repair_plan", "agent_step")
        builder.add_edge("finalize", END)
        builder.add_edge("finalize_failed", END)
        self._graph = builder.compile(checkpointer=checkpointer)

    def _profile_node(
        self,
        phase: ExecutionPhase,
        node: Callable[[AgentState], Awaitable[Any]],
    ) -> Any:
        async def observed(state: AgentState) -> Any:
            actual_phase = (
                ExecutionPhase.REPLAN
                if phase is ExecutionPhase.PLANNING and state.plan is not None
                else phase
            )
            try:
                await self._events.emit(
                    PhaseStarted(
                        run_id=state.run_id,
                        session_id=state.session_id,
                        phase=actual_phase,
                    )
                )
            except Exception:
                pass  # Profiling must not change graph execution.
            started = time.perf_counter()
            success = False
            try:
                result = await node(state)
                success = True
                return result
            finally:
                try:
                    await self._events.emit(
                        PhaseFinished(
                            run_id=state.run_id,
                            session_id=state.session_id,
                            phase=actual_phase,
                            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                            success=success,
                        )
                    )
                except Exception:
                    pass  # Instrumentation cannot mask a node result or failure.

        return observed

    async def run(
        self,
        state: AgentState,
        *,
        thread_id: str | None = None,
    ) -> AgentState:
        try:
            result = await self._graph.ainvoke(
                state,
                config=_thread_config(
                    thread_id,
                    run_id=state.run_id,
                    session_id=state.session_id,
                ),
            )
            converted = _to_agent_state(result)
            if converted.pending_plan_approval is not None:
                return replace(converted, status=RuntimeStatus.INTERRUPTED)
            return converted
        except NexusError:
            await self._persist_failed_attempt_evidence(thread_id)
            raise
        except Exception as exc:
            raise NexusError(
                "The Day 4 graph execution failed.",
                code="GRAPH_EXECUTION_ERROR",
                retryable=True,
            ) from exc

    async def resume(
        self,
        *,
        thread_id: str,
        resume_input: PlanApprovalResumeInput | None = None,
    ) -> AgentState:
        config = cast(RunnableConfig, _thread_config(thread_id))
        try:
            snapshot = await self._graph.aget_state(config)
            current = _to_agent_state(snapshot.values)
            if (
                self._legacy_runtime is not None
                and current.plan is None
                and current.exploration is None
                and bool(current.messages)
            ):
                return await self._legacy_runtime.resume(thread_id=thread_id)
            if (
                "model_response" in snapshot.next
                and self._legacy_runtime is not None
            ):
                return await self._legacy_runtime.resume(thread_id=thread_id)
            if current.pending_plan_approval is not None and resume_input is None:
                await self._emit_approval(current)
                return replace(current, status=RuntimeStatus.INTERRUPTED)
            graph_input: Command[Any] | None = None
            if resume_input is not None:
                graph_input = Command(
                    resume={
                        "decision": resume_input.decision.value,
                        "reason": resume_input.reason,
                    }
                )
            result = await self._graph.ainvoke(graph_input, config=config)
            converted = _to_agent_state(result)
            if converted.pending_plan_approval is not None:
                return replace(converted, status=RuntimeStatus.INTERRUPTED)
            return converted
        except NexusError:
            await self._persist_failed_attempt_evidence(thread_id)
            raise
        except Exception as exc:
            raise NexusError(
                "Nexus could not resume the Day 4 graph execution.",
                code="GRAPH_RESUME_ERROR",
                retryable=True,
            ) from exc

    async def _initialize_run(self, state: AgentState) -> dict[str, object]:
        if state.session_id is None:
            raise NexusError(
                "Day 4 requires an established Session.",
                code="SESSION_PERSISTENCE_REQUIRED",
            )
        return {"status": RuntimeStatus.STARTED}

    async def _explore_repository(self, state: AgentState) -> dict[str, object]:
        session_id = _session_id(state)
        result = await self._explorer.explore(
            ExplorationRequest(state.task, state.run_id, session_id)
        )
        await self._events.emit(
            RepositoryExplored(
                run_id=state.run_id,
                session_id=session_id,
                instruction_paths=tuple(item.path for item in result.instructions),
                manifest_paths=tuple(item.path for item in result.manifests),
                relevant_paths=tuple(item.path for item in result.relevant_files),
                exploration_tool_calls=len(result.tool_results),
                truncated=result.truncated,
            )
        )
        return self._evidence_update(state, exploration=result)

    async def _build_context(self, state: AgentState) -> dict[str, object]:
        if state.exploration is None:
            raise NexusError("Exploration evidence is missing.", code="GRAPH_INVALID_STATE")
        session_id = _session_id(state)
        context = await self._context_builder.build(
            ContextBuildRequest(
                state.task,
                state.exploration,
                state.run_id,
                session_id,
            )
        )
        await self._events.emit(
            ContextBuilt(
                run_id=state.run_id,
                session_id=session_id,
                selected_paths=tuple(item.path for item in context.selected_files),
                retained_characters=sum(len(item.content) for item in context.selected_files),
                truncated=context.truncated,
                selected_chunk_count=len(context.selected_files),
                semantic_retrieval_used=context.semantic_retrieval_used,
                semantic_retrieval_status=context.semantic_retrieval_status,
                selected_skill_ids=tuple(
                    skill.metadata.skill_id for skill in context.selected_skills
                ),
                skill_selection_reason_summary=(
                    None
                    if context.skill_selection_result is None
                    else context.skill_selection_result.selection_reason_summary
                ),
            )
        )
        return self._evidence_update(state, context=context)

    async def _create_plan(self, state: AgentState) -> dict[str, object]:
        if state.context is None:
            raise NexusError("Working context is missing.", code="GRAPH_INVALID_STATE")
        session_id = _session_id(state)
        previous = state.plan
        reason = None
        kind = PlanKind.INITIAL
        if previous is not None:
            if not state.observations or state.observations[-1].replan_reason is None:
                raise NexusError("Material Replan reason is missing.", code="GRAPH_INVALID_STATE")
            kind = PlanKind.REPLAN
            reason = state.observations[-1].replan_reason
        plan = await self._planner.create_plan(
            PlanningRequest(
                state.task,
                state.context,
                kind,
                previous,
                reason,
                state.run_id,
                session_id,
            )
        )
        history = state.plan_history
        replan_count = state.replan_count
        if previous is not None:
            history = (*history, replace(previous, status=PlanStatus.SUPERSEDED))
            replan_count += 1
            await self._events.emit(
                ReplanOccurred(
                    run_id=state.run_id,
                    session_id=session_id,
                    plan_id=plan.plan_id,
                    previous_version=previous.version,
                    new_version=plan.version,
                    reason=reason or "Material approved-scope change required.",
                    replan_count=replan_count,
                )
            )
        await self._events.emit(
            PlanCreated(
                run_id=state.run_id,
                session_id=session_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                plan_kind=plan.kind,
                step_summaries=_step_summaries(plan),
                replan_reason=plan.replan_reason,
            )
        )
        pending = None
        if self._approval_mode is ApprovalMode.APPROVAL:
            pending = await self._plan_approval_service.create_pending(plan)
            await self._events.emit(
                _approval_event(plan, pending)
            )
        return self._evidence_update(
            state,
            plan=plan,
            plan_history=history,
            replan_count=replan_count,
            pending_plan_approval=pending,
            approved_plan=None,
            repair_guidance=None,
        )

    async def _approval_gate(self, state: AgentState) -> Command[Any]:
        plan = state.plan
        if plan is None:
            raise NexusError("Plan approval has no Plan.", code="GRAPH_INVALID_STATE")
        if self._approval_mode is ApprovalMode.AUTO:
            approval = await self._plan_approval_service.create_auto_approved(plan)
        else:
            if state.pending_plan_approval is None:
                raise NexusError(
                    "Pending Plan approval is missing.",
                    code="GRAPH_INVALID_STATE",
                )
            supplied = interrupt(
                {
                    "approval_id": state.pending_plan_approval.approval_id,
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                }
            )
            resume_input = _resume_input(supplied)
            approval = await self._plan_approval_service.persist_user_decision(
                state.pending_plan_approval.approval_id,
                resume_input,
            )
        await self._events.emit(
            ApprovalResolved(
                run_id=state.run_id,
                session_id=_session_id(state),
                approval_id=approval.approval_id,
                subject=ApprovalSubject.PLAN,
                invocation_id=None,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                decision=approval.decision,
                actor_category=(
                    ApprovalActorCategory.USER
                    if approval.actor == "user"
                    else ApprovalActorCategory.POLICY
                ),
            )
        )
        activated = self._plan_approval_service.activate(plan, approval)
        update: dict[str, object] = {
            "plan": activated,
            "pending_plan_approval": None,
        }
        if approval.decision is ApprovalDecision.DENIED:
            update["terminal_status"] = TerminalStatus.FAILED_APPROVAL_DENIED
            return Command(update=update, goto="finalize_failed")
        update["approved_plan"] = self._plan_approval_service.evidence(plan, approval)
        return Command(update=update, goto="agent_step")

    async def _agent_step(self, state: AgentState) -> Command[Any]:
        if state.step_count >= self._max_steps:
            return Command(
                update={"terminal_status": TerminalStatus.STOPPED_MAX_STEPS},
                goto="finalize_failed",
            )
        if state.context is None or state.plan is None:
            raise NexusError("Agent decision context is missing.", code="GRAPH_INVALID_STATE")
        self._ledger.begin_step(state.run_id)
        await self._events.emit(AgentStepStarted(
            run_id=state.run_id,
            session_id=_session_id(state),
            step_count=self._ledger.step_count(state.run_id),
        ))
        context = state.context
        if self._context_manager is not None:
            turns = (() if self._conversation_turns is None
                     else await self._conversation_turns(_session_id(state)))
            context = await self._context_manager.prepare_agent_context(
                working_context=context, plan=state.plan, observations=state.observations,
                conversation_turns=turns,
            )
        request = AgentDecisionRequest(
            state.task,
            context,
            state.plan,
            state.observations,
            state.validation_result,
            state.repair_guidance,
        )
        proposed = await self._agent.decide(request)
        retry_reason = _semantic_retry_reason(state, proposed, self._normalize_argv)
        if retry_reason is not None:
            await self._events.emit(AgentSemanticRetryStarted(
                run_id=state.run_id,
                session_id=_session_id(state),
                step_count=self._ledger.step_count(state.run_id),
                reason=retry_reason,
            ))
            proposed = await self._agent.decide(replace(
                request,
                runtime_feedback=retry_reason,
            ))
            second_reason = _semantic_retry_reason(
                state, proposed, self._normalize_argv,
            )
            if second_reason is not None:
                message = (
                    "Agent repeated a successful read_file after corrective feedback."
                    if second_reason is AgentRuntimeFeedback.REPEATED_SUCCESSFUL_READ
                    else "Agent repeated a deterministically denied Tool action."
                )
                raise ModelError(message, code="INVALID_AGENT_DECISION")
        decision = _require_current_edit_evidence(state, proposed)
        guard_reason = None
        if decision is not proposed:
            guard_reason = (
                "edit_requires_read"
                if decision.action is not None
                else "edit_already_complete"
            )
        if self._model_call_id is not None:
            await self._events.emit(
                AgentStepCompleted(
                    run_id=state.run_id,
                    session_id=_session_id(state),
                    step_count=self._ledger.step_count(state.run_id),
                    decision_kind=decision.kind,
                    model_call_id=self._model_call_id(),
                    guard_reason=guard_reason,
                    decision_summary=decision.summary,
                )
            )
        update: dict[str, object] = {
            "pending_tool_action": decision.action,
        }
        if decision.kind is AgentDecisionKind.TASK_READY:
            update["messages"] = [
                *state.messages,
                ModelMessage(role="assistant", content=decision.summary),
            ]
        update = self._evidence_update(state, **update)
        if decision.kind is AgentDecisionKind.TOOL_ACTION:
            return Command(update=update, goto="execute_tool")
        if decision.kind is AgentDecisionKind.TASK_READY:
            return Command(update=update, goto="validate")
        return Command(update=update, goto="agent_step")

    async def _execute_tool(self, state: AgentState) -> dict[str, object]:
        action = state.pending_tool_action
        authorization = state.approved_plan
        if action is None or authorization is None:
            raise NexusError("Agent Tool execution lacks authority.", code="GRAPH_INVALID_STATE")
        result = await self._tool_runtime.execute(
            ToolInvocation(
                str(uuid4()),
                action.tool_name,
                action.arguments,
                state.run_id,
                _session_id(state),
            ),
            authorization=authorization,
        )
        changed = state.changed_files
        if result.success and result.tool_name in {"edit_file", "apply_patch", "write_file"}:
            changed = _record_change(changed, result)
            recorded = next(
                item for item in changed if item.latest_invocation_id == result.invocation_id
            )
            await self._events.emit(
                ChangedFileRecorded(
                    run_id=state.run_id,
                    session_id=_session_id(state),
                    invocation_id=result.invocation_id,
                    relative_path=recorded.path,
                    change_kind=recorded.change_kind,
                    changed_file_count=len(changed),
                )
            )
        return self._evidence_update(
            state,
            latest_tool_result=result,
            changed_files=changed,
        )

    async def _observe(self, state: AgentState) -> Command[Any]:
        result = state.latest_tool_result
        action = state.pending_tool_action
        if result is None or action is None:
            raise NexusError("Observation evidence is missing.", code="GRAPH_INVALID_STATE")
        code = None if result.error is None else result.error.code
        reason = (
            "The next required Agent action was outside the currently approved Plan scope."
            if code == "PLAN_SCOPE_DENIED"
            else None
        )
        observation = Observation(
            result.invocation_id,
            result.tool_name,
            result.success,
            _observation_summary(result, action),
            code,
            reason,
            _observation_target_path(result, action),
            state.repair_count,
        )
        updated_plan, completed_step = _complete_matching_plan_step(
            state, action, result,
        )
        if completed_step is not None and updated_plan is not None:
            await self._events.emit(PlanStepCompleted(
                run_id=state.run_id,
                session_id=_session_id(state),
                plan_id=updated_plan.plan_id,
                plan_version=updated_plan.version,
                step_id=completed_step.step_id,
                sequence=completed_step.sequence,
            ))
        update = self._evidence_update(
            state,
            observations=(*state.observations, observation),
            pending_tool_action=None,
            last_policy_denial_fingerprint=(
                _action_fingerprint(action, self._normalize_argv)
                if code in {"PERMISSION_DENIED", "COMMAND_DENIED"}
                else None
            ),
        )
        if updated_plan is not None:
            update["plan"] = updated_plan
        if reason is None:
            return Command(update=update, goto="agent_step")
        if state.replan_count >= self._max_replans:
            update["terminal_status"] = TerminalStatus.STOPPED_MAX_REPLANS
            return Command(update=update, goto="finalize_failed")
        return Command(update=update, goto="create_plan")

    async def _validate(self, state: AgentState) -> Command[Any]:
        if (
            state.plan is None
            or state.exploration is None
            or state.context is None
            or state.approved_plan is None
        ):
            raise NexusError("Validation state is incomplete.", code="GRAPH_INVALID_STATE")
        validation_plan = await self._validation_planner.plan(
            task=state.task,
            plan=state.plan,
            exploration=state.exploration,
            context=state.context,
            changed_files=state.changed_files,
        )
        result = await self._validation_runner.run(
            validation_plan,
            run_id=state.run_id,
            session_id=_session_id(state),
            authorization=state.approved_plan,
            repair_count=state.repair_count,
            changed=bool(state.changed_files),
        )
        update = self._evidence_update(state, validation_result=result)
        if result.status is ValidationStatus.PASS:
            return Command(update=update, goto="finalize")
        if (
            result.status is ValidationStatus.FAIL
            and result.repairable
            and state.repair_count < self._max_repair_attempts
        ):
            return Command(update=update, goto="repair_plan")
        if result.status is ValidationStatus.UNKNOWN:
            update["terminal_status"] = TerminalStatus.FAILED_VALIDATION_UNKNOWN
        elif result.repairable and state.repair_count >= self._max_repair_attempts:
            update["terminal_status"] = TerminalStatus.STOPPED_MAX_REPAIRS
        else:
            update["terminal_status"] = TerminalStatus.FAILED_VALIDATION
        return Command(update=update, goto="finalize_failed")

    async def _repair_plan(self, state: AgentState) -> dict[str, object]:
        if state.plan is None or state.context is None or state.validation_result is None:
            raise NexusError("Repair state is incomplete.", code="GRAPH_INVALID_STATE")
        self._ledger.begin_repair(state.run_id)
        attempt = state.repair_count + 1
        await self._events.emit(
            RepairStarted(
                run_id=state.run_id,
                session_id=_session_id(state),
                plan_id=state.plan.plan_id,
                plan_version=state.plan.version,
                repair_count=attempt,
                max_repair_attempts=self._max_repair_attempts,
                failure_summary=state.validation_result.summary,
            )
        )
        guidance = await self._planner.create_repair_guidance(
            RepairPlanningRequest(
                state.task,
                state.context,
                state.plan,
                state.validation_result,
                attempt,
            )
        )
        return self._evidence_update(state, repair_guidance=guidance)

    async def _finalize(self, state: AgentState) -> dict[str, object]:
        evidence = await self._collect_diff(state)
        terminal = (
            TerminalStatus.SUCCEEDED
            if evidence.error_code is None
            else TerminalStatus.FAILED
        )
        if terminal is TerminalStatus.SUCCEEDED and _is_read_only_completion(state):
            content = _latest_assistant_content(state)
        elif terminal is TerminalStatus.SUCCEEDED:
            content = "Task completed with approved changes and validation evidence."
        else:
            content = "Task failed because an exact final diff was unavailable (DIFF_UNAVAILABLE)."
        return await self._emit_final(
            state,
            terminal,
            content,
            evidence.diff,
            evidence.includes_preexisting_changes,
        )

    async def _finalize_failed(self, state: AgentState) -> dict[str, object]:
        evidence = await self._collect_diff(state)
        terminal = state.terminal_status or TerminalStatus.FAILED
        content = f"Task ended with terminal status {terminal.value}."
        if evidence.error_code is not None:
            content += " Exact final diff is unavailable (DIFF_UNAVAILABLE)."
        return await self._emit_final(
            state,
            terminal,
            content,
            evidence.diff,
            evidence.includes_preexisting_changes,
        )

    async def _collect_diff(self, state: AgentState) -> FinalDiffEvidence:
        initial = None if state.exploration is None else state.exploration.initial_git_status
        return await self._diff_collector.collect(
            run_id=state.run_id,
            session_id=_session_id(state),
            changed_files=state.changed_files,
            initial_git_status=initial,
        )

    async def _emit_final(
        self,
        state: AgentState,
        terminal: TerminalStatus,
        content: str,
        diff: str | None,
        includes_preexisting_changes: bool,
    ) -> dict[str, object]:
        status = (
            RuntimeStatus.COMPLETED
            if terminal is TerminalStatus.SUCCEEDED
            else RuntimeStatus.FAILED
        )
        event = FinalResult(
            run_id=state.run_id,
            session_id=_session_id(state),
            content=content,
            terminal_status=terminal,
            changed_files=state.changed_files,
            diff=diff,
            validation_result=state.validation_result,
            includes_preexisting_changes=includes_preexisting_changes,
        )
        await self._events.emit(event)
        update = self._evidence_update(
            state,
            status=status,
            terminal_status=terminal,
            messages=[*state.messages, ModelMessage(role="assistant", content=content)],
        )
        if state.plan is not None:
            update["plan"] = replace(
                state.plan,
                status=(
                    PlanStatus.COMPLETED
                    if terminal is TerminalStatus.SUCCEEDED
                    else PlanStatus.FAILED
                ),
            )
        return update

    async def _emit_approval(self, state: AgentState) -> None:
        pending = state.pending_plan_approval
        plan = state.plan
        if pending is None or plan is None:
            return
        await self._events.emit(
            PlanCreated(
                run_id=plan.run_id,
                session_id=plan.session_id,
                plan_id=plan.plan_id,
                plan_version=plan.version,
                plan_kind=plan.kind,
                step_summaries=_step_summaries(plan),
                replan_reason=plan.replan_reason,
            )
        )
        await self._events.emit(
            _approval_event(plan, pending)
        )

    def _evidence_update(
        self, state: AgentState, **values: object
    ) -> dict[str, object]:
        run_id = state.run_id
        self._ledger.restore(
            run_id,
            tool_call_count=state.tool_call_count,
            model_call_count=state.llm_call_count,
            step_count=state.step_count,
            repair_count=state.repair_count,
            tool_results=state.tool_results,
            token_usage=state.token_usage,
        )
        return {
            **values,
            "step_count": self._ledger.step_count(run_id),
            "tool_call_count": self._ledger.count(run_id),
            "llm_call_count": self._ledger.model_count(run_id),
            "repair_count": self._ledger.repair_count(run_id),
            "token_usage": self._ledger.token_usage(run_id),
            "tool_results": self._ledger.results(run_id),
        }

    async def _persist_failed_attempt_evidence(self, thread_id: str | None) -> None:
        if thread_id is None:
            return
        config = cast(RunnableConfig, _thread_config(thread_id))
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            return
        current = _to_agent_state(snapshot.values)
        await self._graph.aupdate_state(config, self._evidence_update(current))


def _thread_config(
    thread_id: str | None,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> RunnableConfig | None:
    if thread_id is None:
        return None
    metadata = {
        key: value
        for key, value in {"run_id": run_id, "session_id": session_id}.items()
        if value is not None
    }
    return {"configurable": {"thread_id": thread_id}, "metadata": metadata}


def _to_agent_state(value: object) -> AgentState:
    if isinstance(value, AgentState):
        return value
    if not isinstance(value, dict):
        raise NexusError("The graph returned invalid state.", code="GRAPH_INVALID_STATE")
    names = {item.name for item in fields(AgentState)}
    try:
        return AgentState(**{name: value[name] for name in names if name in value})
    except (KeyError, TypeError, ValueError) as exc:
        raise NexusError("The graph returned invalid state.", code="GRAPH_INVALID_STATE") from exc


def _session_id(state: AgentState) -> str:
    if state.session_id is None:
        raise NexusError("Day 4 Session identity is missing.", code="GRAPH_INVALID_STATE")
    return state.session_id


def _resume_input(value: object) -> PlanApprovalResumeInput:
    if not isinstance(value, dict):
        raise NexusError("Plan approval input is invalid.", code="INVALID_APPROVAL_TRANSITION")
    try:
        decision = ApprovalDecision(str(value.get("decision")))
        reason = value.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError
        return PlanApprovalResumeInput(decision, reason)
    except (TypeError, ValueError) as exc:
        raise NexusError(
            "Plan approval input is invalid.",
            code="INVALID_APPROVAL_TRANSITION",
        ) from exc


def _step_summaries(plan: Plan) -> tuple[str, ...]:
    values: list[str] = []
    for step in plan.steps:
        summary = f"{step.sequence}. {step.description}"
        if step.tool_name in {"edit_file", "apply_patch", "write_file"}:
            summary += f" | WRITE {step.tool_name} {step.target_paths[0]}"
        if step.command_argv is not None:
            summary += f" | VALIDATE argv={list(step.command_argv)!r} cwd={step.command_cwd}"
        values.append(summary)
    return tuple(values)


def _approval_event(plan: Plan, pending: ApprovalRequest) -> ApprovalRequested:
    return ApprovalRequested(
        run_id=plan.run_id,
        session_id=plan.session_id,
        approval_id=pending.approval_id,
        invocation_id=None,
        operation="approve_plan",
        risk_level=RiskLevel.WRITE,
        resource_or_command_summary=pending.resource_or_command_summary,
        subject=ApprovalSubject.PLAN,
        plan_id=plan.plan_id,
        plan_version=plan.version,
    )


def _complete_matching_plan_step(
    state: AgentState, action: ToolAction, result: ToolResult,
) -> tuple[Plan | None, PlanStep | None]:
    plan = state.plan
    if plan is None or not result.success or result.tool_name != action.tool_name:
        return None, None
    if action.tool_name not in {"read_file", "edit_file", "write_file", "apply_patch"}:
        return None, None
    path = action.arguments.get("path")
    output_path = None if result.output is None else result.output.get("path")
    if not isinstance(path, str) or output_path != path:
        return None, None
    if action.tool_name == "read_file" and _is_repeated_successful_read(
        state, AgentDecision(AgentDecisionKind.TOOL_ACTION, action, "Read file."),
    ):
        return None, None
    for index, step in enumerate(plan.steps):
        if step.status is not PlanStepStatus.PENDING or step.tool_name != action.tool_name:
            continue
        if action.tool_name != "read_file" and step.target_paths != (path,):
            continue
        completed = replace(step, status=PlanStepStatus.COMPLETED)
        steps = (*plan.steps[:index], completed, *plan.steps[index + 1:])
        return replace(plan, steps=steps), completed
    return None, None


def _record_change(
    current: tuple[ChangedFile, ...],
    result: ToolResult,
) -> tuple[ChangedFile, ...]:
    output = result.output or {}
    path = output.get("path")
    kind_value = output.get("change_kind")
    if not isinstance(path, str) or not isinstance(kind_value, str):
        raise NexusError("Editing Tool omitted change evidence.", code="GRAPH_INVALID_STATE")
    for index, item in enumerate(current):
        if item.path == path:
            updated = replace(item, latest_invocation_id=result.invocation_id)
            return (*current[:index], updated, *current[index + 1 :])
    return tuple(
        sorted(
            (
                *current,
                ChangedFile(
                    path,
                    ChangeKind(kind_value),
                    result.invocation_id,
                    result.invocation_id,
                ),
            ),
            key=lambda item: item.path,
        )
    )


def _require_current_edit_evidence(
    state: AgentState, decision: AgentDecision
) -> AgentDecision:
    action = decision.action
    if action is None or action.tool_name != "edit_file" or state.plan is None:
        return decision
    path = action.arguments.get("path")
    if not isinstance(path, str):
        return decision
    # Out-of-scope requests still reach ToolRuntime for its authoritative denial.
    if ("edit_file", path) not in state.plan.authorization_scope.allowed_write_actions:
        return decision
    if _completed_edit_without_repair(state, path):
        if _all_approved_writes_complete(state):
            return AgentDecision(
                AgentDecisionKind.TASK_READY,
                None,
                "The approved file changes are complete and ready for validation.",
            )
        return AgentDecision(
            AgentDecisionKind.CONTINUE,
            None,
            "The proposed file change is already complete; continue with remaining work.",
        )
    if _has_fresh_read_evidence(state, path):
        return decision
    return AgentDecision(
        AgentDecisionKind.TOOL_ACTION,
        ToolAction("read_file", {"path": path}),
        "Read the current target file before exact replacement.",
    )


def _observation_target_path(result: ToolResult, action: ToolAction) -> str | None:
    if result.tool_name not in {"read_file", "edit_file"}:
        return None
    output_path = None if result.output is None else result.output.get("path")
    if isinstance(output_path, str) and output_path:
        return output_path
    path = action.arguments.get("path")
    return path if isinstance(path, str) and path else None


def _observation_path(
    observation: Observation, results: dict[str, ToolResult]
) -> str | None:
    if observation.target_path is not None:
        return observation.target_path
    result = results.get(observation.invocation_id)
    path = None if result is None or result.output is None else result.output.get("path")
    return path if isinstance(path, str) else None


def _action_fingerprint(
    action: ToolAction, normalize_argv: Callable[[list[str]], list[str]],
) -> str:
    arguments = dict(action.arguments)
    if action.tool_name == "shell":
        argv = arguments.get("argv")
        if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
            arguments["argv"] = normalize_argv(argv)
        arguments.setdefault("cwd", ".")
    canonical = json.dumps(
        {"tool_name": action.tool_name, "arguments": arguments},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _semantic_retry_reason(
    state: AgentState,
    decision: AgentDecision,
    normalize_argv: Callable[[list[str]], list[str]],
) -> AgentRuntimeFeedback | None:
    if _is_repeated_successful_read(state, decision):
        return AgentRuntimeFeedback.REPEATED_SUCCESSFUL_READ
    action = decision.action
    if (
        action is not None
        and state.last_policy_denial_fingerprint is not None
        and _action_fingerprint(action, normalize_argv)
        == state.last_policy_denial_fingerprint
    ):
        return AgentRuntimeFeedback.REPEATED_POLICY_DENIAL
    return None


def _is_repeated_successful_read(state: AgentState, decision: AgentDecision) -> bool:
    action = decision.action
    if action is None or action.tool_name != "read_file":
        return False
    arguments = action.arguments
    if not set(arguments) <= {"path", "start_line", "max_lines"}:
        return False
    path = arguments.get("path")
    start = arguments.get("start_line", 1)
    count = arguments.get("max_lines", _MAX_READ_LINES)
    if (
        not isinstance(path, str)
        or type(start) is not int
        or type(count) is not int
        or start < 1
        or not 1 <= count <= _MAX_READ_LINES
    ):
        return False
    results = {item.invocation_id: item for item in state.tool_results}
    for observation in reversed(state.observations):
        if observation.repair_attempt != state.repair_count:
            continue
        if _observation_path(observation, results) != path:
            continue
        if observation.tool_name in {"edit_file", "apply_patch", "write_file"}:
            return False
        if not observation.success or observation.tool_name != "read_file":
            continue
        prior = results.get(observation.invocation_id)
        output = None if prior is None else prior.output
        if output is None or output.get("start_line") != start:
            continue
        end = output.get("end_line")
        if end == start + count - 1 or (
            output.get("truncated") is False
            and type(end) is int
            and end < start + count - 1
        ):
            return True
    return False


def _has_fresh_read_evidence(state: AgentState, path: str) -> bool:
    results = {item.invocation_id: item for item in state.tool_results}
    latest_read = -1
    latest_successful_edit = -1
    for index, observation in enumerate(state.observations):
        if _observation_path(observation, results) != path:
            continue
        if observation.tool_name == "read_file" and observation.success:
            latest_read = index
        elif observation.tool_name == "edit_file" and observation.success:
            latest_successful_edit = index
    if latest_read <= latest_successful_edit:
        return False
    failures = sum(
        1
        for observation in state.observations[latest_read + 1 :]
        if observation.tool_name == "edit_file"
        and observation.error_code == "EDIT_TARGET_NOT_FOUND"
        and _observation_path(observation, results) in {path, None}
    )
    return failures < 2


def _completed_edit_without_repair(state: AgentState, path: str) -> bool:
    results = {item.invocation_id: item for item in state.tool_results}
    return any(
        item.tool_name == "edit_file"
        and item.success
        and item.repair_attempt >= state.repair_count
        and _observation_path(item, results) == path
        for item in state.observations
    )


def _all_approved_writes_complete(state: AgentState) -> bool:
    if state.plan is None:
        return False
    results = {item.invocation_id: item for item in state.tool_results}
    completed = {
        (item.tool_name, path)
        for item in state.observations
        if item.success and (path := _observation_path(item, results)) is not None
        and item.tool_name in {"edit_file", "apply_patch", "write_file"}
    }
    return set(state.plan.authorization_scope.allowed_write_actions) <= completed


_READ_OBSERVATION_MAX_CHARS = 4096
_READ_OBSERVATION_MAX_PATH_CHARS = 512
_READ_OBSERVATION_EDGE_CHARS = 300
_READ_OBSERVATION_OMISSION = "\n...[read_file content omitted from observation]...\n"


def _read_file_observation(output: dict[str, object]) -> str | None:
    path = output.get("path")
    content = output.get("content")
    start_line = output.get("start_line")
    end_line = output.get("end_line")
    truncated = output.get("truncated")
    if not (
        isinstance(path, str)
        and isinstance(content, str)
        and isinstance(start_line, int)
        and isinstance(end_line, int)
        and isinstance(truncated, bool)
    ):
        return None
    display_path = (
        path
        if len(path) <= _READ_OBSERVATION_MAX_PATH_CHARS
        else f"{path[:256]}...{path[-253:]}"
    )
    header = (
        f"read_file {display_path} start_line={start_line} "
        f"end_line={end_line} truncated={str(truncated).lower()}:\n"
    )
    content_budget = _READ_OBSERVATION_MAX_CHARS - len(header)
    if len(content) <= content_budget:
        return header + content
    visible = content_budget - 2 * len(_READ_OBSERVATION_OMISSION)
    edge = min(_READ_OBSERVATION_EDGE_CHARS, visible // 4)
    middle = visible - 2 * edge
    middle_start = (len(content) - middle) // 2
    return (
        header + content[:edge] + _READ_OBSERVATION_OMISSION
        + content[middle_start:middle_start + middle]
        + _READ_OBSERVATION_OMISSION + content[-edge:]
    )


def _observation_summary(result: ToolResult, action: ToolAction | None = None) -> str:
    if result.success:
        if result.tool_name == "edit_file":
            return "edit_file completed successfully; this exact replacement is complete."
        if result.tool_name == "read_file" and result.output is not None:
            summary = _read_file_observation(result.output)
            if summary is not None:
                return summary
        return f"{result.tool_name} completed successfully."[:512]
    code = "UNKNOWN" if result.error is None else result.error.code
    if result.tool_name == "edit_file":
        details = {
            "EDIT_TARGET_NOT_FOUND": (
                "the exact old_str was not found in the current file; "
                "use the current read evidence for one corrected edit; "
                "after another exact-target failure, read the file again"
            ),
            "EDIT_TARGET_AMBIGUOUS": (
                "old_str matched multiple locations; provide a larger unique exact context"
            ),
            "EDIT_NO_CHANGES": "the replacement would not change the file",
        }
        detail = details.get(code)
        if (
            code == "EDIT_TARGET_NOT_FOUND"
            and result.error is not None
            and result.error.message == "The exact edit target differs only in line endings."
        ):
            detail = (
                "text matches only after line-ending normalization; use the exact CRLF/LF "
                "sequence or a unique old_str without newline characters; "
                "one correction from the current read is allowed"
            )
        if detail is not None:
            return f"edit_file failed with {code}: {detail}."[:512]
    if result.tool_name == "apply_patch" and result.error is not None:
        detail = _SAFE_PATCH_FAILURE_DETAILS.get((code, result.error.message))
        if detail is not None:
            if result.error.message == "Patch hunk counts do not match.":
                count_detail = _patch_count_detail(action)
                if count_detail is not None:
                    detail = f"{detail}; {count_detail}"
            return f"apply_patch failed with {code}: {detail}."[:512]
    return f"{result.tool_name} failed with {code}."[:512]


def _patch_count_detail(action: ToolAction | None) -> str | None:
    if action is None or action.tool_name != "apply_patch":
        return None
    patch = action.arguments.get("patch")
    if not isinstance(patch, str):
        return None
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    for index, line in enumerate(lines):
        match = _PATCH_HUNK_HEADER.match(line)
        if match is None:
            continue
        declared_old = int(match.group(2) or "1")
        declared_new = int(match.group(4) or "1")
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.startswith("@@ "):
                break
            body.append(candidate)
        actual_old = sum(
            1
            for candidate in body
            if candidate != "\\ No newline at end of file"
            and candidate.startswith((" ", "-"))
        )
        actual_new = sum(
            1
            for candidate in body
            if candidate != "\\ No newline at end of file"
            and candidate.startswith((" ", "+"))
        )
        if (declared_old, declared_new) != (actual_old, actual_new):
            return (
                f"the hunk declares old_count={declared_old} and "
                f"new_count={declared_new}, but its body contains "
                f"old_count={actual_old} and new_count={actual_new}; regenerate the full patch "
                "with header counts equal to the body counts"
            )
    return None


def _is_read_only_completion(state: AgentState) -> bool:
    plan = state.plan
    return bool(
        plan is not None
        and not state.changed_files
        and not plan.authorization_scope.allowed_write_actions
        and not plan.authorization_scope.allowed_commands
    )


def _latest_assistant_content(state: AgentState) -> str:
    if state.messages and state.messages[-1].role == "assistant":
        content = state.messages[-1].content.strip()
        if content:
            return content
    raise NexusError(
        "Read-only completion is missing the grounded Agent answer.",
        code="GRAPH_INVALID_STATE",
    )
