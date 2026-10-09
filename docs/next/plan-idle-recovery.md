# Plan idle after mutation: isolated follow-up experiment

Status: live candidate FAILED; the added detector branch was NOT EXERCISED. No demonstrated benefit. The failed V0.3 sprint remains FAILED; stable V0.2 remains the recommendation. The continuing long-term goal authorizes further evidence-driven work, not relabeling that failure.

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

One changed-candidate C13 execution used the same qwen3.8-flash / low / 50 steps / 900s / one sample / no retry, official FeatureBench evaluator, Candidate Freeze and isolation. Compared with final dev4, only the detector and its corresponding guidance change (plus package version); the system prompt, tools, model and budgets stay fixed. Do not claim general improvement or release readiness from a single development case. Further acceptance still requires positive-control preservation, wider required-interface coverage and real SubAgent benefit.

Experiment directory: D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-plan-idle-recovery-20261009

## Live result and decision

Run `20261009T142348Z-9a3bc9b6`, frozen source `bc031f9`, official **FAIL**, execution **TIMED_OUT** at 900.6s. Candidate patch is 0 bytes; no files changed or deleted. The official report says `patch_exists=false`, `patch_successfully_applied=false`, `resolved=false`. Its empty F2P/P2P arrays are **tests not executed**, not 0/202 test passes.

Main requests: 42 started / 41 finished; last started step 41, last finished step 40. One existing transport retry occurred at step 24; no Agent retry. Reported input 1,513,816, output 53,575, cached input subset 1,000,704. Reported-cost subtotal ¥0.65521 using the same historical price basis as the earlier final report; missing failed/in-flight usage is unknown. Model completed-attempt wall time 893.024s; native tools 3.931s. Patch attempts 0, SubAgent calls/steps/cost 0. There is no mutation-then-validation cycle to credit.

The old pre-mutation plan-idle reminder fired after step 11 and was included in the step 12 request. The model kept exploring. This consumed the only permitted reminder, so the added post-mutation branch was never reached. Replaying this actual trajectory through the old and new detectors yields identical reminder events. Therefore the outcome cannot demonstrate either benefit or harm caused by the new branch; it demonstrates another failure of this overall candidate and no evidence for keeping the extension.

Integrity audit PASS: unchanged frozen files/ABK release, source/wheel match, one execution, raw/archive hashes equal, Candidate Freeze and cleanup complete, pinned official verdict confirmed. [Result and receipts](plan-idle-result.json).

**Do not merge/release. Do not carry this unexercised extension into the next independent ablation.** Stable V0.2 remains the recommendation. Do not rerun this failed frozen candidate. The separate [observation retention diagnosis](observation-retention-diagnosis.md) identifies a next hypothesis based on actual re-read/projection evidence; it has no live benefit proof yet. The long-term goal remains unmet and active.
