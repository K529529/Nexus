"""One local estimator over the actual Chat Completions message/tool shapes."""

from __future__ import annotations

import math

from nexus.core.types import Json, Message, ToolSpec, json_text


def wire_message(message: Message) -> Json:
    wire: Json = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": c.arguments_json,
                },
            }
            for c in message.tool_calls
        ]
    if message.tool_call_id is not None:
        wire["tool_call_id"] = message.tool_call_id
    if message.protocol_data:
        fields = message.protocol_data.get("fields", {})
        if isinstance(fields, dict) and isinstance(fields.get("reasoning_content"), str):
            wire["reasoning_content"] = fields["reasoning_content"]
    return wire


def wire_tools(tools: list[ToolSpec]) -> list[Json]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def estimate(messages: list[Message], tools: list[ToolSpec]) -> int:
    request = {"messages": [wire_message(m) for m in messages]}
    if tools:
        request["tools"] = wire_tools(tools)
    return math.ceil(len(json_text(request).encode("utf-8")) / 3) + 8 * len(messages)
