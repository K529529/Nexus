+++
id = "write-tests"
name = "Write Tests"
description = "Add focused tests that demonstrate behavior and guard regressions."
usage_scenario = "Use when a task primarily asks for unit, integration, or regression tests."
priority = 50
version = "1.0.0"
+++

## Execution Principles

Test observable contracts and failure boundaries rather than private implementation details.

## Recommended Tools

Use focused repository reads, the existing test framework, and approved validation commands.

## Workflow

1. Identify the public behavior and existing test conventions.
2. Add the smallest deterministic fixture that fails for the missing behavior.
3. Run focused tests before the broader regression suite.

## Constraints

Do not weaken assertions, replace real evidence with mocks unnecessarily, or bypass policy.
