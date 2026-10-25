# Stage 3.1A acceptance evidence — 2026-10-24

## Scope and branch

- Branch: `codex/nexus-v0.3-stage3-harness-hardening`.
- Fetched starting base: `origin/codex/nexus-v0.3` = `a4248fa109b120d8e7fffc89685cd72b539d1609`.
- Isolated worktree: `C:/Users/Archer/AppData/Local/Temp/nexus-stage3-31a`. The original worktree's edits were not stashed, copied into this branch, reset, or discarded.
- During implementation, another operation pushed `602c60ba712708badf6938556c9d04f7d0084690` (`feat(evaluation): add offline history dashboard`) to V0.3. This branch deliberately retains its verified starting base; no merge/rebase of that unrelated work is included.
- This user-authorized change supersedes only the earlier design's shared model/TUI diff-result representation. Patch DSL, parsing/matching, Agent Loop, compaction thresholds, memory, command arguments and historical assistant patch arguments are unchanged.

## Implementation and event contract

`src/nexus/tools/patch.py` emits one `patch_diff` after each successful per-file commit, before committing the next file. No event is emitted for preflight failure or a no-op. The diff is computed from the validated original bytes and the bytes committed by the executor, not copied from the requested patch. Add/update/delete and exact original/final SHA-256 hashes remain observable. Two local receipt corrections accompany this: body lines starting with `++`/`--` are counted correctly, and a cleanup failure after the destination was successfully replaced/linked retains the completed change before returning `patch_io_error` and stopping the batch.

Each event carries `call_id`, one-based `file_index`, `path`, `status`, `added_lines`, `deleted_lines`, `before_hash`, `after_hash`, `diff`, `diff_bytes`, `diff_limit_bytes`, `truncated`, `omitted`, and `omission_reason`. The standard envelope supplies session/run/sequence/time association. These are audit records, never another tool call or message.

- Diff content limit: **65,536 UTF-8 bytes per file**, **262,144 bytes per call**, independent of the model output budget.
- If a full diff cannot fit, its entire body is omitted (`diff=""`, `omitted=true`, `truncated=true`, `omission_reason="diff_capacity"`). Metadata and original byte count remain. Later smaller diffs may still fit the remaining budget.
- Whole-diff omission avoids cutting a configured secret before the existing `Events.redact()` mechanism sees it. These limits measure original diff payload bytes; event metadata, JSON escaping and redaction replacement text also occupy JSONL bytes. This is not a fixed serialized-row-size promise.
- `Events` redacts and appends the event through the existing SessionLog before publishing it. A failed audit sink stops further file dispatch. Ordinary cooperative cancellation carries completed metadata through the existing `ToolCancelled` handling.
- ToolResult contains bounded file metadata only. `changed_files`, `omitted_files`, created directories, error/detail/failed-file/hunk, partial/no-change/framing semantics and the normal `ToolResult.message()` path remain. Top-level result `truncated` describes omitted model metadata; event `truncated` describes omitted audit diff content. An audit omission alone does not make a complete model receipt truncated.
- Rich TUI displays the first three event file previews, at most six diff lines per file plus omission markers. It retains the final tool success/error/duration and partial/error notices. Previews appear as commits occur, before the final status line. Legacy result-based rendering is retained when no event preview was received; new event previews are not repeated from ToolResult.

## Verification

All commands use Python 3.12.10 and the existing locked dev environment; no model/API requests were made. pytest uses system temporary directories. The full run used a newly created process-level `PYTEST_DEBUG_TEMPROOT` and separate pytest cache because sandbox-created temporary directories are not reusable under the normal user token.

| Check | Result | Evidence |
| --- | --- | --- |
| Targeted patch/context/replay/TUI suites | PASS, **196 passed** in 18.52s | [pytest-targeted.txt](pytest-targeted.txt) |
| Full pytest | PASS, **681 passed, 10 skipped** in 51.75s | [pytest-full.txt](pytest-full.txt) |
| Ruff, whole repository | PASS | [ruff.txt](ruff.txt) |
| Mypy, `src tests` | PASS, 93 source files | [mypy.txt](mypy.txt) |
| `uv lock --check --offline` | PASS, resolved 66 packages | [checks-and-attempts.txt](checks-and-attempts.txt) |
| `uv build --offline` | PASS, wheel and sdist | [checks-and-attempts.txt](checks-and-attempts.txt) |
| `git diff --check` | PASS | no whitespace errors |
| Old Session Resume / new Replay / checkpoint compaction | PASS | `tests/test_patch_audit.py` and the generated before/after sample |
| Real filesystem rename equivalence | PASS, all 12 final hashes equal | [token-comparison.json](token-comparison.json) |
| Paid model / full SWE-bench / live Docker | NOT RUN | not required for this change; the 10 pytest skips are opt-in Docker tests |
| Linux execution / interactive manual TUI | NOT RUN | this run validated Windows and captured real Rich rendering with bounded terminal width |

