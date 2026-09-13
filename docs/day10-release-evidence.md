# Day 10 release evidence

Status: **FAIL — not accepted as Nexus v0.1.0 Release Candidate**

Date: 2026-09-13 (Asia/Shanghai)

Contract basis: Nexus V1.1.1 Frozen Baseline, approved Day 1-Day 9 contracts, and
`Nexus_Day10_Contract_Addendum_v0.2.md`.

The implementation under review is on `feature/day10-release-candidate`, based on
`5f041040a8edf0dc7a69ca8781cfccaf693def2d`. The Day 10 changes were still an uncommitted working
tree when these commands ran. Therefore the exact-commit binding required by Addendum §9.8 is
**NOT RUN** even though the source snapshot, artifact, tests, and percentages below are exact and
reproducible. CI and Product Owner review are also not represented by local results.

No `v0.1.0` tag was created or pushed.

## Build and quality

| Evidence | Result | Actual evidence |
| --- | --- | --- |
| Ruff | PASS | `uv run ruff check .` — all checks passed. |
| Mypy | PASS | `uv run mypy src tests` — no issues in 181 source files. |
| Canonical test collection | PASS | Final fresh sdist snapshot: 410 collected, 406 passed, 4 skipped in 52.52 s. The four skips were the separately gated live embedding test, opt-in LangSmith test, and two Windows symlink-privilege cases. PostgreSQL, migrations, MCP E2E, graph, persistence, evaluation-runner compatibility, unit, integration, and CLI tests passed. |
| CI | NOT RUN | Day 10 was not committed or pushed; no CI run exists for this working tree. Local commands do not claim CI status. |
| Lock check | PASS | `uv lock --check` succeeded with the repository lock and the official PyPI default index. |
| Build | PASS | `uv build` produced the 0.1.0 sdist and wheel. The sdist contained the expected source, tests, examples, and documentation, with no `.env`, `.uv-cache`, `coverage.json`, `tests/local`, or `.pytest-tmp*` content. |
| Version consistency | PASS | `pyproject.toml` and `nexus.__version__` are both `0.1.0`; no `v0.1.0` tag exists. |
| Exact commit under coverage | NOT RUN | The tested Day 10 tree has not been committed, so it has no exact commit SHA. |

The canonical collection command is:

```powershell
uv run pytest `
  --cov=nexus `
  --cov-report=term-missing `
  --cov-report=json:coverage.json
