+++
id = "review-repository"
name = "Review Repository"
description = "Review repository changes for correctness, regressions, and contract drift."
usage_scenario = "Use for code review, architecture review, and change-risk assessment."
priority = 40
version = "1.0.0"
+++

## Execution Principles

Lead with concrete findings and verify each claim against the current diff and contracts.

## Recommended Tools

Use bounded search, focused source reads, git status/diff, and relevant validation evidence.

## Workflow

1. Establish the intended contract and change boundary.
2. Inspect affected call paths and tests for behavioral risk.
3. Report actionable findings by severity with precise evidence.

## Constraints

Do not invent defects, expose private reasoning, or treat successful tests as complete proof.
