"""Resolve Day 1 configuration with the frozen per-field precedence."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nexus.config.models import RuntimeConfig
from nexus.errors import ConfigurationError

_ENV_FIELDS = {
    "NEXUS_MODEL_PROVIDER": "model_provider",
    "NEXUS_MODEL_NAME": "model_name",
    "NEXUS_MODEL_BASE_URL": "model_base_url",
    "NEXUS_MODEL_API_KEY": "model_api_key",
    "NEXUS_DATABASE_URL": "database_url",
    "NEXUS_APPROVAL_MODE": "approval_mode",
    "NEXUS_MAX_STEPS": "max_steps",
    "NEXUS_MAX_REPAIR_ATTEMPTS": "max_repair_attempts",
    "NEXUS_MAX_REPLANS": "max_replans",
}

_CONTEXT_INTEGERS = (
    "max_retrieved_chunks", "max_code_context_tokens", "max_recent_observations",
    "max_model_input_tokens", "max_file_size_bytes",
)
_ENV_FIELDS.update({f"NEXUS_{name.upper()}": name for name in _CONTEXT_INTEGERS})
_ENV_FIELDS.update({
    "NEXUS_SEMANTIC_ENABLED": "semantic_enabled",
    "NEXUS_EMBEDDING_MODEL": "embedding_model",
    "NEXUS_EMBEDDING_DIMENSION": "embedding_dimension",
    "NEXUS_EMBEDDING_BASE_URL": "embedding_base_url",
    "NEXUS_EMBEDDING_API_KEY": "embedding_api_key",
})


def load_runtime_config(
    *,
    cli_model: str | None = None,
    cli_base_url: str | None = None,
    repo_root: Path | None = None,
    user_config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Load configuration per field from lowest to highest priority."""

    environment = os.environ if environ is None else environ
    resolved: dict[str, Any] = {}
    resolved.update(_environment_values(environment))

    user_path = user_config_path or (Path.home() / ".nexus" / "config.toml")
    resolved.update(_toml_values(user_path))

    repository = repo_root or Path.cwd()
    resolved.update(_toml_values(repository / ".nexus" / "config.toml"))

    if cli_model is not None:
        resolved["model_name"] = cli_model
    if cli_base_url is not None:
        resolved["model_base_url"] = cli_base_url

    try:
        return RuntimeConfig.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigurationError("Runtime configuration is invalid.") from exc


def _environment_values(environ: Mapping[str, str]) -> dict[str, Any]:
    return {
        field: environ[variable]
        for variable, field in _ENV_FIELDS.items()
        if variable in environ
    }


def _toml_values(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Could not read Nexus configuration at {path}.") from exc

    model = _table(document, "model", path)
    database = _table(document, "database", path)
    runtime = _table(document, "runtime", path)
    context = _table(document, "context", path)
    embedding = _table(document, "embedding", path)
    if "api_key" in embedding:
        raise ConfigurationError("Embedding API keys are not supported in Nexus TOML.")
    if "api_key" in model:
        raise ConfigurationError(
            f"API keys are not supported in Nexus TOML configuration at {path}."
        )

    values: dict[str, Any] = {}
    for name in _CONTEXT_INTEGERS:
        _copy_optional_integer(context, name, name, values, path)
    if "semantic_enabled" in context:
        if not isinstance(context["semantic_enabled"], bool):
            raise ConfigurationError("semantic_enabled must be a TOML boolean.")
        values["semantic_enabled"] = context["semantic_enabled"]
    for name in ("provider", "model", "base_url"):
        _copy_optional_string(embedding, name, f"embedding_{name}", values, path)
    _copy_optional_integer(embedding, "dimension", "embedding_dimension", values, path)
    _copy_optional_string(model, "provider", "model_provider", values, path)
    _copy_optional_string(model, "name", "model_name", values, path)
    _copy_optional_string(model, "base_url", "model_base_url", values, path)
    _copy_optional_string(database, "url", "database_url", values, path)
    _copy_optional_string(runtime, "approval_mode", "approval_mode", values, path)
    _copy_optional_integer(runtime, "max_steps", "max_steps", values, path)
    _copy_optional_integer(
        runtime,
        "max_repair_attempts",
        "max_repair_attempts",
        values,
        path,
    )
    _copy_optional_integer(runtime, "max_replans", "max_replans", values, path)
    return values


def _table(document: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"[{name}] must be a TOML table in {path}.")
    return value


def _copy_optional_string(
    source: Mapping[str, Any],
    source_key: str,
    target_key: str,
    target: dict[str, Any],
    path: Path,
) -> None:
    if source_key not in source:
        return
    value = source[source_key]
    if not isinstance(value, str):
        raise ConfigurationError(f"{source_key} must be a string in {path}.")
    target[target_key] = value


def _copy_optional_integer(
    source: Mapping[str, Any],
    source_key: str,
    target_key: str,
    target: dict[str, Any],
    path: Path,
) -> None:
    if source_key not in source:
        return
    value = source[source_key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{source_key} must be an integer in {path}.")
    target[target_key] = value
