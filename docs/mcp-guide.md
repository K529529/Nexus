# Nexus Day 6 MCP client guide

Nexus V1 is an MCP client/host. It connects configured MCP servers and adapts their
discovered tools into the existing Nexus `ToolRegistry`; Nexus does not act as an MCP
server.

## Supported transport and configuration

Day 6 implements stdio only. Configure servers in repository or user TOML:

```toml
[mcp]
enabled = true

[[mcp.servers]]
server_id = "everything"
enabled = true
transport = "stdio"
command = "cmd"
args = [
  "/c",
  "npx",
  "-y",
  "@modelcontextprotocol/server-everything@2026.8.31",
]
inherit_environment = true
connect_timeout_seconds = 10.0
tool_timeout_seconds = 30.0
max_connect_attempts = 2

[[mcp.servers.tool_risks]]
tool_name = "echo"
risk_level = "SAFE"
```

Use `command = "npx"` and omit `"/c", "npx"` from `args` on POSIX:

```toml
command = "npx"
args = ["-y", "@modelcontextprotocol/server-everything@2026.8.31"]
```

Repository TOML has higher priority than user TOML. `mcp_servers` is one complete field:
the highest-priority source that supplies `mcp.servers` replaces the lower-priority
tuple. Servers are not merged by `server_id`. `[mcp] servers = []` explicitly clears a
lower-priority tuple. Day 6 has no MCP CLI override and no `NEXUS_MCP_SERVERS` binding.

## Naming, visibility, and risk

A discovered remote tool is registered as:

```text
mcp.<server_id>.<remote_tool_name>
```

For example, the Everything Server `echo` tool becomes `mcp.everything.echo`. Duplicate
server IDs, remote names, final registry names, and native/MCP registry collisions fail
closed; no tool silently overwrites another.

Risk comes only from an exact `tool_risks.tool_name` mapping of the server-advertised
remote name. Nexus never infers risk from a name, description, schema, model judgment, or
server identity. An unconfigured tool has effective risk `DANGEROUS`.

All successfully discovered, validated, and adapted tools enter the unified registry,
but the concrete Planner and Agent receive metadata only for tools whose effective risk
is exactly `SAFE`. `WRITE`, explicit `DANGEROUS`, and unconfigured effective-DANGEROUS
tools remain registered and are intentionally model-invisible.

- `SAFE` executes through the existing ToolRuntime and emits the normal structured
  `ToolStarted`/`ToolFinished` events.
- `DANGEROUS` is hard-denied before the adapter or server is called.
- `WRITE` is also denied before the adapter or server is called, records a denied audit,
  emits no `ApprovalRequested`, and returns `MCP_WRITE_NOT_AUTHORIZED`.

Day 6 deliberately does not extend `Plan.AuthorizationScope` with generic external WRITE
authority. A future contract must define that capability before Nexus can authorize an
MCP WRITE operation.

## Pinned acceptance server

The reproducible Day 6 reference is:

```text
server_id: everything
package: @modelcontextprotocol/server-everything@2026.8.31
transport: stdio
SAFE tool: mcp.everything.echo
```

Run the production Agent-path acceptance test with:

```powershell
uv run pytest -q tests/integration/test_day6_mcp_e2e.py -s
```

It requires `npx`, Git, and the repository's PostgreSQL test service. The test isolates
the npm cache in the OS temporary directory and uses the official npm registry so a
machine-level unwritable cache or stale mirror does not change the result.

## Failure and data-safety behavior

Connection/discovery failure prevents MCP-enabled bootstrap. Calls are bounded by the
configured timeout. Nexus never automatically replays a tool call after it may have
reached a server. Stable public error codes include `MCP_CONNECT_FAILED`,
`MCP_DISCOVERY_FAILED`, `MCP_TOOL_TIMEOUT`, `MCP_CALL_FAILED`, `MCP_INVALID_SCHEMA`,
`MCP_INVALID_RESULT`, and `MCP_TOOL_ERROR`.

MCP outputs are normalized into JSON-compatible `content`, optional
`structured_content`, and `is_error`. Text and binary-like fields are bounded before
they enter a `ToolResult`; raw SDK objects and exception details do not cross the
adapter boundary.

Do not put credentials in repository TOML. When `inherit_environment = true`, the child
may receive process/user environment values, but Nexus does not serialize them into
tool metadata, prompts, events, configuration rendering, or logs. Server commands,
arguments, stderr, SDK objects, and environment values are also excluded from Agent
metadata.

## Current limitations

Day 6 does not implement HTTP/SSE transports, OAuth/login flows, MCP resources or prompts
as Agent primitives, sampling, elicitation, parallel tool execution, automatic call
retry, dynamic model-authored server configuration, or generic MCP WRITE authorization.
The Nexus-owned async manager port preserves a future transport seam, but this guide does
not promise those features.
