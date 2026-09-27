"""Exact workspace patch collection through governed Tool Runtime reads."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from nexus.application.tool_runtime import ToolRuntime
from nexus.domain.tooling import ToolInvocation, ToolResult

_MAX_TEXT_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class FinalDiffEvidence:
    diff: str | None
    includes_preexisting_changes: bool
    tool_results: tuple[ToolResult, ...]
    error_code: str | None = None


def patch_digest(patch: str) -> str:
    return sha256(patch.encode("utf-8")).hexdigest()


class FinalDiffCollector:
    def __init__(self, tool_runtime: ToolRuntime) -> None:
        self._tool_runtime = tool_runtime

    async def collect(self, *, run_id: str, session_id: str) -> FinalDiffEvidence:
        results: list[ToolResult] = []

        async def invoke(name: str, arguments: dict[str, object]) -> ToolResult:
            result = await self._tool_runtime.execute(
                ToolInvocation(str(uuid4()), name, arguments, run_id, session_id)
            )
            results.append(result)
            return result

        status = await invoke("git_status", {"all_untracked": True})
        tracked = await invoke("git_diff", {"against_head": True})
        if not _exact_success(status) or not _exact_success(tracked):
            return _unavailable(results)
        status_text = _stdout(status)
        tracked_text = _stdout(tracked)
        if status_text is None or tracked_text is None or "Binary files " in tracked_text:
            return _unavailable(results)
        untracked = _untracked_paths(status_text)
        if untracked is None:
            return _unavailable(results)
        patches: list[str] = []
        for path in sorted(untracked):
            content = await self._read_complete(
                path, run_id=run_id, session_id=session_id, results=results
            )
            if content is None:
                return _unavailable(results)
            patches.append(_new_file_patch(path, content))
        pieces = [piece.rstrip("\n") for piece in (tracked_text, *patches) if piece]
        return FinalDiffEvidence(
            "\n".join(pieces) + ("\n" if pieces else ""), False, tuple(results)
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
        total_bytes = 0
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
            output = result.output
            content = output.get("content")
            end_line = output.get("end_line")
            if (
                output.get("path") != path
                or output.get("start_line") != start_line
                or not isinstance(content, str)
                or not isinstance(end_line, int)
                or isinstance(end_line, bool)
                or output.get("output_truncated") is True
                or not isinstance(output.get("truncated"), bool)
            ):
                return None
            if end_line != start_line + len(content.splitlines(keepends=True)) - 1:
                return None
            if start_line > 1 and not content:
                return None
            total_bytes += len(content.encode("utf-8"))
            if total_bytes > _MAX_TEXT_BYTES:
                return None
            chunks.append(content)
            if output.get("truncated") is not True:
                return "".join(chunks)
            if end_line < start_line or not content.endswith(("\n", "\r")):
                return None
            start_line = end_line + 1


def _unavailable(results: list[ToolResult]) -> FinalDiffEvidence:
    return FinalDiffEvidence(None, False, tuple(results), "DIFF_UNAVAILABLE")


def _exact_success(result: ToolResult) -> bool:
    return bool(
        result.success
        and result.output is not None
        and result.output.get("truncated") is False
        and result.output.get("output_truncated") is not True
        and isinstance(result.output.get("stdout"), str)
    )


def _stdout(result: ToolResult) -> str | None:
    value = None if result.output is None else result.output.get("stdout")
    return value if isinstance(value, str) else None


def _untracked_paths(status: str) -> set[str] | None:
    if not status:
        return set()
    if not status.endswith("\0") or "\ufffd" in status:
        return None
    records = status[:-1].split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2] != " ":
            return None
        code = record[:2]
        path = record[3:]
        if not path:
            return None
        if code == "??":
            if path.startswith("/") or "\n" in path or "\r" in path or "\\" in path:
                return None
            paths.add(path)
        if "R" in code or "C" in code:
            index += 1
            if index >= len(records) or not records[index]:
                return None
        index += 1
    return paths


def _new_file_patch(path: str, content: str) -> str:
    if not content:
        return f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..e69de29\n"
    segments = content.split("\n")
    lines = [segment + "\n" for segment in segments[:-1]]
    if segments[-1]:
        lines.append(segments[-1])
    header = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    )
    body = "".join(f"+{line}" if line.endswith("\n") else f"+{line}\n" for line in lines)
    if not content.endswith("\n"):
        body += "\\ No newline at end of file\n"
    return header + body
