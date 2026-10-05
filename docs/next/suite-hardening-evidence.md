# Autonomous hardening evidence (in progress)

Scope: improve the unchanged eight-case Next Dev Set and the general single-loop
agent. No case-specific runtime rules, gold patches, verifier modifications,
extra planning/judging models, or automatic model routing. User authorized
independent capability commits (no push), low-cost Bailian first, and paid work
within a CNY 100 mission allowance. This document does not claim mission completion.

## Baseline and experiments

- Pre-mission HEAD: `dcc1959f98d99350f1a89ef319fe5f0faf2b918d`.
- Flash baseline behavior: `8b5cd07`, Qwen3.8-Flash, max_steps=40,
  max_output_tokens=8192, context_window=1000000, request deadline=300s,
  reasoning_effort unset. Same frozen case tasks/images/validators.
- First run `20261005T171354Z-1be8b311`: pvlib reached max_steps with a
  candidate, then Docker Desktop backend crashed during collection. Original
  result remains ERROR and the other seven cases did not start.
- After user reboot, the stopped owned candidate was recovered without another
  model request. Original validator passed. Evidence under
  `pvlib__pvlib-python-1707/recovered-validation/{patch.diff,recovery.json,validator.log}`.
  Patch size 5657 bytes. Agent limited/40 remains distinct from validation PASS.
- Baseline remainder `20261005T175244Z-444cb820`: seven cases launched from
  an isolated Git archive of `8b5cd07`. Five raw hash differences against the
  first manifest were verified to be CRLF vs LF only (session.py, evaluation
  __init__.py/environment.py/validation.py, uv.lock). No behavioral difference.
  The archive is nested in the checkout, so its manifest Git HEAD may reflect
  the parent checkout; per-file hashes and the archived revision are authoritative.
- Cache experiment `20261005T180013Z-fc51ba8a`: Requests then pvlib,
  code `7717e01`, identical model/limits. Not part of the baseline.

Results live under `~/.nexus/evaluation/results/`. Original reports are retained;
no ERROR is overwritten to erase infrastructure failure.

## Verified system changes

| Commit | Capability | Evidence |
| --- | --- | --- |
| 36989db | Execution budget plus bounded verification guidance | Earlier focused offline tests; baseline still shows max_steps, so not claimed sufficient |
| 8b5cd07 | One empty-response retry and configurable request deadline | Adapter regression tests, retries never replay tools |
| f46e5c3 | Optional cached input token accounting and coverage | 98 targeted tests; missing is unknown, not zero |
| 7717e01 | Stable history prefix with current state in an ephemeral request suffix | 14 targeted tests; full offline 448 passed / 10 Docker opt-in skipped; Ruff and mypy passed |

One earlier full run intermittently failed the existing Windows child-process
exit probe. Focused cleanup tests and a subsequent full suite passed without
changing cleanup runtime or weakening the test. Retained as a reliability limit.

## Cost evidence

Initial pvlib known usage: 1,491,589 input, 37,565 output, coverage 40/41;
cache data was unavailable before telemetry. At Flash uncached list prices
(CNY 0.8/M input, 2.7/M output), this is approximately CNY 1.294, not an invoice.
The remaining baseline allocation is within the original CNY 15 provision;
the two-case cache experiment has a CNY 3 provision within the same mission budget.

2026-10-06 approximately 01:57 China time, read-only Bailian billing overview
showed balance CNY 100.43 and historical arrears CNY 0.00. Billing may lag usage.
No recharge, purchase, subscription or account-setting change was made.

Actual cache observation from the Requests cache experiment: request 5 reported
16,233 input tokens, 14,336 cached (88.3%). This proves a reusable history prefix
exists on the live service; it is not yet a complete-run saving or quality verdict.

## Early failure families (provisional)

- Tool syntax/matching recovery consumes turns: pvlib first successful mutation
  at step 18, after three patch errors; another conflict at step 23.
- Scope expansion after local checks: steps 37-39 expanded to additional suites
  and infrastructure investigation; max_steps was reached without final.
- Input pricing was opaque and dynamic system-prefix changes prevented large
  prefix reuse. Addressed with telemetry and request projection, with live hits.

Do not add policies from one sample alone. Finish baseline and group public
observations/results, excluding private reasoning, before further runtime changes.

## Primary references

- [Bailian context cache](https://platform.qianwenai.com/docs/developer-guides/run-and-scale/context-cache): stable prefixes, dynamic suffixes, reported cache hits.
- [Qwen Code prompt](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/core/prompts.ts): runtime reminders can be carried in user/tool messages.
- [mini-SWE-agent control flow](https://mini-swe-agent.com/latest/advanced/control_flow/): a small query/execute loop with explicit cost/step limits and observed exit state. No extra Judge required.
- [Qwen3.8-Flash pricing](https://www.qianwenai.com/models/qwen3.8-flash).
- [DeepSeek-V4.1-Flash on Bailian](https://www.qianwenai.com/models/deepseek-v4.1-flash): candidate low-cost comparison; no new paid DeepSeek run yet in this mission.

## Completed baseline rows so far

| Case | Validation | Agent | Steps | Known input / output | Agent seconds |
| --- | --- | --- | ---: | --- | ---: |
| pvlib | PASS after validation-only recovery | limited/max_steps | 40 | 1,491,589 / 37,565 (40/41 calls) | 745.938 |
| matplotlib | FAIL | completed | 38 | 1,146,812 / 25,538 (38/38) | 559.281 |

Matplotlib is a semantic correctness failure, not absence of editing or final.
The candidate changes an existing public inverse behavior and accepts coarser
cursor formatting; independent required checks reject an exact output regression.
Its self-authored checks and a final completion claim did not prove compatibility.
This is evidence for preserving public contracts and checking independently derived
expected behavior, not permission to inject hidden expected strings into prompts.
Known uncached-equivalent subtotal for these two baseline cases: about CNY 2.28.

## First completed cache experiment

Requests (`20261005T180013Z-fc51ba8a`): validator PASS, Agent completed at
step 36, 54 tool calls, first apply_patch-reported mutation at step 20.
Reported usage coverage 36/36: input 1,164,513; output 26,685; cached input
701,696 (60.26% of input). Agent time 502.125s (8.37 minutes).

At Flash input/cache/output CNY 0.8/0.1/2.7 per million, known usage totals
CNY 0.51247, versus CNY 1.00366 for the same usage with no cache discount.
This is approximately 49% lower list-price cost, not a settled account invoice.
It proves a cheap model can both repair and finish this instance; it does not
establish stable eight-case competence or isolate a quality effect from caching.
The trajectory still expands validation and compares baseline failures around
steps 28-30, so latency/verification discipline remains an open improvement.

Packaging checks: `uv lock --check --offline` and `uv build --offline` passed.
A clean isolated environment installed the wheel with exported locked runtime
dependencies (offline cache was incomplete, so registry access was needed).
From outside the source checkout, isolated import resolved request_context.py
under site-packages and `nexus --help` succeeded. No package was published.
