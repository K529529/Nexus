from __future__ import annotations

import json
from pathlib import Path

from nexus.app.agent_history import read_tree
from nexus.app.tracing import project_run, project_tree
from tests.test_stage8_observability import saved_tree


def test_tree_trace_links_lifecycles_and_requests_without_content(tmp_path: Path) -> None:
    logs, _, _ = read_tree(saved_tree(tmp_path))
    root_rows = logs["root"]
    root_rows.insert(-1, {**root_rows[-2], "kind": "agent_message", "seq": 100,
        "data": {"agent_id": "child", "parent_id": "root", "direction": "to_parent",
                 "request_decision": True, "text": "PRIVATE_MESSAGE"}})
    root_rows[-1]["seq"] = 101
    spans, diagnostics = project_tree(logs, "root-session")
    assert not diagnostics
    root = next(s for s in spans if s["name"] == "nexus.run")
    child = next(s for s in spans if s["name"] == "nexus.agent")
    assert child["parent_id"] == root["span_id"]
    assert {s["trace_id"] for s in spans} == {root["trace_id"]}
    ids = {s["span_id"] for s in spans}
    assert all(s["parent_id"] is None or s["parent_id"] in ids for s in spans)
    requests = [s for s in spans if s["name"] == "nexus.model.request"]
    assert {s["parent_id"] for s in requests} == {root["span_id"], child["span_id"]}
    assert sum(s["attributes"].get("gen_ai.usage.input_tokens", 0) for s in spans) == 30
    message = next(s for s in spans if s["name"] == "nexus.agent.message")
    assert message["attributes"]["nexus.message.request_decision"]
    assert "PRIVATE" not in json.dumps(spans)
    assert "public result" not in json.dumps(spans)


def test_resumed_execution_has_new_identity_without_downtime(tmp_path: Path) -> None:
    logs, _, _ = read_tree(saved_tree(tmp_path))
    rows = logs["root"]
    resumed = [{**r, "seq": r["seq"] + 100, "timestamp": r["timestamp"].replace("T00", "T12"),
                "data": {**r["data"]}} for r in rows]
    next(r for r in resumed if r["kind"] == "run_started")["data"]["execution_id"] = "resume"
    spans, _ = project_run(rows + resumed, "root-session")
    roots = [s for s in spans if s["name"] == "nexus.run"]
    assert len(roots) == 2 and roots[0]["trace_id"] != roots[1]["trace_id"]
    assert all(s["end"] - s["start"] == 6_000_000_000 for s in roots)
    interrupted = [r for r in rows if r["kind"] != "run_finished"]
    spans, notes = project_run(interrupted + resumed, "root-session")
    assert len([s for s in spans if s["name"] == "nexus.run"]) == 1
    assert any("incomplete_run" in note for note in notes)
