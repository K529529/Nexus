from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap
from nexus.app.config import Config, ModelConfig
from nexus.core.types import Limits
from scripts import swebench_v0
from scripts.swebench_v0 import append_json, collect, collect_patch, git, read_jsonl, verify_base
from tests.conftest import ScriptedModel, call, reply


def test_original_base_collects_committed_shell_and_new_changes(tmp_path: Path) -> None:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked").write_text("before\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "fixture base")
    base = git(tmp_path, "rev-parse", "HEAD").decode().strip()
    assert verify_base(tmp_path, base) == base
    (tmp_path / "tracked").write_text("committed fix\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "fixture model commit")
    (tmp_path / "new").write_text("shell addition\n", encoding="utf-8")
    index_before = (tmp_path / ".git/index").read_bytes()
    patch = collect_patch(tmp_path, base)
    assert "+committed fix" in patch and "+shell addition" in patch
    assert (tmp_path / ".git/index").read_bytes() == index_before
    with pytest.raises(ValueError, match="HEAD"):
        verify_base(tmp_path, base)


def test_official_report_association_unknown_and_stale_patch(tmp_path: Path) -> None:
    # This is a parser fixture, never evidence of a real official grading run.
    patch = b"patch bytes\n"
    run = {
        "instance_id": "owner__repo-1",
        "model": "model",
        "run_id": "nexus-run",
        "trajectory": "trajectory.jsonl",
        "environment": {"contamination": "fixture"},
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    runs = tmp_path / "runs.jsonl"
    append_json(runs, run)
    root = tmp_path / "official-run"
    collect(runs, root, "fixture-version", tmp_path / "unknown.jsonl")
    assert read_jsonl(tmp_path / "unknown.jsonl")[0]["official_state"] == "unknown"
    folder = root / "model/owner__repo-1"
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({run["instance_id"]: {"resolved": False}}))
    (folder / "patch.diff").write_bytes(patch)
    collect(runs, root, "fixture-version", tmp_path / "valid.jsonl")
    assert read_jsonl(tmp_path / "valid.jsonl")[0]["official_state"] == "unresolved"
    (folder / "patch.diff").write_bytes(b"different")
    collect(runs, root, "fixture-version", tmp_path / "stale.jsonl")
    assert read_jsonl(tmp_path / "stale.jsonl")[0]["official_state"] == "error"


@pytest.mark.parametrize("write_secret", [False, True])
async def test_predict_exports_only_redacted_final(
    tmp_path: Path, monkeypatch: Any, write_secret: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "commit", "--allow-empty", "-m", "fixture")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    secret = "known-test-api-secret"
    monkeypatch.setenv("TEST_EVAL_KEY", secret)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    config = Config(ModelConfig("test", 32768, api_key_env="TEST_EVAL_KEY"), Limits())
    monkeypatch.setattr(swebench_v0, "load_config", lambda: config)

    class FakeModel(ScriptedModel):
        async def close(self) -> None:
            pass

    replies = [reply(text="Done " + secret)]
    if write_secret:
        replies.insert(
            0,
            reply(
                call(
                    "apply_patch",
                    {
                        "patch": "--- /dev/null\n+++ b/credential.txt\n@@ -0,0 +1 @@\n+"
                        + secret
                        + "\n"
                    },
                )
            ),
        )
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: FakeModel(replies))
    tasks = tmp_path / "tasks.jsonl"
    append_json(
        tasks,
        {
            "instance_id": "test__repo-1",
            "repo": "test/repo",
            "workspace": str(repo),
            "base_commit": base,
            "problem_statement": "test",
        },
    )
    env = tmp_path / "environment.json"
    env.write_text(
        json.dumps(
            {k: "fixture" for k in ("dataset", "dataset_revision", "network", "contamination")}
        )
    )
    output = tmp_path / "output"
    await swebench_v0.predict(tasks, output, env)
    assert all(secret not in p.read_text(encoding="utf-8") for p in output.glob("*.jsonl"))
    assert read_jsonl(output / "runs.jsonl")[0]["runtime"]["outcome"] == "completed"
    if write_secret:
        assert (repo / "credential.txt").read_text().strip() == secret
        assert not (output / "predictions.jsonl").exists()
        assert read_jsonl(output / "runs.jsonl")[0]["error"] == "patch_contains_configured_secret"
