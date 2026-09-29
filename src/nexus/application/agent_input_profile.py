"""Safe local projection of the exact messages sent to an Agent model call."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict

from nexus.context.manager import model_input_tokens
from nexus.domain.model import ModelMessage

_TOOL_NAMES = (
    "read_file", "search_files", "lexical_search", "edit_file", "write_file", "shell",
)
_KNOWN_TOOL_NAMES = frozenset((*_TOOL_NAMES, "apply_patch", "list_files", "git_status", "git_diff"))
_KNOWN_ERROR_CODES = frozenset({
    "PLAN_SCOPE_DENIED", "PERMISSION_DENIED", "COMMAND_DENIED",
    "COMMAND_EXIT_NONZERO", "SANDBOX_EXECUTION_ERROR", "TOOL_EXECUTION_ERROR",
    "INVALID_TOOL_ARGUMENTS", "TOOL_NOT_FOUND", "FILE_NOT_FOUND",
    "FILE_ALREADY_EXISTS", "UNSUPPORTED_FILE", "WORKSPACE_PATH_DENIED",
    "EDIT_TARGET_NOT_FOUND", "EDIT_TARGET_AMBIGUOUS", "EDIT_NO_CHANGES",
    "PATCH_CONFLICT", "PATCH_NO_CHANGES", "INVALID_PATCH",
    "MCP_CALL_FAILED", "MCP_TOOL_ERROR", "MCP_INVALID_RESULT", "MCP_INVALID_SCHEMA",
})
_SAFE_PATH = re.compile(r"[A-Za-z0-9_.\-/]{1,512}\Z")
_SENSITIVE_PATH = re.compile(r"(?i)(secret|private|password|token|credential|api[_-]?key)")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_FILE_RANGE = re.compile(r"\blines (\d+)-(\d+)\b")
_READ_HEADER = re.compile(
    r"^read_file (?P<path>.*?) start_line=(?P<requested_start>\d+) "
    r"end_line=(?P<requested_end>\d+) truncated=(?P<source_truncated>true|false) "
    r"visible_start_line=(?P<visible_start>\d+) "
    r"visible_end_line=(?P<visible_end>\d+) "
    r"observation_truncated=(?P<observation_truncated>true|false) "
    r"next_start_line=(?P<next_start>None|\d+) "
    r"line_content_truncated=(?P<line_truncated>true|false) "
    r"continuation_unavailable=(?P<continuation_unavailable>true|false):"
)


def project_agent_model_input(
    messages: Sequence[ModelMessage], *, max_model_input_tokens: int | None,
) -> tuple[str, ...]:
    """Project only structural facts from the final, serialized model messages."""

    serialized = json.dumps([asdict(message) for message in messages], ensure_ascii=False)
    budget = "unknown" if max_model_input_tokens is None else str(max_model_input_tokens)
    lines = [
        f"  input: messages={len(messages)} estimated_tokens={model_input_tokens(messages)} "
        f"serialized_chars={len(serialized)} max_model_input_tokens={budget}"
    ]
    system = "\n".join(message.content for message in messages if message.role == "system")
    visible_flags = []
    for name in _TOOL_NAMES:
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
        visible_flags.append(f"{name}={'yes' if re.search(pattern, system) else 'no'}")
    visible = " ".join(visible_flags)
    lines.append(f"  tool_visible: {visible}")
    payload = _final_agent_payload(messages)
    if payload is None:
        lines.append("  diagnostic_parse_error=true")
        return tuple(lines)
    plan = payload.get("plan")
    selected = payload.get("selected_files")
    observations = payload.get("observations")
    if (
        not isinstance(plan, dict)
        or not isinstance(selected, list)
        or not isinstance(observations, list)
    ):
        lines.append("  diagnostic_parse_error=true")
        return tuple(lines)

    requirement = plan.get("completion_requirement")
    if requirement not in {
        "WORKSPACE_CHANGE_REQUIRED", "WORKSPACE_CHANGE_NOT_REQUIRED", "UNSPECIFIED"
    }:
        requirement = "unknown"
    version = _nonnegative_int(plan.get("version"))
    steps = plan.get("steps")
    if not isinstance(steps, list):
        steps = []
    plan_tools = [
        _safe_name(step.get("tool_name"))
        for step in steps if isinstance(step, dict) and step.get("tool_name") is not None
    ]
    lines.append(
        f"  completion={requirement} plan_version={version} plan_step_count={len(steps)}"
    )
    lines.append(f"  plan_tools: {', '.join(plan_tools) if plan_tools else '(none)'}")
    active = plan.get("active_step")
    active_sequence = (
        _nonnegative_int(active.get("sequence"))
        if isinstance(active, dict) else "none"
    )
    statuses = []
    for step in steps[:32]:
        if not isinstance(step, dict):
            continue
        sequence = _nonnegative_int(step.get("sequence"))
        status = step.get("status")
        if not isinstance(status, str) or status not in {
            "PENDING", "IN_PROGRESS", "COMPLETED", "SKIPPED", "FAILED",
        }:
            status = "unknown"
        statuses.append(f"{sequence}:{status}")
    omitted = max(0, len(steps) - 32)
    lines.append(
        f"  plan_progress: active={active_sequence} "
        f"statuses={','.join(statuses) if statuses else '(none)'} "
        f"omitted={omitted}"
    )

    selected_windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    lines.append("  selected_files:")
    for item in selected:
        if not isinstance(item, dict):
            continue
        path = _safe_path(item.get("path"))
        content = item.get("content")
        content = content if isinstance(content, str) else ""
        reason = item.get("discovery_reason")
        match = _FILE_RANGE.search(reason) if isinstance(reason, str) else None
        range_text = "unknown"
        if match is not None:
            start, end = int(match[1]), int(match[2])
            if 0 < start <= end:
                range_text = f"{start}-{end}"
                selected_windows[path].append((start, end))
        lines.append(
            f"    {path} lines={range_text} chars={len(content)} "
            f"line_count={len(content.splitlines())}"
        )
    if not selected:
        lines.append("    (none)")
    lines.extend(_coverage_lines("selected_file_coverage", selected_windows))

    compacted = payload.get("compacted_observations")
    compacted_text = compacted if isinstance(compacted, str) else ""
    compacted_present = bool(compacted_text)
    lines.append(
        f"  recent_observations={len(observations)} "
        f"compacted_present={'yes' if compacted_present else 'no'}"
    )
    recent_windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    read_count = 0
    for index, item in enumerate(observations, start=1):
        if not isinstance(item, dict):
            lines.append(f"    [{index}] unknown evidence_chars=0")
            continue
        tool = _safe_name(item.get("tool_name"))
        success = item.get("success")
        outcome = "PASS" if success is True else "FAIL" if success is False else "UNKNOWN"
        error = item.get("error_code")
        if error is None:
            error_text = "none"
        elif isinstance(error, str) and error in _KNOWN_ERROR_CODES:
            error_text = error
        else:
            error_text = "<redacted error>"
        evidence = item.get("evidence_summary")
        evidence = evidence if isinstance(evidence, str) else ""
        path = _safe_path(item.get("target_path"))
        summary = (
            f"    [{index}] {tool} {outcome} error={error_text} "
            f"target={path} evidence_chars={len(evidence)}"
        )
        if tool == "read_file":
            read_count += 1
            parsed = _read_header(evidence.partition("\n")[0])
            if parsed is not None:
                header_path, start, end, read_metadata = parsed
                summary += f" path={header_path} {read_metadata}"
                recent_windows[header_path].append((start, end))
            else:
                summary += " read_header_unavailable=true"
        lines.append(summary)
    lines.extend(_coverage_lines("recent_read_coverage", recent_windows))

    compacted_headers = []
    compacted_read_count = 0
    for line in compacted_text.splitlines():
        if not line.startswith("read_file:"):
            continue
        compacted_read_count += 1
        marker, separator, header = line.partition("evidence=")
        if not separator or not marker:
            continue
        parsed = _read_header(header)
        if parsed is not None:
            path, start, end, _ = parsed
            compacted_headers.append(
                f"    compacted read_file path={path} visible={start}-{end} compacted=true"
            )
    lines.append(
        f"  compacted: present={'yes' if compacted_present else 'no'} "
        f"chars={len(compacted_text)} lines={len(compacted_text.splitlines())} "
        f"read_headers={len(compacted_headers)} "
        f"compacted_contains_read_count={compacted_read_count}"
    )
    lines.extend(compacted_headers)

    latest = observations[-1] if observations else None
    latest_id = latest.get("invocation_id") if isinstance(latest, dict) else None
    latest_evidence = latest.get("evidence_summary") if isinstance(latest, dict) else None
    if not isinstance(latest_id, str) or not _UUID.fullmatch(latest_id):
        latest_id = None
    occurrences = serialized.count(latest_id) if latest_id is not None else 0
    latest_chars = len(latest_evidence) if isinstance(latest_evidence, str) else 0
    lines.append(
        f"  latest_observation: id={latest_id[:8] if latest_id else 'none'} "
        f"occurrences={occurrences} evidence_chars={latest_chars}"
    )
    multiple = sum(len(windows) > 1 for windows in recent_windows.values())
    lines.append(
        f"  continuity: recent_reads={read_count} compacted_reads={compacted_read_count} "
        f"target_paths_with_multiple_windows={multiple} "
        f"latest_observation_present_once={'yes' if occurrences == 1 else 'no'}"
    )
    return tuple(lines)


def _final_agent_payload(messages: Sequence[ModelMessage]) -> dict[str, object] | None:
    for message in reversed(messages):
        if message.role != "user":
            continue
        try:
            value = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and "plan" in value and "observations" in value:
            return value
    return None


def _read_header(value: str) -> tuple[str, int, int, str] | None:
    match = _READ_HEADER.match(value)
    if match is None:
        return None
    path = _safe_path(match["path"])
    start, end = int(match["visible_start"]), int(match["visible_end"])
    if path == "<redacted path>" or not (0 < start <= end):
        return None
    next_start = match["next_start"]
    metadata = (
        f"requested={match['requested_start']}-{match['requested_end']} "
        f"visible={start}-{end} source_truncated={match['source_truncated']} "
        f"observation_truncated={match['observation_truncated']} "
        f"next_start_line={next_start} line_content_truncated={match['line_truncated']} "
        f"continuation_unavailable={match['continuation_unavailable']}"
    )
    return path, start, end, metadata


def _coverage_lines(
    label: str, windows_by_path: dict[str, list[tuple[int, int]]],
) -> list[str]:
    lines = [f"  {label}:"]
    if not windows_by_path:
        return [*lines, "    (none)"]
    for path, windows in sorted(windows_by_path.items()):
        ordered = sorted(windows)
        merged: list[tuple[int, int]] = []
        overlap = False
        for start, end in ordered:
            if merged and start <= merged[-1][1]:
                overlap = True
            if merged and start <= merged[-1][1] + 1:
                previous_start, previous_end = merged[-1]
                merged[-1] = (previous_start, max(previous_end, end))
            else:
                merged.append((start, end))
        lines.append(
            f"    {path} windows={_ranges(ordered)} merged={_ranges(merged)} "
            f"has_overlap={'yes' if overlap else 'no'}"
        )
    return lines


def _ranges(intervals: list[tuple[int, int]]) -> str:
    return ",".join(f"{start}-{end}" for start, end in intervals)


def _safe_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _SAFE_PATH.fullmatch(value)
        or _SENSITIVE_PATH.search(value)
    ):
        return "<redacted path>"
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        return "<redacted path>"
    return value


def _safe_name(value: object) -> str:
    return value if isinstance(value, str) and value in _KNOWN_TOOL_NAMES else "<other tool>"


def _nonnegative_int(value: object) -> str:
    return str(value) if type(value) is int and value >= 0 else "unknown"
