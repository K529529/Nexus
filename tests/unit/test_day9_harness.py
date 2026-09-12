import os
from pathlib import Path

import pytest

from nexus.domain.tooling import RiskLevel
from nexus.evaluation.harness import (
    EvalHarnessCommandPolicy,
    compare_repository_states,
    snapshot_repository,
)
from nexus.evaluation.models import EvalHarnessOperation, FileFingerprint, RepositoryState
from nexus.security.executables import TrustedExecutables


@pytest.fixture
def policy() -> EvalHarnessCommandPolicy:
    return EvalHarnessCommandPolicy(
        TrustedExecutables(
            python=os.path.abspath("python"),
            uv=None,
            git=os.path.abspath("git"),
            pytest=os.path.abspath("pytest"),
            ruff=os.path.abspath("ruff"),
            mypy=os.path.abspath("mypy"),
        )
    )


def _risk(
    policy: EvalHarnessCommandPolicy,
    operation: EvalHarnessOperation,
    argv: tuple[str, ...],
) -> RiskLevel:
    return policy.classify(operation=operation.value, arguments={"argv": argv})


@pytest.mark.parametrize(
    ("operation", "argv"),
    [
        (EvalHarnessOperation.GIT_INIT, ("git", "init")),
        (EvalHarnessOperation.GIT_ADD_BASELINE, ("git", "add", "--all")),
        (
            EvalHarnessOperation.GIT_COMMIT_BASELINE,
            (
                "git",
                "-c",
                "user.name=Nexus Eval",
                "-c",
                "user.email=nexus-eval@local.invalid",
                "commit",
                "-m",
                "nexus-eval-baseline",
            ),
        ),
        (
            EvalHarnessOperation.GIT_STATUS,
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        ),
        (EvalHarnessOperation.GIT_DIFF, ("git", "diff", "--no-ext-diff", "--binary")),
        (EvalHarnessOperation.GIT_LS_FILES, ("git", "ls-files")),
        (EvalHarnessOperation.VALIDATION, ("pytest", "-q", "tests/test_x.py")),
        (
            EvalHarnessOperation.TEST_DISCOVERY,
            ("pytest", "--collect-only", "-q", "tests/test_x.py"),
        ),
        (EvalHarnessOperation.VALIDATION, ("ruff", "check", "src")),
        (EvalHarnessOperation.VALIDATION, ("mypy", "src")),
    ],
)
def test_closed_harness_policy_allows_only_frozen_forms(
    policy: EvalHarnessCommandPolicy,
    operation: EvalHarnessOperation,
    argv: tuple[str, ...],
) -> None:
    assert _risk(policy, operation, argv) is RiskLevel.SAFE


def test_closed_harness_policy_accepts_exact_trusted_absolute_executable(
    policy: EvalHarnessCommandPolicy,
) -> None:
    argv = (os.path.abspath("pytest"), "-q", "tests/test_x.py")
    assert _risk(policy, EvalHarnessOperation.VALIDATION, argv) is RiskLevel.SAFE


@pytest.mark.parametrize(
    "argv",
    [
        ("ruff", "check", "--fix", "src"),
        ("ruff", "check", "--output-format", "json", "src"),
        ("pytest", "-k", "chosen", "tests/test_x.py"),
        ("pytest", "-m", "unit", "tests/test_x.py"),
        ("pytest", "--deselect", "tests/test_x.py::test_a", "tests/test_x.py"),
        ("pytest", "--rootdir", "..", "tests/test_x.py"),
        ("pytest", "--config-file", "pytest.ini", "tests/test_x.py"),
        ("pytest", "--basetemp", "tmp", "tests/test_x.py"),
        ("pytest", "--cache-clear", "tests/test_x.py"),
        ("pytest", "C:/outside/test_x.py"),
        ("pytest", "../outside/test_x.py"),
        ("uv", "run", "pytest", "tests/test_x.py"),
        ("python", "-c", "print(1)"),
        ("python", "script.py"),
        ("curl", "https://example.invalid"),
        ("powershell", "-Command", "pytest"),
        ("pytest", "tests/test_x.py", "&&", "whoami"),
        ("git", "push"),
        ("git", "reset", "--hard"),
        (os.path.abspath("untrusted-pytest"), "tests/test_x.py"),
    ],
)
def test_closed_harness_policy_denies_expansion(
    policy: EvalHarnessCommandPolicy, argv: tuple[str, ...]
) -> None:
    assert _risk(policy, EvalHarnessOperation.VALIDATION, argv) is RiskLevel.DANGEROUS


