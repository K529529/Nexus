"""Small, content-versioned trusted patch image. Builds happen only on explicit setup."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

BASE = "python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
PROTOCOL = "nexus-patch-ack-v2"
LABEL = "io.nexus.sandbox"
FILES = (
    "__init__.py",
    "core/__init__.py",
    "core/types.py",
    "tools/__init__.py",
    "tools/patch.py",
    "tools/patch_format.py",
    "tools/patch_worker.py",
    "tools/docker_shell.py",
)
RECIPE = f"""FROM {BASE}
RUN apt-get update && apt-get install -y --no-install-recommends git bash ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==8.3.5
COPY nexus /opt/nexus
RUN chmod -R a+rX,go-w /opt/nexus && mkdir /workspace
ENV HOME=/tmp TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
USER 65532:65532
CMD ["sleep", "infinity"]
"""


def sources() -> dict[str, bytes]:
    root = Path(__file__).resolve().parents[1]
    # Normalize checkout line endings: installed Windows/Linux wheels share the same identity.
    return {name: (root / name).read_text(encoding="utf-8").encode() for name in FILES}


def fingerprint() -> str:
    digest = hashlib.sha256((PROTOCOL + RECIPE).encode())
    for name, data in sorted(sources().items()):
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()


def default_image() -> str:
    return "nexus-sandbox:" + fingerprint()[:20]


def build_context() -> tempfile.TemporaryDirectory[str]:
    directory = tempfile.TemporaryDirectory(prefix="nexus-docker-build-")
    root = Path(directory.name)
    for name, data in sources().items():
        target = root / "nexus" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root / "Dockerfile").write_text(
        RECIPE + f'LABEL {LABEL}.protocol="{PROTOCOL}" {LABEL}.code="{fingerprint()}"\n',
        encoding="utf-8",
    )
    return directory
