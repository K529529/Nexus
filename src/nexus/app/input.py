"""Safe task editing and typeahead quarantine; no Agent or message policy."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.clipboard import ClipboardData, InMemoryClipboard
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.cursor_shapes import CursorShape, SimpleCursorShapeConfig
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, to_filter
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.typeahead import clear_typeahead, get_typeahead
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

PURPLE = "#a78bfa"
INPUT_HELP = (
    "Enter: send · Ctrl+J/F4: newline · F2: paste mode · F3: expand paste · Ctrl+C: clear/exit"
)
INPUT_STYLE = Style.from_dict({
    "frame.border": PURPLE,
    "prompt": PURPLE + " bold",
    "paste": "bg:#302044 " + PURPLE,
    "bottom-toolbar": "noreverse #888888",
    "completion-menu.completion": "bg:#241b30 #dddddd",
    "completion-menu.completion.current": "bg:#7c3aed #ffffff bold",
    "completion-menu.meta.completion": "bg:#241b30 #aaaaaa",
    "completion-menu.meta.completion.current": "bg:#7c3aed #ffffff",
})
COMMANDS = {
    "/help": "Commands and shortcuts",
    "/new": "New conversation",
    "/resume": "Choose a saved session",
    "/compact": "Compact current history",
    "/memory": "Memory status",
    "/memory on": "Enable Memory",
    "/memory off": "Disable Memory",
    "/memory status": "Memory status",
    "/skills": "List local skills",
    "/skill": "Show skill selection",
    "/skill off": "Clear selected skill",
    "/exit": "Exit safely",
}


class SlashCompleter(Completer):
    def get_completions(self, document: Document, complete_event: CompleteEvent
                        ) -> Iterator[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text or text in COMMANDS:
            return
        for command, description in COMMANDS.items():
            if command.startswith(text):
                yield Completion(command, start_position=-len(text), display_meta=description)


@dataclass
class Paste:
    text: str
    number: int
    folded: bool = True

    @property
    def label(self) -> str:
        if not self.folded:
            return self.text
        lines = self.text.count("\n") + 1 if self.text else 0
        return f"[paste #{self.number} +{lines} lines]"


class PasteDisplay(Processor):
    def __init__(self, blocks: dict[str, Paste]) -> None:
        self.blocks = blocks

    def apply_transformation(self, ti: TransformationInput) -> Transformation:
        # Ordinary input (including CJK) keeps prompt_toolkit's own cursor mapping.
        if not any(c in self.blocks for _, text, *_ in ti.fragments for c in text):
            return Transformation(ti.fragments)
        # Each paste is one editable buffer atom; only its display is expanded.
        fragments: StyleAndTextTuples = []
        source_to_display = [0]
        display_to_source: list[int] = []
        for style, text, *_ in ti.fragments:
            for char in text:
                paste = self.blocks.get(char)
                label = paste.label if paste else char
                fragments.append((style + " class:paste" if paste else style, label))
                display_to_source.extend([len(source_to_display) - 1] * len(label))
                source_to_display.append(source_to_display[-1] + len(label))
        display_to_source.append(len(source_to_display) - 1)
        return Transformation(
            fragments,
            lambda i: source_to_display[min(i, len(source_to_display) - 1)],
            lambda i: display_to_source[min(i, len(display_to_source) - 1)],
        )


class ExpandedHistory(InMemoryHistory):
    def __init__(self, expand: Callable[[str], str]) -> None:
        super().__init__()
        self.expand = expand

    def append_string(self, string: str) -> None:
        super().append_string(self.expand(string))


class ExpandedClipboard(InMemoryClipboard):
    def __init__(self, expand: Callable[[str], str]) -> None:
        super().__init__()
        self.expand = expand

    def set_data(self, data: ClipboardData) -> None:
        super().set_data(ClipboardData(self.expand(data.text), data.type))


class TaskInput:
    """Enter sends; marked paste and explicit F2 capture never execute newlines."""

    def __init__(self, prompt: PromptSession[str]) -> None:
        self.prompt = prompt
        self.prompt.app.erase_when_done = True
        if os.environ.get("INTELLIJ_TERMINAL_COMMAND_BLOCKS_REWORKED") == "1":
            # Reworked places IME preedit inline, increasing the host editor's line
            # width before Python receives any committed text (IJPL-248517).
            # Headroom prevents soft-wrap, but a right rail still shifts during
            # composition. Leave that rail unpainted; retain the Frame's top,
            # bottom, left edge, buffer, completion floats and focus.
            layout = self.prompt.app.layout
            for container in layout.walk():
                if isinstance(container, VSplit) and len(container.children) == 3:
                    left, _, right = container.children
                    if (isinstance(left, Window) and isinstance(right, Window)
                            and left.char == right.char == "│"
                            and left.style == right.style == "class:frame.border"):
                        right.char = " "
                        right.style = ""
            layout.container = VSplit([HSplit([layout.container], width=self.ime_width)])
        self.bindings = KeyBindings()
        self.blocks: dict[str, Paste] = {}
        self.capture: str | None = None
        self.number = 0
        self.approval: asyncio.Future[str] | None = None
        self.approval_text = ""
        self.history = ExpandedHistory(self.expand)
        self.clipboard = ExpandedClipboard(self.expand)

        @self.bindings.add("enter")
        def submit(event: KeyPressEvent) -> None:
            buffer = event.current_buffer
            if self.capture is not None:
                self.blocks[self.capture].text += "\n"
                return
            if buffer.complete_state is not None:
                state = buffer.complete_state
                completion = state.current_completion or state.completions[0]
                buffer.apply_completion(completion)
            elif self.expand(buffer.text).strip():
                buffer.validate_and_handle()

        @self.bindings.add("c-j")
        @self.bindings.add("f4")
        @self.bindings.add("escape", "enter")
        def newline(event: KeyPressEvent) -> None:
            if (self.capture is not None and event.key_sequence[0].key == Keys.ControlJ
                    and event.previous_key_sequence
                    and event.previous_key_sequence[-1].key == Keys.ControlM):
                return  # raw CRLF in F2 capture is a single logical newline
            self.insert("\n")

        @self.bindings.add("f2")
        def capture(event: KeyPressEvent) -> None:
            if self.capture is None:
                self.capture = self.atom("")
                event.current_buffer.insert_text(self.capture)
            else:
                self.capture = None

        @self.bindings.add("f3")
        def expand_block(event: KeyPressEvent) -> None:
            if self.capture is not None:
                self.capture = None
            buffer = event.current_buffer
            pos = buffer.cursor_position
            if buffer.text[pos:pos + 1] not in self.blocks:
                pos -= 1
            if pos >= 0 and (block := self.blocks.get(buffer.text[pos:pos + 1])):
                buffer.save_to_undo_stack()
                text = self.literal(block.text)
                buffer.document = Document(buffer.text[:pos] + text + buffer.text[pos + 1:],
                                           pos + len(text))

        @self.bindings.add(Keys.BracketedPaste)
        def paste(event: KeyPressEvent) -> None:
            self.paste(event.data)

        @self.bindings.add("c-y")
        def yank(event: KeyPressEvent) -> None:
            self.paste(self.clipboard.get_data().text)

        @self.bindings.add(Keys.Any)
        def typed(event: KeyPressEvent) -> None:
            self.insert(event.data)

        @self.bindings.add("tab")
        def tab(event: KeyPressEvent) -> None:
            if self.capture is None and event.current_buffer.text.startswith("/"):
                event.current_buffer.start_completion(select_first=True)
            else:
                self.insert("\t")

        @self.bindings.add("backspace", filter=Condition(lambda: self.capture is not None))
        def backspace(event: KeyPressEvent) -> None:
            assert self.capture is not None
            self.blocks[self.capture].text = self.blocks[self.capture].text[:-1]

        # Capture is an explicit modal operation: no history/cursor/clipboard edits
        # may delete its atom while subsequent input still appends to that paste.
        for key in ("left", "right", "up", "down", "home", "end", "delete",
                    "c-a", "c-e", "c-d", "c-k", "c-u", "c-w", "c-y", "c-_", "c-t"):
            self.bindings.add(key, filter=Condition(lambda: self.capture is not None))(
                lambda event: None
            )

        @self.bindings.add("c-c")
        @self.bindings.add("<sigint>")
        def cancel(event: KeyPressEvent) -> None:
            if event.current_buffer.text or self.capture is not None:
                event.current_buffer.reset()
                self.clear()
                event.app.key_processor.empty_queue()
                self.discard()
            else:
                event.app.exit(exception=KeyboardInterrupt)

    def ime_width(self) -> Dimension:
        columns = self.prompt.app.output.get_size().columns
        return Dimension.exact(min(columns, 100, max(40, columns - 32)))

    def clear(self) -> None:
        self.blocks.clear()
        self.capture = None
        self.number = 0

    def atom(self, text: str, *, folded: bool = True) -> str:
        # Supplementary private-use characters never leave this editor. Expanding
        # is single-pass, so literal marker text or nested atom characters stay literal.
        buffer = self.prompt.default_buffer
        used = buffer.text + text + "".join(self.history.get_strings())
        char = next(chr(i) for i in range(0xF0000, 0xFFFFE)
                    if chr(i) not in self.blocks and chr(i) not in used)
        if folded:
            self.number += 1
        self.blocks[char] = Paste(text, self.number, folded)
        return char

    def expand(self, text: str) -> str:
        return "".join(self.blocks[c].text if c in self.blocks else c for c in text)

    def literal(self, text: str) -> str:
        return "".join(self.atom(c, folded=False) if c in self.blocks else c for c in text)

    def insert(self, text: str) -> None:
        if self.capture is not None:
            self.blocks[self.capture].text += text
        else:
            self.prompt.default_buffer.insert_text(self.literal(text))

    def paste(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.capture is not None:
            self.blocks[self.capture].text += text
        elif text.count("\n") >= 3 or len(text) >= 500:
            self.prompt.default_buffer.insert_text(self.atom(text))
        else:
            self.insert(text)

    def prepare(self) -> None:
        self.prompt.default_buffer.history = self.history
        # The renderer retains previously occupied rows. Keep the input at its
        # current content height instead of stretching the frame into that space.
        for window in self.prompt.app.layout.find_all_windows():
            if (isinstance(window.content, BufferControl)
                    and window.content.buffer is self.prompt.default_buffer):
                window.dont_extend_height = to_filter(True)

    def toolbar(self) -> str:
        if self.capture is not None:
            return "PASTE MODE · Enter adds a line · F2 finishes capture · Ctrl+C clears"
        return "Enter send · F2 paste · F3 expand · F4 newline · Ctrl+C clear/exit"

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
                result = await self.prompt.prompt_async(
                    [("class:prompt", " Nexus › ")],
                    multiline=True,
                    wrap_lines=True,
                    key_bindings=self.bindings,
                    prompt_continuation=[("class:prompt", "│   ")],
                    bottom_toolbar=self.toolbar,
                    show_frame=True,
                    cursor=SimpleCursorShapeConfig(CursorShape.BLINKING_BEAM),
                    refresh_interval=0,
                    style=INPUT_STYLE,
                    input_processors=[PasteDisplay(self.blocks)],
                    completer=SlashCompleter(),
                    complete_while_typing=Condition(
                        lambda: self.capture is None
                        and self.prompt.default_buffer.text.startswith("/")
                    ),
                    clipboard=self.clipboard,
                    pre_run=self.prepare,
                    handle_sigint=False,
                )
                return self.expand(result)
        finally:
            self.clear()
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
                return
            for key in keys:
                if self.approval is None or self.approval.done():
                    continue
                # Pasted text is never an approval. Require a fresh typed word + Enter.
                if key.key == Keys.BracketedPaste:
                    self.approval_text = ""
                elif key.key == Keys.ControlM:
                    if self.approval_text in {"allow", "deny", "cancel"}:
                        self.approval.set_result(self.approval_text)
                    self.approval_text = ""
                elif key.key == Keys.ControlH:
                    self.approval_text = self.approval_text[:-1]
                elif len(key.data) == 1 and key.data.isascii() and key.data.isalpha():
                    self.approval_text = (self.approval_text + key.data.lower())[-12:]

        with source.raw_mode():
            # Preserve a Ctrl+C pressed just after submit, while discarding text.
            self.discard(interrupt)
            with source.attach(ready):
                try:
                    yield
                finally:
                    self.discard()

    async def approve(self) -> str:
        """Read an explicit decision through the already-owned busy input callback."""
        if self.prompt.app.input.closed:
            return "deny"
        self.discard()
        self.approval_text = ""
        self.approval = asyncio.get_running_loop().create_future()
        try:
            return await self.approval
        finally:
            self.approval = None
            self.approval_text = ""
            self.discard()
