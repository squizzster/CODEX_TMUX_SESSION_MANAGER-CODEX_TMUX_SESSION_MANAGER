"""Pure terminal-surface composition over one continuously updated native projection.

The gateway remains the only writer. Native mode relays Codex output with configured
inline menus; semantic mode draws selected typed item text while retaining the native
composer and controls. Rodex-owned frames never enter Codex's editor or projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

import pyte
from pyte import graphics, modes

from .input_menu import ARGUMENT_MENU_FOOTER, InputMenuStage, InputMenuView
from .native_composer import composer_gutter
from .native_terminal_projection import NativeTerminalProjection
from .native_terminal_screen import NativeTerminalScreen
from .presentation_policy import PresentationSnapshot, PresentationSurface
from .prompt_expansion_display import PromptExpansion
from .terminal_cells import TMUX_32_PROFILE, TerminalProfile, terminal_safe_text

_DEFAULT_PRESENTATION = PresentationSnapshot(0, "dark", PresentationSurface.NATIVE, "", (), ())


@dataclass(frozen=True, slots=True)
class TerminalRenderOutput:
    """Ordered terminal stream, replaceable display frame, and exact child replies."""

    stream: bytes = b""
    frame: bytes = b""
    replies: bytes = b""


class TerminalSurfaceRenderer:
    """Render native or semantic surfaces without modifying native editor state."""

    def __init__(self, columns: int = 80, rows: int = 24, *, profile: TerminalProfile = TMUX_32_PROFILE) -> None:
        self.profile = profile
        self.native = NativeTerminalProjection(columns, rows, profile=profile)
        self._clip_cells = partial(_clip_cells, profile=profile)
        self._wrap_cells = partial(_wrap_cells, profile=profile)
        self._semantic_text_lines = partial(_semantic_text_lines, profile=profile)
        self._menu_lines = partial(_menu_lines, profile=profile)
        self._state: InputMenuView | None = None
        self._painted_from_row: int | None = None
        self._suspended = False
        self._presentation = _DEFAULT_PRESENTATION
        self._last_semantic_frame: bytes | None = None
        self._pending_presentation: PresentationSnapshot | None = None
        self._prompt_expansion: PromptExpansion | None = None
        self._native_resize_pending = False

    def show_prompt_expansion(self, expansion: PromptExpansion | None) -> bytes:
        """Display forwarded text locally; retain native draft, history and cursor."""
        if expansion == self._prompt_expansion:
            return b""
        self._prompt_expansion = expansion
        self._last_semantic_frame = None
        if not self.native.at_boundary:
            return b""  # Native output will repaint once its current token is complete.
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            return self._semantic_redraw()
        return self._restore() + self._paint()

    @property
    def presentation_surface(self) -> PresentationSurface:
        return self._presentation.surface

    def native_output(self, data: bytes) -> TerminalRenderOutput:
        semantic = self._presentation.surface == PresentationSurface.SEMANTIC
        restored = b"" if semantic else self._restore()
        output = self.native.feed(data)
        stream = output.controls if semantic else restored + output.native + self._paint()
        frame = self._semantic_redraw() if semantic else b""
        if not semantic and self._native_resize_pending and self.native.paintable:
            frame = self._native_redraw() + self._paint()
        if self._pending_presentation is not None and self.native.paintable:
            frame = self.present(self._pending_presentation) or frame
        return TerminalRenderOutput(stream, frame, output.replies)

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
        if not self.native.paintable:
            self._pending_presentation = snapshot
            return b""
        self._pending_presentation = None
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
        self.native.screen.resize(lines=rows, columns=columns)
        self._painted_from_row = None
        if self._presentation.surface == PresentationSurface.SEMANTIC:
            self._painted_from_row = None
            self._last_semantic_frame = None
            return self._semantic_redraw()
        # Repainted outer rows do not carry the original soft-wrap links. The
        # authoritative grid performs reflow; the delivered resize callback
        # restores its result before subsequent native output. Retain menu
        # ownership, but let the next native update re-anchor the draft.
        self._native_resize_pending = True
        return self._native_redraw()

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
        output = bytearray(_screen_reset(screen.lines))
        heading = self._clip_cells(self._presentation.heading, screen.columns - 1)
        output.extend(_position(0, 0) + b"\x1b[1;36m" + heading.encode() + b"\x1b[0m")
        transcript_rows = max(0, composer_row - 2)
        transcript = (
            self._semantic_text_lines(
                tuple(item.text for item in self._presentation.items),
                screen.columns - 1,
            )[-transcript_rows:]
            if transcript_rows
            else []
        )
        for row, line in enumerate(transcript, start=2):
            output.extend(_position(row, 0) + b"\x1b[0m\x1b[2K" + line.encode())
        for row in range(composer_row, screen.lines):
            output.extend(_native_row(screen, row))

        native_composer = screen.display[composer_row].rstrip()
        cursor = (
            (screen.cursor.y, screen.cursor.x)
            if screen.cursor.y >= composer_row
            else (composer_row, min(self.profile.text_width(native_composer), screen.columns - 1))
        )
        if self._state is not None and not self._suspended and (anchor := self._anchor(self._state)) is not None:
            row, prompt = anchor
            fixed_rows = 2 if self._state.stage == InputMenuStage.COMMANDS else 5
            row = min(row, max(0, screen.lines - fixed_rows - max(1, len(self._state.rows))))
            draft = self._clip_cells(prompt + self._state.draft, screen.columns - 1)
            lines = self._menu_lines(self._state, draft, screen.lines - row)
            cursor = (row, self.profile.text_width(draft)) if self._state.stage == InputMenuStage.COMMANDS else (row, 0)
            for current_row in range(row, screen.lines):
                text, selected = lines[current_row - row] if current_row - row < len(lines) else ("", False)
                style = b"\x1b[1;36m" if selected else b"\x1b[0m"
                output.extend(
                    _position(current_row, 0)
                    + b"\x1b[0m\x1b[2K"
                    + style
                    + self._clip_cells(text, screen.columns - 1).encode()
                )
                if selected and self._state.stage == InputMenuStage.ARGUMENTS:
                    cursor = (current_row, 0)
        output.extend(self._paint_prompt_expansion())
        output.extend(b"\x1b[0m" + _position(*cursor) + b"\x1b[?25h")
        return self._without_wrapping(bytes(output))

    def _native_redraw(self) -> bytes:
        # A complete token is not necessarily a safe repaint point: positioning
        # clears delayed autowrap, and origin/insert modes change frame semantics.
        if not self.native.paintable:
            return b""
        self._native_resize_pending = False
        screen = self.native.screen
        output = bytearray(_screen_reset(screen.lines))
        for row in range(screen.lines):
            output.extend(_native_row(screen, row))
        # Painting temporarily needs the whole viewport. Restore the native
        # scrolling region before the cursor, since setting margins homes it.
        if screen.margins is not None:
            output.extend(f"\x1b[{screen.margins.top + 1};{screen.margins.bottom + 1}r".encode())
        output.extend(b"\x1b[0m" + _position(screen.cursor.y, screen.cursor.x) + _rendition(screen.cursor.attrs))
        output.extend(b"\x1b[?25l" if screen.cursor.hidden else b"\x1b[?25h")
        return self._without_wrapping(bytes(output))

    def _anchor(self, state: InputMenuView) -> tuple[int, str] | None:
        screen = self.native.screen
        if not self.native.paintable:
            return None
        line = screen.row_text(screen.cursor.y, screen.cursor.x)
        gutter = composer_gutter(line)
        if gutter is None:
            return None
        expected = NativeTerminalScreen(screen.columns, 1, profile=self.profile)
        expected.draw(gutter + state.native_prefix)
        if expected.cursor.x != screen.cursor.x or expected.row_text(0, expected.cursor.x) != line:
            return None
        if screen.lines < 3:
            return None
        return screen.cursor.y, gutter

    def _paint(self) -> bytes:
        if self._suspended:
            return b""
        if self._state is None:
            output = self._paint_prompt_expansion()
            if not output:
                return b""
            screen = self.native.screen
            return self._without_wrapping(
                output + _position(screen.cursor.y, screen.cursor.x) + _rendition(screen.cursor.attrs)
            )
        if (anchor := self._anchor(self._state)) is None:
            return b""
        row, prompt = anchor
        screen = self.native.screen
        fixed_rows = 2 if self._state.stage == InputMenuStage.COMMANDS else 5
        row = min(row, max(0, screen.lines - fixed_rows - max(1, len(self._state.rows))))
        self._painted_from_row = row
        draft = self._clip_cells(prompt + self._state.draft, screen.columns - 1)
        lines = self._menu_lines(self._state, draft, screen.lines - row)
        output = bytearray(b"\x1b[0m")
        cursor = (row, self.profile.text_width(draft)) if self._state.stage == InputMenuStage.COMMANDS else (row, 0)
        for current_row in range(row, screen.lines):
            text, selected = lines[current_row - row] if current_row - row < len(lines) else ("", False)
            style = b"\x1b[1;36m" if selected else b"\x1b[0m"
            output.extend(
                _position(current_row, 0)
                + b"\x1b[0m\x1b[2K"
                + style
                + self._clip_cells(text, screen.columns - 1).encode()
            )
            if selected and self._state.stage == InputMenuStage.ARGUMENTS:
                cursor = (current_row, 0)
        output.extend(b"\x1b[0m" + _position(*cursor))
        return self._without_wrapping(bytes(output))

    def _paint_prompt_expansion(self) -> bytes:
        """Reserve at most six rows above the composer; menus and native dialogs win."""
        if self._prompt_expansion is None or self._suspended or self._state is not None or not self.native.paintable:
            return b""
        screen = self.native.screen
        composer_row = _native_composer_row(screen)
        if composer_row is None or composer_row < 3:
            return b""
        available = min(6, composer_row - 1)
        width = max(1, screen.columns - 1)
        original = _terminal_safe_text(self._prompt_expansion.original_text).replace("\n", " ").replace("\t", " ")
        lines = self._wrap_cells(
            self._clip_cells(original, width) + " => " + _terminal_safe_text(self._prompt_expansion.forwarded_text), width
        )
        visible_lines = min(available, len(lines))
        first_row = composer_row - 1 - visible_lines
        if self._presentation.surface == PresentationSurface.NATIVE:
            # Placement only, never submission authority: use blank space directly
            # beneath a visible one-line echo when available. Do not edit native
            # cells or displace later output to manufacture room for the notice.
            display = screen.display
            for row in range(composer_row - 1, -1, -1):
                echoed = display[row].strip()
                gutter = composer_gutter(echoed)
                if gutter is not None and echoed == gutter + self._prompt_expansion.original_text:
                    blank_rows = 0
                    for below in range(row + 1, min(composer_row, row + 1 + visible_lines)):
                        if display[below].strip():
                            break
                        blank_rows += 1
                    if blank_rows:
                        first_row = row + 1
                        visible_lines = blank_rows
                    elif first_row <= row < first_row + visible_lines:
                        return b""  # Never cover the original echo to display its expansion.
                    break
        if len(lines) > visible_lines:
            lines = lines[:visible_lines]
            lines[-1] = self._clip_cells(lines[-1], max(0, width - 1)) + "…"
        self._painted_from_row = first_row
        output = bytearray()
        for row, line in enumerate(lines, first_row):
            output.extend(_position(row, 0) + b"\x1b[0m\x1b[2K" + self._clip_cells(line, width).encode())
        return bytes(output)

    def _restore(self) -> bytes:
        if self._painted_from_row is None:
            return b""
        screen = self.native.screen
        output = bytearray()
        for row in range(self._painted_from_row, screen.lines):
            output.extend(_native_row(screen, row))
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


def _screen_reset(rows: int) -> bytes:
    """Give a Rodex-owned frame a deterministic full viewport."""
    # tmux's full-display clear can append the old surface to scrollback.
    # Row clears remove a presentation frame without manufacturing history.
    return b"\x1b[?7l\x1b[?6l\x1b[0m\x1b[r" + b"".join(_position(row, 0) + b"\x1b[2K" for row in range(rows)) + b"\x1b[H"


def _native_composer_row(screen: pyte.Screen) -> int | None:
    """Locate the bottom-most native composer boundary used for input/control bridging."""
    for row in range(screen.lines - 1, -1, -1):
        line = screen.display[row].lstrip()
        if composer_gutter(line) is not None:
            return row
    return None


def _native_row(screen: NativeTerminalScreen, row: int) -> bytes:
    """Reconstruct physical entries, including padding left behind by edits.

    ECH replaces a raw entry without the neighbouring-glyph cleanup performed
    by printing a space. A scratch screen uses the same cell operations to
    track what each emitted instruction actually establishes.
    """
    output = bytearray(_position(row, 0) + b"\x1b[0m\x1b[2K")
    painted = NativeTerminalScreen(screen.columns, 1, profile=screen.profile)
    painted.reset_mode(7, private=True)
    desired = screen.buffer[row]

    def position(column: int, cell) -> None:
        output.extend(_position(row, column) + _rendition(cell))
        painted.cursor.x = column
        painted.cursor.attrs = cell._replace(data=" ", occupied_cells=1, continuation=False)

    def erase(column: int, cell) -> None:
        position(column, cell)
        output.extend(b"\x1b[X")
        painted.erase_characters(1)

    def draw(column: int, cell) -> None:
        erase(column, cell)
        output.extend(cell.data.encode())
        painted.draw(cell.data)

    def insert(column: int, count: int) -> None:
        position(column, screen.default_char)
        output.extend(f"\x1b[{count}@".encode())
        painted.insert_characters(count)

    def padding(column: int) -> None:
        erase(column, screen.default_char)
        output.extend("界".encode() + _position(row, column) + b"\x1b[P")
        painted.draw("界")
        painted.cursor.x = column
        painted.delete_characters(1)

    def restore(column: int, *, force: bool = False) -> None:
        cell = desired[column]
        if not force and (
            painted.buffer[0][column] == cell or (cell.continuation and painted.buffer[0][column].continuation)
        ):
            return
        if cell.continuation:
            leader = column - 1
            while leader >= 0 and desired[leader].continuation:
                leader -= 1
            if leader >= 0 and leader + desired[leader].occupied_cells > column:
                cluster = NativeTerminalScreen(screen.columns + 2, 1, profile=screen.profile)
                cluster.draw(desired[leader].data)
                if cluster.buffer[0][column - leader].continuation:
                    # A staged margin cluster can displace its predecessor's
                    # padding. Repaint that padding's owner; an orphan donor
                    # would overwrite and shift the already restored suffix.
                    restore(leader, force=True)
                    return
            if column < screen.columns - 1:
                padding(column)
            else:
                # Stage the entire trailing padding run and its predecessor
                # with a spare donor cell, then shift them to the edge.
                first = column
                while first > 0 and desired[first].continuation:
                    first -= 1
                staging = max(0, first - 1)
                draw(staging, desired[first])
                for source in range(first + 1, column + 1):
                    padding(staging + source - first)
                insert(staging, first - staging)
                for displaced in range(staging, first):
                    restore(displaced)
            return
        if len(cell.data) > 1 and column + cell.occupied_cells >= screen.columns:
            # Under nowrap the cursor sticks at the margin before combining.
            # Form the whole cluster to the left, shift its raw entries into
            # place, then restore displaced predecessors with the same rules.
            shift = column + cell.occupied_cells - screen.columns + 1
            staging = max(0, column - shift)
            draw(staging, cell)
            insert(staging, column - staging)
            for displaced in range(staging, column):
                restore(displaced)
        elif cell.data == " ":
            erase(column, cell)
        else:
            draw(column, cell)

    for column in range(min(desired.used, screen.columns)):
        restore(column)
    return bytes(output)


def _semantic_text_lines(
    items: tuple[str, ...], columns: int, *, profile: TerminalProfile = TMUX_32_PROFILE
) -> list[str]:
    lines: list[str] = []
    width = max(1, columns)
    for item in items:
        wrapped = _wrap_cells(_terminal_safe_text(item), max(1, width - 2), profile=profile)
        for index, line in enumerate(wrapped):
            lines.append(_clip_cells(("\u2022 " if index == 0 else "  ") + line, width, profile=profile))
        lines.append("")
    if lines:
        lines.pop()
    return lines


def _terminal_safe_text(text: str) -> str:
    return terminal_safe_text(text)


def _wrap_cells(text: str, columns: int, *, profile: TerminalProfile = TMUX_32_PROFILE) -> list[str]:
    wrapped: list[str] = []
    for logical_line in text.expandtabs(4).split("\n"):
        current: list[str] = []
        used_columns = 0
        for cell in profile.clusters(logical_line):
            if current and used_columns + cell.occupied_cells > columns:
                wrapped.append("".join(current))
                current = []
                used_columns = 0
            current.append(cell.data)
            used_columns += cell.occupied_cells
        wrapped.append("".join(current))
    return wrapped or [""]


def _menu_lines(
    state: InputMenuView, draft: str, available_rows: int, *, profile: TerminalProfile = TMUX_32_PROFILE
) -> list[tuple[str, bool]]:
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
    label_width = max((profile.text_width(row.label) for row in state.rows), default=0)
    for index in range(first_index, min(len(state.rows), first_index + visible_count)):
        row = state.rows[index]
        selected = index == state.selected_index
        label = row.label + " " * (label_width - profile.text_width(row.label))
        selection_marker = "\u203a" if selected else " "
        prefix = f"{selection_marker} {index + 1}. " if state.stage == InputMenuStage.ARGUMENTS else "  "
        lines.append((f"{prefix}{label}   {row.helper_text}", selected))
    if not state.rows:
        lines.append(("  no matches", False))
    return lines + [(line, False) for line in footer]


def _clip_cells(text: str, columns: int, *, profile: TerminalProfile = TMUX_32_PROFILE) -> str:
    visible: list[str] = []
    used_columns = 0
    for cell in profile.clusters(_terminal_safe_text(text).replace("\n", " ").replace("\t", " ")):
        if used_columns + cell.occupied_cells > columns:
            break
        visible.append(cell.data)
        used_columns += cell.occupied_cells
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
