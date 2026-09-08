"""Inline completion state and a native-only screen projection, with no I/O effects.

The gateway remains the only writer. Restore native cells before relaying native
output, then repaint only at a complete terminal-token boundary. Local display
never enters the native projection, editor, or scrollback.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import pyte
from pyte import graphics, modes
from wcwidth import wcswidth, wcwidth


@dataclass(frozen=True)
class TerminalCompletionState:
    draft: str
    native_prefix: str
    completion_text: str
    helper_text: str

    def serialize(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def deserialize(cls, payload: str) -> TerminalCompletionState:
        fields = json.loads(payload)
        if not isinstance(fields, dict) or not all(isinstance(value, str) for value in fields.values()):
            raise ValueError("completion state requires text fields")
        return cls(**fields)


class NativeTerminalProjection:
    """Frame output independently of read sizes; opaque strings never become screen text."""

    def __init__(self, columns: int, rows: int) -> None:
        self.screen = pyte.Screen(columns, rows)
        self._stream = pyte.ByteStream(self.screen)
        self._sequence = bytearray()
        self._frame = "ground"
        self._string_escape = False
        self.synchronized_update = False

    @property
    def paintable(self) -> bool:
        return (
            self._frame == "ground"
            and not self._stream.utf8_decoder.getstate()[0]
            and not self.synchronized_update
            and modes.DECOM not in self.screen.mode
            and modes.IRM not in self.screen.mode
            and self.screen.cursor.x < self.screen.columns
        )

    def feed(self, data: bytes) -> None:
        plain = bytearray()
        for byte in data:
            if self._frame == "ground":
                if byte == 27:
                    self._stream.feed(bytes(plain))
                    plain.clear()
                    self._sequence = bytearray([byte])
                    self._frame = "escape"
                else:
                    plain.append(byte)
                continue
            if self._frame == "string":
                if byte == 7 or (self._string_escape and byte == 92):
                    self._frame = "ground"
                self._string_escape = byte == 27
                continue
            self._sequence.append(byte)
            if self._frame == "escape":
                if byte in b"]P_^X":
                    self._frame = "string"
                    self._string_escape = False
                    self._sequence.clear()
                    continue
                if byte == ord("["):
                    self._frame = "csi"
                    continue
                if 0x20 <= byte <= 0x2F:
                    self._frame = "intermediate"
                    continue
            elif self._frame == "csi" and not 0x40 <= byte <= 0x7E:
                if len(self._sequence) > 4096:
                    self._sequence.clear()
                continue
            elif self._frame == "intermediate" and 0x20 <= byte <= 0x2F:
                continue
            sequence = bytes(self._sequence)
            if sequence in {b"\x1b[?2026h", b"\x1b[?2026l"}:
                self.synchronized_update = sequence.endswith(b"h")
            else:
                self._stream.feed(sequence)
            self._sequence.clear()
            self._frame = "ground"
        self._stream.feed(bytes(plain))


class TerminalCompletionRenderer:
    """Compose one local draft over native cells, without modifying native editor state."""

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self.native = NativeTerminalProjection(columns, rows)
        self._state: TerminalCompletionState | None = None
        self._painted_from_row: int | None = None
        self._suspended = False

    def native_output(self, data: bytes) -> bytes:
        restored = self._restore()
        self.native.feed(data)
        return restored + data + self._paint()

    def display(self, state: TerminalCompletionState | None) -> tuple[bool, bytes]:
        if state is not None and self._state is None and self._anchor(state) is None:
            return False, b""
        restored = self._restore()
        self._state = state
        return True, restored + self._paint()

    def suspend(self) -> bytes:
        self._suspended = True
        return self._restore()

    def resume(self) -> bytes:
        self._suspended = False
        return self._paint()

    def resize(self, columns: int, rows: int) -> bytes:
        self.native.screen.resize(lines=rows, columns=columns)
        restored = self._restore()  # SIGWINCH arrives after the outer viewport changed.
        return restored  # Retain ownership; the native resize redraw re-anchors the draft.

    def _anchor(self, state: TerminalCompletionState) -> tuple[int, str] | None:
        screen = self.native.screen
        if not self.native.paintable:
            return None
        line = screen.display[screen.cursor.y][: screen.cursor.x]
        if line.lstrip() != f"\u203a {state.native_prefix}" or screen.cursor.x != wcswidth(line):
            return None
        if screen.cursor.y + 2 >= screen.lines:
            return None
        return screen.cursor.y, line[: len(line) - len(state.native_prefix)] if state.native_prefix else line

    def _paint(self) -> bytes:
        if self._state is None or self._suspended or (anchor := self._anchor(self._state)) is None:
            return b""
        row, prompt = anchor
        screen = self.native.screen
        self._painted_from_row = row
        draft = _clip_cells(prompt + self._state.draft, screen.columns - 1)
        suggestion = (
            f"  {self._state.completion_text}   {self._state.helper_text}"
            if self._state.completion_text
            else "  no matches"
        )
        output = bytearray(b"\x1b[0m")
        for current_row in range(row, screen.lines):
            text = draft if current_row == row else suggestion if current_row == row + 2 else ""
            output.extend(_position(current_row, 0) + b"\x1b[2K" + _clip_cells(text, screen.columns - 1).encode())
        output.extend(_position(row, wcswidth(draft)))
        return self._without_wrapping(bytes(output))

    def _restore(self) -> bytes:
        if self._painted_from_row is None:
            return b""
        screen = self.native.screen
        output = bytearray()
        previous_style = None
        for row in range(self._painted_from_row, screen.lines):
            output.extend(_position(row, 0))
            for column in range(screen.columns):
                cell = screen.buffer[row][column]
                if not cell.data:  # Wide-character continuation cell.
                    continue
                style = cell[1:]
                if style != previous_style:
                    output.extend(_rendition(cell))
                    previous_style = style
                output.extend(cell.data.encode())
        output.extend(_position(screen.cursor.y, screen.cursor.x) + _rendition(screen.cursor.attrs))
        self._painted_from_row = None
        return self._without_wrapping(bytes(output))

    def _without_wrapping(self, output: bytes) -> bytes:
        # Already-queued local bytes may reach a newly narrowed viewport before
        # its resize event is processed. Never let that generated text scroll.
        restore_wrap = b"\x1b[?7h" if modes.DECAWM in self.native.screen.mode else b"\x1b[?7l"
        return b"\x1b[?7l" + output + restore_wrap


def _position(row: int, column: int) -> bytes:
    return f"\x1b[{row + 1};{column + 1}H".encode()


def _clip_cells(text: str, columns: int) -> str:
    visible = []
    used_columns = 0
    for character in text:
        if not character.isprintable():
            character = " "
        width = max(0, wcwidth(character))
        if used_columns + width > columns:
            break
        visible.append(character)
        used_columns += width
    return "".join(visible)


def _rendition(cell: pyte.screens.Char) -> bytes:
    parameters = ["0"]
    for attribute, code in (
        ("bold", 1),
        ("italics", 3),
        ("underscore", 4),
        ("blink", 5),
        ("reverse", 7),
        ("strikethrough", 9),
    ):
        if getattr(cell, attribute):
            parameters.append(str(code))
    for color, basic, bright, extended in (
        (cell.fg, graphics.FG_ANSI, graphics.FG_AIXTERM, 38),
        (cell.bg, graphics.BG_ANSI, graphics.BG_AIXTERM, 48),
    ):
        if color == "default":
            continue
        names = {name: code for code, name in (basic | bright).items()}
        if color in names:
            parameters.append(str(names[color]))
        else:
            parameters.extend((str(extended), "2", *(str(int(color[index : index + 2], 16)) for index in (0, 2, 4))))
    return f"\x1b[{';'.join(parameters)}m".encode()
