# Native Console TUI evidence — 2026-10-28

Implementation: `d293d601b6a24e5f83638e913d1c5e76c6b9c8ab`.
Baseline: `2ba0c5fb38d7a369e92b8d24f141d8103fc21777`.
See the [report](../../../reports/stage4-tui-cross-platform-hardening.md) and `manifest.json` for installed paths, hashes, scope and limitations.

## Final evidence

- `pytest-final-02.log`: final complete Windows run, 1007 passed / 10 opt-in Docker skipped.
- `linux-ui-final.log`: WSL source TUI tests, 67 passed.
- `ruff-final-02.log`, `mypy-final-02.log`: final static checks.
- `final-02-multiline`: real classic Console, 10 rounds with 13 history lines per round, 7-line drafts, vertical and horizontal resizing after history exists.
- `final-02-conpty`: same final installed candidate and scenario using ConPTY.
- `final-02-classic-input`: ordinary rounds, long unmarked and bracketed paste.
- `final-02-six-lines`: the user's exact six-line input, expected and actual UTF-8 SHA256 match.
- `final-02-reworked-route`: preserved Reworked branch; simulated environment flag, not physical PyCharm GUI verification.
- `final-02-approval`: unchanged HITL keyboard decisions, no actual sandbox/tool execution.
- `installed-help.txt`, `manifest.json`: installed package works and source/installed input.py hashes match; Docker Worker fingerprint unchanged.

Each terminal case stores raw ANSI, emulator snapshots, directly read native Console cells, input hashes and resize checks. Native layout uses `console_layout_pass` / `resize_layout_pass`, not the lossy outer `capture_layout_pass`. The latter is retained, including failures. Resize is via real PTY dimensions, not actual mouse dragging. These tests do not exercise an IME composition GUI or establish universal terminal compatibility.

## Preserved exploration / failures

- `baseline-classic-history`: original code content PASS but actual native layout FAIL.
- `diagnostic-native-inline`, `candidate-classic-input`: moving help inline improves short history only.
- `candidate-classic-history`, `diagnostic-cursor-history`, `diagnostic-reservation-trace`: actual versus logical X drift across wide characters; old rail remains on later rounds.
- `diagnostic-preserve-mode`: preserving additional console mode flags did not fix the issue.
- `diagnostic-reserve`, `candidate-prepare-reserve`: pre-reserving lines helps single-line vertical resizing, but `candidate-growing-editor` and `candidate-width-history` still FAIL. This workaround is absent from final code.
- `candidate-reserved-history`, `candidate-pre-reserve`: failed intermediate reservation; the latter accidentally reran the prior installed wheel after a restricted build failed. It is not evidence for a new implementation.
- `diagnostic-align`, `diagnostic-absolute-no-reserve`: absolute column reconciliation fixes multiline native cursor drift without reserving rows.
- `diagnostic-reflow`, `diagnostic-reflow-no-reserve`: accounting for wrapped old frame rows fixes horizontal resize; final code applies both corrections.
- `final-classic-*`, `final-conpty-*`: pre-final checks before the last wide-cell endpoint refinement. Prefer final-02 for final installed acceptance.
- `logs/nexus-console-unit-final.log`: 54 passed / 13 setup errors from an inaccessible pre-existing system pytest parent; final tests use a fresh owned TEMP parent, no permission changes.

Intermediate directories are diagnostic snapshots of evolving local candidates, not separate released commits. All tests used synthetic text, no paid model call or real user conversation export. `cleanup.json` records cleanup of this run's verified temporary paths, without traversing reparse points or deleting unrelated pytest directories.
