from pathlib import Path

import pytest

from nexus.config.loader import load_runtime_config
from nexus.errors import ConfigurationError


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_observability_defaults_are_disabled(tmp_path: Path) -> None:
    config = load_runtime_config(
        repo_root=tmp_path,
        user_config_path=tmp_path / "missing.toml",
        environ={},
    )
    assert config.console_tracing_enabled is False
    assert config.langsmith_tracing_enabled is False
    assert config.langsmith_project == "nexus"
    assert config.langsmith_api_key is None


def test_console_may_be_enabled_by_repository_but_langsmith_may_not(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / ".nexus" / "config.toml",
        "[observability.console]\nenabled=true\n",
    )
    config = load_runtime_config(
        repo_root=tmp_path,
        user_config_path=tmp_path / "missing.toml",
        environ={},
    )
    assert config.console_tracing_enabled is True

    _write(
        tmp_path / ".nexus" / "config.toml",
        "[observability.langsmith]\nenabled=false\n",
    )
    with pytest.raises(ConfigurationError) as caught:
        load_runtime_config(
            repo_root=tmp_path,
            user_config_path=tmp_path / "missing.toml",
            environ={},
        )
    assert caught.value.code == "TRACE_CONFIGURATION_INVALID"


def test_langsmith_user_settings_and_environment_only_secret(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    _write(
        user,
        "[observability.langsmith]\n"
        "enabled=true\n"
        'project="safe-project"\n'
        'endpoint="https://api.smith.langchain.com"\n'
        'workspace_id="workspace"\n',
    )
    config = load_runtime_config(
        repo_root=tmp_path / "repository",
        user_config_path=user,
        environ={"NEXUS_LANGSMITH_API_KEY": "never-print-this"},
    )
    assert config.langsmith_tracing_enabled is True
    assert config.langsmith_project == "safe-project"
    assert config.langsmith_api_key is not None
    assert "never-print-this" not in repr(config)


def test_enabled_langsmith_without_key_is_safe_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as caught:
        load_runtime_config(
            repo_root=tmp_path,
            user_config_path=tmp_path / "missing.toml",
            environ={"NEXUS_LANGSMITH_TRACING_ENABLED": "true"},
        )
    assert caught.value.code == "TRACE_CONFIGURATION_INVALID"


def test_langsmith_api_key_is_rejected_from_user_toml(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    _write(user, '[observability.langsmith]\napi_key="forbidden"\n')
    with pytest.raises(ConfigurationError) as caught:
        load_runtime_config(
            repo_root=tmp_path / "repository",
            user_config_path=user,
            environ={},
        )
    assert caught.value.code == "TRACE_CONFIGURATION_INVALID"


def test_equivalent_repository_langsmith_key_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path / ".nexus" / "config.toml", '[langsmith]\nproject="forbidden"\n')
    with pytest.raises(ConfigurationError) as caught:
        load_runtime_config(
            repo_root=tmp_path,
            user_config_path=tmp_path / "missing.toml",
            environ={},
        )
    assert caught.value.code == "TRACE_CONFIGURATION_INVALID"


def test_langsmith_endpoint_path_is_not_exposed_in_config_repr(tmp_path: Path) -> None:
    config = load_runtime_config(
        repo_root=tmp_path,
        user_config_path=tmp_path / "missing.toml",
        environ={"NEXUS_LANGSMITH_ENDPOINT": "https://trace.example/private/path"},
    )
    assert "private/path" not in repr(config)
    assert "https://trace.example" in repr(config)
