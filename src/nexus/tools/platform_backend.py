"""Lazy platform selection; Local never constructs a restricted backend."""

from __future__ import annotations

import sys

from nexus.core.security import RestrictedBackend, SecurityPolicy


def platform_backend(
    policy: SecurityPolicy, *, secret_env: tuple[str, ...] = ()
) -> RestrictedBackend:
    if policy.mode == "local":
        raise ValueError("Local uses the original executor, not a sandbox backend")
    if sys.platform == "linux":
        from nexus.tools.linux_docker import LinuxDockerBackend

        return LinuxDockerBackend(policy)
    from nexus.tools.windows_native import UnavailableBackend, WindowsNativeBackend

    return (
        WindowsNativeBackend(policy, secret_env=secret_env)
        if sys.platform == "win32"
        else UnavailableBackend()
    )
