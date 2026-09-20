"""Native terminal state with tmux-compatible cursor and resize semantics.

Only scrollable history is retained: a full native clear ends its restoration
scope. Presentation frames never enter this screen or its bounded history.
"""

from __future__ import annotations

from collections import defaultdict, deque

import pyte
from pyte import modes
from pyte.screens import Char
from wcwidth import wcwidth

NATIVE_SCROLLBACK_LIMIT = 50_000


class TerminalLine(dict[int, Char]):
    """Sparse styled cells plus the soft-wrap boundary needed for width reflow."""

    def __init__(self, default: Char) -> None:
        super().__init__()
        self.default = default
        self.wrapped = False

    def __missing__(self, _column: int) -> Char:
        return self.default

    @property
    def used(self) -> int:
        return max(self, default=-1) + 1


class NativeTerminalScreen(pyte.Screen):
    """Keep the native cursor, row contents and scroll history in one coordinate space."""

    def __init__(self, columns: int, lines: int) -> None:
        self._scrollback: deque[TerminalLine] = deque(maxlen=NATIVE_SCROLLBACK_LIMIT)
        super().__init__(columns, lines)

    def reset(self) -> None:
        super().reset()
        self.buffer = defaultdict(lambda: TerminalLine(self.default_char))
        self._scrollback.clear()

    def draw(self, data: str) -> None:
        for character in data:
            width = wcwidth(character)
            if width > 0 and self.cursor.x + width > self.columns:
                if modes.DECAWM in self.mode:
                    self.buffer[self.cursor.y].wrapped = True
                    self.carriage_return()
                    self.index()
                else:
                    self.cursor.x = max(0, self.columns - width)
            super().draw(character)

    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom:
            self._scrollback.append(self.buffer[top])
        super().index()

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        row = self.buffer[self.cursor.y]
        used = row.used
        super().erase_in_line(how, private)
        # Erasing unused default-background cells does not make a longer line.
        if self.cursor.attrs.bg == "default":
            for column in tuple(row):
                if column >= used:
                    del row[column]
        if how == 2:
            row.wrapped = False
            if self.cursor.attrs.bg == "default":
                row.clear()

    def erase_in_display(self, how: int = 0, *args: object, **kwargs: object) -> None:
        if how == 3:
            self._scrollback.clear()
            return
        super().erase_in_display(how, *args, **kwargs)
        rows = (
            range(self.cursor.y + 1, self.lines) if how == 0 else range(self.cursor.y) if how == 1 else range(self.lines)
        )
        for row in rows:
            self.buffer[row].wrapped = False
            if self.cursor.attrs.bg == "default":
                self.buffer.pop(row, None)
        if how == 2:
            self._scrollback.clear()

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        rows, width = max(1, lines or self.lines), max(1, columns or self.columns)
        if (rows, width) == (self.lines, self.columns):
            return
        old_rows = self.lines
        visible = [self.buffer[row] for row in range(old_rows)]
        if rows < old_rows:
            # Discard below the cursor first; only the remaining reduction scrolls.
            scrolled = max(0, self.cursor.y - rows + 1)
            self._scrollback.extend(visible[:scrolled])
            visible = visible[scrolled : scrolled + rows]
            self.cursor.y -= scrolled
        elif rows > old_rows:
            restored = min(rows - old_rows, len(self._scrollback))
            prefix = [self._scrollback.pop() for _ in range(restored)]
            visible = [*reversed(prefix), *visible]
            self.cursor.y += restored
        visible.extend(TerminalLine(self.default_char) for _ in range(rows - len(visible)))
        self.lines = rows
        self._replace_visible(visible)
        self.margins = None
        if width != self.columns:
            self._reflow(width)
            self.columns = width
            self.tabstops = set(range(8, width, 8))
        self.cursor.y = min(max(0, self.cursor.y), rows - 1)
        self.cursor.x = min(max(0, self.cursor.x), width)  # width itself is delayed autowrap.
        self.dirty = set(range(rows))

    def _replace_visible(self, rows: list[TerminalLine]) -> None:
        self.buffer.clear()
        self.buffer.update(enumerate(rows))

    def _reflow(self, width: int) -> None:
        """Rewrap logical lines without losing styles, wide cells or cursor identity."""
        source = [*self._scrollback, *(self.buffer[row] for row in range(self.lines))]
        cursor_row = len(self._scrollback) + self.cursor.y
        output: list[TerminalLine] = []
        mapped_cursor = (0, 0)
        start = 0
        while start < len(source):
            end = start
            while end + 1 < len(source) and source[end].wrapped:
                end += 1
            cells = [line[column] for line in source[start : end + 1] for column in range(line.used)]
            cursor_offset = None
            if start <= cursor_row <= end:
                cursor_offset = (
                    len(cells)
                    if self.cursor.x >= source[cursor_row].used
                    else sum(line.used for line in source[start:cursor_row]) + self.cursor.x
                )
            segments: list[TerminalLine] = []
            current = TerminalLine(self.default_char)
            offset = column = 0
            for cell in cells:
                if not cell.data:  # Continuation of a wide cell is created with its leading cell.
                    continue
                cell_width = max(1, wcwidth(cell.data[0]))
                if column and column + cell_width > width:
                    current.wrapped = True
                    segments.append(current)
                    current = TerminalLine(self.default_char)
                    column = 0
                if cursor_offset is not None and offset <= cursor_offset < offset + cell_width:
                    mapped_cursor = (column + cursor_offset - offset, len(output) + len(segments))
                current[column] = cell
                if cell_width == 2:
                    current[column + 1] = cell._replace(data="")
                offset += cell_width
                column += cell_width
            if cursor_offset is not None and cursor_offset >= offset:
                mapped_cursor = (min(column, width), len(output) + len(segments))
            segments.append(current)
            output.extend(segments)
            start = end + 1
        output.extend(TerminalLine(self.default_char) for _ in range(self.lines - len(output)))
        history_size = max(0, len(output) - self.lines)
        self._scrollback.clear()
        self._scrollback.extend(output[:history_size])
        self._replace_visible(output[history_size:])
        x, y = mapped_cursor
        self.cursor.x, self.cursor.y = (x, y - history_size) if y >= history_size else (0, 0)
