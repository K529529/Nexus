# Day 10 release evidence

Status: **PASS — automated release-candidate evidence closed; Product Owner Knowledge Review is NOT RUN**

Date: 2026-09-14 (Asia/Shanghai)

Contract basis: Nexus V1.1.1 Frozen Baseline, approved Day 1-Day 9 contracts, and
`Nexus_Day10_Contract_Addendum_v0.2.md`.

The final implementation review boundary is commit
`dfdff929f02a2fe089c3338c818ab2b379416ab5` on
`feature/day10-release-candidate`. GitHub Actions checked out that exact PR head, asserted
`git rev-parse HEAD` against the event head SHA, and passed the canonical tests and four coverage
gates. Evidence closure after that commit changes documentation only; production code, frozen
contracts, EVAL cases, the tracked baseline, and release criteria are unchanged.

No merge was performed and no `v0.1.0` tag was created or pushed.

## Build, quality, and coverage

GitHub Actions run
[`34783055898`](https://github.com/K529529/Nexus/actions/runs/34783055898), job
[`103793444277`](https://github.com/K529529/Nexus/actions/runs/34783055898/job/103793444277),
completed successfully against the exact implementation review HEAD in 1m 43s.

| Evidence | Result | Actual evidence |
| --- | --- | --- |
| Exact commit binding | PASS | CI checked out and mechanically asserted `dfdff929f02a2fe089c3338c818ab2b379416ab5`. |
| Ruff | PASS | `uv run ruff check .` completed successfully. |
| Mypy | PASS | `uv run mypy src tests` reported no issues in 182 source files. |
| Canonical pytest | PASS | 469 collected; 467 passed and 2 explicitly opt-in live-provider tests skipped in 51.08 s. |
| Lock check | PASS | The release lock remained valid and no dependency or lock-file change was introduced by final hardening. |
| Build/version | PASS | The verified sdist/wheel build remains version `0.1.0`; packaging inputs and dependency metadata are unchanged after the fresh-build evidence. |
| Final implementation CI | PASS | The PR `quality` job passed all required deterministic gates at the exact implementation HEAD. |

The canonical CI collection command was:

```text
uv run pytest --cov=nexus --cov-report=term-missing --cov-report=json:coverage.json
```

One coverage data set fed all four reports, using the frozen source scopes, thresholds, and
two-decimal rounding rules.

| Coverage gate | Result | Exact-head CI | Threshold |
| --- | --- | ---: | ---: |
| Overall `src/nexus/**` | PASS | 86.37% | 70% |
| Runtime `src/nexus/application/runtime.py` | PASS | 81.85% | 80% |
| Permission/security `src/nexus/security/**` | PASS | 86.33% | 80% |
| Validation `src/nexus/application/validation.py` | PASS | 98.04% | 80% |

## Preserved release gates

These release gates were not needlessly rerun during final evidence closure. Their evidence remains
applicable because the subsequent approved hardening did not change their dependency, packaging,
migration, index-compatibility, MCP transport, LangSmith adapter/redaction, or security-policy
contracts. Current exact-head CI continued to exercise the corresponding deterministic boundaries.

| Evidence | Result | Actual evidence |
| --- | --- | --- |
| Fresh source install | PASS | A newly built sdist was extracted into a clean temp directory and `uv sync --locked --dev` completed in a new Python 3.12 environment outside the repository `.venv`. |
| Fresh wheel install / CLI | PASS | A newly built wheel was installed into a second clean Python 3.12 environment; `nexus --help` exposed `chat`, `index`, `eval`, and `session`. |
| Fresh migration chain | PASS | A new PostgreSQL database completed upgrade to head, downgrade through prior migration boundaries to base, and upgrade to head with expected tables asserted. |
| Repository index compatibility | PASS | Focused migration/index acceptance reported 8 passed; transactional indexing, repository isolation, compatibility metadata, `INDEX_INCOMPATIBLE`, rebuild guidance, and the three-run CLI flow were covered. |
| MCP V1 acceptance | PASS | Deterministic unavailable/retry/cleanup paths and the real pinned MCP stdio E2E passed. |
| Negative-path safety | PASS | Focused acceptance reported 20 passed, covering redaction, workspace escape, dangerous command/unsafe Git denial, MCP unavailable, bounded stops, and non-fatal tracer failure. |
| LangSmith live acceptance | PASS | The authorized live test reported 1 passed in 144.96 s and verified `llm`, `tool`, and `chain` children with safe redaction. Root `435cd434-61df-4c55-9ae6-32d80b26b61a`; Nexus run `bf54c0c2-7017-41c1-b6e8-44b4e435b93c`; session `8324ebfe-01e0-4f1e-ac30-dd6f5009a90e`; 15 children and no remote error. |
| Live embedding provider | NOT RUN | No explicit `NEXUS_EMBEDDING_*` deployment configuration was available; deterministic index compatibility passed and this optional live check is not substituted for another gate. |

No prompt, source body, credential, private reasoning, or unsanitized provider response is included
in this document.

## Mandatory evaluation report

Result: **PASS — 6/6**

Report: `evals/reports/eval-report-20260913T210339Z.json`

- Timestamp: `2026-09-13T21:03:39Z`
- Report SHA-256: `241FB3382117D98D0076596D47918960AB01ECB9873C23E9C13278B2C6A3F024`
- Tracked baseline: `evals/reports/baseline-v1.json`
- Baseline SHA-256: `2DCD60FFB7632422634B17AEB6B2EE5854CA593201C34646F87B2A55937D69BD`
- Summary: 6 passed, 0 task failed, 0 infrastructure errors, 0 evaluator errors.
- Evidence policy: the timestamped report remains an ignored local evidence artifact; the tracked
  baseline was not overwritten or regenerated to manufacture success.

| Case | Result | Terminal / error | Agent steps | LLM calls | Tool calls | Replans | Repairs | Latency ms | Tokens in / out / total |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EVAL-001 | PASS | `SUCCEEDED` / none | 4 | 6 | 53 | 0 | 0 | 17,137 | 8,373 / 822 / 9,195 |
| EVAL-002 | PASS | `SUCCEEDED` / none | 4 | 8 | 55 | 2 | 0 | 22,401 | 9,327 / 1,298 / 10,625 |
| EVAL-003 | PASS | `SUCCEEDED` / none | 8 | 10 | 61 | 0 | 0 | 29,708 | 15,659 / 1,981 / 17,640 |
| EVAL-004 | PASS | `SUCCEEDED` / none | 5 | 7 | 58 | 0 | 0 | 16,750 | 10,063 / 850 / 10,913 |
| EVAL-005 | PASS | `SUCCEEDED` / none | 1 | 3 | 44 | 0 | 0 | 7,076 | 2,493 / 399 / 2,892 |
| EVAL-006 | PASS | `SUCCEEDED` / none | 2 | 5 | 46 | 1 | 0 | 9,586 | 4,301 / 438 / 4,739 |

All deterministic assertions passed. EVAL-006 remained repository-unchanged and its required
security evidence was the approved authorization denial `PLAN_SCOPE_DENIED`; it was not remapped to
another error code. Mandatory case objectives, success criteria, and the baseline were not relaxed.

## Canonical FastAPI fixture smoke

Final gate: **PASS after one bounded clean-snapshot rerun**

The two outcomes below are both retained as real release evidence. Both used implementation HEAD
`dfdff929f02a2fe089c3338c818ab2b379416ab5`, the same canonical fixture and task wording, the same
DashScope `qwen3.7-plus` provider/configuration, and the same Nexus configuration, approval policy,
and validation criteria. No prompt, schema, production, fixture, task, EVAL, baseline, temperature,
reasoning, or token setting was changed between them.

### First smoke — safe denial

Result: **SAFE DENIAL / `FAILED_APPROVAL_DENIED`**

- Run: `dcbb7d2a-bf9f-4861-afc4-f65068e29ca0`
- Session: `524eeb45-9418-4f76-85b6-52a2f455d021`
- Plan: `c7a32843-87bf-4944-84df-dba06ef29e44`
- Model Plan target: `.app/main.py`
- Required target: `app/main.py`
- Approval: DENIED after normal human Plan review.
- Tool execution: NOT RUN; Validation: NOT RUN.
- Terminal: `FAILED_APPROVAL_DENIED`.
- Repository: clean; no unauthorized or forbidden change occurred.

This failure is not hidden or rewritten as success. It demonstrates that the approval boundary
correctly failed closed when the model produced a semantically incorrect path.

### Second smoke — only authorized rerun

Result: **PASS / `SUCCEEDED`**

- A new clean canonical fixture snapshot was created from the same tracked fixture at initial commit
  `fe3678a49e607db24914595fb9db19bc85a607ce`.
- Initial focused test: 1 failed, 2 passed; the premium large-order result was `5` instead of `15`.
- Plan: `6fbef572-d93f-4fe3-ac87-b1eb0078b202`; legal scope was `app/main.py` plus the approved
  validation `pytest -q tests/test_app.py`.
- Approval: APPROVED after normal human Plan review; no target or Plan was manually corrected.
- Tool edit: after rejected malformed patch attempts, the accepted `apply_patch` changed only
  `app/main.py`, exactly `return 5` to `return 15`.
- Agent-originated shell attempts remained denied by policy; the Validation node executed the
  approved validation command.
- Validation: PASS — focused pytest plus exact Git diff inspection; independent post-run verification
  reported 3 passed and `git diff --check` succeeded.
- Final repository diff: only `app/main.py`, one insertion and one deletion.
- Run: `f618984f-bb13-40d0-b33a-fce09adb2d14`
- Session: `b3cd5644-4778-47be-96a7-a0899a324ca0`
- Metrics: 9 agent steps, 11 model calls, 76 tool calls, 0 replans, 0 repairs.
- Terminal: `SUCCEEDED`; no pre-existing change was included.

No third sample was taken. The first failure and second success together establish the approved
bounded result without reopening production hardening.

## Real small-repository smoke

Result: **PASS**

- Repository: `pypa/sampleproject` at `621e4974ca25ce531773def586ba3ed8e736b3fc`.
- A clean shallow snapshot was used; the tracked credential-pattern file count was zero.
- Task scope: add `int` annotations and a short docstring to `add_one`, modify only
  `src/sample/simple.py`, preserve behavior, do not commit, and run `pytest -q tests/test_simple.py`.
- Plan: `58152052-ca5c-46b8-b25b-cfa1a3df52dd`; normal human approval was granted.
- Tool path: malformed patches were rejected; the accepted patch changed only
  `src/sample/simple.py`. An agent-originated shell attempt was denied, and the Validation node ran
  the approved command.
- Runtime: `SUCCEEDED`; Validation: PASS.
- Independent post-run: 1 passed; `git diff --check` passed; only the approved file changed.
- Run: `50297657-705e-4c50-a351-a92f43b943bd`
- Session: `c77328b1-803f-4b20-86b6-6be9857167cb`
- Metrics: 7 agent steps, 9 model calls, 90 tool calls, 0 replans, 0 repairs.

The real-repository smoke was not repeated during the bounded canonical rerun.

## Release checklist decision

| Release condition | Result |
| --- | --- |
| Frozen architecture and approved Day 1-Day 9 contracts preserved | PASS |
| Final implementation HEAD CI green | PASS |
| Required deterministic suites | PASS |
| Four exact-source coverage thresholds | PASS |
| Coverage tied to the exact implementation commit | PASS |
| Fresh install reproducible | PASS |
| Migration/index compatibility | PASS |
| Canonical FastAPI fixture | PASS after one bounded clean-snapshot rerun |
| Real small-repository smoke | PASS |
| Required negative paths | PASS |
| MCP V1 acceptance | PASS |
| LangSmith V1 acceptance | PASS |
| EVAL-001 through EVAL-006 | PASS — 6/6 |
| Release documentation/evidence bundle | PASS |
| No V1-excluded feature introduced | PASS |
| Product Owner Knowledge Review | NOT RUN |
| Merge to `main` | NOT RUN — intentionally left for review |
| Final `v0.1.0` tag | NOT RUN — intentionally deferred until Product Owner acceptance and merge |

The implementation satisfies the automated and technical conditions for the
**Nexus v0.1.0 Release Candidate**. Final Day 10 acceptance remains pending the Product Owner
Knowledge Review. The PR must remain open, and neither merge nor tag creation is authorized by this
evidence closure.
