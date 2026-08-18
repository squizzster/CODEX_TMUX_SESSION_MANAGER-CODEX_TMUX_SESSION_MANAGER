"""Parse and follow human-readable terminal output from one Rodex session."""

from __future__ import annotations

import re
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TextIO

from .command_contract import TAIL_COMMAND
from .errors import RodexLaunchError
from .runtime import LiveTmuxSession, TmuxScrollbackSnapshot

DEFAULT_TAIL_LINE_COUNT: Final = 10
TAIL_POLL_INTERVAL_SECONDS: Final = 0.4
TAIL_SETTLED_POLL_COUNT: Final = 3
TAIL_USAGE: Final = (
    "usage: rodex _tail [-f|--follow] [-n NUM|--lines NUM|--lines=NUM|-NUM] SESSION_NAME"
)
_SHORT_LINE_COUNT = re.compile(r"-[0-9]+")
_LINE_COUNT = re.compile(r"[+-]?[0-9]+")
_ACTIVE_STATUS_LINE = re.compile(
    r"^[•◦]\s+(?:Working|Waiting for background terminal)\s+"
    r"\((?:[0-9]+h )?(?:[0-9]+m )?[0-9]+s\s+•\s+"
)
_COMPOSER_LINE = re.compile(r"^\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}(?: |$)")
_STATUS_CONTINUATION_LINE = re.compile(r"^\s+\S")
_STATUS_REGION_MAX_LINE_COUNT: Final = 6

CaptureScrollback = Callable[[LiveTmuxSession], TmuxScrollbackSnapshot]
Revalidate = Callable[[], None]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class SessionTailRequest:
    """One parsed terminal-follow request using familiar tail line selection."""

    session_name: str
    line_count: int = DEFAULT_TAIL_LINE_COUNT
    from_start: bool = False


class PlainTailCursor:
    """Follow committed history immediately and debounce the visible pane."""

    def __init__(
        self,
        initial_snapshot: TmuxScrollbackSnapshot,
        *,
        settled_poll_count: int = TAIL_SETTLED_POLL_COUNT,
    ) -> None:
        if settled_poll_count < 1:
            raise ValueError("settled poll count must be positive")
        self._history_lines = initial_snapshot.history_lines
        self._observed_visible = deque(initial_snapshot.visible_lines)
        initial_visible = _settled_visible_lines(initial_snapshot.visible_lines)
        self._published_visible = initial_visible
        self._candidate_visible = initial_visible
        self._candidate_poll_count = 0
        self._settled_poll_count = settled_poll_count

    def advance(self, current_snapshot: TmuxScrollbackSnapshot) -> tuple[str, ...]:
        if len(current_snapshot.history_lines) < len(self._history_lines):
            self._rebaseline(current_snapshot)
            return ()

        emitted = self._new_committed_history(current_snapshot)
        if emitted is None:
            self._rebaseline(current_snapshot)
            return ()
        current_visible = current_snapshot.visible_lines
        settled_visible = _settled_visible_lines(current_visible)
        if settled_visible != self._candidate_visible:
            self._candidate_visible = settled_visible
            self._candidate_poll_count = 1
        else:
            self._candidate_poll_count += 1
        if (
            self._candidate_poll_count >= self._settled_poll_count
            and settled_visible != self._published_visible
        ):
            emitted.extend(_new_rendered_lines(self._published_visible, settled_visible))
            self._published_visible = settled_visible
            self._observed_visible = deque(current_visible)
        return tuple(emitted)

    def _new_committed_history(
        self, current_snapshot: TmuxScrollbackSnapshot
    ) -> list[str] | None:
        current_history = current_snapshot.history_lines
        previous_history = self._history_lines
        if current_history[: len(previous_history)] == previous_history:
            overlap = len(previous_history)
        else:
            overlap = _suffix_prefix_overlap(previous_history, current_history)
            if previous_history and not overlap:
                return None
        newly_committed = current_history[overlap:]
        self._history_lines = current_history
        emitted: list[str] = []
        for line in newly_committed:
            if self._observed_visible:
                observed = self._observed_visible.popleft()
                if line == observed:
                    continue
            emitted.append(line)
        return emitted

    def _rebaseline(self, snapshot: TmuxScrollbackSnapshot) -> None:
        self._history_lines = snapshot.history_lines
        self._observed_visible = deque(snapshot.visible_lines)
        settled_visible = _settled_visible_lines(snapshot.visible_lines)
        self._published_visible = settled_visible
        self._candidate_visible = settled_visible
        self._candidate_poll_count = 0


