<p>
  <img src="docs/next/assets/nexus-wordmark-violet.svg" alt="Nexus" width="360">
</p>

### Understand your project. Write code. Work together.

Nexus is an AI coding assistant for your terminal. Describe a task in plain language,
let it explore the project, edit code and run tests, then work through the results.
For larger tasks, multiple agents can share the work while you follow progress,
add guidance or queue what comes next.

Python 3.12+ · Windows / Linux · [MIT](LICENSE)

**English** / [简体中文](README.zh-CN.md) · [Quick start](#quick-start) · [Multi-Agent](#multi-agent) · [User guide](docs/next/usage-guide.md)

---

## What you can do

| Capability | Put it to work |
| --- | --- |
| **Go from issue to change** | Investigate code, fix bugs and add tests directly in your project. |
| **Share work across agents** | Use scouts for investigation, workers for implementation and reviewers for review. |
| **Stay in the conversation** | Press Enter to guide a running task or Tab to queue the next one. |
| **Pick up where you left off** | Resume saved sessions and manage ongoing work with context compaction and local memory. |
| **Connect your tools** | Choose your models, connect MCP services and use Skills for repeatable workflows. |
| **See what is happening** | Inspect tool results, agent status, session history and usage; enable profiling or tracing as needed. |

## Quick start

### 1. Install

You need Python 3.12+ and Git. Install from source in a dedicated environment:

```sh
git clone https://github.com/K529529/Nexus.git
cd Nexus
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# Linux
source .venv/bin/activate
```

Install and check the version:

```sh
python -m pip install .
python -m nexus --version
```

### 2. Connect a model

Run `nexus`. The first-launch wizard asks for a model name, Base URL, context window
and API-key environment variable name. Settings are saved in `~/.nexus/config.toml`
and shared across projects.

Set the key in the terminal that launches Nexus, then restart it:

```powershell
# Windows PowerShell
$env:NEXUS_MODEL_API_KEY = "<your key>"
```

```sh
# Linux
export NEXUS_MODEL_API_KEY="<your key>"
```

Use OpenAI-compatible, DeepSeek or DashScope services, configure model aliases and
switch with `/model`. For long sessions, configure a separate compaction model too.
See [model and context configuration](docs/next/usage-guide.md#configure).

### 3. Start in your project

Keep the environment activated and change to the project you want to work on:

```sh
cd /path/to/your/project
nexus
```

> Find why the login endpoint handles blank usernames inconsistently. Fix it, add regression coverage and run the relevant tests.

Your launch directory is the workspace. A project `AGENTS.md` can supply coding conventions
and working instructions. You can also run a single task directly:

```sh
nexus exec "Explain this project's entry points, core modules and how to run its tests"
```

<a id="multi-agent"></a>
## Multi-Agent collaboration

Give investigation, implementation and review to different agents, and follow their work
from one terminal.

| Role | Responsibility |
| --- | --- |
| **scout** | Explore code and tests, then report findings to the parent agent. |
| **worker** | Implement changes within an assigned scope, run tools and check the results. |
| **reviewer** | Review changes, inspect edge cases and report issues or recommendations. |

Multi-Agent is off by default. Add this section to your existing `~/.nexus/config.toml`,
then restart Nexus:

```toml
[multi_agent]
enabled = true
max_agents = 3
allowNestedSubagents = false
```

For example:

> Investigate the login flow and existing tests in parallel. Bring the findings together, fix the issue, then have a reviewer check the changes.

The main agent creates children, assigns scopes and brings their results together.
Each child has its own context and conversation while sharing the project directory.
You can configure models, reasoning levels and MCP access by role.
The default limit is 3 active child agents, excluding the main agent.

- **Browse the task tree:** press F6 or enter `/agents` to see task names, roles, short IDs and status.
- **Open a child conversation:** select an agent or enter `/agents ID`, then type and press Enter to guide it.
- **Return to the main agent:** press Esc or enter `/agent root`. Tab in a child view still queues work for the main agent.
- **Manage ongoing work:** use `/agent cancel ID` to cancel a subtree and `/agents history` to browse archived tasks.

Completed child conversations remain read-only. Assign distinct file scopes in the shared
workspace; scout/reviewer responsibilities do not replace system permission controls.
[Explore role configuration, nesting and permissions →](docs/next/usage-guide.md#optional-multi-agent)

## A terminal you can keep working in

| Action | Key / command |
| --- | --- |
| Guide the running task | Type your message and press **Enter** |
| Queue the next task | Type while running and press **Tab** |
| Insert a newline | **F4** / **Alt+Enter** |
| Cancel the current task | **Ctrl+C** |
| Browse history / jump to latest | **PgUp**, **PgDn** / **Ctrl+End** |
| Inspect, edit or withdraw pending input | `/pending`, `/edit ID text`, `/withdraw ID` |
| Switch model / inspect permissions | `/model` / `/permissions` |
| Resume history / start fresh | `/resume` / `/new` |
| Help / exit | `/help` / `/exit` |

Multiline editing and paste are supported. New output does not pull you away from the
history you are reading. When a completion menu is open, Tab selects a completion first.
See [commands and input](docs/next/usage-guide.md#commands-and-input).

## Make it yours

**Context and memory.** A separate compaction model summarizes older conversation while
keeping recent work available. Long-term memory lives in local Markdown files; inspect or
toggle it with `/memory`. Memory is on by default and may download retrieval dependencies
and models on first use. Run `nexus memory off` before starting if you do not need it.

**Tools and extensions.** Connect existing tools through MCP and provide reusable task
instructions through Skills. Web tools and external tracing are explicitly opt-in.
[Tools, Skills and MCP →](docs/next/usage-guide.md#tools-skills-and-mcp)

**Execution permissions.** Local mode uses your current user permissions by default.
Read-only and workspace-write modes use Docker on Linux/WSL and the Native backend on
Windows. Windows enforcement is partial and its network access is unrestricted.
See [permissions and recovery](docs/next/usage-guide.md#permissions-and-recovery).

**Sessions and records.** Continue a saved session with `nexus resume`, inspect run metrics
with `nexus --profile`, or emit JSON events with `nexus exec "task" --json`.
[Logs and observability →](docs/next/usage-guide.md#logs-reports-and-observability)

## Learn more and contribute

[User guide](docs/next/usage-guide.md) ·
[Architecture walkthrough](docs/next/engineering-walkthrough.md) ·
[Development guide](docs/next/development-and-release.md) ·
[Report an issue](https://github.com/K529529/Nexus/issues)

Nexus is available under the [MIT License](LICENSE).
