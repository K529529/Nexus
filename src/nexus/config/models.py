"""Typed Day 1 runtime configuration."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

DEFAULT_DATABASE_URL = "postgresql+asyncpg://nexus:nexus@localhost:5432/nexus"
SUPPORTED_MODEL_PROVIDER = "openai_compatible"


class ApprovalMode(StrEnum):
    APPROVAL = "approval"
    AUTO = "auto"


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
        mode="before",
    )
    @classmethod
    def reject_boolean_limits(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Day 4 limits must be integers, not booleans.")
        return value

    @model_validator(mode="after")
    def validate_day4_limits(self) -> RuntimeConfig:
        if self.max_steps <= 0:
            raise ValueError("max_steps must be greater than zero.")
        if self.max_repair_attempts < 0 or self.max_replans < 0:
            raise ValueError("Repair and Replan limits must not be negative.")
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
            f"max_replans={self.max_replans!r}"
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
