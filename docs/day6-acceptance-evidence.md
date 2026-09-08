# Nexus Day 6 acceptance evidence

Evidence date: 2026-09-08

Contract: `docs/spec-addenda/Nexus_Day6_Contract_Addendum_v0.3.md`

## Real pinned-server Agent path

Command:

```powershell
uv run pytest -q tests/integration/test_day6_mcp_e2e.py -s
```

Observed result:

```text
Starting default (STDIO) server...
1 passed
```

The executable test records and asserts the following production path:

```text
server_id: everything
package/version: @modelcontextprotocol/server-everything@2026.8.31
transport: stdio
selected registry tool: mcp.everything.echo
effective risk: SAFE
Planner evidence: PlanStep.tool_name == mcp.everything.echo
Agent evidence: ToolAction.tool_name == mcp.everything.echo
runtime evidence: exactly one matching ToolStarted and successful ToolFinished
observe evidence: one successful mcp.everything.echo Observation in graph state
sanitized result: structured JSON ToolResult envelope from echo
cleanup evidence: bootstrap context exited after graph completion; manager close completed
```

This is not a direct ToolRuntime fixture. It uses `bootstrap_application`, the production
concrete `ModelPlanner`, production `JsonAgentDecisionAdapter`, Day 4 LangGraph runtime,
the production ToolRuntime/registry/policy, MCP adapter and manager, and a real stdio
Everything Server process. The model gateway is deterministic as explicitly permitted by
the approved contract.

## Deterministic contract coverage

The Day 6 focused suite covers:

- TOML parsing, source replacement/fallthrough/clear behavior, validation, and secret-safe
  rendering;
- disabled startup, sequential connect, retry exhaustion, partial cleanup, idempotent
  reverse close, discovery pagination, schema rejection, and duplicate rejection;
- exact SAFE/WRITE/DANGEROUS classification with no naming heuristic;
- SAFE-visible, WRITE-registered-hidden, and DANGEROUS/unconfigured-registered-hidden
  Planner/Agent metadata;
- WRITE denied audit with `MCP_WRITE_NOT_AUTHORIZED`, no approval request/policy call, and
  no adapter/server call;
- success/error/timeout/invalid schema/invalid result/cancellation mapping;
- model-driven MCP PlanStep and ToolAction through `execute_tool → observe`;
- existing native and MCP-disabled regression paths.

Quality-gate result on the same implementation:

```text
ruff: passed
mypy --strict: passed (126 source files)
pytest --ignore=tests/local: 180 passed, 1 skipped
skip: Day 5 live embedding acceptance lacks deployment NEXUS_EMBEDDING_* values
```

Environment note: the first local launch exposed a machine-level npm cache configured to
an unwritable location and a non-authoritative mirror. The acceptance test therefore uses
a writable OS-temporary npm cache and the official npm registry. MCP configuration and
evidence contain no credentials or machine-specific absolute paths.
