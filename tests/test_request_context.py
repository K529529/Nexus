from copy import deepcopy
from pathlib import Path

from nexus.core.context import Context, ContextBuilder, execution_budget, protected_seqs
from nexus.core.plan import SNAPSHOT_HEADER, project_plan
from nexus.core.request_context import RequestContext, logical_messages
from nexus.core.types import Limits, Message, PlanItem, PlanState, Session, ToolCall, ToolResult


def test_reproject_replaces_notes_without_treating_user_text_as_generated(tmp_path: Path) -> None:
    session = Session(tmp_path, run_id="r")
    # A real request may quote the exact marker. Filtering must use the internal type.
    session.messages = [
        Message("system", "policy", seq=1),
        Message("user", SNAPSHOT_HEADER, seq=2, run_id="r"),
    ]
    original = deepcopy(session.messages)
    context = Context(Limits())
    old = context.task_request(context.project(session, session.messages).messages, "one time")
    session.plan = PlanState("r", (PlanItem("new state", "pending"),))
    context.execution_budget = execution_budget(2, 40)
    fresh = context.task_request(context.project(session, old).messages)
    assert logical_messages(fresh) == original
    assert len(fresh) == 3 and isinstance(fresh[-1], RequestContext)
    assert "one time" not in fresh[-1].content
    assert "new state" in fresh[-1].content
    assert session.messages == original
    fresh[-1].content = "changed copy"
    fresh[-1].sections["plan"] = "changed copy"
    assert "new state" in project_plan(session, session.messages)[-1].content


def test_notes_do_not_consume_recent_group_protection_or_snapshot_references(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, run_id="r")
    session.messages = [
        Message("system", "policy", seq=1),
        Message("user", "goal", seq=2, run_id="r"),
    ]
    for i in range(3):
        session.messages.append(
            Message(
                "assistant", tool_calls=[ToolCall(str(i), "read", "{}")], seq=3 + 2 * i, run_id="r"
            )
        )
        result = ToolResult(str(i), True, {"text": "read"}).message()
        result.seq, result.run_id = 4 + 2 * i, "r"
        session.messages.append(result)
    context = Context(Limits())
    logical = ContextBuilder().build_active_context(session, "r")
    projected = context.task_request(context.project(session, logical).messages, "runtime note")
    assert protected_seqs(session, logical_messages(projected), "r") == {1, 2, 5, 6, 7, 8}
    assert ContextBuilder().build_active_context(session, "r") == logical
    assert all(not isinstance(m, RequestContext) for m in session.messages)
