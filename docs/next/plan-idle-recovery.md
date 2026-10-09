# Plan idle after mutation: isolated follow-up experiment

Status: experimental, real-task benefit NOT YET VERIFIED. The failed V0.3 sprint remains FAILED; stable V0.2 remains the recommendation. The continuing long-term goal authorizes further evidence-driven work, not relabeling that failure.

## Observed failure and proposed change

In final dev4 C13, step 9 patches only seaborn/external/version.py. The plan advances at step 10 to identifying missing Plot methods, then remains there until the 900s timeout. The frozen candidate contains no Plot changes. StagnationDetector previously returned immediately forever after any reported patch mutation, so no later plan-idle reminder was possible.

Keep the same single Main Loop and one request-only nudge per execution window. An observed patch resets the plan-idle counter instead of permanently disabling it. After a patch, eight tool-using steps with unchanged unfinished plan statuses may trigger a reminder. Further patches and plan status changes reset the idle count. Completed/absent plans do not trigger the new condition. No shell parsing, inferred writes, automatic edits, forced child delegation, repeated nudges, completion interception, or workflow stages are added.

The post-mutation reminder accurately refers to model-reported plan status, not absence of any workspace changes or proof of task failure. Partial patch writes count as writes even when the tool reports failure. Existing pre-mutation guidance remains unchanged. Its shell-mutation blind spot is not solved here.

## Historical counterfactual replay

Nine archived trajectories were replayed through the old and proposed detector. Old detector trigger steps exactly match all recorded nudge events, validating the event reconstruction. This does not replay the model or predict correctness.

| Case | Version | Recorded nudge after step | Proposed nudge after step |
| --- | --- | --- | --- |
| C08 | V0.2 / dev3 / dev4 | none | none |
| C17 | V0.2 / dev3 / dev4 | 11 / 11 / 22 | 11 / 11 / 22 |
| C13 | V0.2 | 18 | 18 |
| C13 | dev3 | none | 19 |
| C13 | dev4 | none | 18 |

In particular, V0.2 C13 already received a reminder and still failed. A newly reachable reminder is therefore only a testable opportunity, not evidence of a fix. [Replay data](plan-idle-replay.json) retain run IDs and actual/proposed events. Replay script and baseline source are in the experiment directory below.

## Checks and planned live decision

Windows full suite: 507 passed, 10 optional Docker checks skipped. Targeted detector/SubAgent tests: 41 passed. Ruff PASS; mypy src + tests PASS (64 files). Lock validation PASS; dependency pins unchanged (only the Nexus candidate version changes). Regression coverage includes post-mutation reactivation, repeated-edit reset, completed/absent plan silence, single nudge, partial mutation, and request-only guidance through the real Loop.

One changed-candidate C13 execution will use the same qwen3.8-flash / low / 50 steps / 900s / one sample / no retry, official FeatureBench evaluator, Candidate Freeze and isolation. Compared with final dev4, only the detector and its corresponding guidance change (plus package version); the system prompt, tools, model and budgets stay fixed. Do not claim general improvement or release readiness from a single development case. Further acceptance still requires positive-control preservation, wider required-interface coverage and real SubAgent benefit.

Experiment directory: D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-plan-idle-recovery-20261009

Live execution: NOT RUN at this commit. No merge or release.
