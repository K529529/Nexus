# Lightweight Skills V0.1 and resume UX

User-authorized extension on 2026-10-06. This extends the original V0 exclusion of
Skills; it does not restore the old Nexus Skill/router/workflow architecture.

## Baseline and rollback

- Baseline: `34569d8b2b8fc7a9099ea7529a2c029fbafb4772`, branch `refactor/lean-agent-core`.
- Annotated local tag: `pre-skills-2026-10-06`. No commit or push performed.
- The tag preserves tracked source, dependencies and documentation, not user-local
  model configuration, keys, evaluation artifacts or unrelated untracked files.
- To inspect/run the baseline while preserving current changes, create a separate checkout:

```powershell
git worktree add ..\Nexus-pre-skills pre-skills-2026-10-06
```

Do not reset the current worktree to roll back uncommitted work. The user can push
this specific tag later with `git push origin pre-skills-2026-10-06`.

## User interface

```text
# System terminal
nexus skills list
nexus --skill pytest-regression
nexus exec "Fix the bug and add regression coverage" --skill pytest-regression

# Inside the Nexus prompt
/skills
/skill pytest-regression
/skill off
/new
/resume
```

`/skills` refreshes the local directory and shows available, explicitly selected,
and model-loaded skills. `/skill` without an argument also lists. A new explicit
selection replaces the previous selection; `off` clears explicit selection and
returns to automatic mode, not a blanket prohibition on model-selected Skills.
Model-loaded skills stay active in their run. `/new` creates a session without
inheriting the old selection. Unknown slash commands display help rather than
becoming model requests.

An interactive `--skill` or `/skill <name>` selection applies to all subsequent
runs in that session until changed/cleared. `exec --skill` applies to its single
session/task. Automatically loaded Skills are run-scoped: an ordinary new task
clears them, while explicit resume of an unfinished run restores them.

`/resume` and `nexus resume` show newest-first histories from the current workspace,
with the latest user input preview, update time (UTC), outcome and short identity.
A selection-only session with no run is shown as idle after its writer is closed.
Nine entries appear per page. Up/down browses, Enter opens, 1-9 opens a visible
entry, Esc/Ctrl+C cancels. Completed histories can be opened for a new isolated
run; unfinished histories continue the saved run. Saved tools are never replayed.
Opening a different damaged/busy history reports the error and retains the current
conversation. Selecting the current writer closes it first to release its lock.

## File format and boundaries

Install trusted, self-contained Markdown in `~/.nexus/skills/<name>/SKILL.md`.
See [pytest example](skills/pytest-regression/SKILL.md). Installation is a deliberate
file copy; there is no repository scanning, remote download, marketplace or automatic
installation. New package installs ship no automatically activated Skills.

