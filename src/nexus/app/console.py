"""Keep committed display output for terminal resize, independent of model history."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from rich.console import Console
from rich.control import Control


class DisplayReplay:
    """Replay the same Rich output at the new width; transient Live frames are excluded."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.original_print = console.print
        self.entries: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        console.print = self.print  # type: ignore[method-assign]

    def print(self, *objects: Any, **kwargs: Any) -> None:
        with self.console._lock:
            if not objects or not all(isinstance(item, Control) for item in objects):
                self.entries.append((deepcopy(objects), deepcopy(kwargs)))
            self.original_print(*objects, **kwargs)

    def repaint(self) -> str:
        # Capture avoids writing via prompt_toolkit's asynchronous stdout proxy.
        # The input renderer writes the replay and draft in one serialized redraw.
        with self.console._lock, self.console.capture() as capture:
            for objects, kwargs in self.entries:
                self.original_print(*objects, **kwargs)
        return capture.get()
