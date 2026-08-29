"""Canonical read-only Git argv construction and matching."""

from __future__ import annotations


def status_argv(git: str) -> list[str]:
    return [
        git,
        "-c",
        "core.fsmonitor=false",
        "--no-pager",
        "status",
        "--short",
        "--branch",
    ]


def diff_argv(git: str, *, staged: bool) -> list[str]:
    argv = [
        git,
        "-c",
        "core.fsmonitor=false",
        "--no-pager",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
    ]
    if staged:
        argv.append("--cached")
    return argv


def log_argv(git: str, *, max_entries: int) -> list[str]:
    return [
        git,
        "-c",
        "core.fsmonitor=false",
        "--no-pager",
        "log",
        f"--max-count={max_entries}",
        "--oneline",
        "--no-decorate",
    ]


def is_canonical_git_argv(operation: str, argv: list[str], git: str | None) -> bool:
    if git is None:
        return False
    if operation == "git_status":
        return argv == status_argv(git)
    if operation == "git_diff":
        return argv in (diff_argv(git, staged=False), diff_argv(git, staged=True))
    if operation == "git_log" and len(argv) == 8:
        prefix = [git, "-c", "core.fsmonitor=false", "--no-pager", "log"]
        if argv[:5] != prefix or argv[6:] != ["--oneline", "--no-decorate"]:
            return False
        value = argv[5]
        if not value.startswith("--max-count="):
            return False
        try:
            count = int(value.removeprefix("--max-count="))
        except ValueError:
            return False
        return 1 <= count <= 100
    return False
