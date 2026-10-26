"""Verify recorded P0 functional results, policy exclusions, and unchanged OS ACLs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(directory: Path) -> dict[str, object]:
    def read(name: str):
        return json.loads((directory / name).read_text(encoding="utf-8-sig"))

    first = {r["test"]: r for r in read("functional-01.json")["rows"] if "test" in r}
    assert first["git"]["result"]["error_code"] == "bridge_busy"  # Preserve failed attempt.
    rows = {r["test"]: r for r in read("functional-final.json")["rows"] if "test" in r}
    for name in ("read-only-identity", "read-only-deny-write", "read-only-deny-patch"):
        rows[name] = first[name]  # ACL-only version; final RO is verified again in roundtrip.

    def result(name: str, code: str | None = None) -> dict:
        value = rows[name]["result"]
        assert value["ok"] == (code is None) and value["error_code"] == code, (name, value)
        return value["data"]

    for name in ("read-only-identity", "workspace-write-identity"):
        assert "srt-sandbox" in result(name)["stdout"]
    assert "hello sandbox" in result("read-only-identity")["stdout"]
    assert "1 passed" in result("python-pytest")["stdout"]
    assert "acceptance" in result("git")["stdout"]
    assert "200" in result("allowlisted-network")["stdout"]
    for name in ("patch", "roundtrip-read-only", "roundtrip-workspace-write"):
        result(name)
    result("read-only-deny-patch", "policy_denied")
    for name in (
        "read-only-deny-write",
        "outside-deny-write",
        "git-config-denied",
        "git-hook-denied",
        "roundtrip-deny-write",
    ):
        # PowerShell reports existing-file denial as Category NotSpecified but
        # preserves the UnauthorizedAccess exception/error ID in both forms.
        assert "UnauthorizedAccess" in result(name, "command_exit_nonzero")["stderr"]
    assert rows["host-files"]["read_only_write_denied"]
    assert rows["host-files"]["outside_write_denied"]
    assert rows["host-files"]["patch_contents"] == "patched by SRT\n"
    assert rows["roundtrip-host-read"]["contents"].strip() == "yes"
    host = read("host-final-check.json")
    assert host["git_subject"] == "acceptance"
    assert not host["git_config_overwritten"] and not host["git_hook_created"]
    assert not host["ro_file_created"]
    assert rows["close"]["confirmed"]
    before, after = read("acl-before.json"), read("acl-after.json")
    assert not before["administrator"] and not after["administrator"]
    assert before["paths"] == after["paths"]
    manifest = read("manifest-final.json")
    assert manifest["bridges_match"]
    traces = []
    for name in (
        "functional-trace.jsonl",
        "functional-final-trace.jsonl",
        "profile-trace.jsonl",
        "manual-trace.jsonl",
    ):
        traces.extend(json.loads(line) for line in (directory / name).read_text().splitlines())
    policies = [r["filesystem"] for r in traces if r.get("phase") == "policy"]
    assert len(policies) == 11
    for policy in policies:
        for values in policy.values():
            for value in values:
                assert not any(
                    value.lower() == root or value.lower().startswith(root + "\\")
                    for root in ("c:\\windows", "c:\\program files", "c:\\program files (x86)")
                ), value
    profile = read("init-profile.json")
    assert len(profile) == 3 and all(r.get("initialized") for r in profile)
    manual = read("manual-workspace.json")
    assert manual["result"]["ok"] and "srt-sandbox" in manual["result"]["data"]["stdout"]
    assert manual["workspace"].lower() in manual["result"]["data"]["stdout"].lower()
    assert Path(manual["shell"]) == Path(manifest["shell"])
    reset_timeouts = [r for r in traces if r.get("error") == "ETIMEDOUT"]
    return {
        "P0_and_functional": "PASS",
        "system_acl_unchanged": True,
        "policies_checked": len(policies),
        "native_timeouts": reset_timeouts,
        "cleanup_scope": (
            "Bridge exit confirmed; upstream reset is best effort, not full ACL rollback"
        ),
        "scope": "Normal SRT regression, not an OS security certification",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), indent=2))