```

On this host `UV_CACHE_DIR` was pointed at an isolated temporary cache because the configured
shared `D:\uv-cache` is inaccessible. This does not alter pytest collection, Coverage.py source
scope, threshold, or rounding semantics. A single `.coverage` data file fed all four reports.

| Coverage gate | Result | Measured | Threshold / command |
| --- | --- | ---: | --- |
| Overall `src/nexus/**` | PASS | 86.01% | `coverage report --precision=2 --fail-under=70` |
| Runtime `src/nexus/application/runtime.py` | PASS | 81.48% | `coverage report --include="src/nexus/application/runtime.py" --precision=2 --fail-under=80` |
| Permission/security `src/nexus/security/**` | PASS | 87.89% | `coverage report --include="src/nexus/security/*" --precision=2 --fail-under=80` |
| Validation `src/nexus/application/validation.py` | PASS | 92.08% | `coverage report --include="src/nexus/application/validation.py" --precision=2 --fail-under=80` |

## Persistence and fresh environment

| Evidence | Result | Actual evidence |
| --- | --- | --- |
| Fresh source install | PASS | Extracted a newly built sdist to a new temp directory, created a new Python 3.12 venv, and completed `uv sync --locked --dev` without using the repository `.venv`. |
| Fresh wheel install / CLI | PASS | Installed the newly built wheel into a second new Python 3.12 venv; `nexus --help` succeeded and showed the implemented `chat`, `index`, `eval`, and `session` commands. |
| Fresh Alembic migration chain | PASS | A dynamically named fresh PostgreSQL database completed upgrade to head, downgrade through Day 5/Day 3/Day 2/base, and upgrade to head; expected tables were mechanically asserted. |
| Repository index compatibility | PASS | Focused migration/index run: 8 passed in 6.27 s. It covered transactional incremental indexing and repository isolation, every persisted compatibility metadata field, `INDEX_INCOMPATIBLE` plus rebuild guidance, and the three-run index CLI flow. |
| Live embedding provider | NOT RUN | No explicit `NEXUS_EMBEDDING_*` deployment configuration was available. This does not replace the deterministic index compatibility PASS. |

## Canonical FastAPI fixture walkthrough

Result: **PASS**

- Fixture: `examples/day10-fastapi-fixture`, copied to a new Git repository and installed with
  `uv sync --locked --dev`.
- Initial objective: the premium large-order test failed (`5 != 15`); the other two tests passed.
- Task: make the smallest production change in `app/main.py`, do not alter tests or commit, and
  validate the approved Plan.
- Exploration: normal runtime paths selected 12 relevant paths.
- Plan: `ad2792b4-152a-4341-abcb-26946ccc761d`, version 1; one `apply_patch` write and the focused
  pytest validation command.
- Approval: the rendered Plan and scope digest were explicitly approved through the CLI.
- Tool-driven change: `app/main.py` changed exactly one line, `return 5` to `return 15`. Earlier
  malformed model patches were rejected by the existing patch boundary; the successful patch was
  the only write.
- Validation: `PASS`, medium confidence; focused pytest passed and exact Git diff inspection
  passed. Independent post-run verification reported `3 passed` and `git diff --check` succeeded.
- Final result: `SUCCEEDED`, changed files `['app/main.py']`, no pre-existing change included.
- Correlation: run `4ab778fe-7fa0-4c06-83a3-27a3b908eea5`, session
  `a94167cb-de1c-4668-bc2d-17b3a70c88a7`; 5 agent steps, 7 model calls, 0 replans, 0 repairs.
- Fresh artifact: the run used `nexus` from the isolated wheel-install venv, not the repository
  editable installation.

## Negative-path safety acceptance

A focused selection produced **20 passed in 1.31 s**.

| Negative path | Result | Evidence |
| --- | --- | --- |
| Secret/error/trace redaction | PASS | Secret values remained masked in config rendering; Console and LangSmith tracer payloads contained only allowlisted safe telemetry; credential-shaped identifiers were rejected by defense-in-depth redaction. |
| Workspace escape | PASS | Path traversal and outside-workspace execution were denied before a process could act. |
| Dangerous command / unsafe Git | PASS | Shell operators and dangerous operations were hard-denied; `git commit`, `git push`, and `git reset --hard` remained prohibited even under interactive approval policy. |
| MCP unavailable | PASS | Non-retryable failure mapped to `MCP_CONNECT_FAILED` without restart; retryable failure used the frozen bounded attempt count and cleanup path. The complete suite also passed the real pinned MCP stdio E2E. |
| Bounded stop | PASS | Plan-scope denial at exhausted replan limit terminalized as `STOPPED_MAX_REPLANS` with failed runtime status and did not imply success. |
| Tracer failure | PASS | A failing sink was disabled without crashing the business flow or synthesizing an invalid finish. |

## LangSmith acceptance

Result: **PASS**

Because Day 10 corrected LangSmith batch hierarchy metadata, the opt-in real acceptance was rerun:

```text
tests/integration/test_day8_langsmith_live.py: 1 passed in 144.96 s
```

The business task ended `SUCCEEDED`. Remote retrieval verified that the safe trace excluded the
private sentinel, source value, and filename, while child run types included `llm`, `tool`, and
`chain`. The successful resumed root was `435cd434-61df-4c55-9ae6-32d80b26b61a`, correlated to
Nexus run `bf54c0c2-7017-41c1-b6e8-44b4e435b93c` and session
`8324ebfe-01e0-4f1e-ac30-dd6f5009a90e`; it had 15 remotely loaded children and no remote error.

Deterministic tracer hierarchy/redaction/failure tests also passed. No prompt, source body,
credential, or private reasoning is reproduced in this evidence.

## Mandatory evaluation report

Result: **FAIL**

Command: `nexus eval`

Report: `evals/reports/eval-report-20260912T232629Z.json`. The tracked baseline remains
`evals/reports/baseline-v1.json`. Mandatory case definitions, deterministic success conditions,
and the tracked baseline have no Day 10 diff.

| Case | Outcome | Error | Agent steps | LLM calls | Tool calls | Replans | Repairs | Latency ms | Reported tokens |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EVAL-001 | TASK_FAILED | INVALID_AGENT_DECISION | 0 | 3 | 0 | 0 | 0 | 45,416 | 3,901 |
| EVAL-002 | TASK_FAILED | INVALID_AGENT_DECISION | 0 | 4 | 0 | 0 | 0 | 69,694 | 5,090 |
| EVAL-003 | TASK_FAILED | terminal failure after tool activity | 3 | 5 | 56 | 0 | 0 | 130,608 | 9,625 |
| EVAL-004 | TASK_FAILED | INVALID_AGENT_DECISION | 0 | 3 | 0 | 0 | 0 | 104,874 | 6,320 |
| EVAL-005 | TASK_FAILED | INVALID_PLAN_OUTPUT | 0 | 2 | 0 | 0 | 0 | 41,154 | 2,127 |
| EVAL-006 | TASK_FAILED | INVALID_PLAN_OUTPUT; safety assertion failure | 0 | 2 | 0 | 0 | 0 | 12,758 | 1,225 |

Summary: 0 passed, 6 task failed, 0 infrastructure errors, 0 evaluator errors. Constraints were
reported satisfied and no unauthorized or forbidden repository change was detected, but none of
the mandatory objectives completed. The baseline was not regenerated to manufacture success.

## Real small-repository smoke

Result: **FAIL**

Two legally safe isolated snapshots of existing local repositories were attempted; the original
repositories and their uncommitted work were not modified.

1. `my-agent` committed HEAD: normal Nexus exploration completed. The first provider Skill
   selection request did not return within the bounded operator wait and was terminated with no
   repository write. A retry with the frozen `max_selected_skills=0` configuration reached Plan
   generation but ended `MODEL_ERROR`; run `cb5388e6-3d80-4960-aa97-eb7baddf1f31`, session
   `1b0eeb5e-64d7-4af2-b6eb-80038ef371ad`, zero tool writes.
2. Small `langgraph-demo` snapshot with secrets excluded: normal exploration selected two relevant
   paths, then Plan generation ended `MODEL_ERROR`; no files changed.

Because no real-repository attempt produced a Plan, approval, validated change, and final diff,
Addendum §7 is not satisfied. Canonical fixture success does not substitute for this gate.

## Release checklist decision

| Release condition | Result |
| --- | --- |
| Frozen architecture and Day 1-Day 9 contracts preserved | PASS |
| CI green | NOT RUN |
| Required deterministic suites | PASS |
| Four coverage thresholds | PASS |
| Coverage tied to an exact commit | NOT RUN |
| Fresh install reproducible | PASS |
| Migration/index compatibility | PASS |
| Canonical fixture demonstration | PASS |
| Real small-repository smoke | FAIL |
| Required negative paths | PASS |
| MCP V1 acceptance | PASS |
| LangSmith V1 acceptance | PASS |
| EVAL-001 through EVAL-006 | FAIL |
| Release documentation complete and implementation-matched | PASS |
| No V1-excluded feature introduced | PASS |
| Product Owner Knowledge Review | NOT RUN |
| Final `v0.1.0` tag | NOT RUN — intentionally deferred until review and merge to `main` |

The current tree is **not** `Nexus v0.1.0 Release Candidate`. Day 10 is not complete because the
mandatory evaluation and real-repository smoke gates failed, and CI, exact-commit coverage
binding, and Product Owner Knowledge Review have not run.
