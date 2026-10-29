"""Offline install of the built wheel outside the source tree; no user environment edits."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[4]
wheel = root / "dist/nexus_coding_agent-0.2.0-py3-none-any.whl"
uv = shutil.which("uv")
assert uv and wheel.is_file()

with tempfile.TemporaryDirectory(prefix="nexus-stage6-wheel-") as directory:
    target = Path(directory).resolve()
    assert target.parent == Path(tempfile.gettempdir()).resolve()
    assert not target.is_relative_to(root)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    subprocess.run([uv, "venv", "--python", sys.executable, str(target / "env")],
                   cwd=target, env=env, check=True)
    python = target / ("env/Scripts/python.exe" if os.name == "nt" else "env/bin/python")
    subprocess.run([uv, "pip", "install", "--offline", "--python", str(python), str(wheel)],
                   cwd=target, env=env, check=True)
    probe = """
import json, pathlib, sys
import nexus
from nexus.app.tool_recovery import recover_tools
from nexus.core.durable import prepared
from nexus.core.types import ToolCall
from nexus.tools import patch_worker
assert pathlib.Path(nexus.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix))
assert prepared(ToolCall('c', 'exec_command', '{}'))['phase'] == 'PREPARED'
assert pathlib.Path(patch_worker.__file__).is_file()
print(json.dumps({'installed_from_wheel': True, 'package': nexus.__file__,
                  'recovery_import': recover_tools.__name__}))
"""
    subprocess.run([str(python), "-I", "-c", probe], cwd=target, env=env, check=True)
    subprocess.run([str(python), "-I", "-m", "nexus", "--help"],
                   cwd=target, env=env, check=True, stdout=subprocess.DEVNULL)
print(json.dumps({"wheel": str(wheel), "status": "PASS", "temporary_environment_removed": True}))
