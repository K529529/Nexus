# SubAgent live integration diagnosis and bounded-report candidate

Status: dev7 component probe FAILED with `invalid_finish: length`; dev8 returned a structured report, but source review is PARTIAL. No Main autonomous delegation or official task improvement established. KEEP V0.2 remains the accepted release decision.

## Why this diagnostic

Ten prior real development runs made no spawn_agent calls. Offline tests establish lifecycle/isolation but cannot demonstrate that the configured real provider can finish a useful child report within 6 steps / 150 seconds / requested 2048 output tokens and the 6KB report ceiling.

This is one direct invocation of the production spawn_agent tool, on the C17 xarray image snapshot, asking about coordinate-transform shape/order and caller contracts. It does not run a Main model, official evaluator or hidden checks. It cannot establish autonomous delegation or correctness uplift. The child autonomously chooses inspect_repository calls; the host only supplies the narrow question and known source paths. Parent-to-child factory configuration matches production bootstrap.

## Invalid diagnostic retained, not counted as Agent evidence

The first harness erroneously returned event sequence 0 for every Emit callback. Context projection keys messages by sequence; two different tool results collapsed to the second result and the child eventually failed unpaired_context. An offline replay of the captured public messages reproduces that failure; monotonically increasing event sequences preserve both original call IDs and complete. This is a diagnostic implementation error, not a Nexus/model capability verdict. Its four model calls and ¥0.011581 estimated cost remain recorded.

The corrected harness was frozen separately. It was not a retry of an official FeatureBench Agent failure. All original benchmark failures remain unchanged.

## Valid dev7 diagnostic

Five model requests, ten read/search tool calls, 79.266 seconds; input 27,100 / cached subset 10,496 / output 4,958, all reported; historical estimate ¥0.0277194. Request five returned finish_reason=length; the normal Agent Loop rejected the incomplete answer and returned failed / invalid_finish: length. No structured findings were delivered.

Wire hooks confirm every request used qwen3.8-flash, reasoning_effort=low, max_tokens=2048 and only inspect_repository. The final request reported 3,287 output tokens, so the requested parameter must not be advertised as a strict total cost ceiling. The telemetry does not establish how provider accounting divides reasoning and answer tokens; no unsupported attribution is made.

All 195 xarray Python source hashes match before/after. The Docker root filesystem was read-only, execution user 1000, capabilities dropped, no-new-privileges enabled, model-only internal network used, and the diagnostic directory mounted read-only. Frozen source/wheel/image and cleanup checks passed. Private protocol/reasoning fields were not exported by the diagnostic collector.

[Machine-readable results, hashes, wire limits, costs and offline replay](subagent-live-probe-results.json).

## dev8 hypothesis and limits

The child prompt now requests at most four findings and two uncertainties, whole JSON under 1200 characters, one path:line-backed implementation implication per finding, and no background prose/code fences. Unknowns should be reported as uncertainties rather than consuming the full budget. This is guidance, not an enforced character/count validator; existing 6KB parsing bound and failure semantics remain unchanged.

No output-budget increase, automatic retry, special finalizer tool, workflow graph, partial-report fabrication or workspace mutation capability is added. Relative to c1250f8, runtime changes are child report guidance and version 0.3.0.dev8 only. Model settings, six-step/150-second limits, same Agent Loop and isolation remain unchanged. The diagnostic question is unchanged and contains no task-specific solution.

Preflight: Windows 5 SubAgent tests PASS; Linux 5 SubAgent tests PASS; Ruff PASS; uv lock check PASS; wheel/sdist build PASS. These do not establish behavior improvement. The new candidate must return a parseable, source-supported report within its budget; then Main utilization and real task improvement still require separate evidence. A component success will not replace the three-case release gate.


## Completed dev8 diagnostic and independent report review

Source `0d64822`, same diagnostic script/question bytes as the valid dev7 run, unchanged model and budgets. Runtime differs only in child report guidance and package version. Fresh frozen directory: `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-subagent-report-bounds-20261010`. No repeated execution of this candidate.

| Metric | Valid dev7 probe | dev8 probe |
| --- | --- | --- |
| Child outcome | FAILED, invalid_finish: length | COMPLETED, parsed findings/uncertainties |
| Child steps / model requests | 5 / 5 | 4 / 4 |
| Read/search calls | 10 | 7 |
| Child wall time | 79.266s | 27.511s |
| Input / cached subset | 27,100 / 10,496 | 17,846 / 4,096 |
| Reported output | 4,958 | 1,576 |
| Historical estimated CNY | 0.0277194 | 0.0156648 |
| Usable report at tool boundary | None | Four findings, two uncertainties |

The dev8 final JSON is 1705 characters/bytes: below the enforced 6000-byte ceiling but **above the preferred 1200-character guidance**. No claim of strict adherence to the new instruction. The two unseeded single runs establish a useful observed difference, not causality or repeatability. Total diagnostic estimate including the invalid first harness is ¥0.0549652; none of the runs is omitted from cost accounting.

Manual evidence review:

- Constructor dimension insertion order/default dtype and generate_coords ij meshgrid statements match coordinate_transform.py:28-34,96-109. These are potentially useful implementation facts.
- Adapter dims, shape and coordinate selection match indexing.py:2147,2155,2162-2163. The statement about expected forward output is an implication, not a runtime guarantee for every subclass.
- The claim that vectorized indexing passes only 1-D positions is **incorrect**: indexing.py:526-529 explicitly permits N-D integer arrays; lines 1475-1494 preserve/reshape their dimensions. Its cited local call site does not justify the stronger claim. Main must not apply this conclusion blindly.
- The report says RangeCoordinateTransform.forward was not observed. In fact its body at range_index.py:67-70 was included in the step-2 read. All four context projection diagnostics reconstruct exactly; those lines are present at request 3 but absent at request 4 after COLD compaction. This establishes loss of evidence from the final request, not a proven causal explanation of the report error.

Protocol completion therefore PASS; source-backed report quality **PARTIAL**. Main did not consume the report, no code was implemented from it, and no official evaluator ran. This does not satisfy the SubAgent value gate. The source snapshot is the pinned image **before FeatureBench task preparation/masking**, with implementation bodies present; it is easier than the official reconstruction task. The report and reference facts must not be injected into subsequent official C17 Agent inputs.

Both runtime snapshots and all 195 xarray Python file hashes stayed unchanged during their respective probes. All requests exposed only inspect_repository; frozen manifests, wire limits, read-only container configuration and cleanup were audited. The normal main source/worktree was not edited by a child.

Next unresolved work: retain the source evidence needed for a bounded child synthesis without copying a full expensive Main context; distinguish supported facts from inference; then demonstrate autonomous Main delegation and actual use of a correct child finding on prepared task input. No new live task or release is justified merely by parsed JSON.
