---
name: pytest-regression
description: Write focused pytest regressions for Python bug fixes, especially boundary inputs and rejected writes whose persisted state must remain unchanged.
---

# Focused pytest regression

Use when the task requires a Python defect fix with regression coverage.

- Read the nearby tests and reuse the project's fixtures and test commands. Derive
  expected values from the requested behavior and existing contract.
- Pick a discriminating example: for a filter, include both matching and nonmatching
  records; for a transformation, include text that must remain unchanged internally.
- For rejected writes, check the response/error AND query the persisted state to prove
  no record was created or changed. Check relevant fields rather than only row count.
- Parametrize equivalent boundary inputs without duplicating test setup. Include one
  representative valid input to protect behavior outside the fix.
- Where practical, demonstrate that the focused regression fails on the original
  implementation and passes with the fix. Do not revert unrelated user work to do so.
- Run the focused tests, then the appropriate existing suite. Once the requested
  behavior and relevant checks pass, report exact commands/results and stop.

Do not invent a new test framework or add dependencies solely to use this Skill.
