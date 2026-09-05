"""Policy-safe lexical search and exact Reciprocal Rank Fusion."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import PurePosixPath
from uuid import uuid4

from pathspec import GitIgnoreSpec

from nexus.application.tool_runtime import ToolRuntime
from nexus.context.chunking import identity, language_for, normalize_text, text_hash
from nexus.domain.context import (
    ContextCandidate,
    ContextRequest,
    IndexCompatibilityStatus,
    RetrievalQuery,
    RetrievalResult,
    RetrievalSource,
)
from nexus.domain.ports.context import Chunker, LexicalSearchProvider, SemanticSearchProvider
from nexus.domain.tooling import ToolInvocation
from nexus.errors import ContextError, PermissionDeniedError

_EXCLUDED = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "target",
        "coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)


class ToolRepositoryAccess:
    def __init__(self, runtime: ToolRuntime) -> None:
        self._runtime = runtime
        self._scope: ContextVar[tuple[str, str]] = ContextVar("retrieval_tool_scope")
        self._cache: ContextVar[dict[str, str | None]] = ContextVar("retrieval_file_cache")

    @contextmanager
    def scope(self, run_id: str, session_id: str) -> Iterator[None]:
        token = self._scope.set((run_id, session_id))
        cache = self._cache.set({})
        try:
            yield
        finally:
            self._cache.reset(cache)
            self._scope.reset(token)

    async def invoke(self, name: str, arguments: dict[str, object]) -> dict[str, object] | None:
        run_id, session_id = self._scope.get()
        result = await self._runtime.execute(
            ToolInvocation(str(uuid4()), name, arguments, run_id, session_id),
        )
        if result.error is not None and result.error.code == "WORKSPACE_PATH_DENIED":
            raise PermissionDeniedError(
                "Repository read was denied by workspace policy.", code="WORKSPACE_PATH_DENIED"
            )
        if (
            name == "read_file"
            and str(arguments.get("path", "")).endswith("AGENTS.md")
            and result.error is not None
            and result.error.code == "UNSUPPORTED_FILE"
        ):
            raise ContextError(
                "Applicable repository instructions cannot be read safely.",
                code="CONTEXT_BUILD_FAILED",
            )
        return result.output if result.success else None

    async def read(self, path: str) -> str | None:
        cache = self._cache.get()
        if path in cache:
            return cache[path]
        parts: list[str] = []
        start = 1
        while True:
            output = await self.invoke(
                "read_file", {"path": path, "start_line": start, "max_lines": 400}
            )
            if output is None or not isinstance(output.get("content"), str):
                cache[path] = None
                return None
            content = str(output["content"])
            parts.append(content)
            if not output.get("truncated"):
                break
            end = output.get("end_line")
            if not isinstance(end, int) or end < start or not content:
                cache[path] = None
                return None
            start = end + 1
        cache[path] = normalize_text("".join(parts))
        return cache[path]

    async def allowed(self, path: str) -> bool:
        target = PurePosixPath(path.replace("\\", "/"))
        if (
            target.is_absolute()
            or ".." in target.parts
            or any(p in _EXCLUDED for p in target.parts)
            or target.name == "AGENTS.md"
        ):
            return False
        ignored = False
        parents = list(reversed(target.parents))
        for parent in parents:
            ignore = await self.read(str(parent / ".gitignore"))
            if ignore is None:
                continue
            spec = GitIgnoreSpec.from_lines(ignore.splitlines())
            relative = target.relative_to(parent).as_posix()
            parts = relative.split("/")
            if any(spec.match_file("/".join(parts[:i]) + "/") for i in range(1, len(parts))):
                return False
            result = spec.check_file(relative)
            if result.include is not None:
                ignored = result.include
        return not ignored


def lexical_terms(task: str) -> tuple[str, ...]:
    quoted = re.findall(r'`([^`]+)`|"([^"\n]+)"|\x27([^\x27\n]+)\x27', task)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.:/\\-]*", task)
    paths = [token for token in tokens if "/" in token or "\\" in token]
    words = re.findall(r"[^\W_]{3,}", task, flags=re.UNICODE)
    ignored = {"the", "and", "for", "with", "from", "into", "this", "that", "please"}
    ordered = [next(x for x in match if x) for match in quoted] + paths + tokens + words
    return tuple(dict.fromkeys(term for term in ordered if term.casefold() not in ignored))


class ToolLexicalSearchProvider:
    def __init__(self, access: ToolRepositoryAccess, chunker: Chunker) -> None:
        self._access = access
        self._chunker = chunker

    async def search(self, query: RetrievalQuery) -> tuple[ContextCandidate, ...]:
        candidates: dict[tuple[str, int, int, str], ContextCandidate] = {}
        for term in lexical_terms(query.text):
            output = await self._access.invoke(
                "lexical_search",
                {
                    "pattern": term,
                    "path": ".",
                    "case_sensitive": True,
                },
            )
            if output is None:
                continue
            matches = output.get("matches", [])
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                path, line = match.get("path"), match.get("line")
                if not isinstance(path, str) or not isinstance(line, int):
                    continue
                if not await self._access.allowed(path):
                    continue
                content = await self._access.read(path)
                if not content:
                    continue
                windows = self._chunker.chunk(
                    file_path=path,
                    language=language_for(path),
                    content=content,
                    file_hash=text_hash(content),
                )
                start = 1 if len(content.splitlines()) <= 120 else 1 + ((line - 1) // 100) * 100
                chunk = next((c for c in windows if c.start_line == start), None)
                if chunk is None:
                    continue
                candidate = ContextCandidate(
                    chunk, len(candidates) + 1, None, None, 0.0, (RetrievalSource.LEXICAL,)
                )
                candidates.setdefault(identity(candidate), candidate)
                if len(candidates) >= min(query.limit, 20):
                    return tuple(candidates.values())
        return tuple(candidates.values())


def rrf_merge(
    lexical: tuple[ContextCandidate, ...],
    semantic: tuple[ContextCandidate, ...],
) -> tuple[ContextCandidate, ...]:
    merged: dict[tuple[str, int, int, str], ContextCandidate] = {}
    for candidate in (*lexical, *semantic):
        key = identity(candidate)
        previous = merged.get(key)
        if previous is not None:
            candidate = replace(
                previous,
                lexical_rank=previous.lexical_rank or candidate.lexical_rank,
                semantic_rank=previous.semantic_rank or candidate.semantic_rank,
                semantic_score=(
                    previous.semantic_score
                    if previous.semantic_rank is not None
                    else candidate.semantic_score
                ),
                sources=tuple(
                    source
                    for source in RetrievalSource
                    if source in previous.sources or source in candidate.sources
                ),
            )
        ranks = [r for r in (candidate.lexical_rank, candidate.semantic_rank) if r is not None]
        merged[key] = replace(candidate, rrf_score=sum(1 / (60 + rank) for rank in ranks))
    return tuple(
        sorted(
            merged.values(),
            key=lambda c: (
                -c.rrf_score,
                min(r for r in (c.lexical_rank, c.semantic_rank) if r is not None),
                c.chunk.file_path,
                c.chunk.start_line,
                c.chunk.content_hash,
            ),
        )
    )


class HybridContextProvider:
    def __init__(
        self,
        lexical: LexicalSearchProvider,
        semantic: SemanticSearchProvider | None,
    ) -> None:
        self._lexical = lexical
        self._semantic = semantic

    async def retrieve(self, request: ContextRequest) -> RetrievalResult:
        query = RetrievalQuery(request.repository_id, request.workspace_path, request.task, 20)
        lexical = await self._lexical.search(query)
        semantic: tuple[ContextCandidate, ...] = ()
        status: str | None = None
        used = False
        if self._semantic is not None:
            try:
                compatible = await self._semantic.validate_index(request.repository_id)
                if compatible.status is IndexCompatibilityStatus.COMPATIBLE:
                    semantic = await self._semantic.search(query)
                    used = True
                else:
                    status = compatible.status.value
            except ContextError as exc:
                status = exc.code
            except Exception:
                status = "SEMANTIC_SEARCH_FAILED"
        return RetrievalResult(rrf_merge(lexical, semantic)[:12], used, status)
