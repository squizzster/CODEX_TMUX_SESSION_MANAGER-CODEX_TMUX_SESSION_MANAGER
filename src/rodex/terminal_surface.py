"""Pure terminal-surface composition over one continuously updated native projection.

The gateway remains the only writer. Native mode relays Codex output with configured
inline menus; semantic mode draws selected typed item text while retaining the native
composer and controls. Rodex-owned frames never enter Codex's editor or projection.
"""

from __future__ import annotations

import pyte
from pyte import graphics, modes
from wcwidth import wcswidth, wcwidth

from .input_menu import ARGUMENT_MENU_FOOTER, InputMenuStage, InputMenuView
from .native_composer import composer_gutter
from .presentation_policy import PresentationSnapshot, PresentationSurface

_DEFAULT_PRESENTATION = PresentationSnapshot(0, "dark", PresentationSurface.NATIVE, "", (), ())


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


class TerminalSurfaceRenderer:
    """Render native or semantic surfaces without modifying native editor state."""

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self.native = NativeTerminalProjection(columns, rows)
        self._state: InputMenuView | None = None
        self._painted_from_row: int | None = None
        self._suspended = False
        self._presentation = _DEFAULT_PRESENTATION
        self._last_semantic_frame: bytes | None = None

    @property
    def presentation_surface(self) -> PresentationSurface:
        return self._presentation.surface

    def native_output(self, data: bytes) -> bytes:
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self.native.feed(data)
            return self._semantic_redraw()
        restored = self._restore()
        self.native.feed(data)
        return restored + data + self._paint()

    def display(self, state: InputMenuView | None) -> tuple[bool, bytes]:
        if state is not None and self._state is None and self._anchor(state) is None:
            return False, b""
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self._state = state
            self._last_semantic_frame = None
            return True, self._semantic_redraw()
        restored = self._restore()
        self._state = state
        return True, restored + self._paint()

    def present(self, snapshot: PresentationSnapshot) -> bytes:
        """Select one semantic/native surface without changing the hidden native state."""
        if snapshot == self._presentation:
            return b""
        previous_surface = self._presentation.surface
        if previous_surface == snapshot.surface == PresentationSurface.NATIVE:
            self._presentation = snapshot
            return b""
        self._painted_from_row = None
        self._presentation = snapshot
        self._last_semantic_frame = None
        if snapshot.surface == PresentationSurface.SEMANTIC:
            return self._semantic_redraw()
        return self._native_redraw() + self._paint()

    def redraw(self) -> bytes:
        """Reconstruct the latest complete surface after superseding queued output."""
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self._last_semantic_frame = None
            return self._semantic_redraw()
        return self._native_redraw() + self._paint()

    def suspend(self) -> bytes:
        self._suspended = True
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self._last_semantic_frame = None
            return self._semantic_redraw()
        return self._restore()

    def resume(self) -> bytes:
        self._suspended = False
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self._last_semantic_frame = None
            return self._semantic_redraw()
        return self._paint()

    def resize(self, columns: int, rows: int) -> bytes:
        if self._painted_from_row is not None and rows != self.native.screen.lines:
            self._painted_from_row = 0  # Height changes can move every painted row upward.
        self.native.screen.resize(lines=rows, columns=columns)
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self._painted_from_row = None
            self._last_semantic_frame = None
            return self._semantic_redraw()
        restored = self._restore()  # SIGWINCH arrives after the outer viewport changed.
        return restored  # Retain ownership; the native resize redraw re-anchors the draft.

    def _semantic_redraw(self) -> bytes:
        """Render admitted semantic messages plus the native interactive control region."""
        if not self.native.paintable:
            return b""
        screen = self.native.screen
        composer_row = _native_composer_row(screen)
        # Unknown native states include approval and other interactive dialogs;
        # preserve those controls rather than presenting an unusable blank screen.
        frame = self._native_redraw() if composer_row is None else self._semantic_frame(composer_row)
        if frame == self._last_semantic_frame:
            return b""
        self._last_semantic_frame = frame
        return frame

    def _semantic_frame(self, composer_row: int) -> bytes:
        screen = self.native.screen
        output = bytearray(_screen_reset())
        heading = _clip_cells(self._presentation.heading, screen.columns - 1)
        output.extend(_position(0, 0) + b"\x1b[1;36m" + heading.encode() + b"\x1b[0m")
        transcript_rows = max(0, composer_row - 2)
        transcript = _semantic_text_lines(
            tuple(item.text for item in self._presentation.items),
            screen.columns - 1,
        )[-transcript_rows:]
        for row, line in enumerate(transcript, start=2):
            output.extend(_position(row, 0) + b"\x1b[0m\x1b[2K" + line.encode())
        for row in range(composer_row, screen.lines):
            output.extend(_native_row(screen, row))

        native_composer = screen.display[composer_row].rstrip()
        cursor = (
            (screen.cursor.y, screen.cursor.x)
            if screen.cursor.y >= composer_row
            else (composer_row, min(wcswidth(native_composer), screen.columns - 1))
        )
        if self._state is not None and not self._suspended and (anchor := self._anchor(self._state)) is not None:
            row, prompt = anchor
            fixed_rows = 2 if self._state.stage == InputMenuStage.COMMANDS else 5
            row = min(row, max(0, screen.lines - fixed_rows - max(1, len(self._state.rows))))
            draft = _clip_cells(prompt + self._state.draft, screen.columns - 1)
            lines = _menu_lines(self._state, draft, screen.lines - row)
            cursor = (row, wcswidth(draft)) if self._state.stage == InputMenuStage.COMMANDS else (row, 0)
            for current_row in range(row, screen.lines):
                text, selected = lines[current_row - row] if current_row - row < len(lines) else ("", False)
                style = b"\x1b[1;36m" if selected else b"\x1b[0m"
                output.extend(
                    _position(current_row, 0) + b"\x1b[0m\x1b[2K" + style + _clip_cells(text, screen.columns - 1).encode()
                )
                if selected and self._state.stage == InputMenuStage.ARGUMENTS:
                    cursor = (current_row, 0)
        output.extend(b"\x1b[0m" + _position(*cursor) + b"\x1b[?25h")
        return self._without_wrapping(bytes(output))

    def _native_redraw(self) -> bytes:
        screen = self.native.screen
        output = bytearray(_screen_reset())
        for row in range(screen.lines):
            output.extend(_native_row(screen, row))
        output.extend(b"\x1b[0m" + _position(screen.cursor.y, screen.cursor.x) + _rendition(screen.cursor.attrs))
        return self._without_wrapping(bytes(output))

    def _anchor(self, state: InputMenuView) -> tuple[int, str] | None:
        screen = self.native.screen
        if not self.native.paintable:
            return None
        line = screen.display[screen.cursor.y][: screen.cursor.x]
        gutter = composer_gutter(line)
        if gutter is None or line != gutter + state.native_prefix or screen.cursor.x != wcswidth(line):
            return None
        if screen.lines < 3:
            return None
        return screen.cursor.y, line[: len(line) - len(state.native_prefix)] if state.native_prefix else line

    def _paint(self) -> bytes:
        if self._state is None or self._suspended or (anchor := self._anchor(self._state)) is None:
            return b""
        row, prompt = anchor
        screen = self.native.screen
        fixed_rows = 2 if self._state.stage == InputMenuStage.COMMANDS else 5
        row = min(row, max(0, screen.lines - fixed_rows - max(1, len(self._state.rows))))
        self._painted_from_row = row
        draft = _clip_cells(prompt + self._state.draft, screen.columns - 1)
        lines = _menu_lines(self._state, draft, screen.lines - row)
        output = bytearray(b"\x1b[0m")
        cursor = (row, wcswidth(draft)) if self._state.stage == InputMenuStage.COMMANDS else (row, 0)
        for current_row in range(row, screen.lines):
            text, selected = lines[current_row - row] if current_row - row < len(lines) else ("", False)
            style = b"\x1b[1;36m" if selected else b"\x1b[0m"
            output.extend(
                _position(current_row, 0) + b"\x1b[0m\x1b[2K" + style + _clip_cells(text, screen.columns - 1).encode()
            )
            if selected and self._state.stage == InputMenuStage.ARGUMENTS:
                cursor = (current_row, 0)
        output.extend(b"\x1b[0m" + _position(*cursor))
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


