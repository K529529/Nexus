# Exact command echo references

Status: experimental dev15, real-task benefit UNVERIFIED. KEEP V0.2; no merge/release.

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
