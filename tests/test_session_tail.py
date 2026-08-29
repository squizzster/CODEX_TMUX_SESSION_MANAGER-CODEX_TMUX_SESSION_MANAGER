from __future__ import annotations

import io
from pathlib import Path

import pytest

from rodex.errors import RodexLaunchError
from rodex.runtime import LiveTmuxSession, TmuxScrollbackSnapshot, TmuxScrollbackState
from rodex.session_tail import (
    TAIL_POLL_INTERVAL_SECONDS,
    PlainTailCursor,
    SessionTailRequest,
    follow_session_tail,
    parse_session_tail_request,
)


def _state(
    snapshot: TmuxScrollbackSnapshot,
    *,
    runtime_identity: str = "runtime-1",
) -> TmuxScrollbackState:
    return TmuxScrollbackState(
        history_line_count=snapshot.history_line_count,
        history_tail_lines=snapshot.history_lines,
        visible_lines=snapshot.visible_lines,
        runtime_identity=runtime_identity,
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
        ["_tail", "--include-volatile", "worker"],
    ],
)
def test_tail_parser_rejects_ambiguous_or_incomplete_grammar(
    arguments: list[str],
) -> None:
    with pytest.raises(RodexLaunchError, match=r"^usage: rodex _tail"):
        parse_session_tail_request(arguments)


def test_tail_prints_initial_text_then_only_newly_committed_scrollback() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")
    snapshots = (
        TmuxScrollbackSnapshot(("old history", "visible one", "visible two"), 1),
        TmuxScrollbackSnapshot(("old history", "visible one", "visible two", "new one"), 2),
        TmuxScrollbackSnapshot(
            (
                "old history",
                "visible one",
                "visible two",
                "new one",
                "new two",
            ),
            3,
        ),
        TmuxScrollbackSnapshot(
            (
                "old history",
                "visible one",
                "visible two",
                "new one",
                "new two",
                "prompt",
            ),
            4,
        ),
    )
    states = iter(map(_state, snapshots))
    output = io.StringIO()
    trace: list[str] = []
    polls = 0

    def capture(observed_runtime: LiveTmuxSession) -> TmuxScrollbackSnapshot:
        assert observed_runtime == runtime
        trace.append("capture")
        return snapshots[0]

    def capture_state(observed_runtime: LiveTmuxSession) -> TmuxScrollbackState:
        assert observed_runtime == runtime
        trace.append("state")
        return next(states)

    def sleep(interval: float) -> None:
        nonlocal polls
        assert interval == TAIL_POLL_INTERVAL_SECONDS
        polls += 1
        if polls == 4:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        follow_session_tail(
            SessionTailRequest("worker", 2),
            runtime,
            capture,
            capture_state,
            lambda: trace.append("revalidate"),
            output=output,
            sleep=sleep,
        )

    assert output.getvalue() == "visible one\nvisible two\nnew one\n"
    assert trace == ["state", "capture", "revalidate", "state", "state", "state"]


def test_plain_cursor_suppresses_rows_already_visible_initially() -> None:
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(("history", "visible one", "visible two"), 1)
    )

    assert (
        cursor.advance(
            TmuxScrollbackSnapshot(("history", "visible one", "visible two", "new one"), 2)
        )
        == ()
    )
    assert (
        cursor.advance(
            TmuxScrollbackSnapshot(
                ("history", "visible one", "visible two", "new one", "new two"), 3
            )
        )
        == ()
    )
    assert cursor.advance(
        TmuxScrollbackSnapshot(
            (
                "history",
                "visible one",
                "visible two",
                "new one",
                "new two",
                "prompt",
            ),
            4,
        )
    ) == ("new one",)


def test_plain_cursor_emits_a_visible_row_if_it_changed_before_scrolling() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("history", "Working 1", "prompt"), 1))

    assert cursor.advance(
        TmuxScrollbackSnapshot(("history", "Answer", "prompt", "next"), 2)
    ) == ("Answer",)


def test_plain_cursor_rebaselines_after_tmux_history_is_cleared() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("one", "two", "visible"), 2))

    assert cursor.advance(TmuxScrollbackSnapshot(("replacement", "prompt"), 0)) == ()
    assert (
        cursor.advance(TmuxScrollbackSnapshot(("replacement", "prompt", "next"), 1)) == ()
    )


def test_plain_cursor_rebaselines_after_same_sized_history_replacement() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("one", "two", "visible"), 2))

    assert (
        cursor.advance(
            TmuxScrollbackSnapshot(("replacement one", "replacement two", "prompt"), 2)
        )
        == ()
    )


def test_plain_cursor_follows_committed_rows_after_history_reaches_its_limit() -> None:
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(("h1", "h2", "h3", "visible one", "visible two"), 3)
    )

    assert (
        cursor.advance(
            TmuxScrollbackSnapshot(
                ("h3", "visible one", "visible two", "new one", "new two"), 3
            )
        )
        == ()
    )
    assert cursor.advance(
        TmuxScrollbackSnapshot(
            ("visible one", "visible two", "new one", "new two", "new three"), 3
        )
    ) == ("new one",)


