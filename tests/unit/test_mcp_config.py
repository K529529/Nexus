from pathlib import Path

import pytest

from nexus.config import load_runtime_config
from nexus.config.models import MCPServerConfig, MCPTransport, RuntimeConfig
from nexus.domain.tooling import RiskLevel
from nexus.errors import ConfigurationError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _server(server_id: str, command: str = "npx") -> str:
    return (
        "[[mcp.servers]]\n"
        f'server_id = "{server_id}"\n'
        f'command = "{command}"\n'
    )


def test_mcp_defaults_are_disabled_and_empty() -> None:
    config = RuntimeConfig()

    assert config.mcp_enabled is False
    assert config.mcp_servers == ()


def test_valid_stdio_toml_and_risk_mapping_parse(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / ".nexus" / "config.toml",
        "[mcp]\nenabled = true\n"
        + _server("everything", "cmd")
        + 'args = ["/c", "npx", "-y", "pkg"]\n'
        + "inherit_environment = false\n"
        + "connect_timeout_seconds = 5.0\n"
        + "tool_timeout_seconds = 12.0\n"
        + "max_connect_attempts = 3\n"
        + "[[mcp.servers.tool_risks]]\n"
        + 'tool_name = "echo"\n'
        + 'risk_level = "SAFE"\n',
    )

    config = load_runtime_config(
        repo_root=repo,
        user_config_path=tmp_path / "missing.toml",
        environ={},
    )

    assert config.mcp_enabled is True
    assert len(config.mcp_servers) == 1
    server = config.mcp_servers[0]
    assert server.transport is MCPTransport.STDIO
    assert server.args == ("/c", "npx", "-y", "pkg")
    assert server.inherit_environment is False
    assert server.tool_risks[0].risk_level is RiskLevel.SAFE


def test_repository_servers_replace_complete_user_tuple(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    user = tmp_path / "user.toml"
    repo.mkdir()
    _write(user, "[mcp]\nenabled = true\n" + _server("user-one") + _server("user-two"))
    _write(repo / ".nexus" / "config.toml", _server("repository"))

    config = load_runtime_config(repo_root=repo, user_config_path=user, environ={})

    assert config.mcp_enabled is True
    assert tuple(server.server_id for server in config.mcp_servers) == ("repository",)


def test_repository_server_omission_falls_through_to_user(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    user = tmp_path / "user.toml"
    repo.mkdir()
    _write(user, "[mcp]\n" + _server("user"))
    _write(repo / ".nexus" / "config.toml", "[mcp]\nenabled = true\n")

    config = load_runtime_config(repo_root=repo, user_config_path=user, environ={})

    assert tuple(server.server_id for server in config.mcp_servers) == ("user",)


def test_repository_explicit_empty_servers_clears_user_tuple(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    user = tmp_path / "user.toml"
    repo.mkdir()
    _write(user, "[mcp]\n" + _server("user"))
    _write(repo / ".nexus" / "config.toml", "[mcp]\nservers = []\n")

    config = load_runtime_config(repo_root=repo, user_config_path=user, environ={})

    assert config.mcp_servers == ()


def test_nexus_mcp_servers_environment_variable_is_not_bound(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    config = load_runtime_config(
        repo_root=repo,
        user_config_path=tmp_path / "missing.toml",
        environ={"NEXUS_MCP_SERVERS": '[{"server_id":"forbidden"}]'},
    )

    assert config.mcp_servers == ()


@pytest.mark.parametrize(
    "values",
    [
        {"server_id": "", "command": "npx"},
        {"server_id": "Upper", "command": "npx"},
        {"server_id": "valid", "command": ""},
        {"server_id": "valid", "command": "npx", "args": [""]},
        {"server_id": "valid", "command": "npx", "connect_timeout_seconds": 0},
        {"server_id": "valid", "command": "npx", "tool_timeout_seconds": 301},
        {"server_id": "valid", "command": "npx", "max_connect_attempts": True},
        {"server_id": "valid", "command": "npx", "max_connect_attempts": 4},
        {"server_id": "valid", "command": "npx", "transport": "http"},
    ],
)
def test_invalid_mcp_server_values_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MCPServerConfig.model_validate(values)


def test_duplicate_server_and_tool_risk_names_are_rejected() -> None:
    server = {
        "server_id": "duplicate",
        "command": "npx",
        "tool_risks": [
            {"tool_name": "echo", "risk_level": "SAFE"},
            {"tool_name": "echo", "risk_level": "WRITE"},
        ],
    }
    with pytest.raises(ValueError, match="duplicate tool_name"):
        MCPServerConfig.model_validate(server)
    with pytest.raises(ValueError, match="server_id values must be unique"):
        RuntimeConfig.model_validate(
            {
                "mcp_servers": [
                    {"server_id": "same", "command": "one"},
                    {"server_id": "same", "command": "two"},
                ]
            }
        )


def test_invalid_mcp_toml_shape_maps_to_configuration_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".nexus" / "config.toml", '[mcp]\nservers = "invalid"\n')

    with pytest.raises(ConfigurationError, match="array of tables"):
        load_runtime_config(
            repo_root=repo,
            user_config_path=tmp_path / "missing.toml",
            environ={},
        )


def test_runtime_config_rendering_omits_mcp_commands_and_environment() -> None:
    config = RuntimeConfig(
        mcp_enabled=True,
        mcp_servers=(
            MCPServerConfig(
                server_id="secret-safe",
                command="credential-bearing-command",
                args=("secret-token",),
            ),
        ),
    )

    rendered = repr(config)
    assert "credential-bearing-command" not in rendered
    assert "secret-token" not in rendered
    assert "mcp_server_count=1" in rendered
