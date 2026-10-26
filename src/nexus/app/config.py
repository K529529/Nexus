"""User TOML only. Secrets are resolved from named environment variables."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import tomlkit

from nexus.app.memory import atomic_write
from nexus.core.security import Mode, SecurityPolicy
from nexus.core.types import Json, Limits


class ConfigError(ValueError):
    pass


@dataclass
class ModelConfig:
    name: str
    context_window: int
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "NEXUS_MODEL_API_KEY"
    max_output_tokens: int = 8192
    reasoning_effort: str | None = None
    include_usage: bool = True
    request_timeout_seconds: int = 120
    output_token_parameter: str = "max_tokens"
    enable_thinking: bool | None = None

    def key(self) -> str:
        value = os.environ.get(self.api_key_env, "")
        if not value:
            raise ConfigError(f"Set environment variable {self.api_key_env} before starting nexus.")
        return value


@dataclass
class Config:
    model: ModelConfig
    limits: Limits
    mcp_servers: Json = field(default_factory=dict)
    response_language: str = "zh-CN"
    compaction_model: ModelConfig | None = None
    memory_enabled: bool = True
    memory_qmd_command: tuple[str, ...] = ("qmd",)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)


def config_path() -> Path:
    return Path.home() / ".nexus" / "config.toml"


def validate_acl_authority(policy: SecurityPolicy, path: Path | None = None) -> None:
    if sys.platform != "win32" or policy.mode == "local" or not policy.windows_acl_roots:
        return
    from nexus.tools.sandbox import BridgeError
    from nexus.tools.windows_native import validate_config_authority

    try:
        validate_config_authority(
            path or config_path(),
            (Path.cwd(), *(Path(p) for p in (*policy.writable_roots, *policy.windows_acl_roots))),
        )
    except BridgeError as exc:
        raise ConfigError(str(exc)) from None


def read_toml(path: Path) -> Json:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"Cannot read TOML at {path} ({type(exc).__name__}); file unchanged."
        ) from None


def memory_settings(path: Path | None = None) -> tuple[bool, tuple[str, ...]]:
    """Memory diagnostics and switches do not require model configuration or credentials."""
    memory = _table(read_toml(path or config_path()), "memory", {"enabled", "qmd_command"})
    enabled, command = memory.get("enabled", True), memory.get("qmd_command", ["qmd"])
    if type(enabled) is not bool:
        raise ConfigError("memory.enabled: expected boolean")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part.strip() for part in command)
    ):
        raise ConfigError("memory.qmd_command: expected nonempty command argument array")
    return enabled, tuple(command)


def set_memory_enabled(enabled: bool, path: Path | None = None) -> None:
    """Preserve TOML comments/structure and every setting except memory.enabled."""
    path = path or config_path()
    if path.is_symlink() or path.is_junction():
        raise ConfigError("Memory switch cannot replace a linked config file; file unchanged")
    original = path.read_bytes() if path.exists() else b""
    memory_settings(path)  # Validate before mutation; never repair malformed configuration.
    expected = deepcopy(tomllib.loads(original.decode("utf-8")))
    expected.setdefault("memory", {})["enabled"] = enabled
    document = tomlkit.parse(original.decode("utf-8"))
    if "memory" not in document:
        document["memory"] = tomlkit.table()
    table: Any = document["memory"]
    table["enabled"] = enabled
    updated = tomlkit.dumps(document)
    if tomllib.loads(updated) != expected:
        raise ConfigError("Memory configuration update changed unrelated values; file unchanged")
    if (path.read_bytes() if path.exists() else b"") != original:
        raise ConfigError("Configuration changed during update; file unchanged")
    atomic_write(path, updated)


def _table(data: Json, name: str, allowed: set[str]) -> Json:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: expected a TOML table")
    unknown = value.keys() - allowed
    if unknown:
        raise ConfigError(f"{name}: unknown fields: {', '.join(sorted(unknown))}")
    return value


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{name}: expected integer >= {minimum}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name}: required non-empty string")
    return value


def default_shell() -> str:
    if os.name == "nt":
        return shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"
    return "/bin/sh"


def parse_model(data: Json, section: str) -> ModelConfig:
    model = dict(
        _table(
            data,
            section,
            {
                "name",
                "base_url",
                "api_key_env",
                "context_window",
                "max_output_tokens",
                "reasoning_effort",
                "include_usage",
                "request_timeout_seconds",
                "output_token_parameter",
                "enable_thinking",
            },
        )
    )
    for field_name in ("name", "base_url"):
        if value := os.environ.get(f"NEXUS_{section.upper()}_{field_name.upper()}"):
            model[field_name] = value
    name = _text(model.get("name"), f"{section}.name")
    window = _integer(model.get("context_window", 128000), f"{section}.context_window")
    maximum = _integer(model.get("max_output_tokens", 8192), f"{section}.max_output_tokens")
    if section == "compaction_model" and window - maximum - 1024 <= 0:
        raise ConfigError(f"{section}.context_window must exceed max_output_tokens + 1024")
    base = _text(model.get("base_url", "https://api.openai.com/v1"), f"{section}.base_url")
    url = urlsplit(base)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ConfigError(f"{section}.base_url: expected HTTP(S) URL without credentials")
    if url.query or url.fragment or "<" in base or ">" in base:
        raise ConfigError(f"{section}.base_url: query, fragment and placeholders are not supported")
    key_env = _text(
        model.get("api_key_env", f"NEXUS_{section.upper()}_API_KEY"), f"{section}.api_key_env"
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ConfigError(f"{section}.api_key_env: invalid environment variable name")
    effort = model.get("reasoning_effort")
    if effort is not None:
        effort = _text(effort, f"{section}.reasoning_effort")
    include_usage = model.get("include_usage", True)
    if type(include_usage) is not bool:
        raise ConfigError(f"{section}.include_usage: expected boolean")
    request_timeout = _integer(
        model.get("request_timeout_seconds", 120), f"{section}.request_timeout_seconds"
    )
    if request_timeout > 600:
        raise ConfigError(f"{section}.request_timeout_seconds: expected integer <= 600")
    output_parameter = _text(
        model.get("output_token_parameter", "max_tokens"), f"{section}.output_token_parameter"
    )
    if output_parameter not in {"max_tokens", "max_completion_tokens"}:
        raise ConfigError(
            f"{section}.output_token_parameter: expected max_tokens or max_completion_tokens"
        )
    thinking = model.get("enable_thinking")
    if thinking is not None and type(thinking) is not bool:
        raise ConfigError(f"{section}.enable_thinking: expected boolean")
    return ModelConfig(
        name,
        window,
        base,
        key_env,
        maximum,
        effort,
        include_usage,
        request_timeout,
        output_parameter,
        thinking,
    )


def load_config(path: Path | None = None, *, require_key: bool = True) -> Config:
    path = path or config_path()
    data = read_toml(path)
    unknown = data.keys() - {
        "model",
        "compaction_model",
        "runtime",
        "execution",
        "mcp",
        "memory",
        "security",
    }
    if unknown:
        raise ConfigError(f"{path}: unknown sections: {', '.join(sorted(unknown))}")
    settings = parse_model(data, "model")
    compaction = parse_model(data, "compaction_model") if "compaction_model" in data else None
    runtime = _table(data, "runtime", {"max_steps", "response_language"})
    language = runtime.get("response_language", "zh-CN")
    if language not in ("zh-CN", "en", "auto"):
        raise ConfigError("runtime.response_language: expected zh-CN, en or auto")
    execution = _table(data, "execution", {"shell", "output_limit_bytes"})
    mcp = _table(data, "mcp", {"servers"})
    servers = mcp.get("servers", {})
    memory = _table(data, "memory", {"enabled", "qmd_command"})
    memory_enabled = memory.get("enabled", True)
    if type(memory_enabled) is not bool:
        raise ConfigError("memory.enabled: expected boolean")
    qmd_command = memory.get("qmd_command", ["qmd"])
    if (
        not isinstance(qmd_command, list)
        or not qmd_command
        or any(not isinstance(part, str) or not part.strip() for part in qmd_command)
    ):
        raise ConfigError("memory.qmd_command: expected nonempty command argument array")
    if not isinstance(servers, dict):
        raise ConfigError("mcp.servers: expected table")
    limits = Limits(
        max_steps=_integer(runtime.get("max_steps", 40), "runtime.max_steps"),
        context_window=settings.context_window,
        max_output_tokens=settings.max_output_tokens,
        output_limit_bytes=_integer(
            execution.get("output_limit_bytes", 32768),
            "execution.output_limit_bytes",
            256,
        ),
        shell=_text(execution.get("shell", default_shell()), "execution.shell"),
    )
    if require_key:
        settings.key()
        if compaction is not None:
            compaction.key()
    policy = security_policy(data, path)
    return Config(
        settings, limits, servers, language, compaction, memory_enabled, tuple(qmd_command), policy
    )


def security_policy(data: Json, path: Path | None = None) -> SecurityPolicy:
    security = _table(
        data,
        "security",
        {
            "mode",
            "writable_roots",
            "ask_tools",
            "allow_host_memory",
            "allow_trusted_skills",
            "allowed_domains",
            "denied_domains",
            "node_path",
            "srt_package",
            "windows_koffi",
            "windows_acl_roots",
            "docker_image",
            "docker_network",
            "docker_cpus",
            "docker_memory_mb",
            "docker_pids",
        },
    )
    mode = security.get("mode", "local")
    if not isinstance(mode, str) or mode not in {"local", "read-only", "workspace-write"}:
        raise ConfigError("security.mode: expected local, read-only or workspace-write")
    for name in (
        "writable_roots",
        "ask_tools",
        "allowed_domains",
        "denied_domains",
        "windows_acl_roots",
    ):
        value = security.get(name, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
            raise ConfigError(f"security.{name}: expected string array")
    roots = tuple(security.get("writable_roots", []))
    if any(not Path(root).is_absolute() for root in roots):
        raise ConfigError("security.writable_roots: absolute paths required")
    for name in ("allow_host_memory", "allow_trusted_skills"):
        if name in security and type(security[name]) is not bool:
            raise ConfigError(f"security.{name}: expected boolean")
    if (
        sys.platform in {"win32", "linux"}
        and mode != "local"
        and ("allowed_domains" in security or "denied_domains" in security)
    ):
        raise ConfigError(
            "Network: unrestricted on Windows; bridge/none on Linux. "
            "Remove unsupported allowed_domains/denied_domains"
        )
    acl_roots = tuple(security.get("windows_acl_roots", []))
    docker_configured = any(name.startswith("docker_") for name in security)
    if sys.platform == "win32" and mode != "local" and docker_configured:
        raise ConfigError(
            "Docker settings apply only to Linux; Windows Native network is unrestricted"
        )
    if any(not Path(root).is_absolute() for root in acl_roots):
        raise ConfigError("security.windows_acl_roots: absolute paths required")
    for name in ("node_path", "srt_package", "windows_koffi"):
        if name in security and (
            not isinstance(security[name], str) or not Path(security[name]).is_absolute()
        ):
            raise ConfigError(f"security.{name}: absolute installation path required")
    policy = SecurityPolicy(
        cast(Mode, mode),
        roots,
        tuple(security.get("ask_tools", [])),
        security.get("allow_host_memory", False),
        security.get("allow_trusted_skills", True),
        tuple(security.get("allowed_domains", [])),
        tuple(security.get("denied_domains", [])),
        security.get("node_path"),
        security.get("srt_package"),
        security.get("windows_koffi"),
        acl_roots,
        docker_image=security.get("docker_image"),
        docker_network=security.get("docker_network", "bridge"),
        docker_cpus=security.get("docker_cpus", 2.0),
        docker_memory_mb=security.get("docker_memory_mb", 1024),
        docker_pids=security.get("docker_pids", 128),
        domain_policy_configured="allowed_domains" in security or "denied_domains" in security,
        docker_configured=docker_configured,
    )
    validate_acl_authority(policy, path)
    return policy
