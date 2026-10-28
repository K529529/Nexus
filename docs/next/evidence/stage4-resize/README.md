# Unified Windows / WSL resize evidence

Implementation: `e98190657166179e821f37fe9ad2d054682438fe`.
Baseline: `a07a9c832ec6acd3d3adc2681d34d6e80dbb612f` (WSL baseline installed the previous Stage 4D VT input).
See [report](../../../reports/stage4-tui-cross-platform-hardening.md), `manifest.json`, and `verification.json`.

## Final checks

- `pytest-final-03.log`: 1019 passed / 10 opt-in Docker skipped.
- `linux-ui-final-02.log`: 78 passed; `linux-resize-final.log`: final Ctrl+C increment, 4 passed.
- `ruff-final-02.log`, `mypy-final-02.log`: static checks pass.
- `final-windows`, `final-wsl`: 10-round multiline history, rapid continuous size changes, 8 settled extreme-size checks each.
- `final-windows-empty`, `final-wsl-empty`: same resizing with an empty draft.
- `final-windows-conpty`, `final-wsl-conpty`: ConPTY control cases.
- `final-windows-clear`, `final-wsl-clear`: final build, resize concurrently with Ctrl+C, retype and submit exact original multiline text.
- `final-*-six`: user-provided six-line content matches by SHA256.
- `final-*-approval`: unchanged HITL decisions, no sandbox operation or model call.
- `final-reworked`: preserved environment-selected code path; not actual PyCharm GUI automation.

Each stress run requests 52 size changes after rounds 4 and 7 (about 40ms apart), 40–130 columns, 10–48 rows. Four settled size checkpoints per burst verify draft content, one frame, visible editor text, and retained latest transcript when there is space. A short viewport scrolls inside the editor, so the last draft line is checked instead of demanding all lines be visible simultaneously.

Windows and WSL native layout assertions use actual Console cells. For WSL, `stage4_console_snapshot.py` attaches a separate read-only observer to the test PTY's Console and reads `CONOUT$`; it does not inject GUI input or touch a user window. Raw ANSI / pyte screenshots remain separate and may differ. Reports do not claim mouse-drag or IME-GUI coverage.

## Preserved failures

- `baseline-windows`: prior native code still passes this PTY sequence; does not override user's Windows GUI failure.
- `baseline-wsl`: content corruption / residual layout reproduced under rapid resizing.
- `diagnostic-wsl-nocpr`: no-CPR plus local old-frame geometry protects content but still fails layout.
- `candidate-*`: initial whole-display recovery, preceding the small-height clamp and direct WSL Console inspection.
- `windows-multiline`, `wsl-empty`: old small-height / outer-transcript failures; compare `windows-small`, `wsl-small`, `wsl-empty-02` and final cases.
- `unit-01.log`: initial wrapper/test-injection failures; `unit-02.log`: 70 passed after using the existing Console instance.
- `pytest-full.log`, `pytest-final.log`: outdated minimal fake Application fixtures failed; now use real prompt_toolkit Applications.
- `linux-ui-final.log`: one approval unit-test timeout under concurrent stress; isolated complete run passed without changing any production approval timeout.

`replay-timing.json` measures 1000 synthetic display entries, rendering/capture only, excluding terminal and GUI cost. `cleanup.json` inventories archival and removal of this run's verified temporary paths. No secrets, real user conversations, paid model calls, Docker changes or ACL changes were used.
