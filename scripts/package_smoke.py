"""Offline distribution and installed-resource checks; no model/config/bootstrap calls."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sysconfig
import tarfile
import tomllib
import zipfile
from pathlib import Path


def artifacts(root: Path, directory: Path) -> None:
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    wheel = directory / f"nexus_coding_agent-{version}-py3-none-any.whl"
    sdist = directory / f"nexus_coding_agent-{version}.tar.gz"
    source = root / "src"
    files = [
        p
        for p in (source / "nexus").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
    ]
    with zipfile.ZipFile(wheel) as built, tarfile.open(sdist) as archive:
        prefix = f"nexus_coding_agent-{version}"
        for path in files:
            name = path.relative_to(source).as_posix()
            expected = path.read_bytes()
            assert built.read(name) == expected, f"wheel missing/changed: {name}"
            member = archive.extractfile(f"{prefix}/src/{name}")
            assert member is not None and member.read() == expected, f"sdist mismatch: {name}"
        metadata = built.read(f"nexus_coding_agent-{version}.dist-info/METADATA").decode()
        assert f"\nVersion: {version}\n" in metadata
        for name in (
            "README.md",
            "README.zh-CN.md",
            "uv.lock",
            "scripts/package_smoke.py",
            "scripts/stage8_ui_smoke.py",
            "docs/next/release-0.3.0.md",
        ):
            assert archive.getmember(f"{prefix}/{name}").isfile(), name
    print(
        json.dumps(
            {
                "status": "PASS",
                "version": version,
                "package_files_verified": len(files),
                "artifacts": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel, sdist)
                },
            }
        )
    )


def installed(expected: str) -> None:
    import nexus
    from nexus.core.tokens import qwen_template, tokenizer

    assert nexus.__file__ is not None
    package = Path(nexus.__file__).resolve().parent
    assert package.is_relative_to(Path(sysconfig.get_paths()["purelib"]).resolve()), package
    assert nexus.__version__ == importlib.metadata.version("nexus-coding-agent") == expected
    required = (
        "app/roles/scout.md",
        "app/roles/worker.md",
        "app/roles/reviewer.md",
        "core/tokenizer_data/qwen.jinja",
        "core/tokenizer_data/deepseek.json.gz",
        "core/tokenizer_data/qwen.json.gz",
        "evaluation/history.html",
        "tools/srt_bridge.mjs",
        "tools/windows_native_bridge.mjs",
        "tools/windows_native_guard.mjs",
        "tools/linux_docker.py",
        "tools/patch_worker.py",
        "_vendor/pi_windows/package.json",
        "_vendor/pi_windows/runner.js",
        "_vendor/pi_windows/LICENSE",
        "_vendor/pi_windows/LICENSE.deepseek",
    )
    for name in required:
        assert (package / name).is_file() and (package / name).stat().st_size > 0, name
    for profile in ("deepseek", "qwen"):
        assert tokenizer(profile).encode("Nexus package smoke 中文").ids
    assert qwen_template() is not None
    print(
        json.dumps(
            {
                "status": "PASS",
                "version": expected,
                "package": str(package),
                "resources": len(required),
                "tokenizers": ["deepseek", "qwen"],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("artifacts")
    build.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    build.add_argument("--dist", type=Path, default=Path("dist"))
    smoke = commands.add_parser("installed")
    smoke.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    if args.command == "artifacts":
        artifacts(args.root, args.dist)
    else:
        installed(args.expected_version)


if __name__ == "__main__":
    main()
