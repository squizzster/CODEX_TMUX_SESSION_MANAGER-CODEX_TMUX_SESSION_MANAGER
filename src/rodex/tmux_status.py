"""Shared tmux status formats owned by Rodex."""

from __future__ import annotations

from typing import Final

COMPLETION_TOKEN_OPTION: Final = "@rodex_completion_token"
RODEX_STATUS_LEFT_FORMAT: Final = (
    "#[fg=green,bold] Rodex: #S "
    "#[fg=cyan,bold]| Tools: #{@rodex_tool_calls} "
    "#[fg=yellow,bold]| Mouse: #{?mouse,ON,OFF} #[default]"
)
RODEX_STATUS_LEFT_LENGTH: Final = "82"


def completion_status_left_format(message: str) -> str:
    """Render a passive Rodex completion hint in the ordinary status line."""
    return f"#[fg=magenta,bold] {message} #[default]"
