"""Global Markdown authority. QMD is a disposable index, never the write path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from nexus.core.types import Json

ENTRY = re.compile(
    r"<!-- nexus-memory:([0-9a-f]{32}) -->\n(.*?)\n<!-- /nexus-memory -->", re.DOTALL
)
MAX_CONTENT = 32000
MAX_FILE = 4 * 1024 * 1024


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    """Flush and fsync a sibling file before replace; never truncate the authority."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass(frozen=True)
class Entry:
    id: str
    content: str
    start: int
    end: int

    def public(self) -> Json:
        return {"id": self.id, "content": self.content, "revision": digest(self.content)}


def entries(text: str) -> list[Entry]:
    found = [Entry(m[1], m[2], m.start(), m.end()) for m in ENTRY.finditer(text)]
    if len({e.id for e in found}) != len(found):
        raise ValueError("Duplicate memory IDs; repair the Markdown before modifying it")
    if text.count("<!-- nexus-memory:") != len(found) or text.count(
        "<!-- /nexus-memory -->"
    ) != len(found):
        raise ValueError("Malformed memory markers; file unchanged")
    return found


def block(ident: str, content: str) -> str:
    return f"<!-- nexus-memory:{ident} -->\n{content}\n<!-- /nexus-memory -->"


def content_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CONTENT:
        raise ValueError(f"content must be nonblank text, at most {MAX_CONTENT} characters")
    if "nexus-memory" in value or "\x00" in value:
        raise ValueError("Reserved memory marker or NUL in content")
    return value.replace("\r\n", "\n").strip()


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def path(self, name: str) -> Path:
        if not isinstance(name, str) or not (
            name in {"MEMORY.md", "SCRATCHPAD.md", ".dirty", ".epoch", ".index.json"}
            or re.fullmatch(r"daily/\d{4}-\d{2}-\d{2}\.md", name)
            or re.fullmatch(r"recovery/[0-9a-f]{32}\.json", name)
        ):
            raise ValueError("Invalid memory path")
        if name.startswith("daily/"):
            date.fromisoformat(name[6:-3])
        path = self.root / name
        # Refuse symlinks/junctions in the authority, including an existing parent.
        for part in (path, *path.parents):
            if part.is_symlink() or part.is_junction():
                raise ValueError("Memory paths must not traverse links or junctions")
        return path

    def read(self, name: str) -> str:
        path = self.path(name)
        if not path.exists():
            return ""
        if path.stat().st_size > MAX_FILE:
            raise ValueError("Memory file exceeds the 4 MiB read limit")
        return path.read_text(encoding="utf-8")

    def epoch(self) -> str:
        return self.read(".epoch")

    def _save(self, name: str, old: str, new: str, *, invalidate: bool = False) -> None:
        if len(new.encode("utf-8")) > MAX_FILE:
            raise ValueError("Memory file exceeds the 4 MiB limit")
        if self.read(name) != old:
            raise ValueError("Memory changed since read; retry with current revision")
        # Durable dirty intent precedes the authority change, including crash windows.
        atomic_write(self.path(".dirty"), uuid4().hex)
        if invalidate:
            atomic_write(self.path(".epoch"), uuid4().hex)
        if new.strip():
            atomic_write(self.path(name), new)
        else:
            # QMD 2.8.3 skips empty files without retiring their previous indexed body.
            # Removing the now-empty authority file makes qmd update deactivate that document.
            self.path(name).unlink(missing_ok=True)

    def inspect(self, name: str) -> Json:
        text = self.read(name)
        return {"file": name, "content": text, "entries": [e.public() for e in entries(text)]}

    def write(
        self, name: str, content: str, ident: str | None = None, revision: str | None = None
    ) -> Json:
        content = content_text(content)
        old = self.read(name)
        found = entries(old)
        if ident is None:
            if revision is not None:
                raise ValueError("revision requires id")
            ident = uuid4().hex
            new = old + ("\n\n" if old else "") + block(ident, content) + "\n"
        else:
            target = self._target(found, ident, revision)
            new = old[: target.start] + block(ident, content) + old[target.end :]
        self._save(name, old, new, invalidate=revision is not None)
        return {"file": name, "id": ident, "revision": digest(content), "written": True}

    @staticmethod
    def _target(found: list[Entry], ident: str, revision: str | None) -> Entry:
        target = next((e for e in found if e.id == ident), None)
        if target is None:
            raise ValueError("Memory ID not found; read the file first")
        if digest(target.content) != revision:
            raise ValueError("Memory revision mismatch; read the current entry before changing it")
        return target

    def forget(self, name: str, ident: str, revision: str) -> Json:
        old = self.read(name)
        target = self._target(entries(old), ident, revision)
        recovery_id = uuid4().hex
        record = {
            "version": 1,
            "file": name,
            "entry": target.public(),
            "deleted_at": datetime.now(UTC).isoformat(),
        }
        # Recovery must exist durably before the original can change.
        atomic_write(
            self.path(f"recovery/{recovery_id}.json"), json.dumps(record, ensure_ascii=False)
        )
        self._save(name, old, old[: target.start] + old[target.end :], invalidate=True)
        return {"deleted": True, "file": name, "id": ident, "recovery_id": recovery_id}

    def restore(self, recovery_id: str) -> Json:
        raw = self.read(f"recovery/{recovery_id}.json")
        if not raw:
            raise ValueError("Recovery ID not found")
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError("Invalid recovery record")
        name, entry = record["file"], record["entry"]
        if name not in {"MEMORY.md", "SCRATCHPAD.md"} and not name.startswith("daily/"):
            raise ValueError("Invalid recovery target")
        ident, content = entry["id"], content_text(entry["content"])
        if not re.fullmatch(r"[0-9a-f]{32}", ident) or digest(content) != entry["revision"]:
            raise ValueError("Invalid recovery entry")
        old = self.read(name)
        existing = next((e for e in entries(old) if e.id == ident), None)
        if existing is not None:
            if existing.content != content:
                raise ValueError("Restore conflict: this ID now contains different content")
            return {"restored": False, "already_present": True, "file": name, "id": ident}
        self._save(
            name,
            old,
            old + ("\n\n" if old else "") + block(ident, content) + "\n",
            invalidate=True,
        )
        return {"restored": True, "file": name, "id": ident, "revision": digest(content)}

    def scratchpad(
        self,
        action: str,
        content: str = "",
        ident: str | None = None,
        revision: str | None = None,
    ) -> Json:
        name = "SCRATCHPAD.md"
        if action == "list":
            return self.inspect(name)
        if action == "add":
            return self.write(name, "- [ ] " + content_text(content))
        old = self.read(name)
        target = self._target(entries(old), ident or "", revision)
        if action == "remove":
            return self.forget(name, target.id, revision or "")
        if action not in {"complete", "reopen"} or not target.content.startswith(
            ("- [ ] ", "- [x] ")
        ):
            raise ValueError("Expected complete/reopen/remove on one scratchpad item")
        prefix = "- [x] " if action == "complete" else "- [ ] "
        return self.write(name, prefix + target.content[6:], target.id, revision)
