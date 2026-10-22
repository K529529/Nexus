"""Snapshot source selection; session lifetime and replay belong to Conversation."""

from __future__ import annotations

import os
import re
from datetime import date, timedelta

from nexus.app.memory import MemoryStore, digest, entries
from nexus.core.memory_snapshot import build_snapshot
from nexus.core.tokens import TokenEstimator
from nexus.core.types import Json


def root_id(store: MemoryStore) -> str:
    return digest(os.path.normcase(str(store.root)))


def snapshot(store: MemoryStore, estimator: TokenEstimator) -> Json:
    today = date.today()
    today_path = f"daily/{today.isoformat()}.md"
    yesterday_path = f"daily/{(today - timedelta(days=1)).isoformat()}.md"
    scratchpad = store.read("SCRATCHPAD.md")
    found = entries(scratchpad)
    if found:
        opened = "\n".join(e.content for e in found if e.content.startswith("- [ ] "))
    else:
        opened = "\n".join(re.findall(r"(?m)^- \[ \] .+$", scratchpad))
    value = build_snapshot(
        [
            ("SCRATCHPAD.md (open items)", opened, 2048, "head"),
            (today_path, store.read(today_path), 2048, "tail"),
            ("MEMORY.md", store.read("MEMORY.md"), 4096, "ends"),
            (yesterday_path, store.read(yesterday_path), 1024, "tail"),
        ],
        estimator,
    )
    return {**value, "root_id": root_id(store), "epoch": store.epoch()}
