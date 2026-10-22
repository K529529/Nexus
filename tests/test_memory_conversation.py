from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus.app import bootstrap
from nexus.app.memory import MemoryStore
from nexus.app.session import read_records
from nexus.core.context import ContextBuilder
from tests.conftest import ScriptedModel, call, reply
from tests.test_conversation import config, ignore
from tests.test_session_compaction import SummaryModel, seed, summary_reply


class Main(ScriptedModel):
    async def close(self) -> None:
        pass


async def test_snapshot_stable_across_runs_restart_and_ordinary_write(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    cfg = config(monkeypatch)
    store = MemoryStore(tmp_path / "memory")
    store.write("MEMORY.md", "ORIGINAL_MEMORY: prefer focused pytest regression")
    done = store.scratchpad("add", "COMPLETED_ITEM")
    store.scratchpad("complete", ident=done["id"], revision=done["revision"])
    store.scratchpad("add", "OPEN_ITEM")
    main = Main(
        [
            reply(call("memory_write", {"content": "NEW_MEMORY: preserve failures"})),
            reply(text="saved"),
            reply(text="another run"),
        ]
    )
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: main)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        assert (await convo.turn("do work")).outcome == "completed"
        assert (await convo.turn("next")).outcome == "completed"
        prefixes = [request[0].content for request in main.requests]
        assert len(set(prefixes)) == 1
        assert "ORIGINAL_MEMORY" in prefixes[0] and "OPEN_ITEM" in prefixes[0]
        assert "COMPLETED_ITEM" not in prefixes[0] and "NEW_MEMORY" not in prefixes[0]
        assert convo.writer
        path = convo.writer.path
        records, _ = read_records(convo.writer.stream)
        assert sum(r["kind"] == "memory_snapshot" for r in records) == 1
        assert sum(r["kind"] == "instructions" for r in records) == 1
    finally:
        await convo.close()
    resumed_main = Main([reply(text="resumed")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: resumed_main)
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        await resumed.turn("continue")
        assert resumed_main.requests[0][0].content == prefixes[0]
    finally:
        await resumed.close()
    fresh_main = Main([reply(text="fresh")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: fresh_main)
    fresh = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        await fresh.turn("new conversation")
        assert "NEW_MEMORY" in fresh_main.requests[0][0].content
    finally:
        await fresh.close()


async def test_forget_restore_refresh_before_next_model_request(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    cfg = config(monkeypatch)
    store = MemoryStore(tmp_path / "memory")
    old = store.write("MEMORY.md", "DELETE_ME_FROM_CURRENT_SNAPSHOT")
    main = Main(
        [
            reply(call("memory_forget", {"id": old["id"], "revision": old["revision"]})),
            reply(text="deleted"),
        ]
    )
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: main)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        await convo.turn("forget the obsolete fact")
        assert "DELETE_ME_FROM_CURRENT_SNAPSHOT" in main.requests[0][0].content
        assert "DELETE_ME_FROM_CURRENT_SNAPSHOT" not in main.requests[1][0].content
        recovery = next((store.root / "recovery").glob("*.json")).stem
        main.replies = iter(
            [reply(call("memory_restore", {"recovery_id": recovery})), reply(text="restored")]
        )
        await convo.turn("restore it")
        assert "DELETE_ME_FROM_CURRENT_SNAPSHOT" not in main.requests[2][0].content
        assert "DELETE_ME_FROM_CURRENT_SNAPSHOT" in main.requests[3][0].content
        assert convo.writer
        path = convo.writer.path
    finally:
        await convo.close()
    # Forget between process lifetimes invalidates the persisted snapshot as well.
    store.forget("MEMORY.md", old["id"], old["revision"])
    later = Main([reply(text="checked")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: later)
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        await resumed.turn("continue")
        assert "DELETE_ME_FROM_CURRENT_SNAPSHOT" not in later.requests[0][0].content
    finally:
        await resumed.close()


@pytest.mark.parametrize("automatic", [False, True])
async def test_multiple_compactions_and_resume_preserve_snapshot(
    tmp_path: Path,
    monkeypatch: Any,
    automatic: bool,
) -> None:
    cfg = config(monkeypatch)
    cfg.limits.context_window = 70000
    store = MemoryStore(tmp_path / "memory")
    store.write("MEMORY.md", "SNAPSHOT_ONLY_SENTINEL")
    main = Main([reply(text="first"), reply(text="next")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: main)
    convo = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    compactor = SummaryModel([summary_reply(), summary_reply()])
    try:
        await convo.turn("begin")
        prefix = main.requests[0][0].content
        convo.compaction_model = compactor  # type: ignore[assignment]
        assert convo.events and convo.writer
        await seed(convo.session, convo.events, 32)
        if automatic:
            assert (await convo.turn("next")).outcome == "completed"
        else:
            assert await convo.compact()
        assert convo.session.checkpoint
        await seed(convo.session, convo.events, 32, 32)
        assert await convo.compact()
        assert len(compactor.requests) == 2
        assert "SNAPSHOT_ONLY_SENTINEL" not in str(compactor.requests)
        assert ContextBuilder().build_active_context(convo.session)[0].content == prefix
        path = convo.writer.path
        saved = convo.session.memory_snapshot
    finally:
        await convo.close()
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        assert resumed.session.memory_snapshot == saved
        assert ContextBuilder().build_active_context(resumed.session)[0].content == prefix
        assert resumed.session.checkpoint
    finally:
        await resumed.close()


async def test_opt_out_and_legacy_resume_do_not_import_new_memory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    cfg = config(monkeypatch)
    cfg.memory_enabled = False
    main = Main([reply(text="off"), reply(text="legacy")])
    monkeypatch.setattr(bootstrap, "ChatModel", lambda _: main)
    off = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path)
    try:
        await off.turn("off")
        assert "memory_write" not in off.registry
        assert off.writer
        path = off.writer.path
        prefix = main.requests[0][0].content
    finally:
        await off.close()
    cfg.memory_enabled = True
    MemoryStore(tmp_path / "memory").write("MEMORY.md", "MUST_NOT_IMPORT_ON_LEGACY_RESUME")
    resumed = bootstrap.Conversation(tmp_path, cfg, ignore, home=tmp_path, resume=path)
    try:
        await resumed.turn("legacy resume")
        assert main.requests[-1][0].content == prefix
    finally:
        await resumed.close()
