+++
id = "debug-python"
name = "Debug Python"
description = "Diagnose Python failures from reproducible repository evidence."
usage_scenario = "Use for Python exceptions, failing tests, and behavioral regressions."
priority = 60
version = "1.0.0"
+++

## Execution Principles

Reproduce the failure first and keep observed evidence separate from hypotheses.

## Recommended Tools

Use bounded repository search, focused file reads, and the approved validation tools.

## Workflow

1. Reproduce the smallest failing case.
2. Trace the narrowest relevant call path.
3. Correct the evidenced cause and rerun focused validation.

## Constraints

Do not broaden scope, suppress failures, or bypass Plan, policy, sandbox, and validation.
