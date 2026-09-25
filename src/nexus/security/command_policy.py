"""Centralized SAFE/WRITE/DANGEROUS classification."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeGuard

from nexus.domain.tooling import JsonObject, RiskLevel
from nexus.security.executables import TrustedExecutables
from nexus.security.git_commands import is_canonical_git_argv

_READ_TOOLS = {"list_files", "search_files", "read_file", "lexical_search"}
_GIT_TOOLS = {"git_status", "git_diff", "git_log"}
_EDIT_TOOLS = {"edit_file", "apply_patch", "write_file"}
_OPERATORS = {"&&", "||", "|", ">", ">>", "<", ";"}


class DefaultCommandPolicy:
    def __init__(
        self,
        executables: TrustedExecutables,
        mcp_risks: Mapping[str, RiskLevel] | None = None,
    ) -> None:
        self._executables = executables
        self._mcp_risks = MappingProxyType(dict(mcp_risks or {}))

    def classify(self, *, operation: str, arguments: JsonObject) -> RiskLevel:
        if operation in self._mcp_risks:
            return self._mcp_risks[operation]
        if operation in _READ_TOOLS:
            return RiskLevel.SAFE
        if operation in _GIT_TOOLS:
            return self._classify_git(operation, arguments)
        if operation in _EDIT_TOOLS:
            return RiskLevel.WRITE
        if operation != "shell":
            return RiskLevel.DANGEROUS
        argv = arguments.get("argv")
        if not _is_string_list(argv):
            return RiskLevel.DANGEROUS
        normalized = self._executables.normalize_argv(argv)
        if any(item in _OPERATORS for item in normalized):
            return RiskLevel.DANGEROUS
        if self._is_safe_inspection(normalized):
            return RiskLevel.SAFE
        if self._is_controlled_write(normalized):
            return RiskLevel.WRITE
        return RiskLevel.DANGEROUS

    def _classify_git(self, operation: str, arguments: JsonObject) -> RiskLevel:
        argv = arguments.get("argv")
        if _is_string_list(argv):
            normalized = self._executables.normalize_argv(argv)
            return (
                RiskLevel.SAFE
                if is_canonical_git_argv(operation, normalized, self._executables.git)
                else RiskLevel.DANGEROUS
            )
        if operation == "git_status":
            return RiskLevel.SAFE if arguments == {} else RiskLevel.DANGEROUS
        if operation == "git_diff":
            return (
                RiskLevel.SAFE
                if set(arguments) <= {"staged"}
                and isinstance(arguments.get("staged", False), bool)
                else RiskLevel.DANGEROUS
            )
        value = arguments.get("max_entries", 20)
        return (
            RiskLevel.SAFE
            if set(arguments) <= {"max_entries"}
            and isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= 100
            else RiskLevel.DANGEROUS
        )

    def _is_safe_inspection(self, argv: list[str]) -> bool:
        return argv in [
            [self._executables.python, "--version"],
            [self._executables.python, "-V"],
            *(
                [[self._executables.uv, "--version"]]
                if self._executables.uv is not None
                else []
            ),
            *(
                [[self._executables.git, "--version"]]
                if self._executables.git is not None
                else []
            ),
        ]

    def _is_controlled_write(self, argv: list[str]) -> bool:
        direct = {
            self._executables.pytest,
            self._executables.ruff,
            self._executables.mypy,
        }
        if argv[0] in direct - {None}:
            return argv[0] != self._executables.ruff or argv[1:2] == ["check"]
        uv = self._executables.uv
        if uv is None or argv[0] != uv:
            return False
        if argv[1:2] == ["build"]:
            return True
        return len(argv) >= 3 and argv[1] == "run" and argv[2] in {
            "pytest",
            "mypy",
        } or len(argv) >= 4 and argv[1:3] == ["run", "ruff"] and argv[3] == "check"


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item) for item in value
    )
