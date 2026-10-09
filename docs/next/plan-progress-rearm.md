# One bounded recovery reminder after reported plan progress

Status: dev12 candidate FAILED; new recovery branch NOT EXERCISED. KEEP V0.2. Do not merge/release.

The dev11 C17 trajectory wrote code at steps 18/24, hit a broad-test dependency failure at 27, then spent steps 28–41 in investigation (14 model turns, 151.785s model time). Its original once-only reminder was already used after step 12. The final plan still listed required interfaces as pending. Source task loss was excluded separately for dev10 by exact reconstruction of all 50 observation projections.

This candidate preserves the original initial reminder and its thresholds. After that reminder, at most one further request-only reminder is allowed, and only if the current plan reports more completed items than at the first reminder, still has unfinished items, and has stayed in the same status signature for eight tool-using steps. A reported apply_patch write, including partial writes on a failed patch, resets this idle interval. Cosmetic plan text changes do not reset or rearm anything. No extra reminder occurs at the final model-step boundary. No first reminder means no recovery reminder. A model that ignores the first reminder without reporting progress does not receive repeated reminders.

The recovery text explicitly identifies model-reported plan state; it does not claim the workspace is unchanged or that completed statuses prove code correctness. It asks Main to reassess whether a broad-check blocker is required by the requested behavior, use focused checks where possible, and return to remaining interfaces. There is no shell command parser, inferred mutation permission, workspace snapshot monitor, forced tool call, completion veto, secondary planner, or workflow graph. Main still chooses every action.

The model-reported completed count is a conservative cue, not a correctness measure. Replacing a plan can alter that count; the strict maximum of two reminders still bounds the effect. Successful patches before any first reminder retain the existing behavior: this experiment does not reintroduce the unexercised dev5 post-patch-first-reminder extension. Shell writes are still not recognized as apply_patch mutations; the recovery eligibility uses Plan state instead.

## Offline gate

Eleven historical trajectories were replayed. The baseline detector reproduced every recorded initial reminder. The candidate preserves every initial reminder; all three C08 traces remain silent. New recovery events would occur after C17 V0.2 step 20, dev4 step 41, and dev11 step 27. C17 dev3/dev10 and all three historical C13 traces add none. In particular, the new opportunity is immediately before dev11's long investigation span. This is a counterfactual trigger check, not evidence that the model would behave differently or solve the task.

[Exact replay results](plan-progress-rearm-replay.json). External prototype, unchanged baseline detector and replay script: `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-plan-progress-rearm-20261010`.

## Live gate

Run one fresh prepared/masked C17 with qwen3.8-flash, reasoning low, Main max_steps=50, 900s, samples=1, concurrency=1, unchanged FeatureBench evaluator/isolation/Candidate Freeze and no Agent FAIL retry. Keep SYSTEM exactly as the dev11/V0.2 baseline; tool descriptions, child limits, model config and observation limits are unchanged. Compare dev12 with both V0.2 and its immediate dev11 source baseline; the latter isolates detector/reminder changes, though single unseeded runs cannot establish causal significance.

Check whether the recovery branch actually fires, whether the next actions advance remaining interfaces or repeat the exploration, first/last mutation, final validation, official verdict, errors, model/tool/child costs, and actual Main use of any child findings. If the new branch is not exercised, say so; do not turn an unexercised mechanism into a success claim. Positive-control offline replay is not a new live C08 PASS. C08/C13/holdout NOT RUN for this candidate unless later evidence justifies them.

Regression coverage verifies completed-count gating, no rearm on cosmetic changes, silence for absent/completed/unadvanced plans, reset after partial patch writes, at most one recovery even after further progress, and two request-only deliveries through the real loop without persisting either text. Existing first-reminder/resume/compaction/privacy behavior remains tested.

## Completed live result

Frozen runtime source `872a20b3af3872198e593870d0fd1ef60eb0aef1` (0.3.0.dev12), run `20261009T170421Z-29c29bd0` (UTC ID; local report date 2026-10-10). Official FAIL, F2P 4/14 and P2P 74/74, same counts as stable V0.2 and dev11. Main LIMITED at 50 steps / 50 model calls; zero SubAgent calls/steps/cost. No agent retry.

