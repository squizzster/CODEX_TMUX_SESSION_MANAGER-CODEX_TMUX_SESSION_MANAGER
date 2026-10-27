"""Native terminal state with tmux-compatible cursor and resize semantics.

Only scrollable history is retained: a full native clear ends its restoration
scope. Presentation frames never enter this screen or its bounded history.
"""

from __future__ import annotations

from collections import defaultdict, deque

import pyte
from pyte import modes

from .terminal_cells import TMUX_32_PROFILE, TerminalCell, TerminalProfile

NATIVE_SCROLLBACK_LIMIT = 50_000


class TerminalLine(dict[int, TerminalCell]):
    """Sparse styled cells plus the soft-wrap boundary needed for width reflow."""

    def __init__(self, default: TerminalCell) -> None:
        super().__init__()
        self.default = default
        self.wrapped = False

    def __missing__(self, _column: int) -> TerminalCell:
        return self.default

    @property
    def used(self) -> int:
        return max(self, default=-1) + 1


class NativeTerminalScreen(pyte.Screen):
    """Keep the native cursor, row contents and scroll history in one coordinate space."""

    def __init__(self, columns: int, lines: int, *, profile: TerminalProfile = TMUX_32_PROFILE) -> None:
        self.profile = profile
        self._scrollback: deque[TerminalLine] = deque(maxlen=NATIVE_SCROLLBACK_LIMIT)
        super().__init__(columns, lines)

    def reset(self) -> None:
        super().reset()
        self.cursor.attrs = self.default_char
        self.buffer = defaultdict(lambda: TerminalLine(self.default_char))
        self._scrollback.clear()

    @property
    def default_char(self) -> TerminalCell:
        return TerminalCell(reverse=modes.DECSCNM in self.mode)

    @property
    def display(self) -> list[str]:
        return [self.row_text(row) for row in range(self.lines)]

    def row_text(self, row: int, end_column: int | None = None) -> str:
        """Extract by physical cells, never slice Unicode text with a column."""
        line = self.buffer[row]
        end = self.columns if end_column is None else min(end_column, self.columns)
        return "".join(line[column].data for column in range(end) if not line[column].continuation)

    def draw(self, data: str) -> None:
        data = data.translate(self.g1_charset if self.charset else self.g0_charset)
        for character in data:
            self.draw_character(character)

    def draw_character(self, character: str) -> int | None:
        """Apply one decoded character; return the changed cluster's physical column."""
        width = self.profile.character_width(character)
        line = self.buffer[self.cursor.y]
        column = self.cursor.x
        # Combining is relative to the current grid and survives input
        # chunks, SGR changes and queries without a parallel text buffer.
        previous_column = column - 1
        if self.profile.joined_emoji:
            if previous_column > 0 and line[previous_column].continuation:
                previous_column -= 1
        else:
            while previous_column > 0 and line[previous_column].continuation:
                previous_column -= 1
        if column > 0:
            previous = line[previous_column]
            if not self.profile.joined_emoji or previous.occupied_cells == column - previous_column:
                combined = self.profile.combine(previous, character)
                if combined is not None:
                    line[previous_column] = combined
                    if combined.occupied_cells > previous.occupied_cells:
                        # tmux 3.4 puts padding at the expanded cursor,
                        # leaving the intervening cell intact. Preserve
                        # this profile-specific grid fact through edits.
                        expanded = column + 1
                        line[expanded] = combined.padding()
                        self.cursor.x = expanded if expanded <= self.columns else self.columns - 1
                    self.dirty.add(self.cursor.y)
                    return previous_column
                if (
                    self.profile.joined_emoji
                    and width == 0
                    and len((previous.data + character).encode()) > self.profile.cluster_bytes
                ):
                    # 3.4 falls through to a separate zero-width grid entry
                    # when its 21-byte combining buffer is full.
                    line[column] = self.cursor.attrs._replace(data=character, occupied_cells=0, continuation=False)
                    self.dirty.add(self.cursor.y)
                    return column
        if width <= 0:
            return None
        if width > self.columns:
            return None
        if self.cursor.x + width > self.columns:
            if modes.DECAWM in self.mode:
                line.wrapped = True
                self.carriage_return()
                self.index()
            else:
                return None
        if modes.IRM in self.mode:
            self.insert_characters(width)
        line = self.buffer[self.cursor.y]
        column = self.cursor.x
        collected_ascii = (
            len(character) == 1 and ord(character) < 127 and modes.DECAWM in self.mode and modes.IRM not in self.mode
        )
        self._clear_overwritten_cluster(line, column, collected_ascii=collected_ascii)
        for occupied in range(column + 1, column + width):
            self._clear_overwritten_cluster(line, occupied)
        cell = self.cursor.attrs._replace(data=character, occupied_cells=width, continuation=False)
        line[column] = cell
        for occupied in range(column + 1, column + width):
            line[occupied] = cell.padding()
        self.cursor.x = min(column + width, self.columns if modes.DECAWM in self.mode else self.columns - 1)
        self.dirty.add(self.cursor.y)
        return column

    def _clear_overwritten_cluster(self, line: TerminalLine, column: int, *, collected_ascii: bool = False) -> None:
        cell = line[column]
        if cell.continuation:
            left = column
            while left > 0 and line[left].continuation:
                left -= 1
            if not collected_ascii or line[left].occupied_cells > 1:
                line[left] = self.default_char
            right = column + 1
            while right < self.columns and line[right].continuation:
                line[right] = self.default_char
                right += 1
        elif cell.occupied_cells > 1 or collected_ascii:
            occupied = column + 1
            while occupied < self.columns and line[occupied].continuation:
                line[occupied] = self.default_char
                occupied += 1

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
        if how == 2 or (how == 0 and self.cursor.x == 0) or (how == 1 and self.cursor.x >= self.columns - 1):
            self._break_wrap_at(self.cursor.y)
            if self.cursor.attrs.bg == "default":
                row.clear()

    def _break_wrap_at(self, row: int) -> None:
        """A cleared physical row disconnects both adjacent soft-wrap links."""
        self.buffer[row].wrapped = False
        self._break_wrap_before(row)

    def _break_wrap_before(self, row: int) -> None:
        if row > 0:
            self.buffer[row - 1].wrapped = False
        elif self._scrollback:
            self._scrollback[-1].wrapped = False

    def insert_characters(self, count: int | None = None) -> None:
        super().insert_characters(count)
        # Pyte retains a cell at columns (one past the viewport). It is invisible
        # until reflow, when it otherwise reappears as an extra character.
        self.buffer[self.cursor.y].pop(self.columns, None)

    def delete_characters(self, count: int | None = None) -> None:
        count = min(count or 1, max(0, self.columns - self.cursor.x))
        if not count:
            return
        if count == self.columns - self.cursor.x:
            self.erase_in_line(0)
            return
        line = self.buffer[self.cursor.y]
        used = max(line.used, self.columns - count)
        super().delete_characters(count)
        # Vacated cells still occupy their original positions in a wrapped row;
        # removing their sparse entries must not shorten its logical-line offset.
        blank = self.default_char._replace(bg=self.cursor.attrs.bg)
        for column in range(self.columns - count, used):
            line[column] = blank
        for column in tuple(line):
            if column >= used:
                del line[column]

    def insert_lines(self, count: int | None = None) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        row = self.cursor.y
        if not top <= row <= bottom:
            bottom = self.lines - 1
            if (count or 1) >= bottom - row + 1:
                # Both admitted tmux profiles leave a full-height insertion
                # outside the scroll region unchanged (zero rows to move).
                return
        count = min(count or 1, bottom - row + 1)
        shifted = bottom - row + 1 - count
        # Disconnect the destination boundary before moving its old row object.
        self._break_wrap_before(row + count)
        # Move sparse rows without Pyte's delegated carriage return. tmux
        # preserves even x == columns, which represents delayed autowrap.
        for destination in range(bottom, row - 1, -1):
            source = destination - count
            if source >= row and source in self.buffer:
                self.buffer[destination] = self.buffer.pop(source)
            else:
                self.buffer.pop(destination, None)
        self.dirty.update(range(row, bottom + 1))
        self._break_wrap_before(row)
        self._break_wrap_before(row + shifted)

    def delete_lines(self, count: int | None = None) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        row = self.cursor.y
        if not top <= row <= bottom:
            bottom = self.lines - 1
        count = min(count or 1, bottom - row + 1)
        self._break_wrap_before(row)
        # An absent sparse source row is still a blank row. Leaving its old
        # destination untouched would duplicate text when deleting above it.
        for destination in range(row, bottom + 1):
            source = destination + count
            if source <= bottom and source in self.buffer:
                self.buffer[destination] = self.buffer.pop(source)
            else:
                self.buffer.pop(destination, None)
        self.dirty.update(range(row, bottom + 1))
        self._break_wrap_before(bottom + 1 - count)

    def erase_in_display(self, how: int = 0, *args: object, **kwargs: object) -> None:
        if how == 3:
            self._scrollback.clear()
            return
        super().erase_in_display(how, *args, **kwargs)
        rows = (
            range(self.cursor.y + 1, self.lines) if how == 0 else range(self.cursor.y) if how == 1 else range(self.lines)
        )
        for row in rows:
            self._break_wrap_at(row)
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
            offset = column = occupied_width = 0
            for cell in cells:
                if column and occupied_width + cell.occupied_cells > width:
                    current.wrapped = True
                    segments.append(current)
                    current = TerminalLine(self.default_char)
                    column = occupied_width = 0
                if cursor_offset == offset:
                    mapped_cursor = (column, len(output) + len(segments))
                current[column] = cell
                offset += 1
                column += 1
                occupied_width += cell.occupied_cells
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