def _screen_reset() -> bytes:
    """Give a Rodex-owned frame a deterministic full viewport."""
    return b"\x1b[?7l\x1b[?6l\x1b[0m\x1b[r\x1b[2J\x1b[H"


def _native_composer_row(screen: pyte.Screen) -> int | None:
    """Locate the bottom-most native composer boundary used for input/control bridging."""
    for row in range(screen.lines - 1, -1, -1):
        line = screen.display[row].lstrip()
        if composer_gutter(line) is not None:
            return row
    return None


def _native_row(screen: pyte.Screen, row: int) -> bytes:
    output = bytearray(_position(row, 0) + b"\x1b[0m\x1b[2K")
    previous_style = None
    for column in range(screen.columns):
        cell = screen.buffer[row][column]
        if not cell.data:
            continue
        style = cell[1:]
        if style != previous_style:
            output.extend(_rendition(cell))
            previous_style = style
        output.extend(cell.data.encode())
    return bytes(output)


def _semantic_text_lines(items: tuple[str, ...], columns: int) -> list[str]:
    lines: list[str] = []
    width = max(1, columns)
    for item in items:
        wrapped = _wrap_cells(_terminal_safe_text(item), max(1, width - 2))
        for index, line in enumerate(wrapped):
            lines.append(_clip_cells(("\u2022 " if index == 0 else "  ") + line, width))
        lines.append("")
    if lines:
        lines.pop()
    return lines


