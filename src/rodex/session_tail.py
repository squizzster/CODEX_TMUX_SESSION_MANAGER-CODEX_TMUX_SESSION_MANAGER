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
from .runtime import LiveTmuxSession, TmuxScrollbackSnapshot, TmuxScrollbackState

DEFAULT_TAIL_LINE_COUNT: Final = 10
TAIL_POLL_INTERVAL_SECONDS: Final = 0.4
TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS: Final = 3.2
TAIL_SETTLED_POLL_COUNT: Final = 3
TAIL_CURSOR_HISTORY_LINE_COUNT: Final = 256
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
CaptureScrollbackState = Callable[[LiveTmuxSession], TmuxScrollbackState]
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
        self._history_line_count = initial_snapshot.history_line_count
        self._history_lines = initial_snapshot.history_lines[
            -TAIL_CURSOR_HISTORY_LINE_COUNT:
        ]
        self._observed_visible = deque(initial_snapshot.visible_lines)
        initial_visible = _settled_visible_lines(initial_snapshot.visible_lines)
        self._published_visible = initial_visible
        self._candidate_visible = initial_visible
        self._candidate_poll_count = 0
        self._settled_poll_count = settled_poll_count

    def advance(self, current_snapshot: TmuxScrollbackSnapshot) -> tuple[str, ...]:
        emitted = self._advance(
            current_snapshot.history_lines,
            current_snapshot.history_line_count,
            current_snapshot.visible_lines,
        )
        if emitted is None:
            self._rebaseline(current_snapshot)
            return ()
        return emitted

    def try_advance_state(
        self,
        current_state: TmuxScrollbackState,
    ) -> tuple[str, ...] | None:
        """Advance from a bounded capture, or request a rare full-capture fallback."""
        return self._advance(
            current_state.history_tail_lines,
            current_state.history_line_count,
            current_state.visible_lines,
        )

    def rebaseline(self, snapshot: TmuxScrollbackSnapshot) -> None:
        """Forget continuity after a verified runtime identity transition."""
        self._rebaseline(snapshot)

    def _advance(
        self,
        current_history: tuple[str, ...],
        current_history_line_count: int,
        current_visible: tuple[str, ...],
    ) -> tuple[str, ...] | None:
        emitted = self._new_committed_history(
            current_history,
            current_history_line_count,
        )
        if emitted is None:
            return None
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
        self,
        current_history: tuple[str, ...],
        current_history_line_count: int,
    ) -> list[str] | None:
        previous_history = self._history_lines
        if current_history_line_count < self._history_line_count:
            return None
        previous_end = _history_sequence_end(
            previous_history,
            current_history,
            previous_history_line_count=self._history_line_count,
            current_history_line_count=current_history_line_count,
            observed_visible=tuple(self._observed_visible),
        )
        if previous_history and previous_end is None:
            return None
        newly_committed = current_history[previous_end or 0 :]
        self._history_line_count = current_history_line_count
        self._history_lines = current_history[-TAIL_CURSOR_HISTORY_LINE_COUNT:]
        emitted: list[str] = []
        for line in newly_committed:
            if self._observed_visible:
                observed = self._observed_visible.popleft()
                if line == observed:
                    continue
            emitted.append(line)
        return emitted

    def _rebaseline(self, snapshot: TmuxScrollbackSnapshot) -> None:
        self._history_line_count = snapshot.history_line_count
        self._history_lines = snapshot.history_lines[-TAIL_CURSOR_HISTORY_LINE_COUNT:]
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
    capture_scrollback_state: CaptureScrollbackState,
    revalidate: Revalidate,
    *,
    output: TextIO | None = None,
    sleep: Sleeper = time.sleep,
    poll_interval_seconds: float = TAIL_POLL_INTERVAL_SECONDS,
    max_idle_poll_interval_seconds: float = TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
) -> None:
    """Print recent text, then follow bounded state with adaptive idle probing."""
    if poll_interval_seconds <= 0:
        raise ValueError("tail poll interval must be positive")
    if max_idle_poll_interval_seconds < poll_interval_seconds:
        raise ValueError("tail maximum idle poll interval cannot be shorter than its poll")
    destination = sys.stdout if output is None else output

    observed_state = capture_scrollback_state(runtime)
    initial = capture_scrollback(runtime)
    revalidate()
    _write_lines(_select_initial_lines(initial.lines, request), destination)
    cursor = PlainTailCursor(initial)
    next_poll_interval = poll_interval_seconds

    while True:
        sleep(next_poll_interval)
        current_state = capture_scrollback_state(runtime)
        if current_state == observed_state:
            next_poll_interval = min(
                next_poll_interval * 2,
                max_idle_poll_interval_seconds,
            )
            continue

        if current_state.runtime_identity != observed_state.runtime_identity:
            current = capture_scrollback(runtime)
            revalidate()
            cursor.rebaseline(current)
            emitted: tuple[str, ...] = ()
        else:
            bounded_emitted = cursor.try_advance_state(current_state)
            if bounded_emitted is None:
                current = capture_scrollback(runtime)
                revalidate()
                emitted = cursor.advance(current)
            else:
                emitted = bounded_emitted
        _write_lines(emitted, destination)
        observed_state = current_state
        next_poll_interval = poll_interval_seconds


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


