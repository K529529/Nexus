# Bounded plan-idle reminders before or after a patch

Status: development candidate dev14. Live correctness NOT YET VERIFIED. KEEP V0.2 pending evidence; no merge/release.

The dev12 C17 run wrote a patch before the first reminder and therefore disabled both the first reminder and its dependent recovery reminder. Dev11 instead consumed its first reminder, reported plan progress, and later spent 14 model turns investigating a broad-test dependency while requested interfaces remained unfinished. This candidate replaces those separate conditions with one plan-idle rule, without changing the model prompt, tool set, child limits, observations, or scheduling.

After eight tool-using steps with unchanged unfinished plan statuses, emit a request-only reminder. A reported apply_patch mutation, including a partial write on a failed call, resets the idle interval instead of permanently disabling reminders. Status changes reset it; cosmetic text changes do not. At most two reminders occur per execution window; the second additionally requires more model-reported completed items than at the first reminder. No progress means no repeated reminders. Fully completed plans remain silent. Before any patch and any reminder, the original 24-step fallback still covers absent or constantly changing plans. Never schedule on the last step.

Plan state is a model report, not a correctness or workspace mutation signal. Shell writes are not parsed or inferred. Plan replacement can change the completed count; the hard cap still bounds prompting. The generic plan reminder explicitly describes this uncertainty, encourages focused checks and completion of requested interfaces, and leaves every action to Main. There is no graph, forced tool call, delegated planner, completion veto, or new detector model. The old pre-mutation text is retained only for its matching fallback. The experiment changes both eligibility and plan-specific guidance; it cannot isolate those two effects.

## Offline gate

Replayed 12 historical public trajectories against the unchanged V0.2 detector and candidate. Baseline replay reproduces every recorded reminder. Three C08 traces remain silent. Candidate opportunities: C17 V0.2 11/20, dev3 11, dev4 22/41, dev10 11, dev11 12/27, dev12 25; C13 V0.2 18, dev3 19, dev4 18. These are counterfactual opportunities, not improved model behavior. The dev12 early-patch path and dev11 later investigation are both covered. [Exact replay](plan-idle-bounded-replay.json).

## Live gate

One fresh masked C17, unchanged qwen3.8-flash low, Main 50 steps / 900s, samples 1, concurrency 1, same FeatureBench/isolation/Candidate Freeze, no Agent FAIL retry. Preserve dev13 monotonic execution offsets for future first/last tool completion measurements. Compare official behavior, remaining interfaces, reminders actually delivered and subsequent actions, final validation, tools/errors, model/child usage, and Main use of any child findings. Historical replay does not establish positive-control safety. C08/C13/holdout NOT RUN unless C17 warrants further cost.

External diagnostic and frozen run directory: D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-plan-idle-bounded-20261010. The development branch must remain separate from main.

## Pre-freeze checks

Windows full regression: 512 passed, 10 optional Docker tests skipped. Final focused detector suite: 36 passed. Ruff, mypy (65 files), lock check and wheel build PASS. Tests cover early and partial patch writes, second-reminder progress gating, two-reminder cap, final-step silence, completed plans, request-only delivery, replay/privacy, compaction and resume. A Linux full unit suite was NOT RUN.
