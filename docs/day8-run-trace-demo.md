# Nexus Day 8 Run Trace Demo

This artifact shows the safe event shape and records real-provider acceptance separately. It does
not contain a prompt, model response, Tool arguments/output, command, file content, diff, raw
exception, environment value, or credential.

## Deterministic local trace shape

For one execution segment, ConsoleTracer emits JSON Lines in publisher sequence. Representative
records are abbreviated to approved fields:

```json
{"schema_version":"1.0","event_type":"run.started","sequence":1,"phase":"RUN","severity":"INFO","payload":{"task_character_count":42}}
{"schema_version":"1.0","event_type":"model_call.started","sequence":6,"phase":"MODEL","severity":"INFO","payload":{"model_call_phase":"PLAN","provider":"openai_compatible","model":"fixture-model"}}
{"schema_version":"1.0","event_type":"tool.finished","sequence":12,"phase":"TOOL","severity":"INFO","payload":{"tool_name":"apply_patch","source":"NATIVE","success":true,"final_risk":"WRITE","policy_decision":"ALLOWED","approval_decision":"APPROVED","duration_ms":4,"error_code":null}}
{"schema_version":"1.0","event_type":"validation.finished","sequence":18,"phase":"VALIDATION","severity":"INFO","payload":{"validation_status":"PASS","confidence":"HIGH","executed_check_count":2,"repair_count":0,"duration_ms":31}}
{"schema_version":"1.0","event_type":"run.finished","sequence":19,"phase":"RUN","severity":"INFO","payload":{"runtime_status":"COMPLETED","terminal_status":"SUCCEEDED","changed_file_count":1,"validation_status":"PASS"}}
```

Actual records also include UUID `event_id`, `trace_id`, `execution_id`, `run_id`, optional
`session_id`, span correlation, UTC timestamp, and redaction metadata. `RuntimePhase.MODEL` is
separate from payload `ModelCallPhase.PLAN`.

The lifecycle adapter observes exactly:

```text
start_run(execution)
record(run.started)
record(...publisher sequence...)
record(run.finished)
finish(execution)
```

An interrupted segment ends with `run.interrupted` and `INTERRUPTED`; resume retains
`trace_id=run_id`, creates a new `execution_id`, starts sequence at one, and uses a fresh sink
queue.

## Token completeness

Only provider-reported values are aggregated. `usage_complete=true` means every attempted model
call returned a complete, internally consistent input/output/total triple. Partial or unavailable
calls retain truthful reported subtotals and are never displayed as an exact zero.

## Real LangSmith acceptance record

Status: **NOT RUN**

Reason: implementation validation did not receive explicit authorization to export telemetry.
The opt-in `langsmith_e2e` path requires `NEXUS_RUN_LANGSMITH_E2E=1` in addition to credentials,
uses a disposable fixture, flushes the client, retrieves the remote root and children, and checks
the redaction sentinel remotely.

When an authorized run is performed, record only:

```text
status: PASS | FAIL
langsmith SDK: 0.11.x
project: sanitized project identifier
run_id / trace_id: UUID
execution_id: UUID
timestamp: UTC
redaction sentinel absent remotely: yes | no
```
