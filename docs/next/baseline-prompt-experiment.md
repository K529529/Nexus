# Restore the stable root prompt, keep the tool capability

Candidate dev11 completed and audited: FAILED. KEEP V0.2. No merge/release recommendation.

The previous optional-delegation nudge reached step 12 but no SubAgent was called and C17 correctness remained 4/14. An offline reconstruction now matches every observation diagnostic across all 50 requests: the entire 53,214-character user task stayed intact, including from_xindex. This rules out removal of that requirement by observation projection in this run; it does not prove the model attended to it. The initial plan followed file order, with broad survey first and coordinates.py methods near the end. Main spent 27 steps before its first actual write and did not reach the last two files.

The visibility reconstruction counts persisted non-delta native events with the session-created header offset. ABK envelope seq is a different sequence. An initial diagnostic using ABK seq failed at the first cold projection and was corrected against Events/SessionLog behavior; no raw evidence was modified. All recorded projection diagnostics, including byte/token estimates, then matched. [Replay evidence](task-visibility-dev10.json).

Intervention: restore SYSTEM exactly to stable main/V0.2 and remove dev10's optional nudge addition. Do not add another instruction. Keep V0.2 Plan/Todo and bounded detector, tool definitions with patch/shell feedback, existing observability, isolated read-only SubAgent, child report bounds and compact reads. The SubAgent description still advertises its use, limits and Main integration responsibility. No mandatory delegation, new scheduler, coverage ledger, automatic requirement extraction, or model budget change.

Hypothesis: extra coverage/delegation instructions have not demonstrated benefit and may create instruction burden; restoring the stable prompt is a lower-complexity candidate worth testing. The burden hypothesis is unproven, not a diagnosis. The one fresh C17 run assesses combined removal of the root additions and optional nudge, not independent causality for each sentence. Relative to V0.2, tool implementations and descriptions still differ.

Protocol: unchanged qwen3.8-flash/low, 50 Main steps, 900s, samples=1, concurrency=1, official prepared/masked task, full egress isolation and candidate freeze, no failure retry. No reference-code findings from earlier unmasked probes enter the task. C08/C13/holdout NOT RUN in this candidate unless fresh evidence justifies later work. Acceptance requires real correctness/convergence improvement and actual value if delegation occurs; fewer instructions alone is not success.

## Frozen execution and results

Source `8a8d696c10dc5755d50dc910bda5dd78505a928f`, 0.3.0.dev11. Run `20261009T164500Z-73925e3d` (UTC identifier; local report date 2026-10-10). SYSTEM is byte-equivalent to stable main: 2650 characters, SHA256 `7ab50d8a3e63b061467a8a6c3df3bec54bd226fdbb86ec576d18d0d5d368096c`, down from 4047 characters in dev10. No extra root policy was introduced. Relative to V0.2, remaining runtime changes span 7 files with 360 insertions and 4 deletions, including 290 lines for SubAgent.

| C17 metric | V0.2 | dev10 | dev11 |
| --- | --- | --- | --- |
| Official F2P / P2P | 4/14 / 74/74 | 4/14 / 74/74 | 4/14 / 74/74 |
| Outcome / Main model turns | LIMITED / 50 | LIMITED / 50 | LIMITED / 50 |
| First actual write step | 11 | 28 | 18 |
| Agent seconds (monotonic) | 422.825 | 342.527 | 474.274 |
| Historical estimated CNY | 0.5279054 | 0.5416412 | 0.5783413 |
| Patch failures | 0 | 1 | 1 |
| Child calls / steps / cost | N/A | 0 / 0 / 0 | 0 / 0 / 0 |

Dev11 input 1,740,367 / output 20,167 / cached-input subset 1,240,576; all 50 model usage records are present. Model wall 456.396s; tool wall 15.402s. Relative to V0.2, total agent time increased 12.2% and historical estimated cost increased 9.6%. The shortened prompt did not improve correctness. Earlier implementation than dev10 did not translate into completed interface coverage or a final passing validation.

Two wall-clock backsteps were detected (ABK seq 36→37, 30.689s; 305→306, 22.992s). The first write is reliably step 18 / seq 161; its timestamp offset is 113.323s, but that number must not be used as trustworthy monotonic elapsed time. Last write is step 49 / seq 379, nominal timestamp offset 455.590s. Event sequence establishes ordering; process/model/tool monotonic counters establish durations. This timing caveat is preserved in the result JSON.

