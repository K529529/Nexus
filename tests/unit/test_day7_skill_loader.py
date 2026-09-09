from __future__ import annotations

import asyncio
import errno
import io
from pathlib import Path
from typing import IO

import pytest

from nexus.domain.skills import SkillLocation, SkillSelectionResult, SkillSource
from nexus.errors import ContextError
from nexus.skills.loader import MAX_SKILL_FILE_BYTES, FileSkillLoader
from nexus.skills.registry import DefaultSkillRegistry


def document(
    *,
    skill_id: str = "debug-python",
    version: str = '"1.0.0"',
    priority: str = "60",
    extra: str = "",
    body: str | None = None,
) -> bytes:
    content = body or (
        "## Execution Principles\n\nReproduce from evidence.\n\n"
        "## Recommended Tools\n\nUse bounded repository tools.\n\n"
        "## Workflow\n\nInspect, diagnose, and validate.\n\n"
        "## Constraints\n\nPreserve policy and scope.\n"
    )
    return (
        "+++\n"
        f'id = "{skill_id}"\n'
        'name = "Debug Python"\n'
        'description = "Diagnose Python failures."\n'
        'usage_scenario = "Use for Python debugging."\n'
        f"priority = {priority}\n"
        f"version = {version}\n"
        f"{extra}"
        "+++\n"
        f"{content}"
    ).encode()


def location(skill_id: str = "debug-python") -> SkillLocation:
    return SkillLocation(SkillSource.REPOSITORY, f"{skill_id}/SKILL.md")


def write_skill(root: Path, content: bytes, skill_id: str = "debug-python") -> Path:
    folder = root / skill_id
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_bytes(content)
    return path


async def test_valid_metadata_then_body_load_normalizes_newlines(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    raw = b"\xef\xbb\xbf" + document().replace(b"\n", b"\r\n")
    write_skill(repository, raw)
    loader = FileSkillLoader(repository, tmp_path / "user")

    metadata = await loader.load_metadata(location())
    body = await loader.load_body(metadata)

    assert metadata.skill_id == "debug-python"
    assert metadata.priority == 60
    assert metadata.version == "1.0.0"
    assert metadata.location == location()
    assert "Execution Principles" in body
    assert "\r" not in body and body.endswith("\n")


async def test_repeated_scans_and_loads_release_snapshot_state(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, document())
    loader = FileSkillLoader(repository, tmp_path / "user")
    registry = DefaultSkillRegistry(loader, repository, tmp_path / "user")
    selection = SkillSelectionResult(("debug-python",), "Debugging guidance matches.")

    for _ in range(25):
        catalog = await registry.scan_metadata()
        assert catalog
        snapshots = loader._snapshots.get()
        assert snapshots is not None
        assert len(snapshots) == len(catalog)
        selected = await registry.load_selected(selection)
        assert selected[0].metadata.source is SkillSource.REPOSITORY
        assert loader._snapshots.get() is None


async def test_concurrent_scans_keep_run_snapshots_independent(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, document())
    loader = FileSkillLoader(repository, tmp_path / "user")
    registry = DefaultSkillRegistry(loader, repository, tmp_path / "user")
    first_scanned = asyncio.Event()
    second_scanned = asyncio.Event()

    async def first_run() -> SkillSource:
        await registry.scan_metadata()
        first_scanned.set()
        await second_scanned.wait()
        selected = await registry.load_selected(
            SkillSelectionResult(("debug-python",), "Debugging guidance matches.")
        )
        return selected[0].metadata.source

    async def second_run() -> SkillSource:
        await first_scanned.wait()
        await registry.scan_metadata()
        second_scanned.set()
        selected = await registry.load_selected(
            SkillSelectionResult(("write-tests",), "Testing guidance matches.")
        )
        return selected[0].metadata.source

    first_source, second_source = await asyncio.gather(first_run(), second_run())

    assert first_source is SkillSource.REPOSITORY
    assert second_source is SkillSource.BUILTIN


class _RecordingStream(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.body_reads = 0

    def read(self, size: int | None = -1) -> bytes:
        self.body_reads += 1
        return super().read(size)

    def __enter__(self) -> _RecordingStream:
        return self

    def __exit__(self, *args: object) -> None:
        del args


async def test_metadata_scan_stops_at_front_matter_and_retains_no_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = document(body="PRIVATE BODY MARKER\n" + document().decode().split("+++\n", 2)[2])
    stream = _RecordingStream(raw)
    loader = FileSkillLoader(tmp_path / "repository", tmp_path / "user")

    def open_document(_: SkillLocation) -> IO[bytes]:
        return stream

    monkeypatch.setattr(loader, "_open", open_document)
    monkeypatch.setattr(loader, "_document_state", lambda _: (len(raw), None))
    metadata = await loader.load_metadata(location())

    assert stream.body_reads == 0
    assert stream.tell() == raw.index(b"PRIVATE BODY MARKER")
    assert "PRIVATE BODY MARKER" not in repr(metadata)
    assert "PRIVATE BODY MARKER" not in repr(loader._snapshots)


@pytest.mark.parametrize(
    ("content", "code"),
    (
        (b"not-front-matter", "SKILL_METADATA_INVALID"),
        (document(extra='unknown = "value"\n'), "SKILL_METADATA_INVALID"),
        (document(priority="true"), "SKILL_METADATA_INVALID"),
        (document(priority="101"), "SKILL_METADATA_INVALID"),
        (document(version='"1.0"'), "SKILL_VERSION_INVALID"),
        (document(version="1"), "SKILL_VERSION_INVALID"),
        (document() + b"x" * MAX_SKILL_FILE_BYTES, "SKILL_METADATA_INVALID"),
    ),
    ids=(
        "missing-delimiter",
        "unknown-field",
        "priority-bool",
        "priority-out-of-range",
        "version-non-semver",
        "version-wrong-type",
        "oversized-document",
    ),
)
async def test_invalid_metadata_maps_to_exact_code(
    tmp_path: Path,
    content: bytes,
    code: str,
) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, content)
    with pytest.raises(ContextError) as caught:
        await FileSkillLoader(repository, tmp_path / "user").load_metadata(location())
    assert caught.value.code == code


async def test_invalid_utf8_and_directory_id_mismatch_are_metadata_errors(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, document()[:-1] + b"\xff")
    loader = FileSkillLoader(repository, tmp_path / "user")
    metadata = await loader.load_metadata(location())
    with pytest.raises(ContextError) as changed:
        await loader.load_body(metadata)
    assert changed.value.code == "SKILL_BODY_LOAD_FAILED"

    other = tmp_path / "other"
    write_skill(other, document(skill_id="write-tests"), "debug-python")
    with pytest.raises(ContextError) as mismatch:
        await FileSkillLoader(other, tmp_path / "user").load_metadata(location())
    assert mismatch.value.code == "SKILL_METADATA_INVALID"


@pytest.mark.parametrize(
    "body",
    (
        "",
        "## Execution Principles\n\none\n",
        (
            "## Recommended Tools\n\ntools\n## Execution Principles\n\nprinciples\n"
            "## Workflow\n\nflow\n## Constraints\n\nconstraints\n"
        ),
        (
            "## Execution Principles\n\none\n## Execution Principles\n\ntwo\n"
            "## Recommended Tools\n\ntools\n## Workflow\n\nflow\n"
            "## Constraints\n\nconstraints\n"
        ),
    ),
)
async def test_invalid_selected_body_uses_body_invalid(tmp_path: Path, body: str) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, document(body=body) if body else document(body=" "))
    loader = FileSkillLoader(repository, tmp_path / "user")
    metadata = await loader.load_metadata(location())
    with pytest.raises(ContextError) as caught:
        await loader.load_body(metadata)
    assert caught.value.code == "SKILL_BODY_INVALID"