The format follows the name/description/body convention from the
[Agent Skills specification](https://agentskills.io/specification). This is a bounded
subset, not a claim of supporting every ecosystem feature. YAML uses PyYAML safe_load,
not Python object construction or a handwritten YAML parser. Optional front matter
is inert; `allowed-tools` cannot grant permissions. Referenced scripts/assets are
not resolved/executed by the loader; this release expects a self-contained body.

Local limits (not claimed as upstream defaults):

- 32 discoverable valid Skills, deterministic name order; overflow visibly reported.
- Name: 1..64 lowercase ASCII letters/digits separated by single hyphens; matches folder.
- Description: nonblank string, at most 1024 Unicode characters.
- File: at most 20 KiB UTF-8; body: nonblank and at most 16 KiB UTF-8.
- At most four distinct active names and 32 KiB of active bodies.
- File resolution must stay inside the configured skills directory.

Discovery validates bounded files locally, but exposes only name/description to the
model. Body loading rereads the selected file. `load_skill({"name":"..."})` is a
normal Registry tool, consuming an ordinary tool call and model turn budget. Invalid
arguments/missing or invalid files return `invalid_skill`; the model can recover.
Duplicate activation returns `already_active`, without another snapshot/event.
No independent routing model, vector search, Planner, Judge or additional Agent Loop.

## State, durability and context

Session holds an explicit selected snapshot and a run-scoped tuple of model-loaded
snapshots. Snapshots are frozen and contain name, description, body, and SHA-256 of
canonical JSON `[name, description, body]`. Known configured secrets are redacted
before snapshot hashing. The hash is an integrity/version marker, not a signature.

- `skill_selected`: session-scoped event, `run_id=null`, full snapshot or null.
- `skill_loaded`: current run/call/Agent step plus full snapshot.
- `skill_catalog`: metadata-only audit record when a task has a nonempty/invalid directory.

Both selection and loading append/flush first, update authoritative state second.
Failed writes stop the run before subsequent tools. Replay verifies snapshot bounds,
hash, run/call/step and sequential pending-call association. A committed load without
a ToolResult restores the snapshot and uses existing interrupted-unknown pairing;
it does not execute again. Truncated-tail recovery copies the valid events and state.
Old logs without Skill events work unchanged.

Resume uses saved bodies even if the source changes/disappears. `/skills` refreshes
the available directory, not the active snapshot; explicitly selecting again loads
the current file. The request-only Skill section is authoritative over stale mentions
in history, is subordinate to user/project instructions, and grants no permissions.
Bodies are not copied into ToolResults or durable Message objects. The original SYSTEM
text remains unchanged; Skill guidance appears only when a catalog/active skill exists.

`Context.project_state` composes Plan and Skills consistently in normal requests,
compaction input, protected-content checks, candidate sizing, post-compaction rebuilds,
provider context-limit fallback and usage calibration. Capacity includes the catalog
and bodies; protected over-budget input fails honestly, without dropping Skill text.
Observation working-set/HOT rules and private reasoning continuation are unchanged.
Repeated projection does not accumulate sections. The logical request prefix remains
unchanged; changing state lives in the existing request suffix. Skills add real input
cost, and automatic loading may add a model turn; they are not free tokens.

Custom registry injection retains its override semantics and defaults to no host Skill
discovery. The fixed evaluation runner already uses that injection, so benchmark input
is unaffected by personal installations. Explicit internal `skill_directory` injection
can enable controlled fixtures. No eval case, validator, scoring or model setting changed.

## Verification

Tests cover parsing/bounds, explicit and automatic lifetimes, isolation, normal dispatch,
deduplication, write failures, replay/corruption/truncated tails, actual mock HTTP payloads,
reasoning privacy, compaction/fallback, budget/calibration, slash/CLI selection and resume UX.
Final Windows offline validation (2026-10-06):

- New Skill/CLI tests: 34 passed; existing Plan/Context/conversation/TUI regressions included.
- Full suite: 497 passed, 10 Docker opt-in tests skipped.
- `ruff check .`: PASS; `mypy src tests`: PASS (62 files).
- `uv lock --check`: PASS; wheel and sdist build: PASS.
- Independent venv installed the wheel plus locked production dependencies. Outside the
  source root, `nexus --help`, `nexus skills list`, and an isolated fake-model run with
  selection, source deletion, resume and off all passed. Imports verified site-packages.
- `git diff --check`: PASS.

The default Windows pytest temporary root was inaccessible; tests used fresh project-local
`--basetemp .pytest-tmp-skills-*` directories. A test initially tried to list a still-locked
Windows session as idle; it now checks after closing the writer, preserving lock behavior.
No sandbox/permission boundary was weakened in product code.

No paid model evaluation, real-model Skill selection trial, Docker evaluation, Linux run,
PyPI publication, commit or push was performed in this change. Real Skill selection quality
is not established by mocks; a small user-driven trial remains appropriate. The global
installed Nexus was not upgraded. Source and `dist/nexus_coding_agent-0.2.0-py3-none-any.whl`
contain this candidate; existing installations need an explicit upgrade/reinstall.
