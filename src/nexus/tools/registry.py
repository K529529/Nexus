"""Coding tools, session progress and configured MCP tools share one dictionary."""

from collections.abc import Callable

from nexus.core.types import Session, SkillSnapshot, Tool, ToolSpec
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from nexus.tools.plan import create_update_plan_tool
from nexus.tools.skills import create_load_skill_tool

EXEC_SPEC = ToolSpec(
    "exec_command",
    "Execute a command in the actual local shell. Use for reading/searching files, git, "
    "tests and builds. Default cwd is workspace; stdin is closed. Inspect exit_code and "
    "truncation. timeout_ms: 1..600000 (default 120000).",
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
    "Edit UTF-8 text files inside the workspace with Nexus patch format.\n\n"
    "Example:\n"
    "*** Begin Patch\n"
    "*** Update File: src/app.py\n@@ def run():\n-old()\n+new()\n"
    "*** Add File: tests/new_case.txt\n+fixture\n"
    "*** Delete File: obsolete.txt\n*** End Patch\n\n"
    "Use workspace-relative paths. Each path may appear only once; for multiple edits "
    "in one file, use multiple @@ chunks under one Update File.\n\n"
    "Update body lines start with one space for unchanged context, '-' for removed text, "
    "or '+' for added text. A bare blank line inside an Update chunk is empty unchanged "
    "context.\n\n"
    "An @@ anchor only sets where searching begins; it does not define a function/class "
    "scope. Include enough unchanged/removed context to identify the intended old block "
    "uniquely. For later chunks use bare @@ or a later anchor.\n\n"
    "Do not provide line numbers or hunk counts. No rename, binary, mode-change, or "
    "absolute-path edits.",
    {
        "type": "object",
        "properties": {"patch": {"type": "string"}},
        "required": ["patch"],
        "additionalProperties": False,
    },
)


def native_tools() -> dict[str, Tool]:
    return {"exec_command": Tool(EXEC_SPEC, execute), "apply_patch": Tool(PATCH_SPEC, apply_patch)}


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
