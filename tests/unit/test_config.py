from pathlib import Path

import pytest

from nexus.config import load_runtime_config
from nexus.config.models import DEFAULT_DATABASE_URL
from nexus.errors import ConfigurationError


def _write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("cli_model", "repo_toml", "user_toml", "environment", "expected"),
    [
        (
            "cli-model",
            '[model]\nname = "repo-model"\n',
            '[model]\nname = "user-model"\n',
            {"NEXUS_MODEL_NAME": "env-model"},
            "cli-model",
        ),
        (
            None,
            '[model]\nname = "repo-model"\n',
            '[model]\nname = "user-model"\n',
            {"NEXUS_MODEL_NAME": "env-model"},
            "repo-model",
        ),
        (
            None,
            "",
            '[model]\nname = "user-model"\n',
            {"NEXUS_MODEL_NAME": "env-model"},
            "user-model",
        ),
        (None, "", "", {"NEXUS_MODEL_NAME": "env-model"}, "env-model"),
        (None, "", "", {}, None),
    ],
)
def test_model_name_precedence(
    tmp_path: Path,
    cli_model: str | None,
    repo_toml: str,
    user_toml: str,
    environment: dict[str, str],
    expected: str | None,
) -> None:
    repo_root = tmp_path / "repo"
    user_config = tmp_path / "user" / "config.toml"
    repo_root.mkdir()
    if repo_toml:
        _write_config(repo_root / ".nexus" / "config.toml", repo_toml)
    if user_toml:
        _write_config(user_config, user_toml)

    config = load_runtime_config(
        cli_model=cli_model,
        repo_root=repo_root,
        user_config_path=user_config,
        environ=environment,
    )

    assert config.model_name == expected


def test_precedence_is_resolved_per_field(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    user_config = tmp_path / "user" / "config.toml"
    repo_root.mkdir()
    _write_config(
        repo_root / ".nexus" / "config.toml",
        '[model]\nbase_url = "https://repo.invalid/v1"\n',
    )
    _write_config(
        user_config,
        '[database]\nurl = "postgresql+asyncpg://user:pass@user-db:5432/nexus"\n',
    )

    config = load_runtime_config(
        cli_model="cli-model",
        repo_root=repo_root,
        user_config_path=user_config,
        environ={
            "NEXUS_MODEL_NAME": "env-model",
            "NEXUS_MODEL_BASE_URL": "https://env.invalid/v1",
            "NEXUS_MODEL_API_KEY": "top-secret-key",
            "NEXUS_DATABASE_URL": "postgresql+asyncpg://env:pass@env-db:5432/nexus",
        },
    )

    assert config.model_name == "cli-model"
    assert config.model_base_url == "https://repo.invalid/v1"
    assert config.database_url == "postgresql+asyncpg://user:pass@user-db:5432/nexus"
    assert config.model_api_key is not None
    assert config.model_api_key.get_secret_value() == "top-secret-key"


def test_defaults_and_secret_values_are_safely_rendered(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config = load_runtime_config(
        repo_root=repo_root,
        user_config_path=tmp_path / "missing.toml",
        environ={"NEXUS_MODEL_API_KEY": "do-not-print"},
    )

    rendered = repr(config)
    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.model_name is None
    assert "do-not-print" not in rendered
    assert "nexus:nexus" not in rendered
    assert "***:***@localhost" in rendered


def test_api_key_in_toml_is_rejected(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_config(
        repo_root / ".nexus" / "config.toml",
        '[model]\napi_key = "must-not-be-loaded"\n',
    )

    with pytest.raises(ConfigurationError, match="not supported"):
        load_runtime_config(
            repo_root=repo_root,
            user_config_path=tmp_path / "missing.toml",
            environ={},
        )