def parse_session_tail_request(arguments: Sequence[str]) -> SessionTailRequest:
    """Parse the intentionally small tail-compatible Rodex grammar."""
    if not arguments or arguments[0] != TAIL_COMMAND:
        raise RodexLaunchError(TAIL_USAGE)

    session_name: str | None = None
    line_count = DEFAULT_TAIL_LINE_COUNT
    from_start = False
    index = 1
    options_enabled = True
    while index < len(arguments):
        argument = arguments[index]
        if options_enabled and argument == "--":
            options_enabled = False
            index += 1
            continue
        if options_enabled and argument in {"-f", "--follow"}:
            index += 1
            continue
        if options_enabled and argument in {"-n", "--lines"}:
            index += 1
            if index >= len(arguments):
                raise RodexLaunchError(TAIL_USAGE)
            line_count, from_start = _parse_line_count(arguments[index])
            index += 1
            continue
        if options_enabled and argument.startswith("--lines="):
            line_count, from_start = _parse_line_count(argument.partition("=")[2])
            index += 1
            continue
        if options_enabled and _SHORT_LINE_COUNT.fullmatch(argument):
            line_count, from_start = _parse_line_count(argument)
            index += 1
            continue
        if options_enabled and argument.startswith("-"):
            raise RodexLaunchError(TAIL_USAGE)
        if session_name is not None or not argument:
            raise RodexLaunchError(TAIL_USAGE)
        session_name = argument
        index += 1

    if session_name is None:
        raise RodexLaunchError(TAIL_USAGE)
    return SessionTailRequest(session_name, line_count, from_start)


def follow_session_tail(
    request: SessionTailRequest,
    runtime: LiveTmuxSession,
    capture_scrollback: CaptureScrollback,
    revalidate: Revalidate,
    *,
    output: TextIO | None = None,
    sleep: Sleeper = time.sleep,
    poll_interval_seconds: float = TAIL_POLL_INTERVAL_SECONDS,
) -> None:
    """Print recent plain text, then follow settled rendered changes."""
    if poll_interval_seconds <= 0:
        raise ValueError("tail poll interval must be positive")
    destination = sys.stdout if output is None else output

    initial = capture_scrollback(runtime)
    revalidate()
    _write_lines(_select_initial_lines(initial.lines, request), destination)
    cursor = PlainTailCursor(initial)

    while True:
        sleep(poll_interval_seconds)
        current = capture_scrollback(runtime)
        revalidate()
        _write_lines(cursor.advance(current), destination)


def _parse_line_count(value: str) -> tuple[int, bool]:
    if not _LINE_COUNT.fullmatch(value):
        raise RodexLaunchError(TAIL_USAGE)
    try:
        line_count = int(value.lstrip("+-") or "0")
    except ValueError as error:
        raise RodexLaunchError(TAIL_USAGE) from error
    return line_count, value.startswith("+")


def _select_initial_lines(
    snapshot: tuple[str, ...], request: SessionTailRequest
) -> tuple[str, ...]:
    if request.from_start:
        return snapshot[max(request.line_count - 1, 0) :]
    if request.line_count == 0:
        return ()
    return snapshot[-request.line_count :]


def _settled_visible_lines(visible_lines: tuple[str, ...]) -> tuple[str, ...]:
    """Exclude the live Codex status region and composer from settled text."""
    for index in range(len(visible_lines) - 1, -1, -1):
        if not _COMPOSER_LINE.match(visible_lines[index]):
            continue
        status_region_start = max(index - _STATUS_REGION_MAX_LINE_COUNT, 0)
        for status_index in range(index - 1, status_region_start - 1, -1):
            line = visible_lines[status_index]
            if _ACTIVE_STATUS_LINE.match(line):
                return visible_lines[:status_index]
            if not line or _STATUS_CONTINUATION_LINE.match(line):
                continue
            break
        return visible_lines[:index]
    return visible_lines


def _new_rendered_lines(
    previous: tuple[str, ...], current: tuple[str, ...]
) -> tuple[str, ...]:
    if previous == current:
        return ()

    overlap = _suffix_prefix_overlap(previous, current)
    if overlap:
        return current[overlap:]

    common_prefix = 0
    for previous_line, current_line in zip(previous, current, strict=False):
        if previous_line != current_line:
            break
        common_prefix += 1

    common_suffix = 0
    maximum_suffix = min(len(previous), len(current)) - common_prefix
    while (
        common_suffix < maximum_suffix
        and previous[-common_suffix - 1] == current[-common_suffix - 1]
    ):
        common_suffix += 1

    end = len(current) - common_suffix if common_suffix else len(current)
    return current[common_prefix:end]


def _suffix_prefix_overlap(previous: tuple[str, ...], current: tuple[str, ...]) -> int:
    """Return the longest previous suffix that is also a current prefix in O(n)."""
    if not previous or not current:
        return 0
    sentinel = object()
    combined: list[object] = [*current, sentinel, *previous]
    prefix_lengths = [0] * len(combined)
    for index in range(1, len(combined)):
        candidate = prefix_lengths[index - 1]
        while candidate and combined[index] != combined[candidate]:
            candidate = prefix_lengths[candidate - 1]
        if combined[index] == combined[candidate]:
            candidate += 1
        prefix_lengths[index] = candidate
    return min(prefix_lengths[-1], len(previous), len(current))


def _write_lines(lines: tuple[str, ...], output: TextIO) -> None:
    if not lines:
        return
    output.write("".join(f"{line}\n" for line in lines))
    output.flush()
