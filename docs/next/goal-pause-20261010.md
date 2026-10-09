# Nexus long-term goal: pause checkpoint, 2026-10-10

Paused at the user's request after finishing the current round. Overall objective is not achieved. Recommendation remains KEEP V0.2; no merge/release. No task or model execution remains live after the dev15 C08 runner exited 0 and cleanup completed.

Stable main/origin/main remain b5b25b5c9eb26fe4f11fd5e10f0d42b267e8c80a. Current experimental branch: codex/nexus-command-echo-projection. Frozen dev15 source: afa3b14453b6151e0c650f8f1a7f9f2533bdf027. User files .idea/, bubble_sort.py and i_love_you.txt are untouched.

## Current evidence

- Original V0.3 three-case sprint failed its overall gate; stable V0.2 remains the accepted baseline. Later candidates do not erase those results.
- dev14 tested a unified bounded Plan reminder before/after writes. Both reminders reached Main, but C17 remained official FAIL 4/14 (P2P 74/74), with latency/cost increases. That mechanism was removed; the original V0.2 detector was restored. See [dev14 report](plan-idle-bounded.md).
- dev13 monotonic timing remains: explicit event offsets distinguish actual execution elapsed time from wall-clock jumps. It is observability evidence, not correctness improvement. See [timing report](monotonic-event-timing.md).
- dev15 removes only exact command echoes from model requests when the unique matching call remains present; raw results and logs stay complete. C08 official PASS 21/21, P2P 30/30. The actual trace saves 18,816 repeated observation bytes, but observed total cost and time rose versus V0.2. No overall performance claim. See [current report](command-echo-projection.md).
- Tool-based recursive SubAgent exists, uses the same loop with isolated read-only context, depth 1 and bounded steps/context/output. Component diagnostics are available, but formal Main-driven experiments have not provided a useful child finding that Main integrated. The value requirement is unmet.

## Gate status

| Requirement | Current evidence |
| --- | --- |
| Single Main message-driven loop, no fixed graph | Maintained |
| Tool action/patch and telemetry reliability | Regression/probe evidence; generated calls still sometimes violate contracts |
| C08 normal single-agent correctness | dev15 PASS, no latency/cost improvement |
| C17 multi-module correctness/convergence | Latest tested dev14 FAIL; dev15 NOT RUN |
| C13 convergence/coverage | Prior partial improvement was insufficient; dev15 NOT RUN |
| SubAgent real value and Main integration | NOT PROVEN |
| Overall V0.2 replacement | NOT ACHIEVED; KEEP V0.2 |

Windows dev15 regression: 523 passed, 10 optional Docker tests skipped; Ruff/mypy (66 files), lock check and wheel build PASS. Linux full unit suite NOT RUN; the frozen Linux container executed real C08 successfully. No holdout, full-16 expansion, Agent FAIL retry, merge or release.

## Resume boundary

Do not resume automatically while this pause is in force. When the user resumes, verify the current worktree and this checkpoint before spending another benchmark call. Do not reintroduce failed reminder mechanisms or conflate byte deduplication with model-token/cost/correctness improvement. Further promotion requires actual difficult-task improvement, no positive-control regression and a genuinely useful child result integrated by Main. Existing evidence should guide the next hypothesis rather than adding more generic reminders.
