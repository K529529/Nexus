"""Pi Windows ACL runner behind Nexus's existing execution/audit interface.

Partial file-write enforcement only. Standing ACL changes require explicit root
consent; reads/network are unrestricted. No SRT runtime or local fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from nexus.core.security import SecurityPolicy
from nexus.core.types import Emit, ExecutionContext, Json, ToolCancelled, ToolResult
from nexus.tools.sandbox import BridgeClient, BridgeError

DETAIL = "Windows partial enforcement; Network: unrestricted; child environment filtered."


def reject_linked_path(path: Path) -> None:
    """Do not authorize a root/config through a symlink or junction ancestor."""
    for part in (path.absolute(), *path.absolute().parents):
        if part.is_symlink() or part.is_junction():
            raise BridgeError(f"Linked path/ancestor cannot hold persistent ACL consent: {part}")


def validate_config_authority(path: Path, roots: tuple[Path, ...]) -> None:
    reject_linked_path(path)
    resolved = path.resolve()
    if any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise BridgeError("ACL consent must be in user configuration outside workspace/write roots")


def approved_acl_roots(policy: SecurityPolicy) -> set[Path]:
    # Approval names the saved path, not whatever a future junction redirects to.
    # Unrelated removed workspaces must not invalidate active workspace consent.
    return {Path(os.path.abspath(p)) for p in policy.windows_acl_roots}


def validate_roots(
    workspace: Path, policy: SecurityPolicy, *, require_consent: bool = True
) -> tuple[Path, ...]:
    for candidate in (workspace, *(Path(p) for p in policy.writable_roots)):
        reject_linked_path(candidate)
    roots = tuple(
        dict.fromkeys(
            [
                workspace.resolve(strict=True),
                *(Path(p).resolve(strict=True) for p in policy.writable_roots),
            ]
        )
    )
    protected = [
        Path(value).resolve()
        for name in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
        if (value := os.environ.get(name))
    ]
    home = Path.home().resolve()
    protected.extend(
        (home / name).resolve() for name in (".nexus", ".codex", ".ssh", ".aws", ".azure", ".kube")
    )
    host_temp = Path(tempfile.gettempdir()).resolve()
    for root in roots:
        if not root.is_dir() or root == Path(root.anchor):
            raise BridgeError(f"Not an eligible workspace directory: {root}")
        if (
            home.is_relative_to(root)
            or host_temp.is_relative_to(root)
            or any(root.is_relative_to(p) or p.is_relative_to(root) for p in protected)
        ):
            raise BridgeError(f"Protected system/user directory cannot be an ACL root: {root}")
    if require_consent and policy.mode == "workspace-write":
        consent = approved_acl_roots(policy)
        if missing := [str(root) for root in roots if root not in consent]:
            raise BridgeError(
                "Standing ACL consent required: list these exact roots in "
                f"security.windows_acl_roots or use interactive /permissions: {missing}"
            )
    return roots


class WindowsNativeBackend:
    def __init__(self, policy: SecurityPolicy, *, secret_env: tuple[str, ...] = ()) -> None:
        self.policy, self.secret_env = policy, secret_env
        self.client: BridgeClient | None = None
        self.status, self.detail = "not_initialized", DETAIL
        self.lock = asyncio.Lock()
        self.shell: str | None = None

    def installation(self, workspace: Path, shell: str) -> tuple[tuple[str, ...], Json]:
        if sys.platform != "win32":
            raise BridgeError("Windows Native requires Windows; no fallback")
        if self.policy.allowed_domains or self.policy.denied_domains:
            raise BridgeError("Network: unrestricted; legacy domain policies are unsupported")
        if self.policy.srt_package is not None:
            raise BridgeError("Legacy srt_package configured; migrate to windows_koffi")
        roots = validate_roots(workspace, self.policy)

        def trusted(value: str | Path) -> Path:
            path = Path(value).resolve(strict=True)
            if any(path.is_relative_to(root) for root in roots):
                raise BridgeError(
                    f"Trusted installation must be outside workspace/write roots: {path}"
                )
            return path

        node_name = self.policy.node_path or shutil.which("node")
        if not node_name:
            raise BridgeError("Windows Native requires Node outside authorized workspaces")
        node = trusted(node_name)
        if not self.policy.windows_koffi:
            raise BridgeError(
                "Set security.windows_koffi to the installed koffi 3.3.3 package directory"
            )
        koffi = trusted(self.policy.windows_koffi)
        package = json.loads((koffi / "package.json").read_text(encoding="utf-8"))
        if package.get("name") != "koffi" or package.get("version") != "3.3.3":
            raise BridgeError("Windows Native requires the reviewed koffi version 3.3.3")
        bridge = trusted(Path(__file__).with_name("windows_native_bridge.mjs"))
        python = trusted(sys.executable)
        self.shell = str(trusted(shutil.which(shell) or shell))
        if Path(self.shell).name.lower() not in {"powershell.exe", "pwsh.exe"}:
            raise BridgeError("Windows Native supports Windows PowerShell/pwsh only")
        lock_root = Path.home() / ".nexus" / "native-acl-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_root = trusted(lock_root)
        settings = json.dumps(
            {"koffi": str(koffi), "lock_root": str(lock_root), "secret_env": self.secret_env}
        )
        return (str(node), str(bridge), settings), {
            "mode": self.policy.mode,
            "workspace": str(roots[0]),
            "python": str(python),
            "writable_roots": [str(root) for root in roots[1:]],
            "read_roots": [],  # Native token reads use existing host ACLs, never read grants.
        }

    async def execute(
        self, name: str, arguments: Json, context: ExecutionContext, emit: Emit
    ) -> ToolResult:
        async with self.lock:
            if self.status in {"failed", "closed"}:
                return ToolResult(
                    context.call_id,
                    False,
                    {"execution_state": "not_executed", "detail": self.detail},
                    "backend_unavailable",
                )
            dispatched = False
            try:
                if self.client is None:
                    argv, params = self.installation(context.workspace, context.shell)
                    self.client = BridgeClient(argv, cwd=Path(argv[1]).parent)
                    await self.client.initialize(params)
                    self.status = "ready"
                dispatched = True
                result = await self.client.execute(
                    name, arguments, replace(context, shell=self.shell or context.shell), emit
                )
                if result.data.get("exit_code") == 127 and (
                    "windows-acl-run: Win32 SetNamedSecurityInfoW failed"
                    in result.data.get("stderr", "")
                ):
                    result.data["detail"] = (
                        "Native ACL authorization failed. The directory must be owned by the "
                        "current user and permit WRITE_DAC / WRITE_OWNER. Consent does not "
                        "supply missing OS rights. No elevation, owner change, or Local fallback."
                    )
                if result.error_code == "cleanup_incomplete" or self.client.broken:
                    await self.close()
                    self.status, self.detail = "failed", "Native process cleanup is unconfirmed"
                return result
            except ToolCancelled:
                if self.client and self.client.broken:
                    self.status = "failed"
                raise
            except (OSError, ValueError, BridgeError) as exc:
                self.status, self.detail = "failed", str(exc)
                if self.client:
                    await self.client.close()
                return ToolResult(
                    context.call_id,
                    False,
                    {
                        "execution_state": "interrupted_unknown" if dispatched else "not_executed",
                        "detail": self.detail,
                        "cleanup_incomplete": dispatched,
                    },
                    "cleanup_incomplete" if dispatched else "backend_unavailable",
                )

    async def execute_patch_once(
        self, arguments: Json, context: ExecutionContext, emit: Emit
    ) -> ToolResult:
        async with self.lock:
            if self.policy.mode != "read-only" or self.status in {"failed", "closed"}:
                return ToolResult(context.call_id, False, {}, "dynamic_scope_unsupported")
            one_shot = WindowsNativeBackend(
                replace(self.policy, mode="workspace-write", writable_roots=()),
                secret_env=self.secret_env,
            )
            try:
                return await one_shot.execute("apply_patch", arguments, context, emit)
            finally:
                try:
                    await one_shot.close()
                except OSError:
                    self.status, self.detail = "failed", "One-shot Native cleanup is unconfirmed"
                    raise

    async def close(self) -> None:
        if self.client:
            await self.client.close()
            if not self.client.cleanup_confirmed:
                self.status = "failed"
                raise OSError("Native bridge cleanup unconfirmed: " + self.client.stderr)
        self.status = "closed"


class UnavailableBackend:
    status = "unavailable"
    detail = "Linux Docker awaits Windows manual acceptance; macOS unsupported. No Local fallback."

    async def execute(
        self, name: str, arguments: Json, context: ExecutionContext, emit: Emit
    ) -> ToolResult:
        return ToolResult(
            context.call_id,
            False,
            {"execution_state": "not_executed", "detail": self.detail},
            "backend_unavailable",
        )


def platform_backend(
    policy: SecurityPolicy, *, secret_env: tuple[str, ...] = ()
) -> WindowsNativeBackend | UnavailableBackend:
    return (
        WindowsNativeBackend(policy, secret_env=secret_env)
        if sys.platform == "win32"
        else UnavailableBackend()
    )
