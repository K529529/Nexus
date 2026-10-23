"""Offline comparison of saved eight-case runs; never invokes models or validators.

Usage: python analyze.py BASELINE [CANDIDATE] --output analysis.json
The next same-path patch after a failure is a recovery proxy, not a causal label.
"""
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def arguments(call):
    try:
        value = json.loads(call["arguments_json"])
        return value if isinstance(value, dict) else {}
    except (ValueError, KeyError):
        return {}


def patch_paths(call):
    patch = arguments(call).get("patch", "")
    return set(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M)) if isinstance(patch, str) else set()


def inspect(folder):
    manifest, summary = read(folder / "manifest.json"), read(folder / "summary.json")
    cases, artifacts = [], [fingerprint(folder / n) for n in ("manifest.json", "summary.json")]
    for result in summary["cases"]:
        case_folder = folder / result["case_id"]
        log_name = result.get("artifacts", {}).get("session")
        if not log_name:
            cases.append({"id": result["case_id"], "status": result["status"], "metrics_available": False})
            continue
        log = case_folder / log_name
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        starts = {e["data"]["call_id"]: e for e in rows if e["kind"] == "tool_started"}
        finishes = {e["data"]["call_id"]: e for e in rows if e["kind"] == "tool_finished"}
        messages = [e for e in rows if e["kind"] == "message"]
        receipts = {e["data"]["tool_call_id"]: json.loads(e["data"]["content"]) for e in messages if e["data"]["role"] == "tool"}
        raw_calls = {c["id"]: c for e in messages for c in e["data"].get("tool_calls", [])}
        patches = [e for e in starts.values() if e["data"]["name"] == "apply_patch"]
        patch_ids = {e["data"]["call_id"] for e in patches}
        cmd_ids = {cid for cid, c in raw_calls.items() if c["name"] == "exec_command" and "cmd" in arguments(c)}
        exec_ids = {cid for cid, e in starts.items() if e["data"]["name"] == "exec_command"}
        alias = {cid: r for cid, r in receipts.items() if r["data"].get("argument_alias_used") == "cmd_to_command"}
        failures = {cid: e for cid, e in finishes.items() if not e["data"]["ok"]}
        patch_failures = {cid: e for cid, e in failures.items() if cid in patch_ids}
        recovery = []
        for cid, failed in patch_failures.items():
            receipt = receipts[cid]
            paths = {receipt["data"]["failed_file"]} if receipt["data"].get("failed_file") else patch_paths(starts[cid]["data"])
            following = next((e for e in patches if e["seq"] > failed["seq"] and paths & patch_paths(e["data"])), None)
            end_seq = following["seq"] if following else rows[-1]["seq"] + 1
            interval = [e for e in rows if failed["seq"] < e["seq"] < end_seq]
            recovery.append({"failed_call_id": cid, "failed_seq": failed["seq"], "error_code": receipt["error_code"], "failed_file": receipt["data"].get("failed_file"), "next_same_path_patch_call_id": following["data"]["call_id"] if following else None, "identical_arguments": following["data"]["arguments_json"] == starts[cid]["data"]["arguments_json"] if following else None, "intervening_model_calls": sum(e["kind"] == "model_started" for e in interval), "intervening_tool_calls": sum(e["kind"] == "tool_started" for e in interval)})
        audit = [e for e in rows if e["kind"] == "patch_diff"]
        model_count = sum(e["kind"] == "model_started" for e in rows)
        metrics = result["metrics"]
        assert len(starts) == metrics["tool_calls"]
        assert model_count == metrics["model_calls"]
        assert len(failures) == metrics["tool_failures"]
        assert set(starts) == set(finishes) == set(receipts)
        assert all(starts[cid]["data"]["arguments_json"] == raw_calls[cid]["arguments_json"] for cid in starts)
        counts = dict(
            model_calls=model_count, tool_calls=len(starts), apply_patch_calls=len(patches),
            invalid_patch=sum(e["data"].get("error_code") == "invalid_patch" for e in patch_failures.values()),
            patch_conflict=sum(e["data"].get("error_code") == "patch_conflict" for e in patch_failures.values()),
            patch_failure_same_path_followups=sum(r["next_same_path_patch_call_id"] is not None for r in recovery),
            raw_cmd_calls=len(cmd_ids),
            invalid_arguments_all=sum(e["data"].get("error_code") == "invalid_arguments" for e in failures.values()),
            exec_invalid_arguments=sum(e["data"].get("error_code") == "invalid_arguments" for cid,e in failures.items() if cid in exec_ids),
            cmd_invalid_arguments=sum(e["data"].get("error_code") == "invalid_arguments" for cid,e in failures.items() if cid in cmd_ids),
            alias_marked=len(alias),
            alias_with_exit_code=sum(r["data"].get("exit_code") is not None for r in alias.values()),
            alias_ok=sum(r["ok"] for r in alias.values()),
            patch_results_with_diff=sum(any("diff" in f for f in r["data"].get("files", [])) for cid,r in receipts.items() if cid in patch_ids),
            patch_diff_events=len(audit),
            alias_raw_call_mismatches=sum(cid not in cmd_ids for cid in alias),
        )
        cases.append({"id": result["case_id"], "status": result["status"], "agent": result["agent"], "metrics_available": True, "counts": counts, "steps": metrics["steps"], "at_step_limit": metrics["steps"] >= manifest["limits"]["max_steps"], "usage": metrics["usage"], "timing": result["timing"], "tool_errors": dict(Counter(e["data"].get("error_code") for e in failures.values())), "patch_recovery_windows": recovery, "paired_calls": True, "audit_linked_to_patch": all(e["data"]["call_id"] in patch_ids for e in audit)})
        artifacts += [fingerprint(log), fingerprint(case_folder / "result.json")]
    totals = Counter()
    for c in cases:
        totals.update(c.get("counts", {}))
    return {"run": folder.name, "manifest": manifest, "status_counts": summary["counts"], "reported_totals": summary["totals"], "event_totals": dict(totals), "cases": cases, "artifact_fingerprints": artifacts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path, nargs="?")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = inspect(args.baseline)
    output = {"method": "Read-only saved JSONL/results; recovery windows may overlap; same-path followup is a proxy. Ineffective exploration requires manual review. Alias marked, process exit and ok are separate metrics.", "baseline": baseline}
    if args.candidate:
        current = inspect(args.candidate)
        a, b = baseline["manifest"], current["manifest"]
        keys = ("case_id", "source", "repo", "base_commit", "image", "task_hash", "validator_hash", "reporter_hash", "base_tree")
        output.update(candidate=current, comparison={"selected_equal": a["selected"] == b["selected"], "models_equal": all(a[k] == b[k] for k in ("model", "compaction_model", "limits")), "case_identities_equal": len(a["cases"]) == len(b["cases"]) and all(all(x.get(k) == y.get(k) for k in keys) for x,y in zip(a["cases"],b["cases"],strict=True))})
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"baseline_status": baseline["status_counts"], "baseline_events": baseline["event_totals"]}, indent=2))


if __name__ == "__main__":
    main()
