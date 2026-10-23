# Stage 2 final exit fixes · 2026-10-24

Base: `682db957`; branch: `codex/nexus-v0.3-stage2-long-term-memory`.

## Changes

- Only the copied Exit Summary ModelConfig overrides `reasoning_effort="low"`.
  Main Coding Run and independent Compaction Model configuration remain unchanged.
- Exit Summary events and its failure warning use `Events(..., publish=False)`:
  append/redact JSONL normally, skip terminal consumers and stderr fallback.
  Existing PromptSession/Rich Live teardown order stays unchanged.
- Original prompt, validator, four sections, 20% input budget, 2,048 output cap,
  10-second request deadline and bounded one-second model cleanup are unchanged.
- Existing acceptance script now reads exit diagnostics from JSONL; new opt-in
  `scripts/memory_exit_smoke.py` verifies the actual HTTP payload and captures terminal output.
  No changes to Loop, MemoryStore/QMD, tools, Handoff, Snapshot or compaction thresholds.

## Automated verification

- Targeted Daily/exit tests: **44 passed**.
- Full current workspace: **671 passed, 10 skipped**, 51.58s; see `pytest-workspace.txt`.
  This includes four pre-existing untracked evaluation-history tests.
- Independent staged candidate: **667 passed, 10 skipped**, 52.21s;
  see `pytest-candidate.txt` for the exact delivered test set.
- Ruff: PASS on workspace and staged candidate.
- Mypy: PASS, workspace 94 source files / staged candidate 92 source files.
- Tests cover actual serialized high/high/low requests across two Coding Runs and
  Exit Summary, unchanged main/compaction configuration, both slash-exit and EOF,
  silent success/timeout/API/disk/invalid-output paths, retained JSONL and warnings,
  stderr fallback suppression, and bounded model cleanup. Existing real PromptSession
  `/exit` and Ctrl+D tests confirm input has stopped before summary begins.
- The ten skips are opt-in Docker isolation acceptance tests. This round ran locally
  on Windows; Linux/macOS and Docker live acceptance were not run.

## Real FilePilot acceptance (one attempt)

Used an isolated copy of the prior compacted FilePilot dogfood session; no Coding Run,
new compaction, QMD startup or original workspace/memory writes. Original copied source
SHA-256: `f54cf874d8111e7051f9a6d5da8f7757ba1d2a6ce6bf0a4b793021d2a4af5e37`.

| Item | Observed result |
| --- | --- |
| Actual HTTP model / effort | `deepseek-flash` / `low` |
| Input budget / actual reported input | 25,600 / **25,339 Tokens** |
| Output cap / actual reported output | 2,048 / **1,120 Tokens** |
| Reported total / cached input | 26,459 / 0 Tokens |
| Summary elapsed (JSONL) | **9.061s**, within fixed 10s deadline |
| Summary plus model cleanup | 9.062s |
| Daily write | Successful |
| Terminal output during summary | Empty |
| Source and user configuration | Unchanged |

System Prompt SHA-256 remains
`3a5ab7fe21cad4a1f71627c7553ccd66804f899e35dd6f7f334dc14939ae6dcf`,
identical to the previous final-prompt acceptance. HTTP capture records only safe
fields and hashes, never credentials or full request transcripts. See
`real-low-10s/report.json`, `exit-events.jsonl`, and `exit-summary.md`.

### Content review

The summary preserves the rename scope, dirty-editor blocking behavior, root cause,
pre-fix failures, test outcomes (77/1 and 211/7), commit `c854704`, and +141/-18 diff.
These numbers match source JSONL events 498, 506, 522/530 and 545. It explicitly says
UI behavior was checked in the Node DOM harness, not a real browser. Daily-generation
and harness-modularization suggestions remain labelled unapproved; the cross-session
follow-up remains pending. No obvious fabricated outcome or material error found.

Compression omits secondary details: the complete five-point tooling critique and
the Playwright suggestion (source event 555). It also puts the scratchpad reminder
and code commit in one bullet; the source attributes `c854704` to the rename fix.
These are readability/detail limitations, not evidence of missing the primary work.

This is one successful sample with about 0.94s deadline margin, not a latency guarantee.
The earlier high-effort final-prompt 10s timeout remains preserved in
`../stage2-daily-final/real-final-10s/`; no timeout increase or extra live retry was used.

## Reproduction

From the repository using its configured DeepSeek credentials (paid, opt-in):

```powershell
.\.venv\Scripts\python.exe scripts/memory_exit_smoke.py `
  --source <prior-isolated-FilePilot-session.jsonl> `
  --output <new-evidence-directory>
```

The script requires `deepseek-flash`, a 128,000-token context window, and the fixed
production 10s/2,048 constants. It makes a fresh session copy and retains it for audit.
The source path and retained isolated home are recorded in the report. User's unrelated
evaluation changes are excluded from the delivery candidate.
