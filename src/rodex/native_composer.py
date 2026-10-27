"""Recognize visible native composer text, never manufacture editor state.

Codex uses \u203a ordinarily and » for Ultra. Wrapped and explicit continuation rows
have the same two-column gutter. A snapshot can acknowledge only fully visible
text; collapsed paste placeholders and scrolled drafts cannot establish equality.
"""

from .native_terminal_screen import NativeTerminalScreen
from .terminal_cells import TMUX_32_PROFILE, TerminalProfile

NATIVE_COMPOSER_GLYPHS = ("\u203a", "»")


def composer_gutter(line: str) -> str | None:
    stripped = line.lstrip(" ")
    if stripped[:1] not in NATIVE_COMPOSER_GLYPHS or (len(stripped) > 1 and stripped[1] != " "):
        return None
    return line[: len(line) - len(stripped)] + stripped[:1] + " "


def matches_composer(
    rows: tuple[str, ...], cursor_x: int, text: str, pane_width: int, *, profile: TerminalProfile = TMUX_32_PROFILE
) -> bool:
    """Match all known text in a complete composer ending at the captured cursor.

    tmux omits trailing blanks; Codex can hide separator spaces at soft word
    wraps. Consume only those known spaces or one explicit newline between rows,
    never arbitrary whitespace or missing text. The last row's end is exact.
    """
    if not profile.verified or not rows or (gutter := composer_gutter(rows[0])) is None:
        return False
    # Codex reserves its gutter and one right-margin column for the textarea.
    width = pane_width - len(gutter) - 1
    if width < 1 or not len(gutter) <= cursor_x < len(gutter) + width:
        return False
    offset = 0
    for index, row in enumerate(rows):
        start = offset
        prefix = gutter if index == 0 else " " * len(gutter)
        if not (row.startswith(prefix) or row.rstrip(" ") == prefix.rstrip(" ")):
            return False
        visible = row[len(prefix) :].rstrip(" ")
        if index == len(rows) - 1:
            remaining = text[offset:]
            if "\n" in remaining:
                return False
            expected = NativeTerminalScreen(pane_width, 1, profile=profile)
            expected.draw(prefix + remaining)
            return expected.row_text(0).rstrip(" ") == row.rstrip(" ") and cursor_x == expected.cursor.x
        # Capture text may contain native selector-expansion gaps. Resolve
        # the source prefix by drawing through the same profile, not by
        # treating Unicode indexes as terminal columns.
        expected = NativeTerminalScreen(pane_width, 1, profile=profile)
        expected.draw(prefix)
        matched = None
        for length, character in enumerate(text[offset : offset + pane_width * profile.cluster_bytes], 1):
            if character == "\n":
                break
            expected.draw(character)
            if expected.row_text(0).rstrip(" ") == row.rstrip(" "):
                matched = length
            if expected.cursor.x >= pane_width - 1:
                break
        if matched is None:
            if visible:
                return False
            matched = 0
        offset += matched
        while offset < len(text) and text[offset] == " ":
            offset += 1
        if text[offset : offset + 1] == "\n":
            offset += 1
        else:
            # A soft break must be forced by the actual pane width. Never accept
            # arbitrary rows as concatenated text ("a" / "b" is not "ab").
            consumed = text[start:offset]
            remaining = text[offset:]
            next_fragment = remaining.split("\n", 1)[0].split(" ", 1)[0]
            if not consumed:
                return False
            if not consumed.endswith((" ", "-", "/", "—")):
                next_fragment = remaining[:1]
            # A full logical line has an extra empty insertion row.
            if profile.text_width(consumed + next_fragment) <= width and (
                remaining or profile.text_width(consumed) != width
            ):
                return False
    return False
