"""Typed Day 1 runtime configuration."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from nexus.domain.tooling import RiskLevel
from nexus.errors import ConfigurationError

DEFAULT_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
SUPPORTED_MODEL_PROVIDER = "openai_compatible"


class ApprovalMode(StrEnum):
    APPROVAL = "approval"
    AUTO = "auto"


class MCPTransport(StrEnum):
    STDIO = "stdio"


class MCPToolRiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str
    risk_level: RiskLevel

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("MCP tool_name must not be empty.")
        return value


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    server_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    enabled: bool = True
    transport: MCPTransport = MCPTransport.STDIO
    command: str
    args: tuple[str, ...] = ()
    inherit_environment: bool = True
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    tool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_connect_attempts: int = Field(default=2, ge=1, le=3)
    tool_risks: tuple[MCPToolRiskConfig, ...] = ()

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("MCP command must not be empty.")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("MCP args must contain only non-empty strings.")
        return value

    @field_validator("max_connect_attempts", mode="before")
    @classmethod
    def reject_boolean_attempts(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("MCP max_connect_attempts must be an integer.")
        return value

    @model_validator(mode="after")
    def validate_unique_risks(self) -> MCPServerConfig:
        names = [item.tool_name for item in self.tool_risks]
        if len(names) != len(set(names)):
            raise ValueError("MCP tool_risks contains duplicate tool_name entries.")
        return self


class RuntimeConfig(BaseModel):
    """The exact approved Day 1 configuration surface."""

    model_config = ConfigDict(frozen=True)

    model_provider: str = SUPPORTED_MODEL_PROVIDER
    model_name: str | None = None
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    database_url: str = DEFAULT_DATABASE_URL
    approval_mode: ApprovalMode = ApprovalMode.APPROVAL
    max_steps: int = 30
    max_repair_attempts: int = 3
    max_replans: int = 2
    semantic_enabled: bool = False
    lexical_top_k: int = Field(default=20, ge=20, le=20)
    semantic_top_k: int = Field(default=20, ge=20, le=20)
    rrf_k: int = Field(default=60, ge=60, le=60)
    final_candidate_count: int = Field(default=12, ge=12, le=12)
    max_retrieved_chunks: int = Field(default=12, ge=0, le=12)
    max_exploration_seed_chunks: int = Field(default=6, ge=0, le=6)
    max_code_context_tokens: int = Field(default=12000, ge=0, le=12000)
    max_recent_observations: int = Field(default=8, ge=0, le=8)
    max_model_input_tokens: int = Field(default=24000, ge=1, le=24000)
    max_file_size_bytes: int = Field(default=1048576, ge=1)
    embedding_provider: str = "openai_compatible"
    embedding_model: str = ""
    embedding_dimension: int = Field(default=0, ge=0)
    embedding_base_url: str = ""
    embedding_api_key: SecretStr | None = None
    mcp_enabled: bool = False
    mcp_servers: tuple[MCPServerConfig, ...] = ()

    def require_embedding(self) -> None:
        """Validate only when a semantic/index capability is actually requested."""
        if (
            self.embedding_provider != "openai_compatible"
            or not self.embedding_model.strip()
            or self.embedding_dimension <= 0
            or not self.embedding_base_url.startswith(("https://", "http://"))
            or self.embedding_api_key is None
            or not self.embedding_api_key.get_secret_value().strip()
        ):
            raise ConfigurationError(
                "Configure embedding provider/model/dimension/base_url and "
                "NEXUS_EMBEDDING_API_KEY, or disable semantic retrieval."
            )

    @model_validator(mode="after")
    def validate_context_budget(self) -> RuntimeConfig:
        if self.max_code_context_tokens > self.max_model_input_tokens:
            raise ValueError("Code context budget must not exceed total model input budget.")
        return self

    @field_validator("model_provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value != SUPPORTED_MODEL_PROVIDER:
            raise ValueError(f"unsupported Day 1 model provider: {value!r}")
        return value

    @field_validator("model_name", "model_base_url", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("database URL must not be empty")
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("Day 1 database URL must use postgresql+asyncpg")
        return value

    @field_validator(
        "max_steps",
        "max_repair_attempts",
        "max_replans",
        "lexical_top_k",
        "semantic_top_k",
        "rrf_k",
        "final_candidate_count",
        "max_retrieved_chunks",
        "max_exploration_seed_chunks",
        "max_code_context_tokens",
        "max_recent_observations",
        "max_model_input_tokens",
        "max_file_size_bytes",
        "embedding_dimension",
        mode="before",
    )
    @classmethod
    def reject_boolean_limits(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Runtime limits must be integers, not booleans.")
        return value

    @model_validator(mode="after")
    def validate_day4_limits(self) -> RuntimeConfig:
        if self.max_steps <= 0:
            raise ValueError("max_steps must be greater than zero.")
        if self.max_repair_attempts < 0 or self.max_replans < 0:
            raise ValueError("Repair and Replan limits must not be negative.")
        return self

    @model_validator(mode="after")
    def validate_unique_mcp_servers(self) -> RuntimeConfig:
        server_ids = [server.server_id for server in self.mcp_servers]
        if len(server_ids) != len(set(server_ids)):
            raise ValueError("MCP server_id values must be unique.")
        return self

    def __repr__(self) -> str:
        api_key = "**********" if self.model_api_key is not None else "None"
        return (
            "RuntimeConfig("
            f"model_provider={self.model_provider!r}, "
            f"model_name={self.model_name!r}, "
            f"model_base_url={self.model_base_url!r}, "
            f"model_api_key={api_key}, "
            f"database_url={redact_database_url(self.database_url)!r}, "
            f"approval_mode={self.approval_mode.value!r}, "
            f"max_steps={self.max_steps!r}, "
            f"max_repair_attempts={self.max_repair_attempts!r}, "
            f"max_replans={self.max_replans!r}, "
            f"mcp_enabled={self.mcp_enabled!r}, "
            f"mcp_server_count={len(self.mcp_servers)!r}"
            ")"
        )

    __str__ = __repr__


def redact_database_url(value: str) -> str:
    """Hide embedded database credentials while preserving useful location data."""

    try:
        parts = urlsplit(value)
    except ValueError:
        return "<redacted-database-url>"
    if parts.username is None and parts.password is None:
        return value

    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port is not None else ""
    redacted_netloc = f"***:***@{hostname}{port}"
    return urlunsplit((parts.scheme, redacted_netloc, parts.path, parts.query, parts.fragment))
