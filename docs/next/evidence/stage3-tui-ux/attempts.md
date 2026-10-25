# Validation attempts

All runs used the Stage 3 source and offline tests, with unique system-temp parents.

- Existing CLI/TUI/Memory-exit baseline subset after first implementation: 66 passed.
- First new suite: 10 failed, 4 passed. PromptSession.prompt_async does not accept erase_when_done; moved this setting to its Application. One rendering fixture also inherited TERM=dumb.
- Second new suite: 9 failed, 5 passed. TERM=dumb selected prompt_toolkit's separate prompt application. Fixed production prompt construction to explicitly pass output and keep safe key bindings; tests now supply DummyOutput explicitly.
- Third new suite: 14 passed.
- Expanded focused suite: 79 passed, 2 failed due to old scripted prompts reaching a real Win32 output in a captured test process. Updated their input/output doubles.
- First full suite: 781 passed, 10 failed, 10 skipped. All failures were existing scripted/real-prompt CLI fixtures using the old constructor/submission contract. No core behavior assertions were removed; adapted the doubles and submission sequence.
- Next focused: 158 passed, 1 failed (remaining Profile prompt double with a different parameter name).
- Final focused group: 159 passed. Added real pipe EOF afterwards; new UX suite at that point: 16 passed.
- Full suite after those fixes: 792 passed, 10 skipped (58.19s).
- Final boundary review retained queued Ctrl+C immediately after submission while still discarding queued text; added a regression. Latest UX suite: 17 passed.
- Final full suite: 793 passed, 10 skipped (57.83s). Docker opt-in tests are the skips.
- Final Ruff and Mypy passed; 97 source files checked by Mypy.

Raw intermediate failure logs remain locally in this evidence directory (first-focused.txt, second-focused.txt, focused.txt, focused-final.txt, pytest-full.txt), outside the delivery commit. Final check outputs and this record are included in the delivery.

Ten verified system-temp roots were removed after preserving nonempty failure fixtures in the local untracked failed-test-fixtures directory. Two later roots were retained after the safety check and a subsequent restricted read reported access denied; no permissions were changed:
- nexus-tui-7ec14bfeb7364ef19417b7b7327e772c
- nexus-tui-941164004fb64dbb8b71eeec666b769c

Pre-existing user files were not staged.