| C17 metric | V0.2 | dev11 | dev12 |
| --- | --- | --- | --- |
| Official F2P / P2P | 4/14 / 74/74 | 4/14 / 74/74 | 4/14 / 74/74 |
| First actual write step | 11 | 18 | 14 |
| Agent seconds (monotonic) | 422.825 | 474.274 | 358.139 |
| Historical estimated CNY | 0.5279054 | 0.5783413 | 0.4546737 |
| Patch failures | 0 | 1 | 1 |
| New recovery reminders | N/A | N/A | 0 |

Dev12 reported input 1,493,028 / output 16,323 / cached-input subset 1,119,744. Model wall 349.518s and tool wall 6.108s. All 50 usage records are present. Lower observed latency/cost cannot be credited to the recovery mechanism because it never changed a request. Cache state and unseeded model variation remain confounders.

First write was apply_patch step 14, before the initial Plan reminder could fire; another patch followed at 17. The preserved old first-reminder rule then disabled that initial reminder. The new recovery condition requires an earlier reminder, so it was unreachable throughout the run. Replaying the actual public tool/plan trace through both implementations returns exactly zero reminders. This is an observed coverage gap in the proposed condition, not an implementation/test mismatch and not a live test of the recovery text.

Two wall-clock backsteps were detected: ABK seq 63→64 (35.597s) and 246→247 (22.316s). Nominal first-write timestamp offset is 79.813s and last-write offset 348.653s, but neither should be presented as exact monotonic elapsed time. Use event sequence to order actions and process/model/tool monotonic counters for duration comparisons. Existing logs expose individual durations but do not directly expose a monotonic timestamp for every event; improving that measurement would avoid this ambiguity in future runs.

## Correctness and convergence

The step-13 patch used an absolute /testbed path and was correctly rejected with path_escape; step 14 used a valid relative path. Main repeatedly searched index definitions/usages and later added missing imports in dataarray.py, dataset.py and groupby.py. At step 44 an existing range-index test explicitly failed because Coordinates.from_xindex was missing. Its pipeline returned shell success despite test failure; ToolResult.ok was not interpreted by this audit as a passing test.

Main wrote utils helpers at step 47 and from_xindex/sizes at step 49. The frozen diff shows `@classmethod def from_xindex` inserted below the existing `@property` that originally belonged to dims. Therefore from_xindex became a property, while dims lost its decorator. All ten official failures are TypeError: property object is not callable. The interface name is now present, but behavior remains wrong.

No behavior check follows the final mutation; step 50 only updates the plan. Seven files changed: coordinate_transform.py, coordinates.py, dataarray.py, dataset.py, groupby.py, indexes.py and utils.py. Required merge/formatting coverage remains incomplete. This run illustrates why a final validation opportunity matters, even when an interface is added before the budget expires.

No SubAgent was called; no independent findings were integrated. This requirement remains unmet. Positive C08 historical replay is not a new live positive-control verdict.

## Disposition and verification

Reject this candidate for merge/release. Preserve its experiment branch and source freeze for review, but do not count the added recovery branch as an earned capability. In particular, do not keep stacking changes on the assumption that the second reminder helped. The live evidence instead establishes that a design requiring an earlier pre-mutation reminder misses runs whose first successful patch precedes that reminder. Any later detector redesign must cover that path in offline replay before consuming another live task budget. Do not rerun this failed frozen candidate solely to seek a trigger.

Windows full regression: 511 passed / 10 optional Docker tests skipped; focused detector tests 38 passed; Ruff src/tests, mypy src/tests (64 files), lock check and wheel build PASS. A fresh Linux full unit suite was NOT RUN; the frozen Linux image executed the real C17 task. Source/wheel match (36 Python files), image dependency equivalence apart from Nexus, full egress isolation, frozen inputs, unchanged ABK and FeatureBench source identity, exactly one execution, official report, raw/archive evidence hashes and cleanup PASS. These establish experiment integrity, not task success.

[Audited result and exact failure evidence](plan-progress-rearm-result.json). Full runner/replay/manifest/verifier archive: `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-plan-progress-rearm-20261010`. Evidence hash `8c5737d41efd8b7216c28fd0759c0261936c146b074bd44150f34db7fdea2bf6`.

Main/origin/main remain `b5b25b5c9eb26fe4f11fd5e10f0d42b267e8c80a`. No merge/release. C08/C13/holdout NOT RUN for this candidate. The long-term goal remains active and unachieved.
