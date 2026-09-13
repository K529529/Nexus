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


class QueueGateway(Gateway):
    def __init__(self, *responses: str) -> None:
        super().__init__()
        self.responses = list(responses)

    async def complete(self, messages: Sequence[ModelMessage]) -> ModelResponse:
        self.messages.append(messages)
        return ModelResponse(self.responses.pop(0))


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
    selector = ModelSkillSelector(gateway, 2, guard)
    task = "  debug this exact task  "
    result = await selector.select(
        run_id="unchanged-run",
        task=task,
        available_skills=(metadata(),),
    )
    assert result.selected_skill_ids == ("debug-python",)
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
        result = await ModelSkillSelector(gateway, maximum, guard).select(
            run_id="run",
            task="task",
            available_skills=skills,
        )
        assert result.selected_skill_ids == ()
        assert gateway.messages == [] and guard.calls == []


async def test_budget_failure_precedes_model() -> None:
    gateway = Gateway()
    error = ContextError("too large", code="SKILL_SELECTION_BUDGET_EXCEEDED")
    guard = Guard(error)
    with pytest.raises(ContextError) as caught:
        await ModelSkillSelector(gateway, 2, guard).select(
            run_id="run",
            task="task",
            available_skills=(metadata(),),
        )
    assert caught.value.code == "SKILL_SELECTION_BUDGET_EXCEEDED"
    assert gateway.messages == []


@pytest.mark.parametrize(
    ("content", "category"),
    (
        ("not json", "JSON_DECODE"),
        ("[]", "TOP_LEVEL_TYPE"),
        ('{}', "TOP_LEVEL_KEYS"),
        (
            '{"selected_skill_ids":[],"selection_reason_summary":"none","extra":1}',
            "TOP_LEVEL_KEYS",
        ),
        (
            '{"selected_skill_ids":["missing"],"selection_reason_summary":"unknown"}',
            "SKILL_UNKNOWN_ID",
        ),
        (
            '{"selected_skill_ids":["debug-python","debug-python"],'
            '"selection_reason_summary":"duplicate"}',
            "SKILL_DUPLICATE_IDS",
        ),
        (
            '{"selected_skill_ids":"debug-python","selection_reason_summary":"wrong type"}',
            "SKILL_IDS_SCHEMA",
        ),
        (
            '{"selected_skill_ids":[1],"selection_reason_summary":"wrong item type"}',
            "SKILL_IDS_SCHEMA",
        ),
        ('{"selected_skill_ids":[],"selection_reason_summary":1}', "SKILL_SUMMARY_SCHEMA"),
        ('{"selected_skill_ids":[],"selection_reason_summary":""}', "SKILL_SUMMARY_SCHEMA"),
    ),
)
async def test_invalid_structured_selection_stops_after_one_retry(
    content: str,
    category: str,
) -> None:
    gateway = Gateway(content)
    with pytest.raises(ContextError) as caught:
        await ModelSkillSelector(gateway, 2, Guard()).select(
            run_id="run-a",
            task="task",
            available_skills=(metadata(),),
        )
    assert caught.value.code == "SKILL_SELECTION_FAILED"
    assert caught.value.retryable
    assert f"Category: {category}" in str(caught.value)


async def test_invalid_selection_retries_once_and_returns_valid_replacement() -> None:
    gateway = QueueGateway(
        "SENSITIVE invalid selection",
        '{"selected_skill_ids":["debug-python"],'
        '"selection_reason_summary":"Matches the task."}',
    )

    result = await ModelSkillSelector(gateway, 2, Guard()).select(
        run_id="run",
        task="task",
        available_skills=(metadata(),),
    )

    assert result.selected_skill_ids == ("debug-python",)
    assert len(gateway.messages) == 2
    feedback = gateway.messages[1][-1].content
    assert "Category: JSON_DECODE" in feedback
    assert "SENSITIVE" not in feedback


async def test_retry_budget_failure_prevents_second_model_call() -> None:
    gateway = Gateway("invalid")

    class RetryGuard(Guard):
        def ensure_fits(
            self,
            messages: Sequence[ModelMessage],
            *,
            error_code: str,
        ) -> None:
            super().ensure_fits(messages, error_code=error_code)
            if len(self.calls) == 2:
                raise ContextError("too large", code=error_code)

    with pytest.raises(ContextError) as caught:
        await ModelSkillSelector(gateway, 2, RetryGuard()).select(
            run_id="run",
            task="task",
            available_skills=(metadata(),),
        )

    assert caught.value.code == "SKILL_SELECTION_BUDGET_EXCEEDED"
    assert len(gateway.messages) == 1


async def test_valid_two_skill_order_and_no_match_are_preserved() -> None:
    first = metadata("debug-python")
    second = metadata("write-tests")
    gateway = Gateway(
        '{"selected_skill_ids":["write-tests","debug-python"],'
        '"selection_reason_summary":"Both Skills match in this order."}'
    )
    result = await ModelSkillSelector(gateway, 2, Guard()).select(
        run_id="run",
        task="task",
        available_skills=(first, second),
    )
    assert result.selected_skill_ids == ("write-tests", "debug-python")

    gateway.content = '{"selected_skill_ids":[],"selection_reason_summary":"No match."}'
    no_match = await ModelSkillSelector(gateway, 2, Guard()).select(
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
            await ModelSkillSelector(Gateway(content), maximum, Guard()).select(
                run_id="run",
                task="task",
                available_skills=skills,
            )
        assert caught.value.code == "SKILL_SELECTION_FAILED"


async def test_provider_failure_is_preserved() -> None:
    first = Gateway('{"selected_skill_ids":[],"selection_reason_summary":"No match."}')
    second = Gateway()
    second.error = ModelError("failed")
    selector_a = ModelSkillSelector(first, 2, Guard())
    selector_b = ModelSkillSelector(second, 2, Guard())
    await selector_a.select(run_id="run-a", task="a", available_skills=(metadata(),))
    with pytest.raises(ModelError):
        await selector_b.select(run_id="run-b", task="b", available_skills=(metadata(),))


async def test_configuration_failure_is_preserved() -> None:
    gateway = Gateway()
    gateway.error = ConfigurationError("invalid provider configuration")
    with pytest.raises(ConfigurationError):
        await ModelSkillSelector(gateway, 2, Guard()).select(
            run_id="run",
            task="task",
            available_skills=(metadata(),),
        )


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
