# Shell feedback: bounded output guidance candidate

Status: local contract reproduction and regression checks PASS; Agent behavior improvement NOT RUN. Experimental version `0.3.0.dev7`, branch `codex/nexus-shell-feedback`. Stable V0.2 remains accepted; previous V0.3/dev6 failures remain failures. No merge/release recommendation.

## Evidence and scope

The dev6 C17 trajectory contains failing pytest/import commands piped to `tail`, with `ToolResult.ok=true` and shell exit 0. The final step writes malformed formatting code and then shows SyntaxError, but all 50 model steps are exhausted. The [official report](observation-retention-final.md) shows failed test collection, not a tested solution.

Source inspection confirms this is the documented executor contract: [development design section 5.1](01-development-design.md) returns the actual shell exit code and does not infer each internal command's success. The executor runs the supplied command unchanged (apart from Windows UTF-8 initialization). Automatically setting pipefail, changing default shells, or guessing failure from output text would change that contract. No such changes are made.

Nexus already drains output into a bounded head/tail buffer. Therefore routine checks can be run directly without adding a lossy shell pipeline. The experiment makes that existing capability explicit in the exec_command tool description, explains shell-status scope, and asks the model to inspect check output. No new fields, classifiers, failure parsers, graph nodes or retry mechanisms.

The unaccepted 64k observation cap is reverted to the dev4 16k policy with its original regression expectations. Relative to `e83e9c8`, runtime source differs only in tool description and version. Plan/nudge, prompt, executor, SubAgent, context projection and provider behavior otherwise match that control. This is a separate candidate; do not compare it to dev6 as a one-variable intervention.

## Real process reproduction

[scripts/probe_shell_exit_status.py](../../scripts/probe_shell_exit_status.py) executes the actual Nexus executor against a Python command emitting over 100KB and exiting 7, using a 4096-byte capture budget. It makes no model or network calls.

| Case | Windows PowerShell shell exit / ok | Linux /bin/sh exit / ok |
| --- | --- | --- |
| Direct failing check | 1 / false | 7 / false |
| Same check piped to output filter | 1 / false | 0 / true |
| Successful program prints literal SyntaxError fixture text | 0 / true | 0 / true |
| Explicitly preserve failing child status | 7 / false | 7 / false |
| Failing check followed by successful output command | 0 / true | 0 / true |

Direct output is truncated to 4124 returned stdout bytes (4096 retained bytes plus marker), preserving CHECK START and CHECK FAILED. Both platforms retain the real direct failure status. The initial diagnostic assumed filtered PowerShell behaved like /bin/sh; the actual run disproved that assumption. The final reproduction records platform-specific expectations. Text containing an error name can be valid successful output, so string matching is not a safe substitute for shell/check semantics.

Receipts: [Windows](shell-feedback-probe-windows.json), [Linux](shell-feedback-probe-linux.json). These are process-level evidence, not an official benchmark, not a simulated model success and not a new holdout.

## Checks and limitations

- Windows full suite: 504 passed, 10 optional Docker checks skipped. Initial attempt failed fixture setup because the default pytest temp directory denied access; rerun used a new project-local basetemp and passed. No product fix was made for that environment problem.
- Linux tool regression: 15 passed using an independent temporary Python 3.12 environment, with locked pytest 9.1.1 / pytest-asyncio 1.4.0. The immutable ABK release environment was not modified. Transitive test dependencies were newly resolved, not a full uv.lock installation; this is a tool regression check, not a claim of fully identical dependency environments.
- Ruff PASS, mypy src/tests PASS (64 files), uv lock --check PASS, wheel/sdist build PASS.
- No paid model calls or FeatureBench runs in this experiment yet. No previous failed Agent was retried.

The model may ignore this guidance as it ignored generic exit-status guidance before. More importantly, preserving a failure flag cannot repair C17's final-step syntax error after the step budget has expired. This candidate only improves tool affordance clarity; it does not establish better coverage, convergence or SubAgent adoption. Do not declare the long-term goal achieved or launch a broad benchmark based on these local checks.

The next behavior-level evidence must show that Main actually chooses a status-preserving validation command, reads failure evidence and repairs while budget remains. For SubAgent, an independent finding used by Main is still required; this candidate has not supplied that evidence. Any new live experiment needs a fresh freeze and must retain all previous failures in the comparison.
