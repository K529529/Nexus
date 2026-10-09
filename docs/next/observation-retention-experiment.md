# Observation retention: isolated live experiment

Status: experimental; live benefit NOT YET VERIFIED. Stable V0.2 remains the accepted baseline.

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

Live execution: NOT RUN at this commit. Offline checks do not establish task improvement.
