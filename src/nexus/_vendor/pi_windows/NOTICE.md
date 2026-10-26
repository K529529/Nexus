# Pi Windows ACL runner attribution

Vendored from yandy/pi-packages, pi-sandbox/src/win32, commit
49271fada9814ede5687fcdcc8188872887f0636 (pi-sandbox 1.8.0).
https://github.com/yandy/pi-packages/tree/49271fada9814ede5687fcdcc8188872887f0636/pi-sandbox/src/win32

Copyright (c) 2026 Michael Ding. MIT license in LICENSE.
The upstream code ports DeepSeek's deepseek-harness Windows primitives;
Copyright (c) 2026 DeepSeek. MIT license in LICENSE.deepseek.
Original upstream source attribution comments are preserved.
https://github.com/deepseek-ai/deepseek-harness/blob/master/LICENSE

Nexus changes: ffi.js accepts NEXUS_NATIVE_KOFFI, an absolute trusted dependency
installation selected by the host adapter. acl.js accepts a trusted shared
NEXUS_NATIVE_LOCK_ROOT so private TEMP does not break cross-session root locking.
runner.js accepts NEXUS_NATIVE_PRIVATE_TEMP=1: read-only receives only the owned
session TEMP capability, never a workspace/extra-root capability. This is an
intentional, user-approved semantic change from Pi's entirely unwritable RO.
The runner starts a Nexus guard thread that holds a SYNCHRONIZE-only handle to
its bridge; bridge death terminates the runner and closes the upstream Job.
package.json marks these sources as
ES modules. Token construction, SID derivation, ACL merge/lock/fast path and
suspended-create/Job Object/process termination primitives are unchanged.

The Nexus adapter supplies a newly owned private TEMP/TMP to each session instead
of granting the shared host TEMP. It requires explicit consent for standing ACL
changes on every workspace/write root. Never run this runner directly against
system directories, a user's home, or their shared TEMP.

Inherited limitations: partial write enforcement; reads and network unrestricted;
NTFS hard-link aliases; ambient Everyone/logon SID rights; Low integrity/foreign
AppContainer ACL compatibility; standing capability ACE, FILE_DELETE_CHILD deny
and Low mandatory label remain after session exit. No security certification.
