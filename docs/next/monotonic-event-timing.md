# Monotonic execution timing for event evidence

Status: implementation and Windows tests passed; installed-wheel Linux smoke pending. This is an observability fix, not evidence of improved Coding Agent task correctness.

The dev11 and dev12 C17 trajectories contain backward wall-clock timestamps. Individual model/tool/process durations already use monotonic clocks, but first/last mutation timestamps were derived by subtracting wall-clock event timestamps. Those offsets cannot be trusted across clock corrections. The exact reason for the clock backsteps has not been diagnosed.

Add `execution_elapsed_ms` to run_started/run_finished, model_started/model_finished and tool_started/tool_finished event data. It is an integer millisecond offset from this invocation of run_turn, measured using the same monotonic origin as RunResult.duration_ms. It resets for every invocation, including resumed execution that retains run_id. Consumers must split windows at run_started, not assume a single origin per run_id. A tool_finished measurement includes the elapsed time through recording the paired result; it is an observation of completion, not the instant a write occurred inside the tool.

Child loop events preserve their own execution_elapsed_ms. When these events are forwarded under subagent_* names, the parent adds parent_execution_elapsed_ms without replacing the child origin. SubAgent lifecycle started/finished events, generated outside the child loop, carry the parent offset only. Correlate using existing parent_call_id and child session/run IDs. Parent duration includes waiting for children, so do not sum inclusive parent time and child time as independent wall duration.

These fields are telemetry only. They are not added to model Messages, ToolResult contents, tool schemas, prompts or private continuation state. Wall timestamps and existing duration_ms fields are retained. Old events without the new fields remain readable; absence is unknown, not zero. This change cannot retroactively repair earlier timing evidence.

The unexercised dev12 recovery reminder and its tests are removed in this branch. V0.2's original bounded detector is restored. Compared with dev11, runtime changes are only agent event annotation and package version; no new behavioral mechanism is introduced.

Tests simulate a one-hour backward wall-clock jump while a real file write occurs, inspect persisted JSONL and replay, verify exact monotonic offsets and per-invocation reset, verify resumed execution with the same run_id gets a new origin, and exercise nested Main/child events with independent origins and parent correlation. They also verify no elapsed fields enter either model's requests or tool messages.

Windows full suite: 509 passed / 10 optional Docker tests skipped. Targeted timing tests: 3 passed. Ruff src/tests and mypy src/tests (65 files), uv lock --check and wheel build PASS. No official FeatureBench or remote-model inference has been run for this telemetry change.
