# Compact read output for bounded SubAgent investigations

Status: deterministic information-density improvement verified; dev9 live protocol completed, report quality remains PARTIAL. No claim of Main adoption, official correctness uplift or release acceptance. KEEP V0.2 remains in effect.

The preceding [live component review](subagent-live-probe.md) found that an observed RangeCoordinateTransform.forward body disappeared from the final child request after COLD projection. Before increasing context limits, this candidate removes redundant display bytes: mode=read prints the canonical workspace-relative path once in a `Path:` header, followed by original numbered lines. mode=search continues to print path:line per match because it can span files. No source content or line numbers are removed.

The child still has the same 6 steps, 150s, 24k context, requested 2048 output tokens per model request, 8k bytes per tool observation and 6k report ceiling. Main Loop, prompt, observation policy, model settings, containment, scan limits and report guidance are unchanged. This is a read rendering change, not a larger context or new retrieval subsystem.

## Offline evidence from the actual dev8 trajectory

All four original context projections were reconstructed and matched. Only already-observed read output was reformatted; no missing source was invented or restored from reference code.

| Read | Original bytes | Counterfactual bytes |
| --- | --- | --- |
| coordinate_transform.py first read | 7808 | 3926 |
| range_index.py received prefix | 8000 | 4316 |
| coordinate_transform.py repeated read | 7808 | 3926 |
| indexing.py final read | 6371 | 4073 |

At the final request, the original projection omitted the forward implementation; the compact-read counterfactual retained its exact source expression. The tool budget and 5232-token child observation working-set target did not change. This demonstrates retained information, not better model decisions. A fresh tool read may additionally fit more lines into its existing byte limit; the replay conservatively uses only the old received prefix.

[Machine-readable replay](subagent-read-format-replay.json). External replay script and evidence: `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-subagent-read-compact-20261010`.

## Regression and live gate

The new long-path test reads a 160-line page beginning at line 2. Under the old format, the 8KB limit cut off around line 97 (observed RED). Under the compact format, every requested line through 161 and the exact original source text fit (GREEN). The workspace source remains byte-equivalent after reading. A multi-file literal search test confirms per-match paths and line numbers remain intact. Existing byte-limit, path-confinement, recursion, cancellation and report tests remain applicable.

A fresh dev9 component run will use the identical investigation question/script and pinned pre-mask image source as dev8, with changed Nexus source frozen separately. It must be reviewed for accurate path:line claims, retained source evidence, output/latency and erroneous inferences, not only parsed JSON. The image contains implementation bodies before FeatureBench masking; this remains a component analysis, not the C17 reconstruction task, and no discovered reference facts may be fed to a future official run.

Checks before live freeze: Windows full suite 506 passed / 10 optional Docker skipped; Linux SubAgent tests 7 passed; Ruff and mypy src/tests PASS; uv lock --check PASS; wheel/sdist build PASS.


## Completed dev9 live component result

Source `e0e6930`; 6 model steps / 8 inspection calls / 25.370s. Input 27,180, cached subset 13,312, output 1,222; historical estimated cost ¥0.015725. Report parsed, but it is not independently correct in every claim. One malformed mode (`search>` plus newline) was rejected; Main-free child execution later used a valid search. No tool argument was silently repaired.

Compared with dev8: steps 4 → 6, reads/searches 7 → 8, input 17,846 → 27,180, output 1,576 → 1,222, wall 27.511s → 25.370s, estimated CNY 0.0156648 → 0.015725. These single unseeded runs do not demonstrate lower overall cost or better convergence; cache state and the invalid call complicate comparison. The narrower deterministic claim is that source pages carry less repeated path text at identical byte limits.

Manual review supports the constructor and ij-grid observations. It rejects the stronger claim that returned coordinate keys must be **exactly** coord_names: observed consumers require specific keys but do not reject extras. The class cited at indexing.py:2147 is also mislabeled CoordinateTransformIndex rather than CoordinateTransformIndexingAdapter. Full indexing-body behavior is appropriately marked uncertain, and the previous unsupported 1-D-only claim is absent. A repeated coordinate_transform read remains.

Read-only snapshot, frozen source/wheel/request limits and cleanup audit PASS. No Main model consumed this report and no official verifier ran. [Measurements and independent review](subagent-read-format-result.json).

Retain the compact rendering as a small, tested evidence-density change, not a release justification. Stop component-only tuning here: next validation must return to Main's actual optional delegation and integration on prepared task inputs, without injecting these reference findings.
