# Stage 4 engineering attempts — 2026-10-26

No paid inference, Docker benchmark, SRT initialization, security bypass experiment,
account/ACL/WFP change, or replay of the existing PoC scripts was performed.
All subprocess acceptance below uses ordinary temporary test fixtures. The Node
test manager is explicitly UNSANDBOXED and is not reachable through production config.

## Earlier attempts retained

- Initial Ruff with its repository cache: access denied creating a cache temporary file.
  A no-cache attempt could read the files but could not atomically replace one new
  file. Formatting was subsequently done with normal user permissions; no ACL changes.
- Initial selected pytest command referenced nonexistent `tests/test_cli.py`:
  no tests ran. Corrected selection uses the existing CLI suites.
- Security suite: 23 passed in 1.96 s on the first isolated run.
- Bridge tests under the restricted tool environment: timeout/cancel cleanup returned
  `cleanup_incomplete`; these were not treated as passing cleanup tests.
- First normal-user Bridge run reused the other execution identity's default
  `pytest-of-Archer` directory: 10 fixture setup permission errors. No test body ran.
  All subsequent normal-user suites used a fresh system-temp parent via
  `PYTEST_DEBUG_TEMPROOT`, not a repo-root basetemp and not modified permissions.
- Normal-user Bridge run with fresh parent: 10 passed in 9.38 s.
- Initial combined CLI attempt was interrupted after a fake Conversation without
  the new gateway field caused its test to wait indefinitely. The fake was corrected;
  the production no-editor drive path does not install an approval callback.
- `pytest-full-attempt1.txt`: 865 passed, 7 failed, 10 skipped. Failures were old
  config/Conversation test doubles and old adjacent-event assertions. Existing
  functional assertions remain; plan/skill ordering tests now ignore only the two
  added security audit event kinds. New tests verify those security events separately.
- `pytest-focused-attempt2.txt`: 185 passed in 29.05 s, including the repaired tests,
  real pipe-input approval tests, and the new restricted startup tests.
- `pytest-full-attempt2.txt`: 878 passed, 10 skipped in 70.03 s.
- A strict type check identified config mode's type after adding malformed TOML
  validation; a cast follows the runtime literal validation. No accepted values changed.
- `ruff-attempt1.txt`: formatter left one 104-column fixture string; split it into
  adjacent string literals. `ruff-final.txt` records the final check separately.
- Final regression additionally covers incomplete Bridge result frames. Actual final
  count/status is in `pytest-full-final.txt`, not inferred from the prior run.

Console-only attempt details above are transcribed from tool outputs. They are not
reconstructed raw pytest logs. Full-suite/focused logs and performance samples are
saved as raw command output in this directory.
Exception: pytest's first failure log contains whitespace-only traceback lines.
Its exact original bytes are preserved in `pytest-full-attempt1.txt.gz`; the `.txt`
view normalizes line endings and strips trailing whitespace for `git diff --check`.
No traceback content or failed assertion was removed.

## Performance protocol

`python -m scripts.stage4_engineering_perf` uses ordinary system temporary directories,
10 interleaved pairs of the same real PowerShell command with direct local executor
versus Gateway + the same executor, and the same durable JSONL sink. No model is used.
Five fake-manager Node initializations and 15 real Python Worker invocations measure
transport/process/patch/audit costs, excluding real SRT initialization and enforcement.
The tiny sample is descriptive; neither throughput improvement nor OS isolation is inferred.

## Build and isolated installation

`uv lock --check --offline` and `uv build --offline` succeeded. Wheel was installed
with `uv pip install --offline` into a new system-temp venv, then imported using
`python -I` from outside the source checkout. `srt_bridge.mjs`, `srt_adapter.mjs`,
and `patch_worker.py` were present in the installed package; default policy was Local.
`python -I -m nexus --version` returned 0.2.0; `--help` succeeded.
The installation smoke resolved cached compatible dependencies; the full test suite
used the existing development venv. These are distinct dependency environments.

Only static text from installed SRT 0.0.81's package.json and public .d.ts declarations
was inspected. No SRT module was imported or initialized. The declarations match
the initialize / wrapWithSandboxArgv / reset integration seam used here.
