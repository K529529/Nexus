import inspect
import json
from collections.abc import AsyncIterator, Sequence

import pytest

import nexus.context.budget as budget_module
import nexus.skills.selector as selector_module
from nexus.context.budget import Day5ModelInputBudgetGuard
from nexus.domain.model import ModelChunk, ModelMessage, ModelResponse
from nexus.domain.skills import SkillLocation, SkillMetadata, SkillSource
from nexus.errors import ConfigurationError, ContextError, ModelError
from nexus.skills.selector import ModelSkillSelector


class Gateway:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.messages: list[Sequence[ModelMessage]] = []
        self.error: Exception | None = None

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages.append(messages)
        if self.error is not None:
            raise self.error
        return ModelResponse(self.content)

    async def stream(self, messages: Sequence[ModelMessage]) -> AsyncIterator[ModelChunk]:
        del messages
        if False:
            yield ModelChunk("")


class Guard:
    def __init__(self, error: ContextError | None = None) -> None:
        self.calls: list[tuple[Sequence[ModelMessage], str]] = []
        self.error = error

    def ensure_fits(
        self,
        messages: Sequence[ModelMessage],
        *,
        error_code: str,
    ) -> None:
        self.calls.append((messages, error_code))
        if self.error is not None:
            raise self.error


def metadata(
    skill_id: str = "debug-python",
    source: SkillSource = SkillSource.BUILTIN,
) -> SkillMetadata:
    return SkillMetadata(
        skill_id,
        "Debug Python",
        "Diagnose Python failures.",
        "Use for Python debugging.",
        source,
        50,
        "1.0.0",
        SkillLocation(source, f"{skill_id}/SKILL.md"),
    )


async def test_selector_preserves_task_run_and_exposes_metadata_only() -> None:
    gateway = Gateway(
        '{"selected_skill_ids":["debug-python"],'
        '"selection_reason_summary":"Matches the explicit task."}'
    )
    guard = Guard()
    runs: list[str] = []
    selector = ModelSkillSelector(gateway, 2, runs.append, guard)
    task = "  debug this exact task  "
    result = await selector.select(
        run_id="unchanged-run",
        task=task,
        available_skills=(metadata(),),
    )
    assert result.selected_skill_ids == ("debug-python",)
    assert runs == ["unchanged-run"]
    assert len(guard.calls) == 1 and guard.calls[0][1] == "SKILL_SELECTION_BUDGET_EXCEEDED"
    payload = json.loads(gateway.messages[0][1].content)
    assert payload["task"] == task
    variant = payload["skills"][0]["variants"][0]
    assert set(variant) == {
        "skill_id",
        "name",
        "description",
        "usage_scenario",
        "source",
        "priority",
        "version",
    }
    assert "body" not in gateway.messages[0][1].content
    assert "relative_path" not in gateway.messages[0][1].content


async def test_selector_empty_and_disabled_paths_make_no_calls() -> None:
    for maximum, skills in ((0, (metadata(),)), (2, ())):
        gateway = Gateway()
        guard = Guard()
        runs: list[str] = []
        result = await ModelSkillSelector(gateway, maximum, runs.append, guard).select(
            run_id="run",
            task="task",
            available_skills=skills,
        )
        assert result.selected_skill_ids == ()
        assert gateway.messages == [] and guard.calls == [] and runs == []


async def test_budget_failure_precedes_ledger_and_model() -> None:
    gateway = Gateway()
    error = ContextError("too large", code="SKILL_SELECTION_BUDGET_EXCEEDED")
    guard = Guard(error)
    runs: list[str] = []
    with pytest.raises(ContextError) as caught:
        await ModelSkillSelector(gateway, 2, runs.append, guard).select(
            run_id="run",
            task="task",
            available_skills=(metadata(),),
        )
    assert caught.value.code == "SKILL_SELECTION_BUDGET_EXCEEDED"
    assert runs == [] and gateway.messages == []


