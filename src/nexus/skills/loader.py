"""Bounded metadata-first loader for local and packaged Skill documents."""

from __future__ import annotations

import errno
import hashlib
import io
import tomllib
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import IO

from nexus.domain.skills import SkillLocation, SkillMetadata, SkillSource
from nexus.errors import ContextError

MAX_SKILL_FILE_BYTES = 262_144
MAX_FRONT_MATTER_BYTES = 16_384
_REQUIRED_KEYS = {"id", "name", "description", "usage_scenario", "version"}
_ALLOWED_KEYS = {*_REQUIRED_KEYS, "priority"}
_REQUIRED_HEADINGS = (
    "## Execution Principles",
    "## Recommended Tools",
    "## Workflow",
    "## Constraints",
)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    size: int
    modified_ns: int | None
    front_hash: str


class _VersionInvalid(ValueError):
    pass


class FileSkillLoader:
    def __init__(
        self,
        repository_root: Path,
        user_root: Path,
        *,
        builtin_package: str = "nexus.skills.builtin",
    ) -> None:
        self._roots = {
            SkillSource.REPOSITORY: repository_root,
            SkillSource.USER_GLOBAL: user_root,
        }
        self._builtin_package = builtin_package
        self._snapshots: ContextVar[
            dict[int, tuple[SkillMetadata, _Snapshot]] | None
        ] = ContextVar("skill_loader_snapshots", default=None)

    async def load_metadata(self, location: SkillLocation) -> SkillMetadata:
        try:
            before = self._document_state(location)
            with self._open(location) as document:
                front_bytes = _read_front_matter(document)
            after = self._document_state(location)
            if before != after:
                raise ValueError("Skill document changed during metadata scan.")
            size, modified_ns = after
            metadata = _metadata(location, front_bytes)
        except ContextError:
            raise
        except _VersionInvalid as exc:
            raise ContextError(
                f"Skill version is invalid for {location.source.value}:"
                f"{location.relative_path}.",
                code="SKILL_VERSION_INVALID",
            ) from exc
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise ContextError(
                f"Skill metadata is invalid for {location.source.value}:"
                f"{location.relative_path}.",
                code="SKILL_METADATA_INVALID",
            ) from exc
        snapshot = _Snapshot(size, modified_ns, hashlib.sha256(front_bytes).hexdigest())
        snapshots = dict(self._snapshots.get() or {})
        snapshots[id(metadata)] = (metadata, snapshot)
        self._snapshots.set(snapshots)
        return metadata

    async def load_body(self, metadata: SkillMetadata) -> str:
        snapshots = self._snapshots.get()
        stored = None if snapshots is None else snapshots.get(id(metadata))
        if stored is None or stored[0] is not metadata:
            raise ContextError(
                "Selected Skill metadata did not originate from the current metadata scan.",
                code="SKILL_BODY_LOAD_FAILED",
            )
        snapshot = stored[1]
        try:
            size, modified_ns = self._document_state(metadata.location, selected=True)
            if size != snapshot.size or modified_ns != snapshot.modified_ns:
                raise ContextError(
                    "Selected Skill changed after metadata selection.",
                    code="SKILL_BODY_LOAD_FAILED",
                )
            with self._open(metadata.location, selected=True) as document:
                content = document.read(MAX_SKILL_FILE_BYTES + 1)
            if len(content) > MAX_SKILL_FILE_BYTES:
                raise ContextError(
                    "Selected Skill exceeds the fixed document size limit.",
                    code="SKILL_BODY_LOAD_FAILED",
                )
            if self._document_state(metadata.location, selected=True) != (size, modified_ns):
                raise ContextError(
                    "Selected Skill changed while its body was being loaded.",
                    code="SKILL_BODY_LOAD_FAILED",
                )
            stream = io.BytesIO(content)
            front_bytes = _read_front_matter(stream)
            if hashlib.sha256(front_bytes).hexdigest() != snapshot.front_hash:
                raise ContextError(
                    "Selected Skill front matter changed after metadata selection.",
                    code="SKILL_BODY_LOAD_FAILED",
                )
            if _metadata(metadata.location, front_bytes) != metadata:
                raise ContextError(
                    "Selected Skill metadata changed after selection.",
                    code="SKILL_BODY_LOAD_FAILED",
                )
            body = content[stream.tell() :].decode("utf-8")
        except ContextError:
            raise
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise ContextError(
                "Selected Skill body could not be loaded safely.",
                code="SKILL_BODY_LOAD_FAILED",
                retryable=isinstance(exc, OSError) and _is_transient_io(exc),
            ) from exc
        finally:
            self._discard_snapshot(metadata)
        normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        _validate_body(normalized)
        return normalized

    def _reset_snapshots(self) -> None:
        self._snapshots.set(None)

    def _discard_snapshot(self, metadata: SkillMetadata) -> None:
        snapshots = self._snapshots.get()
        if snapshots is None:
            return
        stored = snapshots.get(id(metadata))
        if stored is None or stored[0] is not metadata:
            return
        remaining = dict(snapshots)
        del remaining[id(metadata)]
        self._snapshots.set(remaining or None)

    def _open(self, location: SkillLocation, *, selected: bool = False) -> IO[bytes]:
        if location.source is SkillSource.BUILTIN:
            resource = self._builtin_resource(location, selected=selected)
            try:
                return resource.open("rb")
            except OSError as exc:
                if selected:
                    raise ContextError(
                        "Selected builtin Skill document is unreadable.",
                        code="SKILL_BODY_LOAD_FAILED",
                        retryable=_is_transient_io(exc),
                    ) from exc
                raise ContextError(
                    "Builtin Skill document is unreadable.",
                    code="SKILL_METADATA_INVALID",
                ) from exc
        document = self._filesystem_document(location, selected=selected)
        try:
            return document.open("rb")
        except OSError as exc:
            if selected:
                raise ContextError(
                    "Selected Skill document is unreadable.",
                    code="SKILL_BODY_LOAD_FAILED",
                    retryable=_is_transient_io(exc),
                ) from exc
            raise

    def _document_state(
        self,
        location: SkillLocation,
        *,
        selected: bool = False,
    ) -> tuple[int, int | None]:
        if location.source is SkillSource.BUILTIN:
            resource = self._builtin_resource(location, selected=selected)
            try:
                if isinstance(resource, Path):
                    stat = resource.stat()
                    _validate_size(stat.st_size)
                    return stat.st_size, stat.st_mtime_ns
                if isinstance(resource, zipfile.Path):
                    size = resource.root.getinfo(resource.at).file_size
                else:
                    raise ValueError(
                        "Builtin Skill resource size cannot be determined without body I/O."
                    )
            except OSError as exc:
                if selected:
                    raise ContextError(
                        "Selected builtin Skill document is unreadable.",
                        code="SKILL_BODY_LOAD_FAILED",
                        retryable=_is_transient_io(exc),
                    ) from exc
                raise
            _validate_size(size)
            return size, None
        try:
            stat = self._filesystem_document(location, selected=selected).stat()
            _validate_size(stat.st_size)
            return stat.st_size, stat.st_mtime_ns
        except OSError as exc:
            if selected:
                raise ContextError(
                    "Selected Skill document is unreadable.",
                    code="SKILL_BODY_LOAD_FAILED",
                    retryable=_is_transient_io(exc),
                ) from exc
            raise

    def _filesystem_document(
        self,
        location: SkillLocation,
        *,
        selected: bool = False,
    ) -> Path:
        root = self._roots[location.source]
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            if selected:
                raise ContextError(
                    "Selected Skill source root is unavailable.",
                    code="SKILL_BODY_LOAD_FAILED",
                    retryable=_is_transient_io(exc),
                ) from exc
            raise ContextError(
                f"Skill path is unsafe for {location.source.value}:"
                f"{location.relative_path}.",
                code="SKILL_PATH_UNSAFE",
            ) from exc
        try:
            document = canonical_root.joinpath(*location.relative_path.split("/")).resolve(
                strict=True
            )
        except OSError as exc:
            if selected:
                raise ContextError(
                    "Selected Skill document is unavailable.",
                    code="SKILL_BODY_LOAD_FAILED",
                    retryable=_is_transient_io(exc),
                ) from exc
            raise ContextError(
                f"Skill path is unsafe for {location.source.value}:"
                f"{location.relative_path}.",
                code="SKILL_PATH_UNSAFE",
            ) from exc
        try:
            document.relative_to(canonical_root)
        except ValueError as exc:
            raise ContextError(
                f"Skill path is unsafe for {location.source.value}:{location.relative_path}.",
                code="SKILL_PATH_UNSAFE",
            ) from exc
        if not document.is_file():
            raise ContextError(
                f"Skill path is not a regular file for {location.source.value}:"
                f"{location.relative_path}.",
                code="SKILL_PATH_UNSAFE",
            )
        return document

    def _builtin_resource(
        self,
        location: SkillLocation,
        *,
        selected: bool = False,
    ) -> Traversable:
        try:
            root = files(self._builtin_package)
            resource = root.joinpath(*location.relative_path.split("/"))
            if isinstance(root, Path) and isinstance(resource, Path):
                canonical_root = root.resolve(strict=True)
                try:
                    canonical_resource = resource.resolve(strict=True)
                except OSError as exc:
                    if selected:
                        raise ContextError(
                            "Selected builtin Skill document is unavailable.",
                            code="SKILL_BODY_LOAD_FAILED",
                            retryable=_is_transient_io(exc),
                        ) from exc
                    raise
                canonical_resource.relative_to(canonical_root)
                resource = canonical_resource
            if not resource.is_file():
                if selected:
                    raise ContextError(
                        "Selected builtin Skill document is unavailable.",
                        code="SKILL_BODY_LOAD_FAILED",
                    )
                raise ValueError("Builtin Skill is not a regular package resource.")
            return resource
        except ContextError:
            raise
        except ModuleNotFoundError as exc:
            if selected:
                raise ContextError(
                    "Selected builtin Skill package is unavailable.",
                    code="SKILL_BODY_LOAD_FAILED",
                ) from exc
            raise ContextError(
                f"Builtin Skill path is unsafe: {location.relative_path}.",
                code="SKILL_PATH_UNSAFE",
            ) from exc
        except (OSError, ValueError) as exc:
            raise ContextError(
                f"Builtin Skill path is unsafe: {location.relative_path}.",
                code="SKILL_PATH_UNSAFE",
            ) from exc


