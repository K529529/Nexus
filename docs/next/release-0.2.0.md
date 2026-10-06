# Nexus 0.2.0 — Next release

Status: release preparation; not yet published by this change.
Package: `nexus-coding-agent`; command: `nexus`; planned tag: `v0.2.0`.
The existing `v0.1.0` release and `pre-skills-2026-10-06` recovery tag remain unchanged.

## What ships

Nexus is a local coding agent for bounded programming tasks. A single hand-written,
message-driven loop calls a tool-capable model, executes tools in order, returns
observations and lets the model decide when to finish. No database or repository
index is required.

- Native shell execution and context-matched text patches, with bounded output,
  timeouts, process cleanup and actionable errors.
- Structured Plan/Todo progress, a bounded stagnation reminder and execution-budget
  guidance. None of these proves task correctness or forces completion.
- Request-time observation projection and safety compaction, with whole tool-call
  groups, protected recent observations and consistent input budgeting.
- Append-only local sessions, interrupted-run recovery without tool replay, and a
  recent-session selector.
- Local Markdown Skills: explicit selection across a session, automatic loading
  for the current run, immutable resume snapshots and `/skill off` to clear explicit
  selection. Off leaves automatic loading available.
- Optional stdio MCP tools through the same registry and model loop.
- Run profiles and evaluation artifacts that distinguish model completion, tool
  results and independent validator verdicts; reported cache usage retains coverage.

## Install and migrate

Requires Python 3.12+. The project targets Windows PowerShell and Linux `/bin/sh`.
After publication:

```sh
uv tool install --force "nexus-coding-agent==0.2.0"
nexus --version
```

Alternatively use a dedicated virtual environment and
`python -m pip install --upgrade "nexus-coding-agent==0.2.0"`.
Before publication, use the locally built wheel as described in the README.

Back up `~/.nexus/config.toml` before upgrading from 0.1.0 and replace old sections
with the minimal configuration in the README. Old graph/database sessions and the
legacy Skill system are not migrated. Historical sessions created by Next use its
JSONL resume path. Check command resolution if an older globally installed `nexus`
shadows the new isolated command. The configured provider bills model API calls.

## Evidence and limitations

The fixed eight-case development suite is an acceptance set, not an official
SWE-bench score. The cost-first Qwen Flash low / 16K total-completion round achieved
6/8 validator PASS, 8/8 completed and zero max-step outcomes; estimated reported-token
cost was CNY 1.536. The earlier baseline had 5/8 PASS and six max-step outcomes and
was executed in segments after Docker interruption. These are historical rounds,
not repeated reliability estimates or a newly rerun release suite.
See [full results, failures and settings](suite-hardening-evidence.md).

User-operated Windows Flaskr acceptance covered title validation, title search,
pagination and follow-up fixes. The later Skill checks covered explicit selection,
clearing selection and automatic loading; the project suite finished at 99 passing
tests. Some tasks required user feedback or tool-error recovery. These checks show
usable workflows; they do not establish that Skills alone improve success rates.

Shell and MCP execute with the user's permissions: this is not an OS sandbox.
Patch workspace confinement does not restrict shell access. Model mistakes,
over-exploration, patch conflicts, service errors and incomplete tasks remain
possible. `completed` means the model stopped normally, not that a verifier passed.
Multi-file patch I/O failures can be partial; JSONL flush is not a durable database
transaction. Local logs may contain source code and private model continuation.
Review logs before sharing.

## Release checklist

- [x] Fast-forward local `next` to the implementation at `6ef5940` without rewriting history.
- [x] Retain package name, package/runtime version 0.2.0 and existing recovery tags.
- [x] Update bilingual installation/migration instructions and include the declared MIT license.
- [x] Require offline checks in the release build before PyPI publishing.
- [x] Complete the current offline suite, Ruff, mypy and lockfile checks.
- [x] Build and inspect wheel/sdist; install outside the source checkout.
- [ ] Finish development scratch cleanup: 30 pytest directories removed; nine remain inaccessible due to Windows ACLs. They are ignored and absent from the distribution.
- [ ] Commit release preparation, push `next`, and confirm Windows/Linux CI.
- [ ] Merge the reviewed release commit into `main` and confirm its CI.
- [ ] Verify the existing PyPI Trusted Publisher configuration and `pypi` environment.
- [ ] Create `v0.2.0` on the final commit and publish the GitHub Release.
- [ ] Confirm PyPI upload and install the published version in a fresh environment.

The existing release workflow starts when a GitHub Release is **published**. A
normal branch push does not publish to PyPI. Release publication and PyPI upload
are separate observable outcomes; do not mark both done from just one success.

Development scratch directories named `.pytest-tmp-*` came from explicit pytest
`--basetemp` arguments. They are ignored and excluded from distributions. Normal
Nexus startup does not create them. Tools invoked during a user's task may still
create ordinary `.pytest_cache`, `__pycache__`, coverage or build output in that
user's project. Nexus sessions, Skills and evaluation outputs live under `~/.nexus/`.

## Preparation verification (2026-10-06)

- Local branch `next` fast-forwarded through `origin/next` to `6ef5940`; no merge
  commit, history rewrite, remote push or change to `main`.
- `uv run --frozen --offline pytest -q --basetemp <system-temp>/pytest`:
  **497 passed, 10 skipped**, 34.98 seconds. Skips are explicit Docker acceptance;
  no paid model or Docker evaluation was run in this preparation.
- `uv run --frozen --offline ruff check .`: PASS.
- `uv run --frozen --offline mypy src tests`: PASS, 62 files.
- `uv lock --check --offline`, `uv build --offline`, `git diff --check`: PASS.
- Wheel and sdist inspected: MIT license included; wheel metadata identifies
  `nexus-coding-agent` 0.2.0 with five direct dependencies. No pytest scratch,
  virtual environment, editor settings, user scratch files or artifact directories.
- Fresh wheel install outside source with isolated Python (`-I`): version/help,
  Skill module imports and `uv pip check` PASS. An initial offline install could
  not find cached PyYAML; ordinary package downloads were then allowed and the
  install completed. This was not a model request or a PyPI upload.
- Final release Windows/Linux CI and installation **from PyPI** remain NOT RUN.
  Source tests and a local wheel cannot substitute for those post-push checks.

Cleanup did not alter `.venv`, `.uv-cache`, `artifacts`, evaluation results,
user configuration, `.idea`, `bubble_sort.py` or `i_love_you.txt`. Test-created
junctions were removed as links before recursively deleting their containing
scratch directory; link targets were not traversed. The nine access-denied names
are `.pytest-tmp-completion-v01-static`, `.pytest-tmp-convergence-v01`,
`.pytest-tmp-empty-retry-targeted`, `.pytest-tmp-model-final-1005`,
`.pytest-tmp-patch-v02-1`, `.pytest-tmp-patch-v02-example`,
`.pytest-tmp-reasoning-targeted`, `.pytest-tmp-reasoning-targeted-final`,
`.pytest-tmp-system-v2-static`. Cleanup requires an account with access to them;
this does not prevent a clean Git checkout or distribution.