Targeted command:

```text
python -m pytest tests/test_patch_audit.py tests/test_patch_v02.py tests/test_mcp_tui.py tests/test_session_context.py tests/test_session_compaction.py tests/test_context_runtime.py tests/test_request_context.py -q
```

The new tests cover actual diffs/counts/hashes for add/update/delete, no-op/conflict/format failure, independent metadata and audit budgets, per-file and total diff omissions, partially committed batches, cleanup failure after commit, cancellation, audit persistence failure, process-stop recovery with missing result, redaction, strict call/result pairing, unchanged assistant patch arguments, context/runtime-fact equivalence with/without audit events, legacy results containing diff, checkpoint compaction/replay, bounded TUI previews and no result/event duplication. Existing tests continue to cover CRLF/no-newline, size limits, framing normalization, path boundaries and concurrency checks. Public trajectory consumers receive `patch_diff`; the number of `tool_started`/`tool_finished` events remains one per actual invocation.

Initial failed attempts are retained in [checks-and-attempts.txt](checks-and-attempts.txt): the original Windows process cleanup test failed inside the sandbox, then a normal-user rerun encountered a pytest temp-directory access denial. Using a fresh system temp root with normal process permissions passed. Initial lint/type findings in the new code were fixed before final checks. No passing count here hides those failures.

## Reproducible token comparison

Run from the Stage 3 repository root with the locked dev environment:

```text
python docs/next/evidence/stage3-1a/reproduce.py
```

This is an **equivalent deterministic rename sample**, not the historical FilePilot trace. It seeds 12 TypeScript files, renames `display_name` to `displayName` across 30 lines per file in three four-file calls, and actually executes both the exact starting commit's `apply_patch` implementation and the new implementation in separate temporary workspaces. A scripted model only supplies the fixed calls; provider usage stays unknown. Both sessions are resumed, all final files are independently compared with expected bytes, and each old/new metadata receipt is compared after removing old diff fields. Baseline source and fixture SHA-256 values are recorded in the JSON report.

Counting basis: sum `TokenEstimator.raw([ToolResult.message()], [])` for each of the three results, same profile on both sides, no adaptive calibration, only `duration_ms` normalized to zero for reproducibility. This measures standalone result envelopes, not billed total requests or a success-rate improvement.

| TokenEstimator profile | Before ToolResult | After ToolResult | Saved | Reduction |
| --- | ---: | ---: | ---: | ---: |
| Bundled DeepSeek (`deepseek-flash`, `api.deepseek.com`) | 19,338 | 1,635 | **17,703** | **91.5451%** |
| Existing fallback estimator | 20,360 | 1,479 | 18,881 | 92.7358% |

- Added audit events: **12**; none omitted.
- Original actual diff payload: **50,404 bytes**.
- Added `patch_diff` JSONL rows, including envelope/newline: **59,682 bytes**.
- Complete JSONL size: **186,020 → 189,055 bytes**, net **+3,035 bytes**. Timing and temporary workspace names can slightly change serialized totals on reruns; normalized token counts and file hashes are reproducible.
- The directory-local `.gitattributes` preserves JSONL byte counts across checkouts and preserves Rich padding in raw terminal captures.
- Raw sessions: [before-session.jsonl](before-session.jsonl), [after-session.jsonl](after-session.jsonl).
- Model-visible receipts: [before-tool-results.jsonl](before-tool-results.jsonl), [after-tool-results.jsonl](after-tool-results.jsonl).
- Rich terminal captures: [before-tui.txt](before-tui.txt), [after-tui.txt](after-tui.txt).

## Changed files and remaining boundaries

Production changes are only `src/nexus/tools/patch.py` and `src/nexus/app/tui.py`. Tests: new `tests/test_patch_audit.py`, updated diff assertions in `tests/test_patch_v02.py` and `tests/test_tools.py`. This evidence directory contains the reproduction script, raw samples, metrics, check outputs and this report.

There is still a crash window between filesystem commit and JSONL append; append-only logging does not provide atomic filesystem/log transactions or durability through power loss. If logging fails, the runtime stops, and missing tool results resume as `interrupted_unknown`; a diff event alone is never promoted to successful completion of the whole call. Existing concurrent-writer/path-boundary limitations remain. Oversized diffs are explicitly omitted rather than guaranteed complete; model file metadata can still be omitted under its existing budget. Redaction protects configured secrets, not every possible sensitive string in source files. Cleanup-failure cases may leave a temporary file for inspection.

Historical assistant Patch parameters are unchanged, and old ToolResults containing diffs remain intact on resume. Therefore historical patch pollution is not eliminated, and patch matching behavior/success rate is not improved by this change. No V0.3/main/Stage 2 merge or publication is part of this delivery. The new branch is for review only.