@pytest.mark.parametrize(
    "content",
    (
        "not json",
        "[]",
        '{}',
        '{"selected_skill_ids":[],"selection_reason_summary":"none","extra":1}',
        '{"selected_skill_ids":["missing"],"selection_reason_summary":"unknown"}',
        '{"selected_skill_ids":["debug-python","debug-python"],'
        '"selection_reason_summary":"duplicate"}',
        '{"selected_skill_ids":"debug-python","selection_reason_summary":"wrong type"}',
        '{"selected_skill_ids":[1],"selection_reason_summary":"wrong item type"}',
        '{"selected_skill_ids":[],"selection_reason_summary":1}',
        '{"selected_skill_ids":[],"selection_reason_summary":""}',
    ),
)
async def test_invalid_structured_selection_has_no_fallback(content: str) -> None:
    gateway = Gateway(content)
    runs: list[str] = []
    with pytest.raises(ContextError) as caught:
        await ModelSkillSelector(gateway, 2, runs.append, Guard()).select(
            run_id="run-a",
            task="task",
            available_skills=(metadata(),),
        )
    assert caught.value.code == "SKILL_SELECTION_FAILED"
    assert caught.value.retryable
    assert runs == ["run-a"]


async def test_valid_two_skill_order_and_no_match_are_preserved() -> None:
    first = metadata("debug-python")
    second = metadata("write-tests")
    gateway = Gateway(
        '{"selected_skill_ids":["write-tests","debug-python"],'
        '"selection_reason_summary":"Both Skills match in this order."}'
    )
    result = await ModelSkillSelector(gateway, 2, lambda _: None, Guard()).select(
        run_id="run",
        task="task",
        available_skills=(first, second),
    )
    assert result.selected_skill_ids == ("write-tests", "debug-python")

    gateway.content = '{"selected_skill_ids":[],"selection_reason_summary":"No match."}'
    no_match = await ModelSkillSelector(gateway, 2, lambda _: None, Guard()).select(
        run_id="run",
        task="task",
        available_skills=(first, second),
    )
    assert no_match.selected_skill_ids == ()


async def test_over_limit_and_overlong_summary_fail_without_fallback() -> None:
    skills = (metadata("debug-python"), metadata("write-tests"))
    for maximum, content in (
        (
            1,
            '{"selected_skill_ids":["debug-python","write-tests"],'
            '"selection_reason_summary":"Too many."}',
        ),
        (
            2,
            json.dumps(
                {
                    "selected_skill_ids": [],
                    "selection_reason_summary": "x" * 1001,
                }
            ),
        ),
    ):
        with pytest.raises(ContextError) as caught:
            await ModelSkillSelector(Gateway(content), maximum, lambda _: None, Guard()).select(
                run_id="run",
                task="task",
                available_skills=skills,
            )
        assert caught.value.code == "SKILL_SELECTION_FAILED"


async def test_provider_failure_is_preserved_and_accounted_per_run() -> None:
    counts: dict[str, int] = {}

    def begin(run_id: str) -> None:
        counts[run_id] = counts.get(run_id, 0) + 1

    first = Gateway('{"selected_skill_ids":[],"selection_reason_summary":"No match."}')
    second = Gateway()
    second.error = ModelError("failed")
    selector_a = ModelSkillSelector(first, 2, begin, Guard())
    selector_b = ModelSkillSelector(second, 2, begin, Guard())
    await selector_a.select(run_id="run-a", task="a", available_skills=(metadata(),))
    with pytest.raises(ModelError):
        await selector_b.select(run_id="run-b", task="b", available_skills=(metadata(),))
    assert counts == {"run-a": 1, "run-b": 1}


async def test_configuration_failure_is_preserved_after_accounting() -> None:
    gateway = Gateway()
    gateway.error = ConfigurationError("invalid provider configuration")
    runs: list[str] = []
    with pytest.raises(ConfigurationError):
        await ModelSkillSelector(gateway, 2, runs.append, Guard()).select(
            run_id="run",
            task="task",
            available_skills=(metadata(),),
        )
    assert runs == ["run"]


def test_selector_depends_on_budget_port_not_bounded_context_manager() -> None:
    source = inspect.getsource(selector_module)
    assert "ModelInputBudgetGuard" in source
    assert "BoundedContextManager" not in source


def test_budget_guard_reuses_day5_counter_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = (ModelMessage("user", "unchanged"),)
    calls: list[Sequence[ModelMessage]] = []

    def count(values: Sequence[ModelMessage]) -> int:
        calls.append(values)
        return 10

    monkeypatch.setattr(budget_module, "model_input_tokens", count)
    guard = Day5ModelInputBudgetGuard(10)
    guard.ensure_fits(messages, error_code="CUSTOM")
    assert calls == [messages]
    assert messages == (ModelMessage("user", "unchanged"),)
    with pytest.raises(ContextError) as caught:
        Day5ModelInputBudgetGuard(9).ensure_fits(messages, error_code="CUSTOM")
    assert caught.value.code == "CUSTOM"