def _history_sequence_end(
    previous: tuple[str, ...],
    current: tuple[str, ...],
    *,
    previous_history_line_count: int,
    current_history_line_count: int,
    observed_visible: tuple[str, ...],
) -> int | None:
    """Locate the prior tail only when count or visible order disambiguates it."""
    if not previous:
        return 0
    history_growth = current_history_line_count - previous_history_line_count
    latest_possible_end = len(current) - history_growth
    if latest_possible_end < 0:
        return None
    if (
        history_growth == 0
        and len(current) >= len(previous)
        and current[-len(previous) :] == previous
    ):
        return len(current)

    candidates = {
        end
        for end in (
            *_subsequence_ends(previous, current),
            *_suffix_prefix_overlaps(previous, current),
        )
        if end <= latest_possible_end
    }
    compatible_candidates = [
        end
        for end in candidates
        if _committed_prefix_matches_visible(current[end:], observed_visible)
    ]
    if len(compatible_candidates) == 1:
        return compatible_candidates[0]
    if not compatible_candidates and history_growth > 0:
        retained_previous_count = min(len(previous), latest_possible_end)
        if (
            latest_possible_end in candidates
            and previous[-retained_previous_count:]
            == current[latest_possible_end - retained_previous_count : latest_possible_end]
        ):
            return latest_possible_end
    return None


def _subsequence_ends(
    pattern: tuple[str, ...],
    values: tuple[str, ...],
) -> tuple[int, ...]:
    """Return every exact pattern end in linear time."""
    prefix_lengths = [0] * len(pattern)
    for index in range(1, len(pattern)):
        candidate = prefix_lengths[index - 1]
        while candidate and pattern[index] != pattern[candidate]:
            candidate = prefix_lengths[candidate - 1]
        if pattern[index] == pattern[candidate]:
            candidate += 1
        prefix_lengths[index] = candidate

    matched = 0
    ends: list[int] = []
    for index, value in enumerate(values):
        while matched and value != pattern[matched]:
            matched = prefix_lengths[matched - 1]
        if value == pattern[matched]:
            matched += 1
        if matched == len(pattern):
            ends.append(index + 1)
            matched = prefix_lengths[matched - 1]
    return tuple(ends)


def _suffix_prefix_overlaps(
    previous: tuple[str, ...],
    current: tuple[str, ...],
) -> tuple[int, ...]:
    """Return every non-empty prior-suffix/current-prefix overlap in O(n)."""
    if not previous or not current:
        return ()
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
    overlap = min(prefix_lengths[-1], len(previous), len(current))
    overlaps: list[int] = []
    while overlap:
        overlaps.append(overlap)
        overlap = prefix_lengths[overlap - 1]
    return tuple(overlaps)


def _committed_prefix_matches_visible(
    committed: tuple[str, ...],
    observed_visible: tuple[str, ...],
) -> bool:
    compared_count = min(len(committed), len(observed_visible))
    return committed[:compared_count] == observed_visible[:compared_count]


def _write_lines(lines: tuple[str, ...], output: TextIO) -> None:
    if not lines:
        return
    output.write("".join(f"{line}\n" for line in lines))
    output.flush()