def test_snapshot_detects_untracked_ignored_mode_and_validation_side_effects(
    tmp_path: Path,
) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_text("before\n", encoding="utf-8")
    forbidden = tmp_path / ".pytest_cache" / "forbidden.txt"
    forbidden.parent.mkdir()
    forbidden.write_text("safe\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    before = snapshot_repository(tmp_path, (".pytest_cache/forbidden.txt",))

    tracked.write_text("after\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("still evidence\n", encoding="utf-8")
    (tmp_path / ".pytest_cache" / "ordinary-cache").write_text("ignored\n", encoding="utf-8")
    forbidden.write_text("changed\n", encoding="utf-8")
    after = snapshot_repository(tmp_path, (".pytest_cache/forbidden.txt",))
    evidence = compare_repository_states(before, after, (), tmp_path)

    assert "tracked.py" in evidence.changed_files
    assert "ignored.txt" in evidence.created_files
    assert not any("ordinary-cache" in path for path in evidence.created_files)
    assert evidence.forbidden_paths[0].changed is True


def test_snapshot_detects_rename_and_excludes_git(tmp_path: Path) -> None:
    old = tmp_path / "old.txt"
    old.write_text("same", encoding="utf-8")
    before = snapshot_repository(tmp_path, ())
    old.rename(tmp_path / "new.txt")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "private").write_text("ignored", encoding="utf-8")
    after = snapshot_repository(tmp_path, ())
    evidence = compare_repository_states(before, after, (), tmp_path)

    assert evidence.deleted_files == ("old.txt",)
    assert evidence.created_files == ("new.txt",)
    assert not any(path.startswith(".git") for path in after.files)


def test_comparison_promotes_workspace_escaping_symlink_to_forbidden_evidence(
    tmp_path: Path,
) -> None:
    before = RepositoryState({}, {})
    after = RepositoryState(
        {"link": FileFingerprint("link", "symlink", None, False, "../../outside")},
        {},
    )

    evidence = compare_repository_states(before, after, (), tmp_path)

    assert evidence.created_files == ("link",)
    assert len(evidence.forbidden_paths) == 1
    assert evidence.forbidden_paths[0].path == "link"
    assert evidence.forbidden_paths[0].changed is True


def test_comparison_detects_mode_change_from_typed_snapshots(tmp_path: Path) -> None:
    before = RepositoryState(
        {"script": FileFingerprint("script", "file", "same", False, None)},
        {},
    )
    after = RepositoryState(
        {"script": FileFingerprint("script", "file", "same", True, None)},
        {},
    )

    evidence = compare_repository_states(before, after, (), tmp_path)

    assert evidence.changed_files == ("script",)


def test_required_text_capture_rejects_non_utf8(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"\xff\xfe")
    state = snapshot_repository(tmp_path, ())

    evidence = compare_repository_states(state, state, ("target.txt",), tmp_path)

    assert "target.txt" not in evidence.final_text_files


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks may require Windows privilege.")
def test_snapshot_detects_symlink_target_change(tmp_path: Path) -> None:
    (tmp_path / "one").write_text("one", encoding="utf-8")
    (tmp_path / "two").write_text("two", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to("one")
    before = snapshot_repository(tmp_path, ())
    link.unlink()
    link.symlink_to("two")
    after = snapshot_repository(tmp_path, ())

    assert "link" in compare_repository_states(before, after, (), tmp_path).changed_files
