"""One publisher-aware pipeline for Rodex tmux status-left rendering."""

from __future__ import annotations

import math
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Final


class StatusBarPart(StrEnum):
    """Stable names for independently changeable Rodex status segments."""

    RODEX_IDENTITY = "rodex_identity"
    TOOL_COUNT = "tool_count"
    MOUSE_MODE = "mouse_mode"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class StatusBarSegment:
    """One independently styled and rendered tmux status segment."""

    part: StatusBarPart
    foreground: str
    content_format: str

    def __post_init__(self) -> None:
        if not self.foreground or not self.content_format:
            raise ValueError("status segment fields must be non-empty")

    def render(self) -> str:
        """Render this segment without inheriting another segment's colour."""
        return f"#[fg={self.foreground}]#[bold]{self.content_format}"


@dataclass(frozen=True, slots=True)
class TmuxStatusBar:
    """An immutable status bar whose named parts can only replace themselves."""

    segments: tuple[StatusBarSegment, ...]

    def __post_init__(self) -> None:
        parts = tuple(segment.part for segment in self.segments)
        if not parts or len(parts) != len(set(parts)):
            raise ValueError("status bar must contain uniquely named segments")

    def update_status_bar(
        self,
        which_part: StatusBarPart | str,
        with_what: StatusBarSegment,
    ) -> TmuxStatusBar:
        """Return a bar with exactly one named segment replaced."""
        part = StatusBarPart(which_part)
        if with_what.part is not part:
            raise ValueError("replacement segment must own the selected status part")
        if part not in {segment.part for segment in self.segments}:
            raise KeyError(part)
        return TmuxStatusBar(
            tuple(
                with_what if segment.part is part else segment for segment in self.segments
            )
        )

    def modify_colour(
        self,
        which_part: StatusBarPart | str,
        foreground: str,
    ) -> TmuxStatusBar:
        """Return a bar with only the selected segment's colour changed."""
        part = StatusBarPart(which_part)
        return self.update_status_bar(
            part,
            replace(self.segment(part), foreground=foreground),
        )

    def modify_content(
        self,
        which_part: StatusBarPart | str,
        content_format: str,
    ) -> TmuxStatusBar:
        """Return a bar with only the selected segment's content changed."""
        part = StatusBarPart(which_part)
        return self.update_status_bar(
            part,
            replace(self.segment(part), content_format=content_format),
        )

    def segment(self, which_part: StatusBarPart | str) -> StatusBarSegment:
        """Return the one segment identified by its stable part name."""
        part = StatusBarPart(which_part)
        for segment in self.segments:
            if segment.part is part:
                return segment
        raise KeyError(part)

    def render_part(self, which_part: StatusBarPart | str) -> str:
        """Render one selected segment."""
        return self.segment(which_part).render()


