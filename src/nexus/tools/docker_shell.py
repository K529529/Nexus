"""Image-owned shell transport: a missing final frame means execution is unknown.

This is not an isolation implementation. Docker owns the process/filesystem boundary;
the host destroys the container on interruption. Child stdin is always closed.
"""

from __future__ import annotations

import base64
import json
import selectors
import subprocess
import sys


def main() -> None:
    raw = sys.stdin.buffer.readline(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024 or not raw.endswith(b"\n"):
        raise ValueError("Invalid shell request size")
    request = json.loads(raw)

    def send(kind: str, **data: object) -> None:
        row = {"id": request["id"], "type": kind, **data}
        sys.stdout.write(json.dumps(row, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    with (
        subprocess.Popen(
            [request["shell"], "-c", request["command"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child,
        selectors.DefaultSelector() as selector,
    ):
        assert child.stdout is not None and child.stderr is not None
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            for key, _ in selector.select():
                stream = key.fileobj
                chunk = stream.read1(4096)  # type: ignore[union-attr]
                if chunk:
                    send("output", stream=key.data, data=base64.b64encode(chunk).decode("ascii"))
                else:
                    selector.unregister(stream)
        send("result", exit_code=child.wait())


if __name__ == "__main__":
    main()
