# Nexus Day 4 Validation Design

Authority: Frozen Baseline v1.1.1 and approved Day 4 Contract Addendum v0.2.3.

Day 4 validation is deterministic and independent from editing. The validation planner
projects only exact shell commands already visible in the approved `Plan.steps`, orders
them by the frozen check-kind priority, and adds `git_diff(staged=False)` inspection when
the run changed a file. It does not call a model, create tests, or interpret command
output.

The validation runner executes checks sequentially through `ToolRuntime`. Before any
execution it rejects a check outside the approved command scope with
`VALIDATION_PLAN_INVALID`. A normal non-zero command exit is `FAIL`; timeout, denial,
unavailable execution, infrastructure failure, or truncated evidence is `UNKNOWN`.
Successful changed-code validation requires a conclusive behavioral/build check and diff
inspection.

Repairability is mechanical. Only required failed TEST, BUILD, LINT, TYPE_CHECK,
GENERATED_TARGETED_TEST, or BASIC_EXECUTION checks whose governed result is exactly
`COMMAND_EXIT_NONZERO` are repairable. Every required failure must satisfy that rule.
There is no stdout classifier, regex heuristic, or LLM repairability decision.

Final diff evidence is collected separately through `git_status`, unstaged `git_diff`,
and bounded `read_file` calls for run-created files. Truncation or incomplete evidence
fails closed as `DIFF_UNAVAILABLE`. Initial dirty-state evidence is retained in
`includes_preexisting_changes`.

