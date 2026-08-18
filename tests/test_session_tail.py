from __future__ import annotations

import io
from pathlib import Path

import pytest

from rodex.errors import RodexLaunchError
from rodex.runtime import LiveTmuxSession
from rodex.session_tail import (
    TAIL_POLL_INTERVAL_SECONDS,
    SessionTailRequest,
    follow_session_tail,
    parse_session_tail_request,
)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["_tail", "worker"], SessionTailRequest("worker", 10)),
        (["_tail", "-5", "worker"], SessionTailRequest("worker", 5)),
        (["_tail", "worker", "-n", "25"], SessionTailRequest("worker", 25)),
        (
            ["_tail", "--lines=+3", "worker", "--follow"],
            SessionTailRequest("worker", 3, from_start=True),
        ),
        (
            ["_tail", "-f", "--lines", "-2", "worker"],
            SessionTailRequest("worker", 2),
        ),
        (["_tail", "-n", "0", "worker"], SessionTailRequest("worker", 0)),
        (["_tail", "--", "worker"], SessionTailRequest("worker", 10)),
    ],
)
def test_tail_parser_accepts_the_basic_familiar_line_options(
    arguments: list[str], expected: SessionTailRequest
) -> None:
    assert parse_session_tail_request(arguments) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        ["_tail"],
        ["_tail", "first", "second"],
        ["_tail", "-n"],
        ["_tail", "--lines="],
        ["_tail", "--lines", "+", "worker"],
        ["_tail", "--unknown", "worker"],
    ],
)
def test_tail_parser_rejects_ambiguous_or_incomplete_grammar(
    arguments: list[str],
) -> None:
    with pytest.raises(RodexLaunchError, match=r"^usage: rodex _tail"):
        parse_session_tail_request(arguments)


def test_tail_prints_initial_selection_then_only_new_or_changed_rendered_lines() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")
    snapshots = iter(
        (
            ("one", "two", "three", "four"),
            ("one", "two", "three", "four"),
            ("one", "two", "three", "four", "five"),
            ("two", "three", "four", "five", "six"),
            ("two", "three", "four", "five", "SIX"),
        )
    )
    trace: list[str] = []
    output = io.StringIO()
    sleeps = 0

    def capture(observed_runtime: LiveTmuxSession) -> tuple[str, ...]:
        assert observed_runtime == runtime
        trace.append("capture")
        return next(snapshots)

    def revalidate() -> None:
        trace.append("revalidate")

    def sleep(interval: float) -> None:
        nonlocal sleeps
        assert interval == TAIL_POLL_INTERVAL_SECONDS
        sleeps += 1
        if sleeps == 5:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        follow_session_tail(
            SessionTailRequest("worker", 2),
            runtime,
            capture,
            revalidate,
            output=output,
            sleep=sleep,
        )

    assert output.getvalue() == "three\nfour\nfive\nsix\nSIX\n"
    assert trace == ["capture", "revalidate"] * 5


def test_tail_plus_line_selection_starts_at_the_one_based_retained_line() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")
    output = io.StringIO()

    def stop(_interval: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        follow_session_tail(
            SessionTailRequest("worker", 3, from_start=True),
            runtime,
            lambda _runtime: ("one", "two", "three", "four"),
            lambda: None,
            output=output,
            sleep=stop,
        )

    assert output.getvalue() == "three\nfour\n"


def test_tail_never_emits_a_snapshot_that_fails_runtime_revalidation() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")
    output = io.StringIO()

    def reject() -> None:
        raise RuntimeError("runtime changed")

    with pytest.raises(RuntimeError, match="runtime changed"):
        follow_session_tail(
            SessionTailRequest("worker"),
            runtime,
            lambda _runtime: ("untrusted",),
            reject,
            output=output,
            sleep=lambda _interval: None,
        )

    assert output.getvalue() == ""


def test_tail_requires_a_positive_poll_interval() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")

    def capture(_runtime: LiveTmuxSession) -> tuple[str, ...]:
        return ()

    with pytest.raises(ValueError, match="poll interval must be positive"):
        follow_session_tail(
            SessionTailRequest("worker"),
            runtime,
            capture,
            lambda: None,
            poll_interval_seconds=0,
        )
