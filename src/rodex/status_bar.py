"""Typed Rodex status-bar parts, palette, layout, and dynamic rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final


class StatusBarPart(StrEnum):
    """Stable names for independently changeable Rodex status segments."""

    RODEX_IDENTITY = "rodex_identity"
    TOOL_COUNT = "tool_count"
    MOUSE_MODE = "mouse_mode"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class StatusBarColours:
    """One authoritative palette for every Rodex-owned status colour role."""

    base_foreground: str
    base_background: str
    primary_blue: str
    tool_count: str
    mouse_mode: str
    context_warning: str
    context_danger: str
    context_compaction_foregrounds: tuple[str, ...]
    sharing_shared: str
    sharing_private: str
    completion: str
    safety_foreground: str
    safety_background: str
    prefix_foreground: str
    prefix_background: str
    animation_foreground: str
    animation_arrival_backgrounds: tuple[str, ...]
    animation_departure_backgrounds: tuple[str, ...]

    def __post_init__(self) -> None:
        scalar_colours = (
            self.base_foreground,
            self.base_background,
            self.primary_blue,
            self.tool_count,
            self.mouse_mode,
            self.context_warning,
            self.context_danger,
            self.sharing_shared,
            self.sharing_private,
            self.completion,
            self.safety_foreground,
            self.safety_background,
            self.prefix_foreground,
            self.prefix_background,
            self.animation_foreground,
        )
        animation_colours = (
            *self.context_compaction_foregrounds,
            *self.animation_arrival_backgrounds,
            *self.animation_departure_backgrounds,
        )
        if not all((*scalar_colours, *animation_colours)):
            raise ValueError("status-bar colours must be non-empty")


@dataclass(frozen=True, slots=True)
class StatusBarSegment:
    """One independently styled and rendered status segment."""

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
        self.segment(part)
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

    def render(self) -> str:
        """Render every segment once in the authoritative tuple order."""
        return "".join(segment.render() for segment in self.segments)


@dataclass(frozen=True, slots=True)
class _ContextColourBand:
    minimum_percent: float
    foreground: str


RODEX_CONTEXT_STATUS_OPTION: Final = "@rodex_context_status"
RODEX_TOOL_CALL_STATUS_OPTION: Final = "@rodex_tool_calls"
RODEX_STATUS_COLOURS: Final = StatusBarColours(
    base_foreground="black",
    base_background="#B6FF00",
    primary_blue="#1402D8",
    tool_count="cyan",
    mouse_mode="yellow",
    context_warning="yellow",
    context_danger="red",
    context_compaction_foregrounds=(
        "colour45",
        "colour51",
        "colour87",
        "colour123",
        "colour159",
        "colour195",
        "colour231",
        "colour231",
        "colour195",
        "colour159",
        "colour123",
        "colour87",
        "colour51",
        "colour45",
    ),
    sharing_shared="yellow",
    sharing_private="green",
    completion="magenta",
    safety_foreground="black",
    safety_background="yellow",
    prefix_foreground="white",
    prefix_background="colour24",
    animation_foreground="colour231",
    animation_arrival_backgrounds=(
        "colour17",
        "colour18",
        "colour19",
        "colour54",
        "colour55",
        "colour56",
        "colour57",
        "colour93",
        "colour129",
        "colour165",
        "colour201",
        "colour198",
        "colour165",
        "colour129",
        "colour93",
        "colour57",
        "colour45",
        "colour51",
        "colour45",
        "colour87",
        "colour45",
        "colour39",
        "colour33",
        "colour24",
        "colour22",
    ),
    animation_departure_backgrounds=(
        "colour93",
        "colour129",
        "colour165",
        "colour201",
        "colour198",
        "colour165",
        "colour129",
        "colour93",
        "colour57",
        "colour56",
        "colour55",
        "colour54",
        "colour19",
        "colour18",
        "colour17",
        "colour22",
        "colour28",
        "colour34",
        "colour40",
        "colour46",
        "colour40",
        "colour34",
        "colour28",
        "colour22",
        "colour22",
    ),
)
RODEX_STATUS_STYLE: Final = (
    f"bg={RODEX_STATUS_COLOURS.base_background},fg={RODEX_STATUS_COLOURS.base_foreground}"
)

_CONTEXT_FALLBACK_SEGMENT: Final = StatusBarSegment(
    part=StatusBarPart.CONTEXT,
    foreground=RODEX_STATUS_COLOURS.primary_blue,
    content_format="| Context: -- | ",
)
_BASE_STATUS_BAR: Final = TmuxStatusBar(
    (
        StatusBarSegment(
            part=StatusBarPart.RODEX_IDENTITY,
            foreground=RODEX_STATUS_COLOURS.primary_blue,
            content_format=" Rodex: #S ",
        ),
        StatusBarSegment(
            part=StatusBarPart.TOOL_COUNT,
            foreground=RODEX_STATUS_COLOURS.tool_count,
            content_format=f"| Tools: #{{{RODEX_TOOL_CALL_STATUS_OPTION}}} ",
        ),
        StatusBarSegment(
            part=StatusBarPart.MOUSE_MODE,
            foreground=RODEX_STATUS_COLOURS.mouse_mode,
            content_format="| Mouse: #{?mouse,ON,OFF} ",
        ),
        StatusBarSegment(
            part=StatusBarPart.CONTEXT,
            foreground=RODEX_STATUS_COLOURS.primary_blue,
            content_format=(
                f"#{{?#{{{RODEX_CONTEXT_STATUS_OPTION}}},"
                f"#{{E:{RODEX_CONTEXT_STATUS_OPTION}}},"
                f"{_CONTEXT_FALLBACK_SEGMENT.content_format}}}"
            ),
        ),
    )
)

_CONTEXT_DANGER_BAND: Final = _ContextColourBand(
    minimum_percent=75,
    foreground=RODEX_STATUS_COLOURS.context_danger,
)
_CONTEXT_COLOUR_BANDS: Final = (
    _CONTEXT_DANGER_BAND,
    _ContextColourBand(
        minimum_percent=70,
        foreground=RODEX_STATUS_COLOURS.context_warning,
    ),
    _ContextColourBand(
        minimum_percent=0,
        foreground=RODEX_STATUS_COLOURS.primary_blue,
    ),
)
CONTEXT_COMPACTION_FRAME_INTERVAL_SECONDS: Final = 0.12
_CONTEXT_COMPACTION_ACTIVITY_FRAMES: Final = (
    "▏",
    "▎",
    "▍",
    "▌",
    "▋",
    "▊",
    "▉",
    "█",
    "▉",
    "▊",
    "▋",
    "▌",
    "▍",
    "▎",
)

RODEX_BASE_STATUS_LEFT_FORMAT: Final = f"{_BASE_STATUS_BAR.render()}#[default]"
PREFIX_MODE_STATUS_FORMAT: Final = (
    f"#[bg={RODEX_STATUS_COLOURS.prefix_background}]"
    f"#[fg={RODEX_STATUS_COLOURS.prefix_foreground}]"
    "#[bold] CTRL-B MODE #[default]"
)
RODEX_STATUS_LEFT_FORMAT: Final = (
    "#{?#{&&:#{client_prefix},#{==:#{prefix},C-b}},"
    f"{PREFIX_MODE_STATUS_FORMAT},"
    f"{RODEX_BASE_STATUS_LEFT_FORMAT}"
    "}"
)
RODEX_STATUS_LEFT_LENGTH: Final = "160"
RODEX_STATUS_RIGHT_FORMAT: Final = (
    "#{?session_many_attached,"
    f"#[fg={RODEX_STATUS_COLOURS.sharing_shared}]#[bold] "
    "[Shared with #{e|-:#{session_attached},1} "
    "#{?#{==:#{session_attached},2},other,others}] #[default],"
    f"#[fg={RODEX_STATUS_COLOURS.sharing_private}]#[bold] "
    "[Private session] #[default]}"
    " | %H:%M %d-%b-%y"
)
RODEX_STATUS_RIGHT_LENGTH: Final = "64"


def context_status_segment(context_percent: float | None) -> str:
    """Render the live context fill using the authoritative context palette."""
    if context_percent is None:
        return _CONTEXT_FALLBACK_SEGMENT.render()
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
    return replace(
        _CONTEXT_FALLBACK_SEGMENT,
        foreground=foreground,
        content_format=f"| Context: {displayed_percent}% | ",
    ).render()


def compacting_status_segment(frame_index: int) -> str:
    """Render one fixed-width, colour-pulsing context-compaction frame."""
    if isinstance(frame_index, bool) or not isinstance(frame_index, int):
        raise ValueError("compaction frame index must be an integer")
    frame_position = frame_index % len(_CONTEXT_COMPACTION_ACTIVITY_FRAMES)
    activity = _CONTEXT_COMPACTION_ACTIVITY_FRAMES[frame_position]
    foreground = RODEX_STATUS_COLOURS.context_compaction_foregrounds[frame_position]
    return replace(
        _CONTEXT_FALLBACK_SEGMENT,
        foreground=foreground,
        content_format=f"| COMPACTING {activity} | ",
    ).render()
