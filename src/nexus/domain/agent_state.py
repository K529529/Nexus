"""Nexus-owned graph state through Day 4."""

from dataclasses import dataclass, field

from nexus.domain.agent_decision import Observation, ToolAction
from nexus.domain.approvals import ApprovalRequest
from nexus.domain.exploration import ExplorationResult, WorkingContext
from nexus.domain.model import ModelMessage, TokenUsageAggregate
from nexus.domain.planning import (
    ApprovedPlanEvidence,
    ChangedFile,
    Plan,
    RepairGuidance,
    TerminalStatus,
)
from nexus.domain.runtime_events import RuntimeStatus
from nexus.domain.tooling import ToolResult
from nexus.domain.validation import ValidationResult


@dataclass(frozen=True, slots=True)
class AgentState:
    """Nexus-owned state passed through the graph boundary."""

    task: str
    messages: list[ModelMessage]
    run_id: str
    session_id: str | None
    status: RuntimeStatus
    exploration: ExplorationResult | None = None
    context: WorkingContext | None = None
    plan: Plan | None = None
    plan_history: tuple[Plan, ...] = ()
    observations: tuple[Observation, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    pending_tool_action: ToolAction | None = None
    latest_tool_result: ToolResult | None = None
    pending_plan_approval: ApprovalRequest | None = None
    approved_plan: ApprovedPlanEvidence | None = None
    repair_guidance: RepairGuidance | None = None
    validation_result: ValidationResult | None = None
    changed_files: tuple[ChangedFile, ...] = ()
    step_count: int = 0
    tool_call_count: int = 0
    llm_call_count: int = 0
    replan_count: int = 0
    repair_count: int = 0
    terminal_status: TerminalStatus | None = None
    token_usage: TokenUsageAggregate = field(default_factory=TokenUsageAggregate)
