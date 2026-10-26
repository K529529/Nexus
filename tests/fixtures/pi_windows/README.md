# Pinned upstream Win32 unit tests

MIT, Michael Ding (2026), copied from yandy/pi-packages commit
49271fada9814ede5687fcdcc8188872887f0636, pi-sandbox/tests/win32-*.test.ts.
Licenses and attribution: src/nexus/_vendor/pi_windows/NOTICE.md.

These API-mocked tests do not establish real OS isolation. Do not run the
upstream E2E unchanged: it grants shared host TEMP and uses home fixtures.
Nexus real acceptance: scripts/stage4_windows_native_acceptance.py.

Adaptation: win32-cli.test.ts omits only the import and cross-package assertions
for Pi confine.ts, which Nexus does not vendor/use. Parser/failure-constant
assertions remain. Nexus private TEMP regressions extend runner tests.

Run with koffi 3.3.3 and vitest 5.0.5 in a dedicated dependency directory. Copy
vendor JS + package.json into src/win32/ and these tests into tests/ there;
run vitest run --root that-directory. Leave NEXUS_NATIVE_* env overrides unset
for the baseline suite. Windows symlink-entry case skips by upstream design.
