"""Coding tools, session progress and configured MCP tools share one dictionary."""

from nexus.core.types import Session, Tool, ToolSpec
from nexus.tools.execution import execute
from nexus.tools.patch import apply_patch
from nexus.tools.plan import create_update_plan_tool

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
    "Apply a UTF-8 unified diff inside workspace. Use --- a/path and +++ b/path, "
    "with @@ -old,count +new,count @@ hunks; /dev/null for add/delete. No rename, "
    "binary, mode changes or Codex Begin Patch syntax. Example:\n"
    "--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
    "New file example (no extra blank lines outside hunks):\n"
    "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+pass\n",
    {
        "type": "object",
        "properties": {"patch": {"type": "string"}},
        "required": ["patch"],
        "additionalProperties": False,
    },
)


def native_tools() -> dict[str, Tool]:
    return {"exec_command": Tool(EXEC_SPEC, execute), "apply_patch": Tool(PATCH_SPEC, apply_patch)}


def default_tools(session: Session, coding_tools: dict[str, Tool] | None = None) -> dict[str, Tool]:
    """Shared assembly for local and eval execution environments."""
    return {
        **(native_tools() if coding_tools is None else coding_tools),
        "update_plan": create_update_plan_tool(session),
    }
