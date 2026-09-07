"""Day 5 context policy, including v0.1.2's sole eviction order."""

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from pathlib import PurePosixPath

from nexus.context.builder import _applicable_instructions
from nexus.context.chunking import estimated_tokens, identity, language_for, text_hash
from nexus.context.explorer import _weakens_nexus_safety
from nexus.context.retrieval import ToolRepositoryAccess
from nexus.domain.agent_decision import Observation
from nexus.domain.context import ContextBudget, ContextCandidate, ContextRequest
from nexus.domain.exploration import RepositoryInstruction, SelectedFileContext, WorkingContext
from nexus.domain.model import ModelMessage
from nexus.domain.persistence import SessionTurn
from nexus.domain.planning import Plan
from nexus.domain.ports.context import Chunker, ContextProvider
from nexus.errors import ContextError


def context_payload(context: WorkingContext) -> dict[str, object]:
    """Only the selected view is model-visible; ranking evidence is not a second code copy."""
    return {
        "task": context.task,
        "repository_instructions": [asdict(item) for item in context.repository_instructions],
        "selected_files": [asdict(item) for item in context.selected_files],
        "manifests": [asdict(item) for item in context.manifest_summaries],
        "top_level_paths": context.top_level_paths,
        "observations": [asdict(item) for item in context.recent_observations],
        "conversation": [
            {"role": item.role, "content": item.content}
            for item in context.recent_conversation_turns
        ],
        "compacted_observations": context.compacted_observations,
        "compacted_conversation": context.compacted_conversation,
    }


def model_input_tokens(messages: Sequence[ModelMessage]) -> int:
    # Count complete serialized messages, including roles/JSON escaping, plus a small
    # conservative envelope estimate. This remains the approved character heuristic.
    serialized = json.dumps([asdict(message) for message in messages], ensure_ascii=False)
    return estimated_tokens(serialized) + 16 * len(messages) + 16


def _observation_summary(observations: Sequence[Observation]) -> str | None:
    if not observations:
        return None
    return "\n".join(
        f"{o.tool_name}: success={o.success}; error={o.error_code}; "
        f"evidence={o.evidence_summary[:256]}; replan={o.replan_reason}"
        for o in observations
    )


def _conversation_summary(turns: Sequence[SessionTurn]) -> str | None:
    return "\n".join(f"{t.role}: {t.content[:256]}" for t in turns) or None