def test_bounded_tail_state_follows_append_and_history_rollover() -> None:
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(("h1", "h2", "h3", "visible one", "visible two"), 3)
    )

    assert (
        cursor.try_advance_state(
            TmuxScrollbackState(
                history_line_count=3,
                history_tail_lines=("h3", "visible one", "visible two"),
                visible_lines=("new one", "new two"),
                runtime_identity="runtime-1",
            )
        )
        == ()
    )
    assert cursor.try_advance_state(
        TmuxScrollbackState(
            history_line_count=3,
            history_tail_lines=("visible one", "visible two", "new one"),
            visible_lines=("new two", "new three"),
            runtime_identity="runtime-1",
        )
    ) == ("new one",)


def test_bounded_tail_state_requests_full_fallback_for_history_replacement() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("h1", "h2", "visible"), 2))
    replacement_state = TmuxScrollbackState(
        history_line_count=2,
        history_tail_lines=("replacement one", "replacement two"),
        visible_lines=("prompt",),
        runtime_identity="runtime-1",
    )

    assert cursor.try_advance_state(replacement_state) is None
    assert (
        cursor.advance(
            TmuxScrollbackSnapshot(("replacement one", "replacement two", "prompt"), 2)
        )
        == ()
    )


def test_plain_cursor_emits_a_settled_visible_insertion_without_waiting_for_scroll() -> (
    None
):
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(("history", "old response", "prompt"), 1),
        settled_poll_count=3,
    )
    current = TmuxScrollbackSnapshot(
        ("history", "old response", "new response", "prompt"), 1
    )

    assert cursor.advance(current) == ()
    assert cursor.advance(current) == ()
    assert cursor.advance(current) == ("new response",)
    assert cursor.advance(current) == ()


def test_plain_cursor_publishes_history_while_a_spinner_keeps_changing() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("history", "visible", "Working 1"), 1))

    assert (
        cursor.advance(
            TmuxScrollbackSnapshot(("history", "visible", "Working 2", "next"), 2)
        )
        == ()
    )
    assert cursor.advance(
        TmuxScrollbackSnapshot(("history", "visible", "committed", "Working 3", "next"), 3)
    ) == ("committed",)


@pytest.mark.parametrize(
    "status_template",
    [
        "• Working ({seconds}s • esc to interrupt)",
        "◦ Working ({seconds}s • esc to interrupt)",
        (
            "• Waiting for background terminal (1m {seconds}s • esc to interrupt) "
            "· 1 background terminal running"
        ),
    ],
)
def test_plain_cursor_settles_content_above_a_changing_activity_line(
    status_template: str,
) -> None:
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(
            (
                "history",
                "old response",
                status_template.format(seconds=1),
                "  └ background detail",
                "    wrapped status detail",
                "",
                "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} prompt",
            ),
            1,
        ),
        settled_poll_count=3,
    )

    for seconds in (2, 3):
        assert (
            cursor.advance(
                TmuxScrollbackSnapshot(
                    (
                        "history",
                        "old response",
                        "new result",
                        status_template.format(seconds=seconds),
                        "  └ background detail",
                        "    wrapped status detail",
                        "",
                        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} prompt",
                    ),
                    1,
                )
            )
            == ()
        )
    assert cursor.advance(
        TmuxScrollbackSnapshot(
            (
                "history",
                "old response",
                "new result",
                status_template.format(seconds=4),
                "  └ background detail",
                "    wrapped status detail",
                "",
                "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} prompt",
            ),
            1,
        )
    ) == ("new result",)


def test_plain_cursor_does_not_treat_quoted_working_text_as_live_status() -> None:
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(
            (
                "history",
                "old response",
                "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} prompt",
            ),
            1,
        ),
        settled_poll_count=3,
    )
    current = TmuxScrollbackSnapshot(
        (
            "history",
            "old response",
            "• Working (4s • esc to interrupt)",
            "quoted explanation",
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} prompt",
        ),
        1,
    )

    assert cursor.advance(current) == ()
    assert cursor.advance(current) == ()
    assert cursor.advance(current) == (
        "• Working (4s • esc to interrupt)",
        "quoted explanation",
    )


def test_tail_plus_line_selection_starts_at_the_one_based_retained_line() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")
    output = io.StringIO()

    def stop(_interval: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        follow_session_tail(
            SessionTailRequest("worker", 3, from_start=True),
            runtime,
            lambda _runtime: TmuxScrollbackSnapshot(("one", "two", "three", "four"), 2),
            lambda _runtime: _state(
                TmuxScrollbackSnapshot(("one", "two", "three", "four"), 2)
            ),
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
            lambda _runtime: TmuxScrollbackSnapshot(("untrusted",), 0),
            lambda _runtime: _state(TmuxScrollbackSnapshot(("untrusted",), 0)),
            reject,
            output=output,
            sleep=lambda _interval: None,
        )

    assert output.getvalue() == ""


def test_tail_requires_a_positive_poll_interval() -> None:
    runtime = LiveTmuxSession(Path("/tmp/rodex-test.sock"), "worker")

    with pytest.raises(ValueError, match="tail poll interval must be positive"):
        follow_session_tail(
            SessionTailRequest("worker"),
            runtime,
            lambda _runtime: TmuxScrollbackSnapshot((), 0),
            lambda _runtime: _state(TmuxScrollbackSnapshot((), 0)),
            lambda: None,
            poll_interval_seconds=0,
        )
