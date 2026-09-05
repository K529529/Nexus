"""Exact Day 4 final-diff collection through Tool Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.planning import ChangedFile, ChangeKind
from nexus.domain.tooling import ToolInvocation, ToolResult


@dataclass(frozen=True, slots=True)
class FinalDiffEvidence:
    diff: str | None
    includes_preexisting_changes: bool
    tool_results: tuple[ToolResult, ...]
    error_code: str | None = None


class FinalDiffCollector:
    def __init__(self, tool_runtime: ToolRuntime) -> None:
        self._tool_runtime = tool_runtime

    async def collect(
        self,
        *,
        run_id: str,
        session_id: str,
        changed_files: tuple[ChangedFile, ...],
        initial_git_status: ToolResult | None,
    ) -> FinalDiffEvidence:
        if not changed_files:
            return FinalDiffEvidence(
                "",
                _dirty(initial_git_status),
                (),
            )
        if initial_git_status is None or not _exact_success(initial_git_status):
            return FinalDiffEvidence(None, False, (), "DIFF_UNAVAILABLE")
        results: list[ToolResult] = []

        async def invoke(name: str, arguments: dict[str, object]) -> ToolResult:
            result = await self._tool_runtime.execute(
                ToolInvocation(str(uuid4()), name, arguments, run_id, session_id)
            )
            results.append(result)
            return result

        status = await invoke("git_status", {})
        tracked = await invoke("git_diff", {"staged": False})
        if not _exact_success(status) or not _exact_success(tracked):
            return FinalDiffEvidence(
                None,
                _dirty(initial_git_status),
                tuple(results),
                "DIFF_UNAVAILABLE",
            )
        untracked = _untracked_paths(status)
        if any(
            changed.change_kind is ChangeKind.MODIFIED and changed.path in untracked
            for changed in changed_files
        ):
            return FinalDiffEvidence(
                None,
                _dirty(initial_git_status),
                tuple(results),
                "DIFF_UNAVAILABLE",
            )
        tracked_text = _stdout(tracked)
        new_patches: list[str] = []
        for changed in changed_files:
            if changed.change_kind is not ChangeKind.ADDED:
                continue
            content = await self._read_complete(
                changed.path,
                run_id=run_id,
                session_id=session_id,
                results=results,
            )
            if content is None:
                return FinalDiffEvidence(
                    None,
                    _dirty(initial_git_status),
                    tuple(results),
                    "DIFF_UNAVAILABLE",
                )
            new_patches.append(_new_file_patch(changed.path, content))
        pieces = [piece for piece in (tracked_text, *new_patches) if piece]
        return FinalDiffEvidence(
            "\n".join(piece.rstrip("\n") for piece in pieces) + ("\n" if pieces else ""),
            _dirty(initial_git_status),
            tuple(results),
        )

    async def _read_complete(
        self,
        path: str,
        *,
        run_id: str,
        session_id: str,
        results: list[ToolResult],
    ) -> str | None:
        chunks: list[str] = []
        start_line = 1
        while True:
            result = await self._tool_runtime.execute(
                ToolInvocation(
                    str(uuid4()),
                    "read_file",
                    {"path": path, "start_line": start_line, "max_lines": 400},
                    run_id,
                    session_id,
                )
            )
            results.append(result)
            if not result.success or result.output is None:
                return None
            content = result.output.get("content")
            end_line = result.output.get("end_line")
            if not isinstance(content, str) or not isinstance(end_line, int):
                return None
            chunks.append(content)
            if result.output.get("truncated") is not True:
                return "".join(chunks)
            if end_line < start_line:
                return None
            start_line = end_line + 1


def _exact_success(result: ToolResult) -> bool:
    return bool(
        result.success
        and result.output is not None
        and result.output.get("truncated") is not True
    )


def _stdout(result: ToolResult) -> str:
    value = None if result.output is None else result.output.get("stdout")
    return value if isinstance(value, str) else ""


def _dirty(result: ToolResult | None) -> bool:
    if result is None or not _exact_success(result):
        return False
    return any(
        line.strip() and not line.startswith("## ")
        for line in _stdout(result).splitlines()
    )


def _untracked_paths(result: ToolResult) -> set[str]:
    return {
        line[3:]
        for line in _stdout(result).splitlines()
        if line.startswith("?? ") and not line[3:].startswith('"')
    }


def _new_file_patch(path: str, content: str) -> str:
    lines = content.splitlines(keepends=True)
    count = len(lines)
    header = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n"
    )
    body = "".join(
        f"+{line}" if line.endswith(("\n", "\r")) else f"+{line}\n"
        for line in lines
    )
    if lines and not lines[-1].endswith(("\n", "\r")):
        body += "\\ No newline at end of file\n"
    return header + body