class BoundedContextManager:
    def __init__(
        self,
        provider: ContextProvider,
        access: ToolRepositoryAccess,
        chunker: Chunker,
        budget: ContextBudget,
        max_model_input_tokens: int,
    ) -> None:
        self._provider = provider
        self._access = access
        self._chunker = chunker
        self._budget = budget
        self._maximum = max_model_input_tokens

    async def build(self, request: ContextRequest) -> WorkingContext:
        with self._access.scope(request.run_id, request.session_id):
            retrieval = await self._provider.retrieve(request)
            seeds: list[ContextCandidate] = []
            for evidence in request.exploration.relevant_files[:8]:
                if not await self._access.allowed(evidence.path):
                    continue
                content = await self._access.read(evidence.path)
                if not content:
                    continue
                chunks = self._chunker.chunk(
                    file_path=evidence.path,
                    language=language_for(evidence.path),
                    content=content,
                    file_hash=text_hash(content),
                )
                seeds.extend(
                    ContextCandidate(c, None, None, None, 0.0, ())
                    for c in chunks
                    if c.start_line <= 400
                )
            unique: dict[tuple[str, int, int, str], tuple[ContextCandidate, str]] = {}
            seed_limit_reached = False
            for candidate in seeds:
                key = identity(candidate)
                if key in unique:
                    continue
                if len(unique) >= self._budget.max_exploration_seed_chunks:
                    seed_limit_reached = True
                    continue
                unique[key] = (candidate, "exploration seed")
            for candidate in retrieval.candidates:
                unique.setdefault(identity(candidate), (candidate, "hybrid retrieval"))
            selected: list[SelectedFileContext] = []
            instructions = list(request.exploration.instructions)
            for instruction in tuple(instructions):
                if instruction.truncated:
                    content = await self._access.read(instruction.path)
                    if content is None:
                        raise ContextError(
                            "Required repository instructions could not be read.",
                            code="CONTEXT_BUILD_FAILED",
                        )
                    instructions[instructions.index(instruction)] = replace(
                        instruction,
                        content=content,
                        truncated=False,
                    )
            tokens = 0
            truncated = request.exploration.truncated or seed_limit_reached
            for candidate, reason in unique.values():
                chunk = candidate.chunk
                cost = estimated_tokens(chunk.content)
                if (
                    len(selected) >= self._budget.max_retrieved_chunks
                    or tokens + cost > self._budget.max_code_context_tokens
                ):
                    truncated = True
                    continue
                if not await self._access.allowed(chunk.file_path):
                    truncated = True
                    continue
                # Never present deleted/changed/now-ignored indexed text as current code.
                current = await self._access.read(chunk.file_path)
                if current is None or text_hash(current) != chunk.file_hash:
                    truncated = True
                    continue
                await self._instructions(chunk.file_path, instructions)
                selected.append(
                    SelectedFileContext(
                        chunk.file_path,
                        chunk.content,
                        _applicable_instructions(chunk.file_path, tuple(instructions)),
                        f"{reason}; lines {chunk.start_line}-{chunk.end_line}",
                        False,
                    )
                )
                tokens += cost
            context = WorkingContext(
                request.task,
                tuple(instructions),
                request.exploration.manifests,
                request.exploration.top_level_paths,
                tuple(selected),
                truncated,
                retrieval.candidates,
                None,
                retrieval.semantic_used,
                retrieval.semantic_status,
            )
            return await self.prepare_agent_context(
                working_context=context,
                plan=None,
                observations=(),
                conversation_turns=(),
            )

    async def _instructions(self, path: str, instructions: list[RepositoryInstruction]) -> None:
        known = {item.path for item in instructions if not item.truncated}
        for parent in reversed(PurePosixPath(path).parents):
            name = str(parent / "AGENTS.md")
            if name in known:
                continue
            content = await self._access.read(name)
            if content is None:
                if any(item.path == name for item in instructions):
                    raise ContextError(
                        "Required repository instructions could not be read.",
                        code="CONTEXT_BUILD_FAILED",
                    )
                continue
            if _weakens_nexus_safety(content):
                raise ContextError(
                    "Repository instructions conflict with Nexus safety.",
                    code="REPOSITORY_INSTRUCTION_CONFLICT",
                )
            instructions[:] = [item for item in instructions if item.path != name]
            instructions.append(
                RepositoryInstruction(
                    name,
                    str(parent),
                    len(parent.parts),
                    content,
                    False,
                )
            )
        instructions.sort(key=lambda item: (item.depth, item.path))

    async def prepare_agent_context(
        self,
        *,
        working_context: WorkingContext,
        plan: Plan | None,
        observations: Sequence[Observation],
        conversation_turns: Sequence[SessionTurn],
    ) -> WorkingContext:
        count = self._budget.max_recent_observations
        turns = tuple(sorted(conversation_turns, key=lambda turn: turn.sequence))
        recent_observations = tuple(observations[-count:]) if count else ()
        older_observations = observations[:-count] if count else observations
        context = replace(
            working_context,
            recent_observations=recent_observations,
            compacted_observations=_observation_summary(older_observations),
            recent_conversation_turns=turns[-6:],
            compacted_conversation=_conversation_summary(turns[:-6]),
        )

        def render(view: WorkingContext) -> Sequence[ModelMessage]:
            payload = context_payload(view)
            payload["plan"] = None if plan is None else asdict(plan)
            return [ModelMessage("user", json.dumps(payload, default=str, ensure_ascii=False))]

        return self.fit_model_input(context, render)

    def fit_model_input(
        self,
        context: WorkingContext,
        render: Callable[[WorkingContext], Sequence[ModelMessage]],
    ) -> WorkingContext:
        """Fit the actual final prompt, including system and adapter-specific fields.

        Rendering is pure; all reductions and their order remain owned here. The callback
        also covers Plan/repair prompts without changing their public provider contracts.
        """

        def fits() -> bool:
            return model_input_tokens(render(context)) <= self._maximum

        if fits():
            return context
        # 1. Reverse selection priority: hybrid last, seed first in the original view.
        while context.selected_files and not fits():
            context = replace(context, selected_files=context.selected_files[:-1], truncated=True)
        # 2. Older observations become bounded factual summaries before recent ones.
        while context.recent_observations and not fits():
            summary = _observation_summary(context.recent_observations[:1])
            context = replace(
                context,
                recent_observations=context.recent_observations[1:],
                truncated=True,
                compacted_observations="\n".join(
                    filter(
                        None,
                        (
                            context.compacted_observations,
                            summary,
                        ),
                    )
                ),
            )
        # 3. Conversation reduction cannot run before the observation stage.
        while context.recent_conversation_turns and not fits():
            summary = _conversation_summary(context.recent_conversation_turns[:1])
            context = replace(
                context,
                recent_conversation_turns=context.recent_conversation_turns[1:],
                compacted_conversation="\n".join(
                    filter(
                        None,
                        (
                            context.compacted_conversation,
                            summary,
                        ),
                    )
                ),
                truncated=True,
            )
        # 4. Non-authoritative repository evidence.
        while context.manifest_summaries and not fits():
            context = replace(
                context, manifest_summaries=context.manifest_summaries[:-1], truncated=True
            )
        while context.top_level_paths and not fits():
            context = replace(context, top_level_paths=context.top_level_paths[:-1], truncated=True)
        # 5. Remaining optional compacted context, oldest entries first.
        for field in ("compacted_conversation", "compacted_observations"):
            while getattr(context, field) and not fits():
                value = str(getattr(context, field))
                reduced = "\n".join(value.splitlines()[1:]) or None
                context = (
                    replace(context, compacted_conversation=reduced, truncated=True)
                    if field == "compacted_conversation"
                    else replace(context, compacted_observations=reduced, truncated=True)
                )
        if not fits():
            raise ContextError(
                "Required authoritative context exceeds the configured model input budget.",
                code="CONTEXT_BUILD_FAILED",
            )
        return context
