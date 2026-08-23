# ADR 005: CLI-first, Runtime-independent

## Context

Nexus V1 is CLI-first, but the execution engine must remain reusable by evaluation and
future adapters.

## Decision

Keep Typer handlers limited to argument parsing, configuration overrides, invoking the
Composition Root, calling `NexusRuntime`, and rendering `RuntimeEvent` values. Assemble
concrete dependencies only under `src/nexus/infrastructure/bootstrap/`.

## Alternatives

- Construct providers and graphs inside each command handler.
- Make CLI output callbacks part of the runtime.
- Build an unused web/API layer first.

## Why

A thin CLI keeps application behavior testable and prevents terminal concerns from owning
the product lifecycle.

## Trade-offs

The Composition Root and renderer add small indirection. In return, CLI framework types do
not leak into application or domain code.

## Future Migration

Interactive approval and later commands can remain adapters over the same runtime. Future
API, VSCode, or web adapters require approved scope and do not require replacing the core
runtime boundary.