def _read_front_matter(document: IO[bytes]) -> bytes:
    consumed = 0
    first = document.readline(MAX_FRONT_MATTER_BYTES + 1)
    consumed += len(first)
    if first.startswith(b"\xef\xbb\xbf"):
        first = first[3:]
    if _line_value(first) != b"+++":
        raise ValueError("Skill front matter opening delimiter is missing.")
    front_lines: list[bytes] = []
    while consumed <= MAX_FRONT_MATTER_BYTES:
        line = document.readline(MAX_FRONT_MATTER_BYTES + 1 - consumed)
        consumed += len(line)
        if not line:
            raise ValueError("Skill front matter closing delimiter is missing.")
        if consumed > MAX_FRONT_MATTER_BYTES:
            break
        if _line_value(line) == b"+++":
            return b"".join(front_lines)
        front_lines.append(line)
    raise ValueError("Skill front matter exceeds the fixed size limit.")


def _line_value(line: bytes) -> bytes:
    return line.removesuffix(b"\n").removesuffix(b"\r")


def _metadata(location: SkillLocation, front_bytes: bytes) -> SkillMetadata:
    document = tomllib.loads(front_bytes.decode("utf-8"))
    if set(document) != _ALLOWED_KEYS or not _REQUIRED_KEYS <= set(document):
        raise ValueError("Skill front matter keys are invalid.")
    skill_id = document["id"]
    name = document["name"]
    description = document["description"]
    usage_scenario = document["usage_scenario"]
    priority = document.get("priority", 0)
    version = document["version"]
    if not isinstance(version, str):
        raise _VersionInvalid("Skill version must be a string.")
    if (
        not isinstance(skill_id, str)
        or not isinstance(name, str)
        or not isinstance(description, str)
        or not isinstance(usage_scenario, str)
        or isinstance(priority, bool)
        or not isinstance(priority, int)
    ):
        raise ValueError("Skill front matter field types are invalid.")
    try:
        return SkillMetadata(
            skill_id=skill_id,
            name=name,
            description=description,
            usage_scenario=usage_scenario,
            priority=priority,
            version=version,
            source=location.source,
            location=location,
        )
    except ValueError as exc:
        if "SemVer" in str(exc):
            raise _VersionInvalid(str(exc)) from exc
        raise


