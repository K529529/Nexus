"""Coding tools, session progress and configured MCP tools share one dictionary."""

from collections.abc import Callable

from nexus.core.types import (
    Emit,
    ExecutionContext,
    Json,
    Session,
    SkillSnapshot,
    Tool,
    ToolResult,
    ToolSpec,
)
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from nexus.tools.plan import create_update_plan_tool
from nexus.tools.skills import create_load_skill_tool

EXEC_SPEC = ToolSpec(
    "exec_command",
    "Execute in the configured shell under the current Local or platform sandbox policy. "
    "Use for reading/searching files, git, "
    'tests and builds. Use the required key command, not cmd: {"command":"pytest -q"}. '
    "Default cwd is workspace; stdin is closed. Inspect exit_code and "
    "truncation. command is executed directly in the shell named in Environment; "
    "do not wrap it in another shell unless deliberately switching languages. "
    "Use workdir instead of shell-specific cd syntax. "
    "timeout_ms: 1..600000 (default 120000).",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "workdir": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600000},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)
PATCH_SPEC = ToolSpec(
    "apply_patch",
    "Edit UTF-8 files inside the workspace. Pass JSON with one patch string. "
    "Start with *** Begin Patch and finish with *** End Patch on separate lines; "
    "no Markdown fences. Do not omit the End marker.\n\n"
    "Each path may appear only once: put all edits to that file in one File Section. "
    "Each @@ starts a separate Update chunk and must be followed by its own edit body "
    "containing '+' or '-'. An anchor-only or context-only chunk is invalid. "
    "Keep context and edits together until the next @@ or File Section. "
    "Use chunks in source order.\n\n"
    "Update lines: space = unchanged, '-' = removed, '+' = added. Copy current old text, "
    "including leading indentation. An @@ anchor starts searching AFTER that line; "
    "it does not define a function/class scope. Use enough context for one unique match. "
    "Add File: EVERY body line needs '+', including empty lines (a lone '+').\n\n"
    "Example:\n"
    "*** Begin Patch\n"
    "*** Update File: src/app.py\n@@ def run():\n-    old()\n+    new()\n"
    "*** Add File: tests/new_case.txt\n+fixture\n+\n+second line\n"
    "*** Delete File: obsolete.txt\n*** End Patch\n\n"
    "Use relative paths. Do not provide line numbers or hunk counts. "
    "No rename, binary or mode changes. On conflict, read the failed file and rebuild "
    "the indicated chunk against current content; do not resend unchanged old text.",
    {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Complete patch: *** Begin Patch, file sections, *** End Patch. "
                "Prefix every Add File body line with '+', including empty lines.",
            }
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
)


def native_tools() -> dict[str, Tool]:
    return {
        "exec_command": Tool(EXEC_SPEC, execute, "shell"),
        "apply_patch": Tool(PATCH_SPEC, apply_patch, "patch"),
    }


def permission_tool() -> Tool:
    async def unavailable(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
        return ToolResult(context.call_id, False, {}, "policy_denied")

    return Tool(
        ToolSpec(
            "request_permissions",
            "After a trusted read-only apply_patch policy denial, request user approval to execute "
            "that exact patch once in this workspace. Provide denied_call_id and a reason. "
            "Does not grant shell, external-directory or persistent access. "
            "Nonzero shell exits are not evidence and cannot be escalated by this tool.",
            {
                "type": "object",
                "properties": {"denied_call_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["denied_call_id", "reason"],
                "additionalProperties": False,
            },
        ),
        unavailable,
        "permission-request",
    )


def default_tools(
    session: Session,
    coding_tools: dict[str, Tool] | None = None,
    *,
    skill_loader: Callable[[str], SkillSnapshot] | None = None,
) -> dict[str, Tool]:
    """Shared assembly for local and eval execution environments."""
    return {
        **(native_tools() if coding_tools is None else coding_tools),
        "update_plan": create_update_plan_tool(session),
        **({"load_skill": create_load_skill_tool(session, skill_loader)} if skill_loader else {}),
    }