async def test_changed_document_and_foreign_metadata_fail_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    path = write_skill(repository, document())
    loader = FileSkillLoader(repository, tmp_path / "user")
    metadata = await loader.load_metadata(location())
    path.write_bytes(document(body="## Execution Principles\n\nchanged\n"))
    with pytest.raises(ContextError) as changed:
        await loader.load_body(metadata)
    assert changed.value.code == "SKILL_BODY_LOAD_FAILED"

    second_loader = FileSkillLoader(repository, tmp_path / "user")
    with pytest.raises(ContextError) as foreign:
        await second_loader.load_body(metadata)
    assert foreign.value.code == "SKILL_BODY_LOAD_FAILED"


async def test_selected_document_missing_is_nonretryable_body_load_failure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    path = write_skill(repository, document())
    loader = FileSkillLoader(repository, tmp_path / "user")
    metadata = await loader.load_metadata(location())
    path.unlink()
    with pytest.raises(ContextError) as caught:
        await loader.load_body(metadata)
    assert caught.value.code == "SKILL_BODY_LOAD_FAILED"
    assert not caught.value.retryable


async def test_mutation_during_selected_body_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, document())
    loader = FileSkillLoader(repository, tmp_path / "user")
    metadata = await loader.load_metadata(location())
    snapshot = loader._document_state(location())
    states = iter((snapshot, (snapshot[0] + 1, snapshot[1])))

    def changing_state(
        _: SkillLocation,
        *,
        selected: bool = False,
    ) -> tuple[int, int | None]:
        del selected
        return next(states)

    monkeypatch.setattr(loader, "_document_state", changing_state)
    with pytest.raises(ContextError) as caught:
        await loader.load_body(metadata)
    assert caught.value.code == "SKILL_BODY_LOAD_FAILED"


@pytest.mark.parametrize(
    ("number", "retryable"),
    ((errno.EAGAIN, True), (errno.EACCES, False)),
)
async def test_selected_io_retryability_is_limited_to_transient_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    number: int,
    retryable: bool,
) -> None:
    repository = tmp_path / "repository"
    write_skill(repository, document())
    loader = FileSkillLoader(repository, tmp_path / "user")
    metadata = await loader.load_metadata(location())

    def fail(
        _: SkillLocation,
        *,
        selected: bool = False,
    ) -> tuple[int, int | None]:
        del selected
        raise OSError(number, "fixture I/O failure")

    monkeypatch.setattr(loader, "_document_state", fail)
    with pytest.raises(ContextError) as caught:
        await loader.load_body(metadata)
    assert caught.value.code == "SKILL_BODY_LOAD_FAILED"
    assert caught.value.retryable is retryable


async def test_external_symlink_escape_is_path_unsafe(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(document())
    repository.mkdir()
    link = repository / "debug-python"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are not available on this platform.")
    with pytest.raises(ContextError) as caught:
        await FileSkillLoader(repository, tmp_path / "user").load_metadata(location())
    assert caught.value.code == "SKILL_PATH_UNSAFE"


async def test_non_regular_skill_document_is_path_unsafe(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "debug-python" / "SKILL.md").mkdir(parents=True)
    with pytest.raises(ContextError) as caught:
        await FileSkillLoader(repository, tmp_path / "user").load_metadata(location())
    assert caught.value.code == "SKILL_PATH_UNSAFE"
