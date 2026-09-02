# Nexus Day 4 Coding Loop Demo Trace

The automated fixture `tests/integration/test_day4_coding_loop.py` exercises the approved
Day 4 lifecycle against a temporary Git repository and PostgreSQL persistence:

```text
TaskStarted
-> governed selective repository Tool calls
-> RepositoryExplored
-> ContextBuilt
-> PlanCreated (visible WRITE path and exact pytest argv/cwd)
-> persisted ApprovalRequested
-> durable RunInterrupted
-> typed APPROVED resume input
-> agent_step
-> apply_patch through scoped ToolRuntime authority
-> deterministic observe
-> agent_step / TASK_READY
-> ValidationStarted
-> approved pytest command
-> governed diff inspection
-> ValidationFinished(PASS)
-> exact final git diff
-> FinalResult(SUCCEEDED)
```

The fixture asserts that the approved file alone is changed by Nexus, the focused test
passes, the exact diff contains the requested change, the workspace is not falsely marked
as pre-dirty, and the terminal result is successful. Run it with:

```powershell
uv --cache-dir .uv-cache run pytest -q tests/integration/test_day4_coding_loop.py
```
