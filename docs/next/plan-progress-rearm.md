# One bounded recovery reminder after reported plan progress

Status: dev12 candidate prepared; live C17 result pending. No release recommendation.

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
