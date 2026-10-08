"""Active-turn terminal-title projection without native TUI animation."""

from __future__ import annotations

import math
from typing import Final

TERMINAL_TITLE_IDLE_FIELD: Final = "---------"
TERMINAL_TITLE_FRAME_INTERVAL_SECONDS: Final = 0.25
TERMINAL_TITLE_ELAPSED_INTERVAL_SECONDS: Final = 5.0
TERMINAL_TITLE_ELAPSED_VISIBLE_SECONDS: Final = 1.0

# A fixed-width breathing pulse made only from the title's nine dash cells.
TERMINAL_TITLE_ANIMATION_FRAMES: Final = (
    "---------",
    " ------- ",
    "  -----  ",
    "   ---   ",
    "    -    ",
    "   ---   ",
    "  -----  ",
    " ------- ",
)


def working_terminal_title_field(elapsed_seconds: float) -> str:
    """Render the active title field for one monotonic elapsed duration."""
    if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("working title elapsed time must be finite and non-negative")
    cycle_position = elapsed_seconds % TERMINAL_TITLE_ELAPSED_INTERVAL_SECONDS
    if (
        elapsed_seconds >= TERMINAL_TITLE_ELAPSED_INTERVAL_SECONDS
        and cycle_position < TERMINAL_TITLE_ELAPSED_VISIBLE_SECONDS
    ):
        return format_working_elapsed(math.floor(elapsed_seconds))
    frame_index = math.floor(elapsed_seconds / TERMINAL_TITLE_FRAME_INTERVAL_SECONDS)
    return TERMINAL_TITLE_ANIMATION_FRAMES[frame_index % len(TERMINAL_TITLE_ANIMATION_FRAMES)]


def terminal_title_pane_escape(field: str) -> bytes:
    """Encode one trusted field as the pane title consumed by tmux."""
    if not field or any(character not in " -0123456789dhms" for character in field):
        raise ValueError("terminal title field contains unsupported characters")
    return f"\x1b]2;{field}\x07".encode()


def format_working_elapsed(total_seconds: int) -> str:
    """Use seconds below an hour, then favour the two most useful larger units."""
    if not isinstance(total_seconds, int) or isinstance(total_seconds, bool) or total_seconds < 0:
        raise ValueError("working title elapsed seconds must be a non-negative integer")
    if total_seconds >= 24 * 60 * 60:
        days, remainder = divmod(total_seconds, 24 * 60 * 60)
        hours, remainder = divmod(remainder, 60 * 60)
        minutes = remainder // 60
        return f"{days}d{hours:02}h{minutes:02}m"
    if total_seconds >= 60 * 60:
        hours, remainder = divmod(total_seconds, 60 * 60)
        minutes = remainder // 60
        return f"{hours}h{minutes:02}m"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m{seconds:02}s"
