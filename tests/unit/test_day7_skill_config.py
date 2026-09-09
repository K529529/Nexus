from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus.config import load_runtime_config
from nexus.config.models import RuntimeConfig
from nexus.errors import ConfigurationError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_skill_selection_default_and_boundaries() -> None:
    assert RuntimeConfig().max_selected_skills == 2
    assert RuntimeConfig(max_selected_skills=0).max_selected_skills == 0
    with pytest.raises(ValidationError):
        RuntimeConfig(max_selected_skills=3)
    with pytest.raises(ValidationError):
        RuntimeConfig(max_selected_skills=True)


@pytest.mark.parametrize(
    ("repo", "user", "environment", "expected"),
    (
        ("max_selected_skills=1", "max_selected_skills=2", "0", 1),
        ("", "max_selected_skills=1", "0", 1),
        ("", "", "0", 0),
        ("", "", None, 2),
    ),
)
def test_skill_config_precedence(
    tmp_path: Path,
    repo: str,
    user: str,
    environment: str | None,
    expected: int,
) -> None:
    repository = tmp_path / "repo"
    user_config = tmp_path / "user" / "config.toml"
    repository.mkdir()
    if repo:
        _write(repository / ".nexus" / "config.toml", f"[skills]\n{repo}\n")
    if user:
        _write(user_config, f"[skills]\n{user}\n")
    environ = {} if environment is None else {"NEXUS_MAX_SELECTED_SKILLS": environment}
    config = load_runtime_config(
        repo_root=repository,
        user_config_path=user_config,
        environ=environ,
    )
    assert config.max_selected_skills == expected


def test_skill_config_rejects_boolean_and_ignores_unknown_keys(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write(
        repository / ".nexus" / "config.toml",
        "[skills]\nmax_selected_skills=true\n",
    )
    with pytest.raises(ConfigurationError):
        load_runtime_config(
            repo_root=repository,
            user_config_path=tmp_path / "missing",
            environ={},
        )
    _write(repository / ".nexus" / "config.toml", "[skills]\nremote='ignored'\n")
    assert load_runtime_config(
        repo_root=repository,
        user_config_path=tmp_path / "missing",
        environ={},
    ).max_selected_skills == 2
