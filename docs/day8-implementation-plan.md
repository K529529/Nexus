# Nexus Day 8 Observability Implementation Plan

Authority: `docs/spec-addenda/Nexus_Day8_Contract_Addendum_v0.3.md`

Status: APPROVED FOR IMPLEMENTATION — DAY 8 ONLY

## Boundaries

- Preserve the existing graph nodes, edges, sequential Tool execution, security, approval,
  retrieval, MCP, Skill, persistence ownership, and CLI command surface.
- Add no database migration, Evaluation/Day 9 code, OpenTelemetry, dashboard, UI, or raw-content
  tracing.
- Keep RuntimeEvent and TelemetryEvent separate and keep all tracer I/O outside the Agent critical
  path.

## Dependency-ordered implementation

1. Domain contracts
   - Add RunExecutionContext, TokenUsage/TokenUsageAggregate, telemetry enums/envelope,
     TraceRunStart/TraceRunFinish, Tracer port, and approved RuntimeEvent additions.
   - Preserve backward-compatible constructors, especially ValidationFinished and model values.
2. Execution context and live publication
   - Bind exactly one execution context at each NexusRuntime run/resume boundary.
   - Replace drain-after-graph delivery with an in-process publisher/live iterator without changing
     the public AsyncIterator signature.
3. Model and token instrumentation
   - Add ObservedModelGateway as the only real-call counter/event owner.
   - Apply semantic phases at every model consumer and normalize provider-reported usage without
     estimation or duplicate streaming aggregation.
4. Telemetry safety and lifecycle
   - Implement event-specific deny-by-default allowlists, bounded serialization, correlation, and
     structured logging.
   - Give every enabled sink/execution one FIFO worker/control queue ordered
     TraceStart -> TelemetryEvent record* -> TraceFinish, including the approved failure semantics.
5. Adapters and trusted configuration
   - Add default-off ConsoleTracer and manual sanitized LangSmithTracer.
   - Enforce environment-only API key and user/environment-only remote tracing configuration;
     reject repository LangSmith configuration.
   - Add only `langsmith>=0.11.1,<0.12` and verify the lock/wheel.
6. Runtime, graph, CLI, resume, and persistence integration
   - Emit the approved missing events at existing ownership points, record validation duration and
     changed files, carry token aggregate through checkpoints, persist the reported subtotal, and
     render safe live summaries.
7. Verification and deliverables
   - Add domain, ordering/race, failure, concurrency, redaction, model usage, resume, integration,
     and opt-in `langsmith_e2e` coverage.
   - Add the Observability Guide and deterministic run trace demo.
   - Run targeted tests, full non-live tests, Ruff, strict Mypy, lock/dependency/wheel checks, and
     verify no migration, Day 9, graph topology, or security-policy drift.

## Stop rule

If implementation requires a contract, graph, security, persistence-schema, CLI, or dependency
change beyond v0.3, stop with evidence and request Architect direction.
