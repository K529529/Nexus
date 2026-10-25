"""Replay exact archived FilePilot patch arguments without model calls."""
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
from nexus.core.types import ExecutionContext
from nexus.tools.patch import apply_patch
from nexus.tools.patch_format import parse_nexus_patch


async def main():
    fixture = ROOT / "tests/fixtures/stage31b"
    source = json.loads((fixture / "rename-failures.json").read_text(encoding="utf-8"))
    outputs = []
    for sample in source["samples"]:
        events = []
        async def emit(kind, data):
            events.append({"kind": kind, "data": data})
        with tempfile.TemporaryDirectory(prefix="nexus-stage31b-replay-") as temporary:
            root = Path(temporary)
            if sample["result"]["error_code"] == "patch_conflict":
                target = root / "tests/web/create_delete_harness.js"
                target.parent.mkdir(parents=True)
                content = (fixture / "create_delete_harness.js").read_bytes()
                assert hashlib.sha256(content).hexdigest() == source["harness_sha256"]
                target.write_bytes(content)
            context = ExecutionContext(root, sample["call"]["id"], "/bin/sh")
            result = await apply_patch(json.loads(sample["call"]["arguments_json"]), context, emit)
            assert not result.ok and result.error_code == sample["result"]["error_code"]
            assert result.data["failed_file"] == sample["result"]["data"]["failed_file"]
            assert result.data["failed_hunk"] == sample["result"]["data"]["failed_hunk"]
            assert not events and not result.data["partial"]
            repair = json.loads(sample["recovery_call"]["arguments_json"])
            parse_nexus_patch(repair["patch"])
            record = {"original_result_seq": sample["result_seq"], "original_detail": sample["result"]["data"]["detail"], "new_tool_result": json.loads(result.message().content), "requires_model_repair": True, "real_repair_parses": True, "filesystem_repair": "NOT RUN: original multi-file preimages not reconstructed"}
            if sample["result"]["error_code"] == "patch_conflict":
                repaired = await apply_patch(repair, context, emit)
                assert repaired.ok
                assert repaired.data["files"][0]["after_hash"] == "09bb8514537ef79afbb83a348911d00a74887c00da3830fb95e6d2ccdf7fed98"
                record["filesystem_repair"] = "PASS: matches historical after_hash"
                record["repaired_result"] = json.loads(repaired.message().content)
                record["audit_count"] = len(events)
            outputs.append(record)
    (Path(__file__).parent / "failure-comparison.json").write_text(json.dumps({"source_session": source["source_session"], "source_sha256": source["source_sha256"], "samples": outputs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS: 4 archived failures reproduce; all real repairs parse; stale-old-block real repair matches historical SHA256")


if __name__ == "__main__":
    asyncio.run(main())
