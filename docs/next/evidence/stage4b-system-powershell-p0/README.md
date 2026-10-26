# Stage 4B System PowerShell P0 evidence

Normal functional regression only. No model calls, administrator token, SRT
provisioning/forget/recover commands, old security PoC, or upstream package edits.

- `original-failure-tool.json`: tool error only from the user's manual session.
- `manifest.json`: first wheel and explicit System32 shell; ACL fix before the
  subsequently discovered Patch completion race fix.
- `functional-01.json` / `.log`: first real run, including **Git bridge_busy**;
  runner exit zero does not make this a full pass.
- `profile-trace.jsonl`, `init-profile.json` / `.log`: sequential, initialization-only
  root groups, no executed commands. Same ACL policy as final; earlier wheel.
- `manifest-final.json`, `functional-final.json` / `.log`: final wheel; normal
  PowerShell/Python/Patch/Git/network/denial tests and WW→RO→WW.
- `functional*-trace.jsonl`, `manual-trace.jsonl`: opt-in in-memory observer times
  native operations and manager methods; no command argv, environment or tokens.
- `manual-workspace.json`: final installed wheel against the user's exact workspace,
  only `whoami; Get-Location`, using real SRT and System32 Windows PowerShell.
- `acl-before.json`, `acl-after.json`: ordinary caller token and read-only DACL
  snapshots of PowerShell, System32 and Program Files. No ACL repair attempted.
- `root-inventory.json`: read-only runtime file counts, without following links.
- `unit-attempt-01.txt`: initial test setup failure against an existing inaccessible
  pytest directory. No test body ran; no permissions changed.
- `unit-focused.txt`, `pytest-full.txt`: ACL-policy revision checks.
- `unit-final-focused.txt`, `pytest-final.txt`, `ruff.txt`, `mypy.txt`: final checks,
  including delayed IPC unlink regression.
- `verification.json`: independent result assertions from `scripts/stage4b_verify_p0.py`.
- `verification-attempt-01.txt`: verifier text-category mismatch and correction;
  `host-final-check.json` independently checks the Git commit and denied writes.
- `timing-summary.json`: all trace groups and step timings in one file.
- `processes-after.json`: no remaining process whose PID/parent belongs to traced bridges.
- `test-temp-*.json`: verified scope and cleanup of this run's ordinary test temps.
- `retained-artifacts.json`: retained real acceptance fixtures and final installation.

`close.confirmed` describes bridge exit. Native `acl restore` can time out and SRT
reset logs it without throwing; it does **not** prove complete removal of persisted
grants. Old dead-holder System32 recovery warnings are retained in the raw output.
The timing groups share the machine's existing SRT state and caches; they are not
clean-machine causal estimates or a benchmark of a speed optimization.

Reproduction: install the wheel outside all workspace/write roots. Run
`scripts/stage4b_srt_acceptance.py` with `--shell` pointing to system PowerShell,
and `--temp-root D:/WorkSpace`. The trace requires process-local `NODE_OPTIONS`
`--import file:///.../scripts/stage4b_srt_trace.mjs`, `NEXUS_SRT_TRACE` output path,
and `NEXUS_SRT_TRACE_PACKAGE` pointing to the existing SRT 0.0.81 package. Run
the init profile sequentially, never concurrently with another acceptance session.
The deliberately reduced init-only root groups are not supported execution modes.
