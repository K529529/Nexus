"""One asynchronous QMD owner: coalesced CLI indexing and reusable private MCP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from nexus.app.memory import MemoryStore, atomic_write, digest
from nexus.app.memory_setup import NODE_VERSION, QMD_VERSION, managed_root
from nexus.core.types import Emit, Json, json_text

EMBED_MODEL = "hf:Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf"
RERANK_MODEL = "hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf"
EXPANSION_MODEL = "hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf"
COLLECTION = "nexus-memory"
MASK = "{MEMORY.md,SCRATCHPAD.md,daily/*.md}"


def installed_command() -> tuple[str, ...]:
    """Resolve a user-local install without editing PATH or running an npm installer."""
    executable = shutil.which("qmd")
    node = shutil.which("node")
    managed = managed_root()
    local_node = managed / f"node-v{NODE_VERSION}" / ("node.exe" if os.name == "nt" else "bin/node")
    if node is None and local_node.is_file():
        node = str(local_node)
    if executable and not executable.lower().endswith((".cmd", ".bat")):
        return (executable,)
    folders = [Path(executable).parent] if executable else []
    folders += [managed / f"qmd-{QMD_VERSION}", managed / "qmd"]
    for folder in folders:
        entry = folder / "node_modules" / "@tobilu" / "qmd" / "dist" / "cli" / "qmd.js"
        if node and entry.is_file():
            return (node, str(entry))
    return (executable or "qmd",)


class MemoryIndex:
    def __init__(
        self,
        store: MemoryStore,
        command: tuple[str, ...] = ("qmd",),
        *,
        prepare: Callable[[Emit], Awaitable[tuple[str, ...]]] | None = None,
    ) -> None:
        self.store, self.command = store, command
        self.prepare = prepare
        self.state: Json = {"status": "not_started"}
        self.task: asyncio.Task[None] | None = None
        self.queue: asyncio.Queue[tuple[str, int, asyncio.Future[Json]] | None] = asyncio.Queue()
        self.dirty = False
        self.emit: Emit | None = None
        self.client: Any = None
        self.synced = ""
        self.closed = False
        self.search_announced = False
        self.updated = asyncio.Event()
        self.state_dir = store.root / ".retrieval"
        self.env = {
            **os.environ,
            "QMD_CONFIG_DIR": str(self.state_dir),
            "INDEX_PATH": str(self.state_dir / "index.sqlite"),
            "QMD_EMBED_MODEL": EMBED_MODEL,
            "LLAMA_LOG_LEVEL": "error",
            "GGML_LOG_LEVEL": "error",
            "GGML_BACKEND_SILENT": "1",
        }

    def fingerprint(self) -> str:
        names = ["MEMORY.md", "SCRATCHPAD.md"]
        folder = self.store.path("daily/2000-01-01.md").parent
        if folder.exists():
            names += sorted("daily/" + p.name for p in folder.glob("*.md"))
        return digest(json_text({n: digest(self.store.read(n)) for n in names}))

    def start(self, emit: Emit) -> None:
        if self.task is None and not self.closed:
            self.emit = emit
            self.task = asyncio.create_task(self._worker(), name="nexus-memory-qmd")

    def changed(self) -> None:
        self.dirty = True
        if self.state.get("status") == "ready":
            self.state = {**self.state, "status": "pending"}
        # At most one wakeup for a burst of writes.
        if self.queue.empty():
            self.queue.put_nowait(None)

    async def _status(self, status: str, **details: Any) -> None:
        self.state = {"status": status, "embedding_model": EMBED_MODEL, **details}
        self.updated.set()
        try:
            atomic_write(self.store.path(".index.json"), json_text(self.state))
        except (OSError, ValueError) as exc:
            self.state["status_write_error"] = str(exc)
        if self.emit:
            await self.emit("memory_index", self.state)

    async def _command(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *self.command,
            *args,
            "--index",
            "index",
            cwd=self.store.root,
            # Keep background embedding off the GPU occupied by the reusable MCP models.
            env={**self.env, "QMD_FORCE_CPU": "1"} if args[0] == "embed" else self.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(1800):
                output, _ = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        text = output.decode("utf-8", errors="replace")
        if process.returncode:
            raise RuntimeError(
                f"qmd {' '.join(args[:2])} exit {process.returncode}: {text[-3000:]}"
            )
        return text

    async def _setup(self) -> None:
        if not shutil.which(self.command[0]):
            raise RuntimeError("QMD not installed/on PATH; configure memory.qmd_command")
        self.store.path(".dirty")  # Validate the authority path before creating directories.
        if self.state_dir.is_symlink() or self.state_dir.is_junction():
            raise ValueError("QMD state directory must not be a link/junction")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not (self.state_dir / "index.yml").exists():
            await self._command(
                "collection", "add", str(self.store.root), "--name", COLLECTION, "--mask", MASK
            )
            if self.emit:
                await self.emit(
                    "memory_setup",
                    {"stage": "collection", "detail": "Memory collection initialized"},
                )
        # The file is owned by this integration, and must not run hooks or index other roots.
        import yaml

        config = yaml.safe_load((self.state_dir / "index.yml").read_text(encoding="utf-8"))
        self._validate_config(config)
        saved = self.store.read(".index.json")
        if saved and (self.state_dir / "index.sqlite").exists():
            try:
                previous = json.loads(saved)
                if previous.get("embedding_model") == EMBED_MODEL:
                    self.synced = previous.get("fingerprint", "")
            except (ValueError, AttributeError):
                self.synced = ""

    def _validate_config(self, config: Any) -> None:
        expected = config.get("collections", {}) if isinstance(config, dict) else {}
        collection = expected.get(COLLECTION, {})
        if (
            set(expected) != {COLLECTION}
            or collection.get("pattern") != MASK
            or Path(collection.get("path", "")).resolve() != self.store.root.resolve()
            or collection.get("update")
            or config.get("models", {}).get("embed", EMBED_MODEL) != EMBED_MODEL
            or config.get("models", {}).get("rerank", RERANK_MODEL) != RERANK_MODEL
            or config.get("models", {}).get("generate", EXPANSION_MODEL) != EXPANSION_MODEL
        ):
            raise ValueError("Unexpected private QMD collection/configuration; inspect .retrieval")

    async def _sync(self) -> None:
        self.dirty = False
        before = self.fingerprint()
        await self._status("updating")
        await self._command("update")
        await self._status("embedding")
        if self.emit:
            await self.emit(
                "memory_setup",
                {
                    "stage": "models",
                    "detail": "Preparing index / embedding model (first use may download GGUF)",
                },
            )
        await self._command("embed")
        if self.fingerprint() != before:
            self.changed()
            await self._status("pending")
            return
        self.synced = before
        if self.client is not None:
            await self._health()
            await self._status("ready", fingerprint=before)
            self.store.path(".dirty").unlink(missing_ok=True)
        else:
            await self._status("connecting", fingerprint=before)

    async def _health(self) -> None:
        result = await self.client.call_tool("status", {}, read_timeout_seconds=30)
        health = result.structured_content
        if result.is_error or not isinstance(health, dict):
            raise RuntimeError("QMD index health unavailable")
        if health.get("needsEmbedding", 1) != 0 or (
            health.get("totalDocuments", 0) > 0 and not health.get("hasVectorIndex")
        ):
            raise RuntimeError("QMD index incomplete: " + json_text(health))

    async def _worker(self) -> None:
        pending: asyncio.Future[Json] | None = None
        try:
            if self.prepare is not None:
                assert self.emit
                self.state = {"status": "preparing_dependencies"}
                self.command = await self.prepare(self.emit)
                self.env["PATH"] = (
                    str(Path(self.command[0]).parent) + os.pathsep + self.env.get("PATH", "")
                )
            await self._setup()
            self.dirty = bool(self.store.read(".dirty")) or self.synced != self.fingerprint()
            async with AsyncExitStack() as stack:
                while True:
                    if self.dirty:
                        # One debounce interval per batch; changed during sync schedules another.
                        await asyncio.sleep(0.3)
                        try:
                            await self._sync()
                        except Exception as exc:
                            await self._status("failed", detail=str(exc))
                            self.dirty = False
                    if self.client is None and self.state.get("status") != "failed":
                        self.client = await stack.enter_async_context(
                            Client(
                                StdioServerParameters(
                                    command=self.command[0],
                                    args=[*self.command[1:], "mcp", "--index", "index"],
                                    env=self.env,
                                    cwd=str(self.store.root),
                                ),
                                read_timeout_seconds=300,
                                input_required_max_rounds=0,
                            )
                        )
                        found = await self.client.list_tools()
                        if not any(
                            t.name == "query" and "query" in t.input_schema.get("properties", {})
                            for t in found.tools
                        ):
                            raise RuntimeError(
                                "QMD MCP query contract unsupported; need QMD 2.8.3+"
                            )
                        try:
                            await self._health()
                        except RuntimeError:
                            # Repair missing vectors even when the source fingerprint is unchanged.
                            await self._sync()
                        if self.dirty or self.fingerprint() != self.synced:
                            self.changed()
                            await self._status("pending")
                        else:
                            await self._status("ready", fingerprint=self.synced)
                            self.store.path(".dirty").unlink(missing_ok=True)
                    request = await self.queue.get()
                    if request is None:
                        continue
                    query, limit, pending = request
                    if pending.cancelled():
                        pending = None
                        continue
                    try:
                        if self.state.get("status") != "ready" or self.client is None:
                            raise RuntimeError(f"QMD index unavailable: {self.state}")
                        if not self.search_announced:
                            cache = (
                                Path(self.env.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                                / "qmd/models"
                            )
                            names = [
                                "hf_ggml-org_qwen3-reranker-0.6b-q8_0.gguf",
                                "hf_tobil_qmd-query-expansion-1.7B-q4_k_m.gguf",
                            ]
                            if self.emit and not all((cache / name).is_file() for name in names):
                                await self.emit(
                                    "memory_setup",
                                    {
                                        "stage": "search_models",
                                        "detail": "Preparing search models / GGUF downloads",
                                    },
                                )
                            self.search_announced = True
                        response = await self.client.call_tool(
                            "query",
                            {
                                "query": query,
                                "collections": [COLLECTION],
                                "limit": limit,
                                "rerank": True,
                            },
                            read_timeout_seconds=300,
                        )
                        if response.is_error or response.result_type != "complete":
                            raise RuntimeError("QMD query failed: " + str(response.content)[:1000])
                        result = response.structured_content
                        if not isinstance(result, dict) or not isinstance(
                            result.get("results"), list
                        ):
                            raise RuntimeError("QMD returned no structured search results")
                        # No deleted/changed source can leak through an old index response.
                        if self.fingerprint() != self.synced:
                            self.changed()
                            raise RuntimeError("Memory changed during search; indexing pending")
                        current = []
                        for hit in result["results"]:
                            name = str(hit.get("file", ""))
                            if not name.startswith(COLLECTION + "/"):
                                continue
                            name = name[len(COLLECTION) + 1 :]
                            source = self.store.read(name)
                            if not source.strip():
                                continue
                            source_id = hashlib.sha256(
                                self.store.path(name).read_bytes()
                            ).hexdigest()
                            if source.strip() and hit.get("docid") == "#" + source_id[:6]:
                                current.append(hit)
                        omitted = len(result["results"]) - len(current)
                        result = {"results": current, "discarded_stale_results": omitted}
                        if not pending.done():
                            pending.set_result(
                                {
                                    **result,
                                    "index_status": "stale" if omitted else "ready",
                                    "retrieval": "qmd-hybrid-rerank",
                                }
                            )
                    except Exception as exc:
                        if not pending.done():
                            pending.set_exception(ValueError(str(exc)))
                    finally:
                        pending = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._status("unavailable", detail=str(exc))
        finally:
            self.client = None
            if pending is not None and not pending.done():
                pending.set_exception(ValueError("QMD connection closed"))
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if request is not None and not request[2].done():
                    request[2].set_exception(ValueError("QMD connection closed"))

    async def search(self, query: str, limit: int) -> Json:
        if self.state.get("status") != "ready" or not self.task or self.task.done():
            raise ValueError(f"QMD search unavailable; index state: {self.state}")
        if self.fingerprint() != self.synced:
            self.changed()
            raise ValueError("Memory index pending; read authoritative memory or retry later")
        future: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((query, limit, future))
        async with asyncio.timeout(310):
            return await future

    async def close(self) -> None:
        self.closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def wait_ready(self) -> None:
        async with asyncio.timeout(1800):
            while self.state.get("status") != "ready" or self.client is None:
                if self.state.get("status") in {"failed", "unavailable"} or (
                    self.task is not None and self.task.done()
                ):
                    raise RuntimeError(str(self.state))
                self.updated.clear()
                await self.updated.wait()