class TmuxStatusOption:
    """Publish one rendered status value through one pane-stable tmux option."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        tmux_pane_target: str,
        option_name: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        if not tmux_pane_target.strip() or not option_name.startswith("@"):
            raise ValueError("tmux pane target and user option must be valid")
        self._command_prefix = (
            tmux_binary,
            "-S",
            str(tmux_server_socket_path),
            "set-option",
            "-t",
            tmux_pane_target,
            option_name,
        )
        self._run = runner

    def publish(self, value: str) -> None:
        """Publish one non-empty value without exposing tmux mechanics to its owner."""
        if not isinstance(value, str) or not value:
            raise ValueError("tmux status option value must be non-empty")
        self._run(
            [*self._command_prefix, value],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


@dataclass(frozen=True, slots=True)
class _ContextColourBand:
    """One lower-bound colour decision owned only by the context segment."""

    minimum_percent: float
    foreground: str


STATUS_LEFT_CLAIM_PRIORITY_OPTION: Final = "@rodex_status_left_claim_priority"
STATUS_LEFT_CLAIM_PUBLISHER_OPTION: Final = "@rodex_status_left_claim_publisher"
STATUS_LEFT_CLAIM_TOKEN_OPTION: Final = "@rodex_status_left_claim_token"
STATUS_ANIMATION_TOKEN_OPTION: Final = "@rodex_status_animation_token"
STATUS_ANIMATION_FRAME_INTERVAL_SECONDS: Final = 0.2
RODEX_CONTEXT_STATUS_OPTION: Final = "@rodex_context_status"
RODEX_TOOL_CALL_STATUS_OPTION: Final = "@rodex_tool_calls"
STATUS_LEFT_PUBLISHER_COMPLETION: Final = "completion"
STATUS_LEFT_PUBLISHER_SHARED_CTRL_C: Final = "shared-ctrl-c"
_CONTEXT_COMPACTION_FRAMES: Final = (
    "COMPACTING   ",
    "COMPACTING.  ",
    "COMPACTING.. ",
    "COMPACTING...",
)
_RODEX_IDENTITY_FOREGROUND: Final = "#0A22FF"
_TOOL_COUNT_FOREGROUND: Final = "cyan"
_MOUSE_MODE_FOREGROUND: Final = "yellow"
_CONTEXT_NORMAL_FOREGROUND: Final = "#0A22FF"
_CONTEXT_WARNING_FOREGROUND: Final = "#E6FF47"
_CONTEXT_DANGER_FOREGROUND: Final = "#FF002E"
_BASE_STATUS_BAR: Final = TmuxStatusBar(
    (
        StatusBarSegment(
            part=StatusBarPart.RODEX_IDENTITY,
            foreground=_RODEX_IDENTITY_FOREGROUND,
            content_format=" Rodex: #S ",
        ),
        StatusBarSegment(
            part=StatusBarPart.TOOL_COUNT,
            foreground=_TOOL_COUNT_FOREGROUND,
            content_format=f"| Tools: #{{{RODEX_TOOL_CALL_STATUS_OPTION}}} ",
        ),
        StatusBarSegment(
            part=StatusBarPart.MOUSE_MODE,
            foreground=_MOUSE_MODE_FOREGROUND,
            content_format="| Mouse: #{?mouse,ON,OFF} ",
        ),
        StatusBarSegment(
            part=StatusBarPart.CONTEXT,
            foreground=_CONTEXT_NORMAL_FOREGROUND,
            content_format="| Context: -- | ",
        ),
    )
)
_CONTEXT_DANGER_BAND: Final = _ContextColourBand(
    minimum_percent=75,
    foreground=_CONTEXT_DANGER_FOREGROUND,
)
_CONTEXT_WARNING_BAND: Final = _ContextColourBand(
    minimum_percent=70,
    foreground=_CONTEXT_WARNING_FOREGROUND,
)
_CONTEXT_NORMAL_BAND: Final = _ContextColourBand(
    minimum_percent=0,
    foreground=_CONTEXT_NORMAL_FOREGROUND,
)
_CONTEXT_COLOUR_BANDS: Final = (
    _CONTEXT_DANGER_BAND,
    _CONTEXT_WARNING_BAND,
    _CONTEXT_NORMAL_BAND,
)
RODEX_BASE_STATUS_LEFT_FORMAT: Final = (
    f"{_BASE_STATUS_BAR.render_part(StatusBarPart.RODEX_IDENTITY)}"
    f"{_BASE_STATUS_BAR.render_part(StatusBarPart.TOOL_COUNT)}"
    f"{_BASE_STATUS_BAR.render_part(StatusBarPart.MOUSE_MODE)}"
    f"#{{?#{{{RODEX_CONTEXT_STATUS_OPTION}}},"
    f"#{{E:{RODEX_CONTEXT_STATUS_OPTION}}},"
    f"{_BASE_STATUS_BAR.render_part(StatusBarPart.CONTEXT)}}}"
    "#[default]"
)
PREFIX_MODE_STATUS_FORMAT: Final = "#[bg=colour24]#[fg=white]#[bold] CTRL-B MODE #[default]"
RODEX_STATUS_LEFT_FORMAT: Final = (
    "#{?#{&&:#{client_prefix},#{==:#{prefix},C-b}},"
    f"{PREFIX_MODE_STATUS_FORMAT},"
    f"{RODEX_BASE_STATUS_LEFT_FORMAT}"
    "}"
)
RODEX_STATUS_LEFT_LENGTH: Final = "160"

BoundTmuxCommand = Callable[..., subprocess.CompletedProcess[str]]


class StatusLeftPriority(IntEnum):
    """Stable precedence for competing passive status-left messages."""

    COMPLETION = 10
    SAFETY_WARNING = 100


class TmuxStatusLeftPipeline:
    """Atomically publish and restore pane status-left claims."""

    def __init__(self, tmux: BoundTmuxCommand, pane_target: str) -> None:
        self._tmux = tmux
        self._pane_target = pane_target

    def publish_transient(
        self,
        *,
        publisher: str,
        token: str,
        priority: StatusLeftPriority,
        status_format: str,
    ) -> bool:
        """Atomically claim and render unless a higher priority is present."""
        if not publisher or not token or not status_format:
            raise ValueError("status publisher, token, and format must be non-empty")
        claim_and_render = _tmux_command_sequence(
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_LEFT_CLAIM_PUBLISHER_OPTION,
                publisher,
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_LEFT_CLAIM_TOKEN_OPTION,
                token,
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_LEFT_CLAIM_PRIORITY_OPTION,
                str(int(priority)),
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                "status-left",
                status_format,
            ),
        )
        result = self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            f"#{{<=:#{{{STATUS_LEFT_CLAIM_PRIORITY_OPTION}}},{int(priority)}}}",
            claim_and_render,
        )
        if result.returncode != 0:
            return False
        advertised = self._tmux(
            "show-options",
            "-v",
            "-t",
            self._pane_target,
            STATUS_LEFT_CLAIM_TOKEN_OPTION,
        )
        return advertised.returncode == 0 and advertised.stdout.strip() == token

    def restore_if_token_matches(self, token: str) -> None:
        """Atomically restore only when the exact publisher token still matches."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            f"#{{==:#{{{STATUS_LEFT_CLAIM_TOKEN_OPTION}}},{token}}}",
            self._restore_base_status_command(),
        )

    def restore_if_publisher_matches(self, publisher: str) -> None:
        """Atomically restore only when the named subsystem still publishes."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            f"#{{==:#{{{STATUS_LEFT_CLAIM_PUBLISHER_OPTION}}},{publisher}}}",
            self._restore_base_status_command(),
        )

    def reset_to_base_status(self) -> bool:
        """Clear every transient claim and publish the normal Rodex status."""
        result = self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            "1",
            self._restore_base_status_command(),
        )
        return result.returncode == 0

    def _restore_base_status_command(self) -> str:
        return _tmux_command_sequence(
            *(
                (
                    "set-option",
                    "-u",
                    "-t",
                    self._pane_target,
                    option,
                )
                for option in (
                    STATUS_LEFT_CLAIM_PUBLISHER_OPTION,
                    STATUS_LEFT_CLAIM_TOKEN_OPTION,
                    STATUS_LEFT_CLAIM_PRIORITY_OPTION,
                )
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                "status-left",
                RODEX_STATUS_LEFT_FORMAT,
            ),
        )


def _tmux_command_sequence(*commands: tuple[str, ...]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def completion_status_left_format(message: str) -> str:
    """Render a passive Rodex completion hint in the ordinary status line."""
    return f"#[fg=magenta,bold] {message} #[default]"


def context_status_segment(context_percent: float | None) -> str:
    """Render the live context fill using Rodex's compaction-warning bands."""
    if context_percent is None:
        return _BASE_STATUS_BAR.render_part(StatusBarPart.CONTEXT)
    if (
        isinstance(context_percent, bool)
        or not isinstance(context_percent, (int, float))
        or not math.isfinite(context_percent)
        or context_percent < 0
    ):
        raise ValueError("context percent must be a finite non-negative number or None")
    exact_percent = float(context_percent)
    displayed_percent = math.floor(exact_percent + 0.5)
    foreground = next(
        band.foreground
        for band in _CONTEXT_COLOUR_BANDS
        if exact_percent >= band.minimum_percent
    )
    return (
        _BASE_STATUS_BAR.modify_colour(StatusBarPart.CONTEXT, foreground)
        .modify_content(
            StatusBarPart.CONTEXT,
            f"| Context: {displayed_percent}% | ",
        )
        .render_part(StatusBarPart.CONTEXT)
    )


def compacting_status_segment(frame_index: int) -> str:
    """Render one fixed-width frame of the live compaction indicator."""
    if isinstance(frame_index, bool) or not isinstance(frame_index, int):
        raise ValueError("compaction frame index must be an integer")
    frame = _CONTEXT_COMPACTION_FRAMES[frame_index % len(_CONTEXT_COMPACTION_FRAMES)]
    return (
        _BASE_STATUS_BAR.modify_colour(
            StatusBarPart.CONTEXT,
            _CONTEXT_DANGER_BAND.foreground,
        )
        .modify_content(StatusBarPart.CONTEXT, f"| {frame} | ")
        .render_part(StatusBarPart.CONTEXT)
    )
