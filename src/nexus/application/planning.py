"""Model-backed Day 4 Planner and structured Agent-decision adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from nexus.application.execution_ledger import ToolExecutionLedger
from nexus.domain.agent_decision import (
    AgentDecision,
    AgentDecisionKind,
    AgentDecisionRequest,
    ToolAction,
)
from nexus.domain.model import ModelMessage
from nexus.domain.planning import (
    Plan,
    PlanKind,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    RepairGuidance,
    compute_scope_digest,
    derive_authorization_scope,
)
from nexus.domain.ports.model_gateway import ModelGateway
from nexus.domain.ports.planning import PlanningRequest, RepairPlanningRequest
from nexus.domain.tooling import ApprovalDecision
from nexus.errors import ModelError


class ModelPlanner:
    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        normalize_argv: Callable[[list[str]], list[str]] | None = None,
        ledger: ToolExecutionLedger | None = None,
    ) -> None:
        self._model_gateway = model_gateway
        self._normalize_argv = normalize_argv or (lambda argv: list(argv))
        self._ledger = ledger

    async def create_plan(self, request: PlanningRequest) -> Plan:
        try:
            if self._ledger is not None:
                self._ledger.begin_model(request.run_id)
            response = await self._model_gateway.complete(
                [
                    ModelMessage(
                        role="system",
                        content=(
                            "Return only one JSON object for a bounded coding Plan. "
                            "Schema: {rationale_summary:string,steps:[{description:string,"
                            "tool_name:string|null,target_paths:[string],"
                            "command_argv:[string]|null,command_cwd:string|null}]}. "
                            "Use apply_patch for existing files, write_file for new files, "
                            "and shell only for exact validation commands. Prefix validation "
                            "descriptions with TEST:, BUILD:, LINT:, TYPE_CHECK:, "
                            "GENERATED_TARGETED_TEST:, BASIC_EXECUTION:, or "
                            "REPOSITORY_COMMAND:. Do not include patch/file bodies."
                        ),
                    ),
                    ModelMessage(role="user", content=_planning_payload(request)),
                ]
            )
            payload = _json_object(response.content)
            _require_exact_keys(payload, {"rationale_summary", "steps"})
            steps = self._steps(payload.get("steps"))
            rationale = _required_text(payload.get("rationale_summary"))
            if request.kind is PlanKind.INITIAL:
                if request.reason is not None:
                    raise ValueError("INITIAL planning request is inconsistent.")
                plan_id = str(uuid4())
                version = 1
                reason = None
            else:
                previous_plan = request.previous_plan
                if previous_plan is None or not (request.reason or "").strip():
                    raise ValueError("REPLAN request is incomplete.")
                if (
                    request.run_id != previous_plan.run_id
                    or request.session_id != previous_plan.session_id
                ):
                    raise ValueError("REPLAN cannot change Run or Session identity.")
                plan_id = previous_plan.plan_id
                version = previous_plan.version + 1
                reason = request.reason
            scope = derive_authorization_scope(steps)
            return Plan(
                plan_id,
                request.run_id,
                request.session_id,
                version,
                request.kind,
                PlanStatus.CREATED,
                ApprovalDecision.PENDING,
                steps,
                scope,
                rationale,
                reason,
                None,
                compute_scope_digest(plan_id, version, scope),
                datetime.now(UTC),
                None,
            )
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(
                "The model returned an invalid Plan.",
                code="INVALID_PLAN_OUTPUT",
                retryable=True,
            ) from exc

    async def create_repair_guidance(
        self,
        request: RepairPlanningRequest,
    ) -> RepairGuidance:
        try:
            if self._ledger is not None:
                self._ledger.begin_model(request.plan.run_id)
            response = await self._model_gateway.complete(
                [
                    ModelMessage(
                        role="system",
                        content=(
                            "Return only JSON repair guidance with schema "
                            "{failure_summary:string,steps:[PlanStep-like objects]}. "
                            "Use only Tool/path and exact validation command actions already "
                            "present in the approved Plan. Patch bodies are chosen later."
                        ),
                    ),
                    ModelMessage(role="user", content=_repair_payload(request)),
                ]
            )
            payload = _json_object(response.content)
            _require_exact_keys(payload, {"failure_summary", "steps"})
            steps = self._steps(payload.get("steps"))
            scope = derive_authorization_scope(steps)
            approved = request.plan.authorization_scope
            writes_expand = not set(scope.allowed_write_actions) <= set(
                approved.allowed_write_actions
            )
            commands_expand = not set(scope.allowed_commands) <= set(
                approved.allowed_commands
            )
            if writes_expand or commands_expand:
                raise ModelError(
                    "Repair guidance attempted to expand approved Plan scope.",
                    code="REPAIR_SCOPE_EXPANSION",
                )
            return RepairGuidance(
                request.plan.plan_id,
                request.plan.version,
                request.repair_attempt,
                steps,
                _required_text(payload.get("failure_summary")),
            )
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(
                "The model returned invalid Repair guidance.",
                code="INVALID_PLAN_OUTPUT",
                retryable=True,
            ) from exc

    def _steps(self, value: object) -> tuple[PlanStep, ...]:
        if not isinstance(value, list) or not value:
            raise ValueError("Plan steps must be a non-empty list.")
        steps: list[PlanStep] = []
        for sequence, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise ValueError("Each Plan step must be an object.")
            _require_exact_keys(
                item,
                {
                    "description",
                    "tool_name",
                    "target_paths",
                    "command_argv",
                    "command_cwd",
                },
            )
            tool_name = item.get("tool_name")
            if tool_name is not None and not isinstance(tool_name, str):
                raise ValueError("Plan step tool_name is invalid.")
            raw_paths = item.get("target_paths", [])
            if not isinstance(raw_paths, list) or not all(
                isinstance(path, str) for path in raw_paths
            ):
                raise ValueError("Plan step target_paths are invalid.")
            raw_argv = item.get("command_argv")
            if raw_argv is not None and (
                not isinstance(raw_argv, list)
                or not raw_argv
                or not all(isinstance(part, str) and part for part in raw_argv)
            ):
                raise ValueError("Plan step command_argv is invalid.")
            argv = None if raw_argv is None else tuple(self._normalize_argv(raw_argv))
            cwd = item.get("command_cwd")
            if argv is not None and cwd is None:
                cwd = "."
            if cwd is not None and not isinstance(cwd, str):
                raise ValueError("Plan step command_cwd is invalid.")
            steps.append(
                PlanStep(
                    str(uuid4()),
                    sequence,
                    _required_text(item.get("description")),
                    tool_name,
                    tuple(sorted(set(raw_paths))),
                    argv,
                    cwd,
                    PlanStepStatus.PENDING,
                )
            )
        return tuple(steps)


class JsonAgentDecisionAdapter:
    def __init__(
        self,
        model_gateway: ModelGateway,
        *,
        ledger: ToolExecutionLedger | None = None,
    ) -> None:
        self._model_gateway = model_gateway
        self._ledger = ledger

    async def decide(self, request: AgentDecisionRequest) -> AgentDecision:
        try:
            if self._ledger is not None:
                self._ledger.begin_model(request.plan.run_id)
            response = await self._model_gateway.complete(
                [
                    ModelMessage(
                        role="system",
                        content=(
                            "Return only one JSON Agent decision. Schema: "
                            "{kind:TOOL_ACTION|CONTINUE|TASK_READY,summary:string,"
                            "action:{tool_name:string,arguments:object}|null}. "
                            "Return exactly one Tool action at most. Use apply_patch for an "
                            "existing file and write_file for a new file."
                        ),
                    ),
                    ModelMessage(role="user", content=_agent_payload(request)),
                ]
            )
            payload = _json_object(response.content)
            _require_exact_keys(payload, {"kind", "summary", "action"})
            kind = AgentDecisionKind(_required_text(payload.get("kind")))
            action_value = payload.get("action")
            action: ToolAction | None = None
            if action_value is not None:
                if not isinstance(action_value, dict) or set(action_value) != {
                    "tool_name",
                    "arguments",
                }:
                    raise ValueError("Agent action schema is invalid.")
                arguments = action_value.get("arguments")
                if not isinstance(arguments, dict):
                    raise ValueError("Agent Tool arguments must be an object.")
                action = ToolAction(
                    _required_text(action_value.get("tool_name")),
                    dict(arguments),
                )
            return AgentDecision(kind, action, _required_text(payload.get("summary")))
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(
                "The model returned an invalid Agent decision.",
                code="INVALID_AGENT_DECISION",
                retryable=True,
            ) from exc


def _json_object(content: str) -> dict[str, Any]:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Model output must be one JSON object.")
    return value


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A required text field is missing.")
    return value.strip()


def _require_exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("Structured model output has unexpected or missing fields.")


def _planning_payload(request: PlanningRequest) -> str:
    payload = {
        "task": request.task,
        "kind": request.kind.value,
        "reason": request.reason,
        "previous_plan_version": None
        if request.previous_plan is None
        else request.previous_plan.version,
        "repository_instructions": [
            {
                "path": item.path,
                "scope_path": item.scope_path,
                "content": item.content,
                "truncated": item.truncated,
            }
            for item in request.context.repository_instructions
        ],
        "manifests": [item.summary for item in request.context.manifest_summaries],
        "top_level_paths": list(request.context.top_level_paths),
        "files": [
            {
                "path": item.path,
                "content": item.content,
                "instructions": list(item.applicable_instruction_paths),
            }
            for item in request.context.selected_files
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _repair_payload(request: RepairPlanningRequest) -> str:
    payload = {
        "task": request.task,
        "plan_id": request.plan.plan_id,
        "plan_version": request.plan.version,
        "repair_attempt": request.repair_attempt,
        "validation_summary": request.validation_result.summary,
        "allowed_write_actions": request.plan.authorization_scope.allowed_write_actions,
        "allowed_commands": request.plan.authorization_scope.allowed_commands,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _agent_payload(request: AgentDecisionRequest) -> str:
    payload = {
        "task": request.task,
        "plan": {
            "id": request.plan.plan_id,
            "version": request.plan.version,
            "steps": [
                {
                    "description": step.description,
                    "tool_name": step.tool_name,
                    "target_paths": step.target_paths,
                    "command_argv": step.command_argv,
                    "command_cwd": step.command_cwd,
                }
                for step in request.plan.steps
            ],
        },
        "selected_files": [
            {"path": item.path, "content": item.content}
            for item in request.context.selected_files
        ],
        "repository_instructions": [
            {
                "path": item.path,
                "scope_path": item.scope_path,
                "content": item.content,
                "truncated": item.truncated,
            }
            for item in request.context.repository_instructions
        ],
        "observations": [
            {
                "tool_name": item.tool_name,
                "success": item.success,
                "error_code": item.error_code,
                "summary": item.evidence_summary,
            }
            for item in request.observations[-8:]
        ],
        "validation": None
        if request.validation_result is None
        else {
            "status": request.validation_result.status.value,
            "summary": request.validation_result.summary,
        },
        "repair": None
        if request.repair_guidance is None
        else {
            "attempt": request.repair_guidance.repair_attempt,
            "summary": request.repair_guidance.failure_summary,
            "steps": [step.description for step in request.repair_guidance.steps],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
