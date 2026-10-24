"""Safe task editing and typeahead quarantine; no Agent or message policy."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from prompt_toolkit import PromptSession
from prompt_toolkit.input.typeahead import clear_typeahead, get_typeahead
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout


class TaskInput:
    """Enter always edits: unbracketed paste cannot accidentally submit a task."""

    def __init__(self, prompt: PromptSession[str]) -> None:
        self.prompt = prompt
        self.prompt.app.erase_when_done = True
        self.bindings = KeyBindings()

        @self.bindings.add("enter")
        @self.bindings.add("c-j")
        def newline(event: KeyPressEvent) -> None:
            # Raw CRLF is one logical newline too, without auto-indenting pasted code.
            previous = event.previous_key_sequence
            if (event.key_sequence[0].key == Keys.ControlJ and previous
                    and previous[-1].key == Keys.ControlM):
                return
            event.current_buffer.insert_text("\n")

        @self.bindings.add("tab")
        def tab(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\t")

        @self.bindings.add("escape", "enter")
        def submit(event: KeyPressEvent) -> None:
            if event.current_buffer.text.strip():
                event.current_buffer.validate_and_handle()

        @self.bindings.add("c-c")
        @self.bindings.add("<sigint>")
        def cancel(event: KeyPressEvent) -> None:
            if event.current_buffer.text:
                event.current_buffer.reset()
                # Do not turn queued paste/submit keys into a second draft or run.
                event.app.key_processor.empty_queue()
                self.discard()
            else:
                event.app.exit(exception=KeyboardInterrupt)

    def discard(self, interrupt: Callable[[], None] | None = None) -> None:
        source = self.prompt.app.input
        keys = get_typeahead(source)
        if not source.closed:
            keys.extend(source.read_keys())
            keys.extend(source.flush_keys())
        if interrupt and any(key.key == Keys.ControlC for key in keys):
            interrupt()

    async def read(self) -> str:
        loop = asyncio.get_running_loop()
        active = True

        def deliver() -> None:
            if active:
                self.prompt.app.key_processor.send_sigint()

        def interrupt(signum: int, frame: object) -> None:
            loop.call_soon_threadsafe(deliver)

        # Windows prompt_toolkit cannot install an asyncio signal handler.
        # Use the same draft/idle binding for both a key and an OS signal.
        previous = signal.signal(signal.SIGINT, interrupt)
        try:
            with patch_stdout(raw=True):
                return await self.prompt.prompt_async(
                    "You › ",
                    multiline=True,
                    wrap_lines=True,
                    key_bindings=self.bindings,
                    prompt_continuation="    · ",
                    bottom_toolbar="Enter: newline · Esc then Enter: submit · Ctrl+C: clear / exit",
                    handle_sigint=False,
                )
        finally:
            active = False
            signal.signal(signal.SIGINT, previous)

    @contextmanager
    def busy(self, interrupt: Callable[[], None]) -> Iterator[None]:
        """Discard input during execution/cleanup, forwarding only Ctrl+C once upstream.

        The caller owns cancellation. In particular, this does not start another
        task around Conversation (MCP contexts must retain their owning task).
        """
        source = self.prompt.app.input
        if source.closed:
            clear_typeahead(source)
            yield
            return

        def ready() -> None:
            keys = source.read_keys()
            if any(key.key == Keys.ControlC for key in keys):
                interrupt()

        with source.raw_mode():
            # Preserve a Ctrl+C pressed just after submit, while discarding text.
            self.discard(interrupt)
            with source.attach(ready):
                try:
                    yield
                finally:
                    self.discard()
