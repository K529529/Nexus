"""Six model-driven memory tools; storage works independently of retrieval."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date

from nexus.app.memory import ENTRY, MAX_CONTENT, MemoryStore
from nexus.core.types import Emit, ExecutionContext, Json, Tool, ToolResult, ToolSpec, json_text

TEXT = {"type": "string", "minLength": 1}
TARGET = {
    "target": {"type": "string", "enum": ["memory", "daily", "scratchpad"]},
    "date": {"type": "string", "description": "Daily date YYYY-MM-DD; defaults to today"},
}
IDENTITY = {"id": TEXT, "revision": TEXT}


def spec(name: str, description: str, fields: Json, required: list[str]) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        {
            "type": "object",
            "properties": fields,
            "required": required,
            "additionalProperties": False,
        },
    )


SPECS = [
    spec(
        "memory_write",
        "Save valuable durable facts/preferences/engineering lessons or daily "
        "notes across sessions. Include project names when relevant. Append by default; to "
        "correct an entry, first memory_read then supply its exact id and revision. Never "
        "save secrets. Ordinary writes do not refresh the current session snapshot.",
        {**TARGET, **IDENTITY, "content": {**TEXT, "maxLength": MAX_CONTENT}},
        ["content"],
    ),
    spec(
        "memory_read",
        "Read authoritative memory, including IDs/revisions for safe updates. "
        "Use offset/limit to page large files. Missing memory is empty, not inferred.",
        {
            **TARGET,
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        [],
    ),
    spec(
        "memory_search",
        "Search global memory with QMD BM25 + embedding + RRF + reranking. "
        "Indexing is asynchronous; inspect index status. Read originals to confirm current facts.",
        {"query": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        ["query"],
    ),
    spec(
        "memory_forget",
        "Recoverably delete exactly one entry read by ID and revision. "
        "Returns a recovery_id. No fuzzy matching or whole-file deletion.",
        {**TARGET, **IDENTITY},
        ["id", "revision"],
    ),
    spec(
        "memory_restore",
        "Restore one recovery_id without overwriting later writes. "
        "A conflicting existing ID is rejected; repeated restores are idempotent.",
        {"recovery_id": TEXT},
        ["recovery_id"],
    ),
    spec(
        "scratchpad",
        "Cross-session tasks: add/list/complete/reopen/remove. Mutations of "
        "existing items require exact id and revision from list; remove is recoverable. "
        "Clean up completed items individually by ID, never clear the whole file.",
        {
            "action": {"type": "string", "enum": ["add", "list", "complete", "reopen", "remove"]},
            "content": {**TEXT, "maxLength": MAX_CONTENT},
            **IDENTITY,
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ["action"],
    ),
]


def filename(args: Json) -> str:
    target = args.get("target", "memory")
    if target == "daily":
        day = args.get("date", date.today().isoformat())
        if not isinstance(day, str):
            raise ValueError("date must be YYYY-MM-DD")
        return f"daily/{date.fromisoformat(day).isoformat()}.md"
    if "date" in args:
        raise ValueError("date only applies to daily")
    if target not in {"memory", "scratchpad"}:
        raise ValueError("target must be memory/daily/scratchpad")
    return "MEMORY.md" if target == "memory" else "SCRATCHPAD.md"


def validate(args: Json, schema: ToolSpec) -> None:
    fields = schema.input_schema["properties"]
    if args.keys() - fields.keys() or set(schema.input_schema["required"]) - args.keys():
        raise ValueError("Missing required or unknown memory arguments")
    for key, value in args.items():
        rule = fields[key]
        if rule["type"] == "string":
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be nonblank text")
            if len(value) > rule.get("maxLength", MAX_CONTENT):
                raise ValueError(f"{key} too long")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"Invalid {key}")
        elif type(value) is not int or not rule.get("minimum", 0) <= value <= rule.get(
            "maximum", 10**9
        ):
            raise ValueError(f"Invalid integer {key}")


def create_memory_tools(
    store: MemoryStore,
    *,
    search: Callable[[str, int], Awaitable[Json]] | None = None,
    changed: Callable[[], None] | None = None,
    invalidate: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Tool]:
    def make(schema: ToolSpec) -> Tool:
        async def execute(args: Json, context: ExecutionContext, emit: Emit) -> ToolResult:
            try:
                validate(args, schema)
                name = schema.name
                if name == "memory_search":
                    if search is None:
                        return ToolResult(
                            context.call_id,
                            False,
                            {"detail": "QMD unavailable; use memory_read"},
                            "memory_search_unavailable",
                        )
                    data = await search(args["query"], args.get("limit", 5))
                elif name == "memory_read":
                    data = store.inspect(filename(args))
                    # Page entries, not arbitrary byte slices that destroy IDs/revisions.
                    offset, limit = args.get("offset", 0), args.get("limit", 20)
                    all_entries = data.pop("entries")
                    content = data.pop("content")
                    data.update(
                        entries=all_entries[offset : offset + limit], total=len(all_entries)
                    )
                    unmanaged = ENTRY.sub("", content).strip()
                    if unmanaged:
                        raw = unmanaged.encode("utf-8")
                        cap = max(128, context.output_limit_bytes // 4)
                        data["unmanaged_content"] = raw[:cap].decode("utf-8", errors="ignore")
                        data["unmanaged_truncated"] = len(raw) > cap
                elif name == "memory_write":
                    target = filename(args)
                    if target == "SCRATCHPAD.md":
                        raise ValueError("Use scratchpad for task writes")
                    data = store.write(
                        target, args["content"], args.get("id"), args.get("revision")
                    )
                elif name == "memory_forget":
                    data = store.forget(filename(args), args["id"], args["revision"])
                elif name == "memory_restore":
                    data = store.restore(args["recovery_id"])
                else:
                    if args["action"] != "list" and ("offset" in args or "limit" in args):
                        raise ValueError("offset/limit only apply to scratchpad list")
                    data = store.scratchpad(
                        args["action"],
                        args.get("content", ""),
                        args.get("id"),
                        args.get("revision"),
                    )
                    if args["action"] == "list":
                        data.pop("content")
                        all_items = data["entries"]
                        offset, limit = args.get("offset", 0), args.get("limit", 20)
                        data["entries"] = all_items[offset : offset + limit]
                        data["total"] = len(all_items)
                mutated = name not in {"memory_search", "memory_read"} and not (
                    name == "scratchpad" and args["action"] == "list"
                )
                if mutated:
                    data["index_status"] = "pending"
                    if changed:
                        changed()
                    if invalidate:
                        await invalidate()
                truncated = False
                if "entries" in data:
                    selected: list[Json] = []
                    for entry in data["entries"]:
                        item = dict(entry)
                        raw = item["content"].encode("utf-8")
                        budget = max(128, context.output_limit_bytes // 2)
                        if len(raw) > budget:
                            item["content"] = raw[:budget].decode("utf-8", errors="ignore")
                            item["content_truncated"] = True
                            truncated = True
                        if selected and len(json_text(selected + [item]).encode()) > budget:
                            truncated = True
                            break
                        selected.append(item)
                    data["entries"] = selected
                    data["next_offset"] = (
                        args.get("offset", 0) + len(selected)
                        if args.get("offset", 0) + len(selected) < data.get("total", len(selected))
                        else None
                    )
                return ToolResult(context.call_id, True, data, truncated=truncated)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return ToolResult(context.call_id, False, {"detail": str(exc)}, "memory_error")

        return Tool(schema, execute)

    return {s.name: make(s) for s in SPECS}
