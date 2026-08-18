"""Parse and follow human-readable terminal output from one Rodex session."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TextIO

from .command_contract import TAIL_COMMAND
from .errors import RodexLaunchError
from .runtime import LiveTmuxSession

DEFAULT_TAIL_LINE_COUNT: Final = 10
TAIL_POLL_INTERVAL_SECONDS: Final = 0.5
TAIL_USAGE: Final = (
    "usage: rodex _tail [-f|--follow] [-n NUM|--lines NUM|--lines=NUM|-NUM] SESSION_NAME"
)
_SHORT_LINE_COUNT = re.compile(r"-[0-9]+")
_LINE_COUNT = re.compile(r"[+-]?[0-9]+")

CaptureScrollback = Callable[[LiveTmuxSession], tuple[str, ...]]
Revalidate = Callable[[], None]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class SessionTailRequest:
    """One parsed terminal-follow request using familiar tail line selection."""

    session_name: str
    line_count: int = DEFAULT_TAIL_LINE_COUNT
    from_start: bool = False


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
    """Print the selected snapshot, then emit changed rendered lines until stopped."""
    if poll_interval_seconds <= 0:
        raise ValueError("tail poll interval must be positive")
    destination = sys.stdout if output is None else output

    previous = capture_scrollback(runtime)
    revalidate()
    _write_lines(_select_initial_lines(previous, request), destination)

    while True:
        sleep(poll_interval_seconds)
        current = capture_scrollback(runtime)
        revalidate()
        _write_lines(_new_rendered_lines(previous, current), destination)
        previous = current


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
