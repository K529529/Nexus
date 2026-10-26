"""Trusted user consent for standing Windows ACL changes; never a model tool."""

from __future__ import annotations

import asyncio
import sys
import tomllib
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import tomlkit

from nexus.app.config import ConfigError, config_path
from nexus.app.memory import atomic_write
from nexus.app.session import SessionError, _lock
from nexus.core.security import SecurityPolicy
from nexus.tools.sandbox import BridgeError
from nexus.tools.windows_native import (
    approved_acl_roots,
    reject_linked_path,
    validate_config_authority,
    validate_roots,
)

ConfirmACL = Callable[[Path], Awaitable[bool]]


def save_workspace_consent(workspace: Path, policy: SecurityPolicy, path: Path) -> SecurityPolicy:
    """Merge only the approved canonical workspace into trusted user TOML."""
    roots = validate_roots(workspace, policy, require_consent=False)
    validate_config_authority(path, roots)
    lock_path = path.with_suffix(path.suffix + ".lock")
    reject_linked_path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+b") as lock:
            _lock(lock)  # Same nonblocking OS writer lock used by SessionLog.
            original = path.read_bytes() if path.exists() else b""
            data = tomllib.loads(original.decode("utf-8"))
            security = data.get("security", {})
            if not isinstance(security, dict):
                raise ConfigError("security must be a TOML table; consent not saved")
            saved = security.get("windows_acl_roots", [])
            if not isinstance(saved, list) or any(
                not isinstance(p, str) or not Path(p).is_absolute() for p in saved
            ):
                raise ConfigError("Invalid windows_acl_roots; consent not saved")
            canonical = str(roots[0])
            updated_roots = list(saved)
            if roots[0] not in approved_acl_roots(replace(policy, windows_acl_roots=tuple(saved))):
                updated_roots.append(canonical)
            expected = deepcopy(data)
            expected.setdefault("security", {})["windows_acl_roots"] = updated_roots
            document = tomlkit.parse(original.decode("utf-8"))
            if "security" not in document:
                document["security"] = tomlkit.table()
            table: Any = document["security"]
            table["windows_acl_roots"] = updated_roots
            updated = tomlkit.dumps(document)
            if tomllib.loads(updated) != expected:
                raise ConfigError("Consent update changed unrelated configuration; not saved")
            if validate_roots(workspace, policy, require_consent=False) != roots:
                raise ConfigError("Workspace changed during consent; not saved")
            validate_config_authority(path, roots)
            if (path.read_bytes() if path.exists() else b"") != original:
                raise ConfigError("Configuration changed during consent; not saved")
            atomic_write(path, updated)
    except (tomllib.TOMLDecodeError, UnicodeError, SessionError) as exc:
        raise ConfigError(f"Cannot save workspace ACL consent: {exc}") from None
    return replace(policy, windows_acl_roots=tuple(updated_roots))


async def ensure_workspace_consent(
    workspace: Path,
    policy: SecurityPolicy,
    *,
    confirm: ConfirmACL | None = None,
    path: Path | None = None,
) -> SecurityPolicy | None:
    """None means user denied/cancelled; no command/ACL operation has occurred."""
    if sys.platform != "win32" or policy.mode != "workspace-write":
        return policy
    try:
        roots = validate_roots(workspace, policy, require_consent=False)
        authority = path or config_path()
        validate_config_authority(authority, roots)
        consent = approved_acl_roots(policy)
        missing_extra = [str(p) for p in roots[1:] if p not in consent]
        if missing_extra:
            raise ConfigError(
                "Extra roots need explicit user TOML windows_acl_roots approval: "
                + ", ".join(missing_extra)
            )
        if roots[0] in consent:
            return policy
        if confirm is None:
            raise ConfigError(
                "Workspace ACL consent missing. Open interactive Nexus and choose "
                "/permissions → Workspace-write, or approve the exact path in user "
                f"security.windows_acl_roots: {roots[0]}. No command executed."
            )
        if not await confirm(roots[0]):
            return None
        return await asyncio.to_thread(save_workspace_consent, workspace, policy, authority)
    except BridgeError as exc:
        raise ConfigError(str(exc)) from None
