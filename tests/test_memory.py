from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus.app import memory
from nexus.app.memory import MemoryStore, entries
from nexus.core.types import ExecutionContext
from nexus.tools.memory import create_memory_tools
from tests.conftest import Recorder


def test_write_correct_and_restore_preserves_later_writes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    a = store.write("MEMORY.md", "Nexus: use pytest")
    assert store.epoch() == ""
    b = store.write("MEMORY.md", "Nexus: use uv", a["id"], a["revision"])
    assert store.epoch()
    with pytest.raises(ValueError, match="revision"):
        store.forget("MEMORY.md", a["id"], a["revision"])
    removed = store.forget("MEMORY.md", b["id"], b["revision"])
    store.write("MEMORY.md", "FilePilot: preserve junctions")
    restored = store.restore(removed["recovery_id"])
    assert restored["restored"]
    assert [e.content for e in entries(store.read("MEMORY.md"))] == [
        "FilePilot: preserve junctions",
        "Nexus: use uv",
    ]
    assert store.restore(removed["recovery_id"])["already_present"]
    store.write("MEMORY.md", "Nexus: updated again", b["id"], b["revision"])
    with pytest.raises(ValueError, match="conflict"):
        store.restore(removed["recovery_id"])


def test_recovery_failure_never_deletes(tmp_path: Path, monkeypatch: Any) -> None:
    store = MemoryStore(tmp_path)
    a = store.write("MEMORY.md", "keep this")
    before = store.read("MEMORY.md")
    original = memory.atomic_write

    def fail(path: Path, text: str) -> None:
        if path.parent.name == "recovery":
            raise OSError("disk full")
        original(path, text)

    monkeypatch.setattr(memory, "atomic_write", fail)
    with pytest.raises(OSError):
        store.forget("MEMORY.md", a["id"], a["revision"])
    assert store.read("MEMORY.md") == before


def test_delete_failure_after_recovery_can_be_restored(tmp_path: Path, monkeypatch: Any) -> None:
    store = MemoryStore(tmp_path)
    a = store.write("MEMORY.md", "keep this")
    original = Path.unlink

    def fail(path: Path, missing_ok: bool = False) -> None:
        if path.name == "MEMORY.md":
            raise OSError("remove failed")
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(OSError):
        store.forget("MEMORY.md", a["id"], a["revision"])
    recovery = next((tmp_path / "recovery").glob("*.json"))
    assert store.restore(recovery.stem)["already_present"]
    assert store.read(".dirty")


def test_scratchpad_lifecycle_and_daily(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    a = store.scratchpad("add", "verify FilePilot")
    b = store.scratchpad("complete", ident=a["id"], revision=a["revision"])
    assert "- [x] verify" in store.read("SCRATCHPAD.md")
    c = store.scratchpad("reopen", ident=b["id"], revision=b["revision"])
    assert "- [ ] verify" in store.read("SCRATCHPAD.md")
    removed = store.scratchpad("remove", ident=c["id"], revision=c["revision"])
    store.restore(removed["recovery_id"])
    assert len(store.scratchpad("list")["entries"]) == 1
    store.write("daily/2026-10-23.md", "Today: tested memory")
    with pytest.raises(ValueError):
        store.write("daily/2026-02-30.md", "invalid date")


@pytest.mark.parametrize("path", ["../other.md", "daily/../../other.md", "/etc/passwd"])
def test_path_rejected(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        MemoryStore(tmp_path).write(path, "bad")


def test_unmanaged_text_preserved_and_marker_injection_rejected(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# Human notes\nkeep\n", encoding="utf-8")
    store = MemoryStore(tmp_path)
    a = store.write("MEMORY.md", "new entry")
    store.forget("MEMORY.md", a["id"], a["revision"])
    assert store.read("MEMORY.md").startswith("# Human notes\nkeep\n")
    with pytest.raises(ValueError):
        store.write("MEMORY.md", "<!-- /nexus-memory -->")


async def test_tools_validate_and_work_without_qmd(execution: ExecutionContext) -> None:
    store = MemoryStore(execution.workspace / "memory")
    tools = create_memory_tools(store)
    emit = Recorder()
    for args in (
        {"content": "x", "overwrite": True},
        {"content": ""},
        {"content": "x", "target": "../x"},
    ):
        assert not (await tools["memory_write"].execute(args, execution, emit)).ok
    written = await tools["memory_write"].execute({"content": "use pytest"}, execution, emit)
    assert written.ok and written.data["index_status"] == "pending"
    read = await tools["memory_read"].execute({}, execution, emit)
    assert read.data["entries"][0]["id"] == written.data["id"]
    missing = await tools["memory_search"].execute({"query": "pytest"}, execution, emit)
    assert not missing.ok and missing.error_code == "memory_search_unavailable"
    assert not (await tools["memory_read"].execute({"limit": True}, execution, emit)).ok
    assert json.loads(written.message().content)["ok"] is True


async def test_scratchpad_large_list_is_paged(execution: ExecutionContext) -> None:
    store = MemoryStore(execution.workspace / "memory")
    for i in range(30):
        store.scratchpad("add", f"item {i}: " + "中" * 3000)
    tool = create_memory_tools(store)["scratchpad"]
    result = await tool.execute({"action": "list"}, execution, Recorder())
    assert result.ok and result.truncated
    assert "content" not in result.data and result.data["total"] == 30
    assert len(result.message().content.encode("utf-8")) < execution.output_limit_bytes
    next_page = await tool.execute(
        {"action": "list", "offset": result.data["next_offset"]}, execution, Recorder()
    )
    assert next_page.data["entries"][0]["id"] != result.data["entries"][0]["id"]


async def test_corrupt_recovery_is_an_explicit_tool_error(execution: ExecutionContext) -> None:
    store = MemoryStore(execution.workspace / "memory")
    recovery = store.path("recovery/" + "a" * 32 + ".json")
    recovery.parent.mkdir(parents=True)
    recovery.write_text('{"version":1,"file":42,"entry":{}}', encoding="utf-8")
    result = await create_memory_tools(store)["memory_restore"].execute(
        {"recovery_id": "a" * 32}, execution, Recorder()
    )
    assert not result.ok and result.error_code == "memory_error"


async def test_read_keeps_human_notes_alongside_managed_entries(
    execution: ExecutionContext,
) -> None:
    store = MemoryStore(execution.workspace / "memory")
    store.root.mkdir()
    store.path("MEMORY.md").write_text("Human project convention", encoding="utf-8")
    store.write("MEMORY.md", "Managed lesson")
    result = await create_memory_tools(store)["memory_read"].execute({}, execution, Recorder())
    assert result.ok and result.data["unmanaged_content"] == "Human project convention"
    assert result.data["entries"][0]["content"] == "Managed lesson"
