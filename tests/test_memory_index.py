from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.app import memory_index
from nexus.app.memory import MemoryStore
from nexus.app.memory_index import MemoryIndex
from tests.conftest import Recorder


async def ready(index: MemoryIndex) -> None:
    async with asyncio.timeout(5):
        while index.state.get("status") != "ready":
            index.updated.clear()
            await index.updated.wait()


class FakeClient:
    opens = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        type(self).opens += 1
        self.owner = asyncio.current_task()
        return self

    async def __aexit__(self, *args: Any) -> None:
        assert self.owner is asyncio.current_task()

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[SimpleNamespace(name="query", input_schema={"properties": {"query": {}}})]
        )

    async def call_tool(self, name: str, args: Any, **kwargs: Any) -> Any:
        assert name == "query" and args["rerank"] is True
        assert args["collections"] == ["nexus-memory"]
        return SimpleNamespace(
            is_error=False,
            result_type="complete",
            structured_content={"results": [{"title": args["query"]}]},
        )


class FakeIndex(MemoryIndex):
    def __init__(self, store: MemoryStore) -> None:
        super().__init__(store)
        self.commands: list[tuple[str, ...]] = []
        self.fail = False

    async def _setup(self) -> None:
        pass

    async def _command(self, *args: str) -> str:
        self.commands.append(args)
        if self.fail:
            raise RuntimeError("fake embedding failure")
        return ""


async def test_coalesced_updates_reuse_private_client(tmp_path: Path, monkeypatch: Any) -> None:
    FakeClient.opens = 0
    monkeypatch.setattr(memory_index, "Client", FakeClient)
    store = MemoryStore(tmp_path)
    index = FakeIndex(store)
    emit = Recorder()
    index.start(emit)
    try:
        for i in range(8):
            store.write("MEMORY.md", str(i))
            index.changed()
        await ready(index)
        assert index.commands == [("update",), ("embed",)]
        assert (await index.search("中文 English", 5))["discarded_stale_results"] == 1
        assert (await index.search("second", 5))["index_status"] == "stale"
        assert FakeClient.opens == 1
        assert not store.read(".dirty")
        a = store.inspect("MEMORY.md")["entries"][0]
        store.forget("MEMORY.md", a["id"], a["revision"])
        with pytest.raises(ValueError, match="pending"):
            await index.search("deleted", 5)
        await ready(index)
        assert index.commands == [("update",), ("embed",)] * 2
        assert FakeClient.opens == 1
    finally:
        await index.close()


async def test_failed_sync_keeps_dirty_and_restart_repairs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(memory_index, "Client", FakeClient)
    store = MemoryStore(tmp_path)
    store.write("MEMORY.md", "persist before index")
    failed = FakeIndex(store)
    failed.fail = True
    failed.start(Recorder())
    try:
        async with asyncio.timeout(5):
            while failed.state.get("status") != "failed":
                failed.updated.clear()
                await failed.updated.wait()
        assert store.read(".dirty")
        with pytest.raises(ValueError):
            await failed.search("anything", 5)
    finally:
        await failed.close()
    resumed = FakeIndex(store)
    resumed.start(Recorder())
    try:
        await ready(resumed)
        assert not store.read(".dirty")
        assert "persist before index" in store.read("MEMORY.md")
    finally:
        await resumed.close()


async def test_missing_qmd_is_explicit_and_does_not_block_writes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    index = MemoryIndex(store, ("nexus-nonexistent-qmd-executable",))
    index.start(Recorder())
    assert index.task
    await index.task
    assert index.state["status"] == "unavailable"
    store.write("MEMORY.md", "works offline")
    with pytest.raises(ValueError):
        await index.search("works", 5)
    await index.close()