def _validate_size(size: int) -> None:
    if size > MAX_SKILL_FILE_BYTES:
        raise ValueError("Skill document exceeds the fixed size limit.")


def _is_transient_io(error: OSError) -> bool:
    return error.errno in {
        errno.EINTR,
        errno.EAGAIN,
        errno.ENFILE,
        errno.EMFILE,
        errno.ETIMEDOUT,
    } or getattr(error, "winerror", None) in {32, 33}


def _validate_body(body: str) -> None:
    if not body.strip() or any(line == "+++" for line in body.splitlines()):
        raise ContextError("Selected Skill body is invalid.", code="SKILL_BODY_INVALID")
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and line.startswith("## "):
            headings.append((index, line))
    for heading in _REQUIRED_HEADINGS:
        if sum(value == heading for _, value in headings) != 1:
            raise ContextError("Selected Skill body is invalid.", code="SKILL_BODY_INVALID")
    positions = [
        next(index for index, value in headings if value == heading)
        for heading in _REQUIRED_HEADINGS
    ]
    if positions != sorted(positions) or any(
        value not in _REQUIRED_HEADINGS and index < positions[-1]
        for index, value in headings
    ):
        raise ContextError("Selected Skill body is invalid.", code="SKILL_BODY_INVALID")
    for offset, position in enumerate(positions):
        end = positions[offset + 1] if offset + 1 < len(positions) else len(lines)
        if not "\n".join(lines[position + 1 : end]).strip():
            raise ContextError("Selected Skill body is invalid.", code="SKILL_BODY_INVALID")