## What the trajectory actually shows

- Step 16 apply_patch omitted `*** End Patch`. The tool returned invalid_patch with the boundary error and changed_files=0. This is a malformed model action; the parser correctly rejected it. Step 18 rewrote coordinate_transform.py through shell; step 24 inserted index helpers through shell. No successful apply_patch occurred.
- Step 25 tried a nonexistent test path; step 27 used the correct existing index tests and got 1 failed / 25 passed, blocked by missing asarray. Both commands piped into tail, so ToolResult.ok was true despite actual test failure. The shell contract still reports the real shell status; prompts did not reliably prevent masked status.
- Steps 28–41 consumed 14 model turns and 151.785s of monotonic model time investigating asarray/get_array_namespace and related source. This is a reproducible post-implementation exploration span, not a semantic count of every repeated search.
- Steps 42/45 wrote additional duck_array_ops helpers and reran index tests. The later run still had 8 failed / 68 passed. This repair work did not complete the requested last modules.
- Step 48 appended dict_equiv without a separating newline, causing SyntaxError. Step 49 repaired that concatenation; `import xarray.structure.merge` then printed `import ok`. Import success is not behavioral validation. Step 50 updated the plan with five pending items, including drop_dims_from_indexers, remaining merge helpers, array_repr, Coordinates methods including from_xindex, and tests. There was no final response or passing final behavior check.

The frozen diff changes coordinate_transform.py, indexes.py, utils.py, structure/merge.py, and additionally duck_array_ops.py. It still omits coordinates.py and formatting.py. All ten official F2P failures are missing Coordinates.from_xindex. The exact public task is not lost; in the final plan the model explicitly retains the unfinished interface. The observed problem is incomplete execution within the budget, not proof of missing prompt information.

The V0.2 detector issued its one reminder after step 12, included in step 13. Because all writes used shell and the detector observes apply_patch mutations only, mutation_seen did not represent these writes; however its once-only nudge budget was already consumed. No new nudge interrupted the later 14-turn investigation span. This describes existing behavior, not proof that another reminder would improve it. The earlier dev5 post-mutation experiment was not exercised by its empty-patch run; it cannot be cited as proof either way for this later trace.

SubAgent remained available through its tool description, but Main never invoked it. No source-finding or integration value exists in this run. No forced delegate call was added to manufacture adoption.

## Disposition

Candidate acceptance FAILED. Keep stable V0.2. Do not promote this branch on the basis of shorter text or smaller runtime diff. Preserve it as a completed simplification experiment. C08/C13/holdout are NOT RUN for this candidate; prior results must not be relabeled as dev11. No repeat run of this failed agent attempt and no expansion to the 16-case set.

This turn rules out lost task text in dev10 and supplies a concrete dev11 post-mutation exploration sequence. Future runtime work should first reproduce the relevant behavior offline and distinguish a real decision-policy gap from prompt wording. These findings do not justify another blind root-prompt sweep or a claim of overall capability improvement.

## Verification

Windows final full regression: 506 passed / 10 optional Docker tests skipped. Initially one obsolete prompt-text assertion failed after restoration; the exact stable-baseline assertion was restored, focused 63 tests passed, and the final full suite passed. Ruff src/tests, mypy src (36 files), uv lock --check and wheel build PASS. A new Linux full unit suite was NOT RUN; the frozen Linux image ran the real task.

Source/wheel equivalence (36 Python files), dependency equivalence apart from Nexus, official evaluator and unchanged ABK source identity, network isolation, Candidate Freeze, exactly one execution, raw/archive evidence hash equality and cleanup all PASS. Integrity is not task correctness.

[Audited result and behavior evidence](baseline-prompt-result.json). External runner/manifest/source hashes/raw archive/official verifier outputs: `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-baseline-prompt-c17-20261010`. Evidence hash `0e5437b84b8328b4441ef4eaf0044caec4855cd87a93afd9c4755875fdf209c4`.

No merge or release. Main/origin/main remain `b5b25b5c9eb26fe4f11fd5e10f0d42b267e8c80a`. The long-term goal remains unachieved: no clear overall improvement over V0.2, and still no genuine Main/SubAgent integration evidence.
