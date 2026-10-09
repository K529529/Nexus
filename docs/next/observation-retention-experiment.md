# Observation retention: isolated live experiment

Status: C13 shows partial convergence improvement but official FAIL; C08/C17 controls pending. No release acceptance. Stable V0.2 remains the accepted baseline.

## Intervention and comparison

The [historical diagnosis](observation-retention-diagnosis.md) found repeated reads whose earlier full source observations were already replaced by previews. This experiment changes only the full-observation working-set cap from 16,384 to 65,536 estimated tokens. The existing quarter-of-context-budget bound, HOT protection, contiguous recency rule, 1k/2k cold previews, raw-history preservation and safety compaction remain unchanged.

The unexercised post-mutation nudge introduced in dev5 is explicitly removed. Agent Loop, detector, tests for that detector, system prompt, tools and provider behavior return to the final dev4 control (`e83e9c8`). Relative to that control, runtime source differs only in the observation cap and package version `0.3.0.dev6`. No new architecture, shell classification, scheduling or task-specific rule is added. SubAgent retains the same smaller context, which is still governed by the quarter-budget bound.

Hypothesis: keeping previously inspected source available can reduce repeated exploration and bring core implementation forward. Risk: larger inputs may cost more or slow requests without improving correctness. Historical replay is evidence of retained information, not evidence of better decisions.

## Verification and live decision

A regression test reproduces old-source projection under the 16k cap (RED), then verifies preservation under the 64k cap, unchanged raw history/tool pairing and continued small-context bounding (GREEN). The existing long-run test now retains one additional recent observation at a 128k window; its savings and history/unknown-usage checks remain intact.

Live: one C13 execution, qwen3.8-flash / reasoning low / max_steps 50 / 900s / samples 1 / concurrency 1; pinned official evaluator, unchanged ABK release, Candidate Freeze and model-only network isolation. No Agent FAIL retry. Judge official verdict, first core modification, final checks, repeated reads, model turns, reported input/output/cache, tool/model timing and estimated cost together. Empty patch is a failed candidate, not a tested solution.

If this improves real implementation/correctness materially, continue with C08 positive control and C17 multi-module coverage under the same candidate. Do not claim a replacement for V0.2 from one development case. SubAgent value, reliability and overall acceptance remain required. Do not merge or release this experimental branch.

Experiment directory: D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-observation-retention-20261009

Checks: Windows full suite 505 passed / 10 optional Docker checks skipped; Ruff PASS; mypy src + tests PASS (64 files); uv lock --check PASS, dependency pins unchanged. The new retention regression was observed RED on the 16k control before GREEN on this candidate.

Offline checks do not establish task improvement; the completed C13 live result follows below.


## C13 official result

Run `20261009T145046Z-1b95e73d`, source `3251ba9`; official **FAIL**, F2P **168/202**, P2P **245/245**. Relative to V0.2: 9 newly passing tests, 3 newly failing tests, net +6. Failures still include paired interfaces, facet labels and theme HTML. Exact tests are retained in the result JSON; no official test details were fed to the running Agent.

| Metric | V0.2 | dev6 / 64k |
| --- | --- | --- |
| Official | FAIL | FAIL |
| Agent terminal | LIMITED | COMPLETED |
| Main steps | 50 | 41 |
| Model attempts | 51 (one transport retry) | 41 |
| First actual helper write | step 9 / 25.5s | step 8 / 38.6s |
| First Plot implementation | step 41 / 404.9s | step 26 / 285.5s |
| Last mutation | step 47 | step 35 |
| Agent process time | 461.9s | 494.3s |
| Model / native tool time | 455.7s / 3.71s | 471.9s / 19.40s |
| Patch failures | 3 | 3 |
| Reported input / cached subset | 1,407,694 / 946,688 | 1,485,006 / 1,397,504 |
| Reported output | 26,435 | 28,225 |
| Historical-price reported cost | ¥0.53485, missing usage | ¥0.28596, complete usage |
| SubAgent calls / steps / cost | 0 / 0 / 0 | 0 / 0 / 0 |

All successful edits were shell writes; `first_patch_mutation=None` must not be interpreted as no modification. The first three patch attempts failed because old context did not match. After core implementation, an inline smoke exposed a SubFigure NameError, which Main corrected. Post-final-mutation smoke and some existing tests passed, but those checks did not cover all required interfaces. Main's final claim that all required interfaces were restored/verified is stronger than its evidence; official FAIL remains authoritative.

Policy exposure is proven: 41 context projections match reconstructed public events; 24 differ from a 16k counterfactual over the same history, starting at request 18. At request 41, actual tool observations total an estimated 44,825 tokens, versus 25,778 at 16k; no raw observations were discarded from storage. In the consistent restricted comparison of successful numeric sed reads of Plot/subplots before the first Plot mutation, overlap pairs are V0.2=8, dev4=22, dev6=0. These are overlapping range pairs, not all search activity or a causal measurement.

Cache-hit proportion rose from 67.3% to 94.1%; input and output totals did not fall. Stable retained prefixes may help caching, but one unseeded sample and prior cache state cannot attribute the whole cost reduction to this policy. Latency increased about 7%. A pip download attempt was blocked by the model-only proxy (tool duration 7.75s); a shell pipeline still reported ok. The stronger network isolation differs from the earliest baseline environment and must remain disclosed.

Integrity PASS: one execution, unchanged ABK/evaluator and frozen source, Candidate Freeze, raw/archive equality and cleanup. [Measurements and receipts](observation-retention-c13-result.json).

Decision: the earlier core implementation, fewer turns, net additional test passes and lower observed cost justify checking C08 and C17 with the **same wheel**, without further C13 tuning. This is not enough to replace V0.2: correctness remains incomplete, some tests regressed and SubAgent value is still unproven. Controls are frozen separately at `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-observation-retention-controls-20261009` to preserve the completed C13 experiment.
