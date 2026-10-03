"""Target source discovery across inactive interpreters, vendor trees and archives."""

import json
import runpy
import zipfile
from pathlib import Path

from nexus.evaluation.cases import SUITE


def test_scan_finds_inactive_vendored_and_archived_sources(tmp_path: Path) -> None:
    scan = runpy.run_path(str(SUITE / "environments/isolate_target.py"))["scan"]
    for name in (
        "opt/old/lib/site-packages/requests/__init__.py",
        "usr/lib/pip/_vendor/requests/auth.py",
        "workspace/requests/auth.py",
        "opt/active/lib/site-packages/urllib3/__init__.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# source", encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "installer.whl", "w") as archive:
        archive.writestr("pip/_vendor/requests/auth.py", "# vendored source")
    cache = tmp_path / "opt/miniconda3/pkgs"
    cache.mkdir(parents=True)
    (cache / "opaque-cache").write_bytes(b"cached source")
    result = scan("requests", str(tmp_path))
    assert not result["errors"]
    assert {f["path"] for f in result["findings"]} == {
        "/opt/old/lib/site-packages/requests",
        "/usr/lib/pip/_vendor/requests",
        "/installer.whl",
        "/opt/miniconda3/pkgs",
    }


def test_scan_allows_only_workspace_editable_metadata_and_rejects_bad_archives(
    tmp_path: Path,
) -> None:
    scan = runpy.run_path(str(SUITE / "environments/isolate_target.py"))["scan"]
    for version, location in (("1", "workspace"), ("2", "future")):
        path = tmp_path / f"site-packages/pytest-{version}.dist-info"
        path.mkdir(parents=True)
        (path / "direct_url.json").write_text(
            json.dumps({"url": f"file:///{location}", "dir_info": {"editable": True}}),
            encoding="utf-8",
        )
    (tmp_path / "source.tar.gz").write_bytes(b"unreadable archive")
    result = scan("pytest", str(tmp_path))
    assert result["findings"] == [
        {"path": "/site-packages/pytest-2.dist-info", "kind": "target_copy"}
    ]
    assert len(result["errors"]) == 1
