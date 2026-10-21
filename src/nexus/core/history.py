"""Continuous session history, safe groups and append-only checkpoint replay."""

from __future__ import annotations

from nexus.core.types import ContextSnapshot, Json, Message, ModelError, Session


def groups(messages: list[Message]) -> list[list[Message]]:
    grouped: list[list[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        count = 1 + len(message.tool_calls)
        group = messages[index : index + count]
        if message.role == "tool" or len(group) != count:
            raise ModelError("unpaired_context")
        for call, result in zip(message.tool_calls, group[1:], strict=True):
            if (
                result.role != "tool"
                or result.tool_call_id != call.id
                or result.run_id != message.run_id
            ):
                raise ModelError("unpaired_context")
        grouped.append(group)
        index += count
    return grouped


class ContextBuilder:
    """Run identifiers describe execution; they do not filter conversation visibility."""

    def build_active_context(self, session: Session, run_id: str | None = None) -> list[Message]:
        snapshot = session.checkpoint
        messages = list(session.messages)
        if snapshot is not None:
            messages = (
                [m for m in messages if m.role == "system"]
                + [m for m in snapshot.messages if m.role != "system"]
                + [m for m in messages if m.role != "system" and m.seq > snapshot.through_seq]
            )
        groups(messages)
        return messages

    def restore_compaction(
        self, session: Session, run_id: str | None, data: Json, seq: int
    ) -> None:
        active = self.build_active_context(session)
        scope = data.get("scope")
        if scope == "session":
            from nexus.core.compaction import summary_message, validate_summary

            summary = summary_message(validate_summary(data["summary"]), seq)
            if data.get("version") != 1:
                raise ModelError("invalid_compaction_version")
            source = [m for m in active if m.role != "system"]
            replaced, kept = data["replaced_seqs"], data["kept_seqs"]
            if (
                not replaced
                or replaced + kept != [m.seq for m in source]
                or any(type(n) is not int for n in replaced + kept)
                or len(set(replaced + kept)) != len(source)
                or seq <= max(replaced + kept, default=0)
            ):
                raise ModelError("invalid_compaction_references")
            removed = source[: len(replaced)]
            recent = source[len(replaced) :]
            groups(removed)
            groups(recent)
            if not recent:
                raise ModelError("invalid_compaction_empty_tail")
            updated = [summary] + recent
        elif scope == "run":
            # Read-only compatibility: fold old run snapshots into the one session projection.
            source = [m for m in active if m.role == "system" or m.run_id == run_id]
            keep, removed = data["kept_seqs"], data["replaced_seqs"]
            refs = keep + removed
            by_seq = {m.seq: m for m in source}
            index, text = data["summary_index"], data["summary"]
            if (
                not removed
                or any(type(n) is not int for n in refs)
                or len(set(refs)) != len(refs)
                or set(refs) != set(by_seq)
                or keep != [m.seq for m in source if m.seq not in removed]
                or type(index) is not int
                or not 0 <= index <= len(keep)
                or not isinstance(text, str)
                or not text.strip()
                or any(m.role in {"system", "user"} and m.seq in removed for m in source)
            ):
                raise ModelError("invalid_legacy_compaction")
            local = [by_seq[n] for n in keep]
            local.insert(index, Message("user", text, seq=seq, run_id=run_id))
            groups(local)
            # Map the old summary's insertion point into the complete session chronology.
            before = {m.seq for m in local[:index]}
            position = next(
                (i for i, m in enumerate(active) if m in source and m.seq not in before),
                len(active),
            )
            updated = [m for m in active[:position] if m.seq not in removed]
            updated.append(local[index])
            updated += [m for m in active[position:] if m.seq not in removed]
        else:
            raise ModelError("invalid_compaction_scope")
        groups(updated)
        session.checkpoint = ContextSnapshot(updated, seq)
