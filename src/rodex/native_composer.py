"""Recognize visible native composer text, never manufacture editor state.

Codex uses \u203a ordinarily and » for Ultra. Wrapped and explicit continuation rows
have the same two-column gutter. A snapshot can acknowledge only fully visible
text; collapsed paste placeholders and scrolled drafts cannot establish equality.
"""

from wcwidth import wcswidth

NATIVE_COMPOSER_GLYPHS = ("\u203a", "»")


def composer_gutter(line: str) -> str | None:
    stripped = line.lstrip(" ")
    if stripped[:1] not in NATIVE_COMPOSER_GLYPHS or (len(stripped) > 1 and stripped[1] != " "):
        return None
    return line[: len(line) - len(stripped)] + stripped[:1] + " "


def matches_composer(rows: tuple[str, ...], cursor_x: int, text: str, pane_width: int) -> bool:
    """Match all known text in a complete composer ending at the captured cursor.

    tmux omits trailing blanks; Codex can hide separator spaces at soft word
    wraps. Consume only those known spaces or one explicit newline between rows,
    never arbitrary whitespace or missing text. The last row's end is exact.
    """
    if not rows or (gutter := composer_gutter(rows[0])) is None:
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
            return "\n" not in remaining and remaining.rstrip(" ") == visible and cursor_x == wcswidth(prefix + remaining)
        if not text.startswith(visible, offset):
            return False
        offset += len(visible)
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
            if wcswidth(consumed + next_fragment) <= width and (remaining or wcswidth(consumed) != width):
                return False
    return False