def _terminal_safe_text(text: str) -> str:
    return "".join(character if character in "\n\t" or character.isprintable() else " " for character in text)


def _wrap_cells(text: str, columns: int) -> list[str]:
    wrapped: list[str] = []
    for logical_line in text.expandtabs(4).split("\n"):
        current: list[str] = []
        used_columns = 0
        for character in logical_line:
            width = max(0, wcwidth(character))
            if current and used_columns + width > columns:
                wrapped.append("".join(current))
                current = []
                used_columns = 0
            current.append(character)
            used_columns += width
        wrapped.append("".join(current))
    return wrapped or [""]


def _menu_lines(state: InputMenuView, draft: str, available_rows: int) -> list[tuple[str, bool]]:
    """Fit the selected row and fixed picker footer before admitting optional spacing."""
    if state.stage == InputMenuStage.COMMANDS:
        heading, footer = [draft, ""], []
    elif available_rows >= 6:
        heading, footer = [state.heading, state.subheading, ""], ["", ARGUMENT_MENU_FOOTER]
    else:
        heading, footer = [state.heading], [ARGUMENT_MENU_FOOTER]
    lines = [(line, False) for line in heading]
    visible_count = max(1, available_rows - len(heading) - len(footer))
    first_index = max(0, (state.selected_index or 0) - visible_count + 1)
    label_width = max((wcswidth(row.label) for row in state.rows), default=0)
    for index in range(first_index, min(len(state.rows), first_index + visible_count)):
        row = state.rows[index]
        selected = index == state.selected_index
        label = row.label + " " * (label_width - wcswidth(row.label))
        prefix = f"{'\u203a' if selected else ' '} {index + 1}. " if state.stage == InputMenuStage.ARGUMENTS else "  "
        lines.append((f"{prefix}{label}   {row.helper_text}", selected))
    if not state.rows:
        lines.append(("  no matches", False))
    return lines + [(line, False) for line in footer]


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
