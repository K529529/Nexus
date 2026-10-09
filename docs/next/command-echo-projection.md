# Exact command echo references

Status: experimental dev15 C08 positive control PASS; exact request-byte savings verified. Overall task/cost improvement remains UNPROVEN. KEEP V0.2; no merge/release. Work pauses after this round at the user's explicit request.

Dev14 again showed failed shell checks masked by a later command. The executor already follows the explicit contract of returning the shell's actual status. Changing it with inferred failure text, implicit pipefail or shell rewriting would be a different contract; none is done here. Investigation also found long script commands duplicated verbatim in the assistant tool call and its result. This candidate removes that exact redundancy at request time without dropping output evidence.

For a unique exec_command caller in a complete assistant/tool group in the same logical request, compare parsed command strings exactly. If the native result call_id matches and its data.command equals the caller argument, replace that one echo with data.command_ref containing the call ID, only when the resulting UTF-8 JSON is smaller. Every other result field is preserved. Foreign tools, unavailable/duplicate/unpaired callers, malformed JSON, mismatched commands/IDs, existing reference fields and non-saving replacements retain their original result. The executor, public durable ToolResult, JSONL, replay and safety snapshots keep the full original command.

Selection of HOT/WARM/COLD observations and its 16k-token target remain unchanged and use original sizes. Apply deduplication after that selection; existing COLD previews already omit the command and retain original source_bytes. Thus the candidate claims fewer redundant request bytes, not a larger retained working set or better source coverage. Re-projection always returns to raw observations first, so references do not compound across retries/resume. New policy provenance exact-call-v1 and per-request reference/saved-byte counts accompany the existing compact-v1 diagnostics. Full counts now mean full output/facts, allowing a bound reference to duplicate command input.

## Evidence before live evaluation

Reconstructed all 50 dev14 C17 observation projections and matched every existing diagnostic. Production candidate matches every expected message after the narrow replacement. Across those historical requests, 382,458 bytes are removed out of 2,534,133 visible observation bytes (15.09%); 51 unique results can be referenced, with up to 21,074 bytes saved in one request. The local serialization-based estimate drops by 134,773 tokens summed across requests. These sums count repeated transmission of the same history; they are not unique stored bytes, provider-token measurements, invoice savings or improved Agent behavior. Private reasoning is excluded from this offline reconstruction. [Exact replay](command-echo-projection-replay.json).

Windows full suite: 523 passed / 10 optional Docker tests skipped. Ruff, mypy (66 files), lock check and wheel build PASS. New tests cover exact fact reconstruction, all ambiguous binding cases, no-savings fallback, COLD source provenance, compaction, real failed shell command output and durable replay. Two initial focused runs failed because test assertions indexed the temporary Plan suffix as though it were a tool message; assertions were corrected to bind actual tool messages/call IDs. No product behavior was relaxed for those failures. Linux full unit suite NOT RUN.

## Live gate

First run one fresh C08 positive control with qwen3.8-flash/low, 50 steps/900s, samples 1/concurrency 1 and the same FeatureBench evaluator/isolation/Candidate Freeze. No Agent FAIL retry. Compare official verdict, actual mutation/check behavior, tokens, timings and per-request dedup facts. Do not infer that positive-control success proves C17/C13 correctness or SubAgent value. C17/C13/holdout NOT RUN for this candidate until evidence warrants them. The stable V0.2 comparison predates stronger egress isolation.

Candidate scope relative to dev13 source (the runtime restored at 6afd4f5) is observations.py and version metadata only. SYSTEM, tools, model configuration, child limits, original V0.2 detector, execution budget and dev13 timing are unchanged. No forced delegation or new graph.

External experiment directory: D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-command-echo-projection-20261010.

## Completed C08 positive control

Frozen source `afa3b14453b6151e0c650f8f1a7f9f2533bdf027`, version 0.3.0.dev15; wheel SHA256 `83007aa89fb68829e358df47c9f2ce212fcfabc40558a887212c842824c3b503`; image `sha256:ddb055545581db91184dd87537dae37069657bb50a7927881074b3a83eec7645`. Run `20261009T180016Z-412e2b70` (UTC ID; local date 2026-10-10). Exactly one execution, no retries; official PASS 21/21 and P2P 30/30. Agent COMPLETED.

| C08 metric | V0.2 | dev15 |
| --- | --- | --- |
| Official F2P / P2P | 21/21 / 30/30 | 21/21 / 30/30 |
| Model turns / steps | 15 / 15 | 14 / 14 |
| First mutation step | 5 | 5 |
| First mutation time | 40.054s nominal wall offset | 64.422s monotonic tool completion |
| Last mutation step | 11 | 11 |
| Agent process seconds | 133.115 | 149.986 |
| Model / tool wall seconds | 126.522 / 4.456 | 144.190 / 3.518 |
| Input / output tokens | 187,749 / 7,571 | 197,251 / 10,059 |
| Cached input subset | 159,744 | 168,704 |
| Historical estimated CNY | 0.0588201 | 0.0668673 |
| Patch errors / attempts | 1 / 6 | 1 / 4 |

Observed process latency +12.67%, estimated total cost +13.68%. One fewer model turn did not make this run faster or cheaper. Historical formula remains ((input-cached)*0.8 + cached*0.1 + output*2.7)/1e6 CNY, not current tariff/invoice. All usage records are complete. Cache state, unseeded model variation, generated implementation and stronger evaluation egress isolation prevent causal attribution to the projection change. Do not describe lower request bytes as measured provider-cost improvement.

Step 3 submitted a patch without required framing and was correctly rejected. Steps 5/6 wrote the implementation. Steps 8/9 checked numeric cases; the first script printed one failing expectation for smoothed perfect correlation, and Main subsequently distinguished the all-counts-equal case from partial-support perfect correlation. A step-10 public import check failed because exports were missing; Main fixed exports at 11 and rechecked at 12, then completed its plan at 13 and responded at 14. Official tests independently passed. No stagnation reminder or SubAgent call occurred; this positive-control task does not demonstrate SubAgent usefulness.

Exact reconstruction of all 14 recorded observation projections PASS. Seven unique tool results were referenced; over the actual requests, 18,816 duplicate bytes were removed from a counterfactual 146,393 visible observation bytes (12.85%), with a local serialized estimate reduction of 6,473 tokens. Every changed result reconstructs to the original JSON by restoring command from the still-present matching caller; every other field is identical. This is same-trace input-efficiency evidence, not a second model run or proof of changed reasoning. All 58 timed lifecycle events have nondecreasing offsets; core completion 147.759s differs from the outer process duration because startup/exit work is outside the loop.

Integrity PASS: source/wheel/image hashes, 36 installed Python sources, dependency equivalence apart from Nexus, frozen evaluator/ABK inputs, full egress isolation, one execution, official report, raw/archive evidence equality and cleanup. Evidence hash `da22b69dccad1155b370b6abb7fbb7ff2df284a674e73c39c10f5eadd5185147`. [Audited result](command-echo-projection-result.json).

Retain this change only on the experimental branch for review: it has exact redundancy reduction and one positive-control correctness check, but no demonstrated overall improvement over V0.2. C17, C13 and holdout were not run with this candidate. Multi-module convergence, final behavioral validation and autonomous Main use of child findings remain unresolved. No further candidate or benchmark is started after the user's pause request.
