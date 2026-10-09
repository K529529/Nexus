# Observation retention diagnosis (not a live improvement claim)

The running plan-idle experiment is unchanged. This is an independent read-only analysis of already completed C13 trajectories. It identifies a potential next intervention, not a passing candidate or a new architecture.

## Exact implementation and evidence

`observations.project()` limits the complete recent tool working set to `min(16384, budget // 4)` estimated tokens, regardless of the much larger configured context window. Older successful observations are projected to at most 1024 bytes; failures to at most 2048. The newest unconsumed tool group is protected even if it exceeds the target. Raw session/JSONL data remain intact, but the model receives previews.

The experiment configuration uses a 1,000,000-token context window and 16,384 maximum output tokens. Using original public message/tool events, the replay reconstructs every `context_projection` and checks recorded hot counts, compacted counts, original bytes and projected bytes exactly. No private reasoning is read or exported. Tool-only estimates do not include assistant history, continuation data, user/system text or tool schemas.

| C13 version | Verified projections before core mutation | Literal sed overlap pairs | Pairs whose prior observation was compacted | Same pairs compacted with offline 64k target |
| --- | --- | --- | --- | --- |
| V0.2 | 41 | 10 | 9 | 0 |
| dev3 | 38 | 4 | 3 | 0 |
| dev4 | 44 (no core mutation) | 22 | 22 | 0 |

Only literal numeric `sed -n 'start,endp' path` ranges are considered, with at least 20 overlapping lines. A read can overlap multiple prior reads, so these are pairs, not counts of redundant commands. The baseline/dev3 analysis stops before the first Plot mutation to avoid shifting line numbers. The dev4 frozen patch confirms Plot was never modified. This conservative subset does not measure all searches or prove that any particular repeated read was unnecessary.

For example, dev4 step 5 reads Plot lines 100–470. At steps 17/18/19, reads of lines 140–260, 336–432 and 300–336 overlap that observation, which has already been compacted. A 64k full-observation target retains the original result at those requests.

At the final analyzed request, tool-observation estimates would change as follows:

| Version | Existing projected tokens | Offline 64k projected tokens |
| --- | --- | --- |
| V0.2 | 24,738 | 42,464 |
| dev3 | 22,195 | 32,466 |
| dev4 | 30,791 | 61,675 |

This is a measurable prompt-cost tradeoff. More retained evidence may reduce re-reading, but may also increase latency and input cost without changing correctness. No model execution used the 64k setting here, and no benefit is claimed. The observed policy is shared with V0.2, so it cannot by itself explain why dev4 regressed. It is a candidate for a controlled ablation after the current detector experiment finishes, not a reason to silently change its inputs.

[Compact measurements](observation-retention-diagnosis.json) include run IDs, exact final projection statistics and overlap examples. Full replay scripts and output are stored in `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-plan-idle-recovery-20261009` (`replay_observations.py`, `observation-replay-{v02,dev3,dev4}.json`).
