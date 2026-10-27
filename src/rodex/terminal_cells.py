"""Terminal-profile clusters: text, occupied cells and style travel together.

These are tmux grid clusters, not a claim that every Unicode grapheme has the
same occupancy on every terminal. The runtime binds the exact server profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from wcwidth import wcwidth


class TerminalCell(NamedTuple):
    """One grid entry; a leading cluster carries its width, padding carries zero."""

    data: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False
    occupied_cells: int = 1
    continuation: bool = False

    @property
    def style(self) -> tuple:
        return self[1:9]

    def padding(self) -> TerminalCell:
        return self._replace(data="", occupied_cells=0, continuation=True)


@dataclass(frozen=True, slots=True)
class TerminalProfile:
    name: str
    server_version: str
    joined_emoji: bool = False
    verified: bool = True
    cluster_bytes: int = 21

    def character_width(self, character: str) -> int:
        if not self.joined_emoji and 0x1F1E6 <= ord(character) <= 0x1F1FF:
            return 1
        return wcwidth(character)

    def combine(self, previous: TerminalCell, character: str) -> TerminalCell | None:
        """Return the native cluster continuation, including width-changing VS16."""
        width = self.character_width(character)
        modifier = 0x1F1E6 <= ord(character) <= 0x1F1FF or 0x1F3FB <= ord(character) <= 0x1F3FF
        force_wide = self.joined_emoji and (character == "\ufe0f" or modifier)
        joins = width == 0 or (
            self.joined_emoji
            and len(character.encode()) > 1
            and (previous.data.endswith("\u200d") or (modifier and len(previous.data.encode()) > 1))
        )
        if not joins or previous.continuation or len((previous.data + character).encode()) > self.cluster_bytes:
            return None
        return previous._replace(
            data=previous.data + character,
            occupied_cells=max(previous.occupied_cells, 2 if force_wide else 1),
        )

    def clusters(self, text: str) -> list[TerminalCell]:
        """Group source text using the authoritative grid continuation rules.

        Source groups retain selectors/joiners while their occupied span comes
        from cursor movement. In 3.4 a selector's intervening grid cell can be
        the next combining target; the previous logical cluster alone cannot
        decide whether another character joins.
        """
        from .native_terminal_screen import NativeTerminalScreen

        screen = NativeTerminalScreen(max(2, len(text) * 2 + 2), 1, profile=self)
        result: list[TerminalCell] = []
        start_column = 0
        for character in text:
            before = screen.cursor.x
            changed = screen.draw_character(character)
            if changed is None:
                continue
            if result and changed < before:
                result[-1] = result[-1]._replace(
                    data=result[-1].data + character, occupied_cells=screen.cursor.x - start_column
                )
            else:
                start_column = before
                result.append(TerminalCell(data=character, occupied_cells=screen.cursor.x - before))
        return result

    def text_width(self, text: str) -> int:
        return sum(cell.occupied_cells for cell in self.clusters(text))


TMUX_32_PROFILE = TerminalProfile("tmux-3.2a-separate-emoji", "3.2a")
TMUX_34_PROFILE = TerminalProfile("tmux-3.4-joined-emoji", "3.4", joined_emoji=True)


def terminal_profile(server_version: str) -> TerminalProfile:
    version = server_version.strip()
    if version == "3.2a":
        return TMUX_32_PROFILE
    if version == "3.4":
        return TMUX_34_PROFILE
    return TerminalProfile(f"unverified-tmux-{version or 'unavailable'}", version, verified=False)


def terminal_safe_text(text: str) -> str:
    return "".join(character if character in "\n\t\u200c\u200d" or character.isprintable() else " " for character in text)
