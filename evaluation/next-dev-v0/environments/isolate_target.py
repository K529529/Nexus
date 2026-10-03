"""Remove target copies before Agent access; scan without models or third-party imports.

Executed inside disposable Linux containers (including Python 3.6), never on the host.
The workspace is the sole source authority. Installed dependencies remain in place.
"""

import json
import os
import re
import shutil
import sys
import tarfile
import zipfile

TARGETS = {
    "pvlib": ("pvlib",),
    "matplotlib": ("matplotlib", "mpl_toolkits"),
    "django": ("django",),
    "requests": ("requests",),
    "sphinx": ("sphinx",),
    "click": ("click",),
    "pytest": ("pytest", "_pytest", "py.test"),
    "rich": ("rich",),
}
EXCLUDED = {"/proc", "/sys", "/dev", "/workspace"}
CACHES = (
    "/opt/miniconda3/pkgs",
    "/root/.cache",
    "/root/.local/share/uv",
    "/var/cache/pip",
    "/tmp",
    "/opt/nexus-generated",
)


def target_name(name, key):
    return any(re.match(r"^" + re.escape(n) + r"($|[-.])", name.lower()) for n in TARGETS[key])


def editable_metadata(path):
    """pip may install metadata, but all editable source must resolve to /workspace."""
    if not path.endswith(".dist-info"):
        return False
    try:
        with open(os.path.join(path, "direct_url.json")) as stream:
            data = json.load(stream)
        return data.get("url") == "file:///workspace" and data.get("dir_info", {}).get("editable")
    except (OSError, ValueError):
        return False


def archive_has_target(path, key):
    if path.endswith((".whl", ".zip", ".egg")):
        with zipfile.ZipFile(path) as archive:
            return any(target_name(p, key) for n in archive.namelist() for p in n.split("/"))
    if path.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".conda")):
        # Conda bundles nested compressed package archives. No package cache belongs here.
        if path.endswith(".conda"):
            return True
        with tarfile.open(path) as archive:
            return any(target_name(p, key) for n in archive for p in n.name.split("/"))
    return False


def scan(key, root="/"):
    findings, errors = [], []
    counts = {"files": 0, "archives": 0}

    def error(exc):
        errors.append(str(exc))

    for current, dirs, files in os.walk(root, topdown=True, onerror=error):
        for name in list(dirs) + files:
            path = os.path.join(current, name)
            relative = "/" + os.path.relpath(path, root).replace(os.sep, "/")
            if relative in EXCLUDED:
                if name in dirs:
                    dirs.remove(name)
                continue
            kind = None
            if relative in CACHES:
                if os.path.isdir(path) and os.listdir(path):
                    kind = "cache_or_build_copy"
            if target_name(name, key):
                # Console entrypoints contain imports only, not project implementations.
                entrypoint = relative in ("/usr/local/bin/pytest", "/usr/local/bin/py.test")
                if not editable_metadata(path) and not (key == "pytest" and entrypoint):
                    kind = "target_copy"
            if name in files:
                counts["files"] += 1
                if path.endswith(
                    (
                        ".whl",
                        ".zip",
                        ".egg",
                        ".tar",
                        ".tar.gz",
                        ".tgz",
                        ".tar.bz2",
                        ".tar.xz",
                        ".conda",
                    )
                ):
                    counts["archives"] += 1
                    try:
                        if archive_has_target(path, key):
                            kind = "archive_copy"
                    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
                        errors.append(path + ": " + str(exc))
            if kind:
                findings.append({"path": relative, "kind": kind})
                if name in dirs:
                    dirs.remove(name)
    return {"target": key, "findings": findings, "errors": errors, "scanned": counts}


def clean(key):
    if sys.platform != "linux" or not os.path.isfile("/.dockerenv"):
        raise RuntimeError("Cleanup requires a disposable Docker Linux container")
    before = scan(key)
    if before["errors"]:
        raise RuntimeError(json.dumps(before))
    for finding in before["findings"]:
        path = finding["path"]
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
            if path == "/tmp":
                os.mkdir(path, 0o1777)
                os.chmod(path, 0o1777)
        else:
            os.unlink(path)
    after = scan(key)
    if after["findings"] or after["errors"]:
        raise RuntimeError(json.dumps(after))
    return {"removed": before["findings"], "scan": after}


if __name__ == "__main__":
    key = sys.argv[2]
    if key not in TARGETS:
        raise ValueError("Unknown fixed evaluation target")
    result = clean(key) if sys.argv[1] == "clean" else scan(key)
    print(json.dumps(result, sort_keys=True))
    if result.get("findings") or result.get("errors"):
        sys.exit(1)
