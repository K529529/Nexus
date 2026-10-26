"""Check actual Stage 4B acceptance facts independently of runner exit codes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(directory: Path) -> dict[str, object]:
    def read(name: str) -> tuple[dict[str, dict], list[dict]]:
        rows = json.loads((directory / name).read_text(encoding="utf-8"))["rows"]
        return {r["test"]: r for r in rows if "test" in r}, rows

    hitl, events = read("hitl-real-04.json")
    functional, _ = read("srt-attempt-12.json")
    stopped, _ = read("srt-attempt-10.json")

    def result(rows: dict[str, dict], name: str, code: str | None = None) -> dict:
        value = rows[name]["result"]
        assert value["error_code"] == code, (name, value)
        assert value["ok"] == (code is None), (name, value)
        return value["data"]

    for name in ("initial-read-only", "user-allow-once", "baseline-read"):
        result(hitl, name)
    assert "srt-sandbox" in result(hitl, "baseline-read")["stdout"]
    assert "approved once" in result(hitl, "baseline-read")["stdout"]
    result(hitl, "user-deny", "approval_denied")
    result(hitl, "cannot-reuse", "policy_denied")
    result(hitl, "baseline-deny-write", "command_exit_nonzero")
    assert "403" in result(hitl, "default-network-denied", "command_exit_nonzero")["stderr"]
    assert hitl["deny-no-file"]["passed"]
    assert hitl["host-files"]["approved_file"] == "approved once\n"
    assert hitl["host-files"]["baseline_write_denied"] and hitl["close"]["confirmed"]
    approved = next(
        i
        for i, r in enumerate(events)
        if r.get("event") == "approval_resolved" and r["data"]["choice"] == "allow_once"
    )
    patches = [i for i, r in enumerate(events) if r.get("event") == "patch_diff"]
    assert len(patches) == 1 and patches[0] > approved

    for name in (
        "workspace-write-identity",
        "python-pytest",
        "patch",
        "git",
        "allowlisted-network",
        "roundtrip-read-only",
        "roundtrip-workspace-write",
    ):
        result(functional, name)
    assert "srt-sandbox" in result(functional, "workspace-write-identity")["stdout"]
    assert "1 passed" in result(functional, "python-pytest")["stdout"]
    assert "acceptance" in result(functional, "git")["stdout"]
    assert "200" in result(functional, "allowlisted-network")["stdout"]
    for name in (
        "outside-deny-write",
        "git-config-denied",
        "git-hook-denied",
        "roundtrip-deny-write",
    ):
        data = result(functional, name, "command_exit_nonzero")
        assert "denied" in data["stderr"].lower(), (name, data)
    assert functional["host-files"]["outside_write_denied"]
    assert functional["host-files"]["patch_contents"] == "patched by SRT\n"
    assert functional["roundtrip-host-read"]["contents"].strip() == "yes"
    assert functional["close"]["confirmed"]
    for name, code in (("timeout", "command_timeout"), ("cancel", "cancelled")):
        data = result(stopped, name, code)
        assert data["requires_reset"] and not data["cleanup_incomplete"]
    result(stopped, "reuse-after-cancel")
    assert stopped["close"]["confirmed"]
    return {
        "status": "PASS",
        "evidence": ["hitl-real-04.json", "srt-attempt-12.json", "srt-attempt-10.json"],
        "scope": "Recorded functional facts; not an OS security certification",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), indent=2))
