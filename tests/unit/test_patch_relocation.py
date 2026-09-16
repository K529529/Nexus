"""Focused tests for unified-diff hunk relocation."""

import pytest

from nexus.errors import ToolExecutionError
from nexus.tools.editing import _apply_unified_patch


def _patch(old_start: int, body: str) -> str:
    return f"--- a/target.py\n+++ b/target.py\n@@ -{old_start},2 +{old_start},2 @@\n{body}"


def test_hunk_at_declared_line_keeps_fast_path_with_duplicate_context() -> None:
    source = "anchor\nold\nseparator\nanchor\nold\n"
    patch = _patch(4, " anchor\n-old\n+new\n")

    assert _apply_unified_patch("target.py", source, patch) == (
        "anchor\nold\nseparator\nanchor\nnew\n"
    )


def test_offset_hunk_relocates_to_unique_exact_context() -> None:
    prefix = "".join(f"line {number}\n" for number in range(1, 75))
    source = prefix + "anchor\nold\nend\n"
    patch = _patch(67, " anchor\n-old\n+new\n")

    assert _apply_unified_patch("target.py", source, patch) == prefix + "anchor\nnew\nend\n"


def test_missing_context_remains_patch_conflict() -> None:
    source = "prefix\nother\nold\n"
    patch = _patch(1, " anchor\n-old\n+new\n")

    with pytest.raises(ToolExecutionError) as error:
        _apply_unified_patch("target.py", source, patch)

    assert error.value.code == "PATCH_CONFLICT"


def test_multiple_context_matches_remain_patch_conflict() -> None:
    source = "prefix\nanchor\nold\nseparator\nanchor\nold\n"
    patch = _patch(3, " anchor\n-old\n+new\n")

    with pytest.raises(ToolExecutionError) as error:
        _apply_unified_patch("target.py", source, patch)

    assert error.value.code == "PATCH_CONFLICT"


def test_relocation_respects_no_newline_marker() -> None:
    source = "anchor\nold\nseparator\nanchor\nold"
    patch = _patch(
        1,
        " anchor\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n",
    )

    assert _apply_unified_patch("target.py", source, patch) == (
        "anchor\nold\nseparator\nanchor\nnew"
    )
