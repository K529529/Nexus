# Monotonic execution timing for event evidence

Status: Windows regression and installed-wheel Linux smoke passed. This is an observability fix, not evidence of improved Coding Agent task correctness.

The dev11 and dev12 C17 trajectories contain backward wall-clock timestamps. Individual model/tool/process durations already use monotonic clocks, but first/last mutation timestamps were derived by subtracting wall-clock event timestamps. Those offsets cannot be trusted across clock corrections. The exact reason for the clock backsteps has not been diagnosed.

Add `execution_elapsed_ms` to run_started/run_finished, model_started/model_finished and tool_started/tool_finished event data. It is an integer millisecond offset from this invocation of run_turn, measured using the same monotonic origin as RunResult.duration_ms. It resets for every invocation, including resumed execution that retains run_id. Consumers must split windows at run_started, not assume a single origin per run_id. A tool_finished measurement includes the elapsed time through recording the paired result; it is an observation of completion, not the instant a write occurred inside the tool.

Child loop events preserve their own execution_elapsed_ms. When these events are forwarded under subagent_* names, the parent adds parent_execution_elapsed_ms without replacing the child origin. SubAgent lifecycle started/finished events, generated outside the child loop, carry the parent offset only. Correlate using existing parent_call_id and child session/run IDs. Parent duration includes waiting for children, so do not sum inclusive parent time and child time as independent wall duration.

These fields are telemetry only. They are not added to model Messages, ToolResult contents, tool schemas, prompts or private continuation state. Wall timestamps and existing duration_ms fields are retained. Old events without the new fields remain readable; absence is unknown, not zero. This change cannot retroactively repair earlier timing evidence.

The unexercised dev12 recovery reminder and its tests are removed in this branch. V0.2's original bounded detector is restored. Compared with dev11, runtime changes are only agent event annotation and package version; no new behavioral mechanism is introduced.

Tests simulate a one-hour backward wall-clock jump while a real file write occurs, inspect persisted JSONL and replay, verify exact monotonic offsets and per-invocation reset, verify resumed execution with the same run_id gets a new origin, and exercise nested Main/child events with independent origins and parent correlation. They also verify no elapsed fields enter either model's requests or tool messages.

Windows full suite: 509 passed / 10 optional Docker tests skipped. Targeted timing tests: 3 passed. Ruff src/tests and mypy src/tests (65 files), uv lock --check and wheel build PASS. No official FeatureBench or remote-model inference has been run for this telemetry change.

## Installed-wheel Linux evidence

Frozen code commit `11b14c07486eae7d82d3bc72d9982116a7cd57c2`, version 0.3.0.dev13; wheel SHA256 `432678a5efac6f3e3de3693b527aa633f60699bdb9f96d863260c9b29f5cd726`.

The built wheel was installed without dependencies into an ephemeral /tmp target in the existing pinned Linux image. Container network was none, root filesystem read-only, user 1000, capabilities dropped, no-new-privileges enabled; only /tmp and the separate temporary workspace were writable. A scripted model drove the actual recursive read-only child inspection, native apply_patch, Linux exec_command assertion, final response and SessionLog replay. No credentials or remote model requests were involved. This is an infrastructure smoke and does not establish autonomous child value.

The actual patch changed subtraction to addition in a temporary calc.py. A subprocess asserted add(2,3)==5 and returned exit_code=0 with `validated`. Public wall timestamps were deliberately moved backward by the probe without changing the host clock. Main event offsets stayed ordered: child tool completion 32ms, patch completion 43ms, final validation completion 76ms, run finish 87ms. Child elapsed began at 0 while its parent was at 10ms, and child finish was 21ms while parent time was 32ms. JSONL replay matched session messages; neither model received the new fields.

Event run-finish elapsed and RunResult.duration_ms share an origin but are sampled at slightly different points; do not require exact equality in real executions. CLI/process duration also includes work outside run_turn. Compare measurements only when their scope matches. A new full Linux pytest suite was NOT RUN; the installed-wheel integration smoke above was run.

[Machine-readable smoke receipt](monotonic-event-timing-result.json). Reproducible probe, runner, wheel, source identity and stdout/stderr are under `D:/WorkSpace/AgentBenchKit/.agentbenchkit/experiments/nexus-monotonic-events-20261010`.

This improves the reliability of future first/last tool-completion measurements despite wall-clock corrections. It does not establish higher correctness, better convergence or lower cost, and cannot recover earlier missing monotonic offsets. KEEP V0.2 remains the release recommendation; no merge/release. The long-term goal remains active and unachieved.
