"""Verify the installed wheel against the frozen source tree without importing from it."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import nexus
from nexus.tools.docker_image import default_image, fingerprint

repo, destination = map(Path, sys.argv[1:])
if sys.platform == "linux":
    # A Windows-managed worktree's .git pointer is not a Linux path. Do not rewrite it.
    windows_manifest = json.loads((repo / "docs/next/evidence/stage4-linux-docker/windows-wheel-manifest.json").read_text())
    commit = windows_manifest["code_commit"]
else:
    windows_manifest = None
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
source = repo / "src" / "nexus"
installed = Path(nexus.__file__).parent
names = [
    "app/bootstrap.py", "app/cli.py", "app/config.py", "app/tui.py", "app/sandbox_setup.py",
    "core/security.py", "tools/patch.py", "tools/patch_worker.py", "tools/windows_native.py",
    "tools/platform_backend.py", "tools/linux_docker.py", "tools/docker_image.py", "tools/docker_shell.py",
]
rows = []
for name in names:
    data = (installed / name).read_text(encoding="utf-8").encode()
    rows.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(),
                 "matches_source": data == (source / name).read_text(encoding="utf-8").encode()})
assert all(r["matches_source"] for r in rows)
if windows_manifest:
    assert rows == windows_manifest["files"]
wheel = repo / "dist/nexus_coding_agent-0.2.0-py3-none-any.whl"
result = {"code_commit": commit, "python": sys.executable, "installed": str(installed), "files": rows,
          "image_tag": default_image(), "worker_fingerprint": fingerprint(),
          "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
print("Installed source hashes match", commit)
