# AGENTS.md

## Project

This repository contains **Nexus**, a transparent and extensible coding-agent runtime.

The authoritative product and engineering specification is:

`docs/Nexus_V1_Product_Requirements_and_10-Day_Engineering_Specification_v1.1.1_中文.md`

That specification is the frozen implementation baseline for Nexus V1.

## Authority and Scope

1. Treat the Nexus V1.1.1 specification as the authoritative product and architecture contract.
2. Implement only the currently assigned Day Specification.
3. Future Day sections are architecture context only and MUST NOT be implemented early.
4. Do not silently reinterpret, weaken, redesign, or replace frozen decisions.
5. If the current task conflicts with the frozen specification, STOP implementation and report:
   - the conflict,
   - supporting evidence,
   - implementation impact,
   - available options.
6. Await an approved specification or ADR change before proceeding with any change that crosses the Codex decision boundary.

## Frozen Architecture Constraints

Preserve these principles unless the specification is explicitly amended:

- CLI-first, runtime-independent architecture.
- Async-first core.
- LangGraph implementation behind Nexus-owned runtime/domain boundaries.
- Domain must not depend on LangGraph, SQLAlchemy, MCP, Typer, or concrete model vendors.
- All side effects must pass through Tool Runtime, policy, approval, and sandbox boundaries as applicable.
- Context must be selected and bounded; never dump the entire repository or full session history into the model.
- Code modifications require validation.
- Runtime behavior must be observable through safe structured events without exposing private chain-of-thought.
- Concrete dependency assembly belongs only in `src/nexus/infrastructure/bootstrap/`.
- Provider boundaries must use dependency inversion where required by the specification.
- V1 tool execution is sequential by default.
- Existing files should normally be modified with `apply_patch`; `write_file` is for new files.
- Do not implement prohibited Git operations such as `git commit`, `git push`, or `git reset --hard` as Nexus Agent capabilities.

## Codex Implementation Decision Boundary

Codex MAY decide:

- private helper structure,
- local variable names,
- small refactors within the current module,
- test fixture details,
- error wording,
- standard-library usage,
- clearly equivalent low-level implementation details.

Codex MUST NOT independently change or invent:

- domain models,
- graph node responsibilities or edges,
- public/provider/tool contracts,
- database schema,
- security policy or risk classification,
- retrieval/chunking/ranking algorithms,
- evaluation success criteria,
- CLI public contract,
- persistence ownership boundaries,
- major/new dependencies,
- future features.

For any decision in the MUST NOT category:

`STOP → Report conflict → Explain options → Await approved specification change`

## Daily Workflow

For each Day task:

1. Read the relevant global constraints in the authoritative specification.
2. Read the complete 18-item specification for the assigned Day.
3. Inspect the current repository state before editing.
4. Produce a concise implementation plan mapped to the current Day.
5. Implement only the current Day scope.
6. Do not perform unrelated refactors or future-scope work.
7. Run all checks required by the current Day specification.
8. Report:
   - changed files,
   - tests/lint/type-check results,
   - acceptance-criteria mapping,
   - unresolved issues,
   - intentionally deferred future-Day work.

## Quality Gates

Before a Day is considered complete, preserve the project governance defined in the specification:

1. Acceptance Criteria
2. Code & Architecture Review
3. Tests / CI / Evaluation as applicable
4. Product Owner Knowledge Review

A Day is not complete merely because the code runs.

## General Safety

- Never log or commit secrets.
- Never bypass workspace containment, approval policy, command policy, or sandbox restrictions.
- Do not introduce raw `subprocess` usage in agent orchestration.
- Do not scatter raw SQL through services or agent nodes.
- Do not expose private chain-of-thought in events, logs, traces, or user output.
- Prefer minimal, specification-aligned changes over speculative abstraction.
