"""Cancellation boundaries and continued output compared with the native tmux parser."""

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.native_terminal_projection import NativeTerminalProjection
from rodex.presentation_policy import PresentationSnapshot, PresentationSurface
from rodex.terminal_surface import TerminalSurfaceRenderer

CANCELLABLE = [
    b"\x1b",
    b"\x1b%",
    b"\x1b[",
    b"\x1b[31",
    b"\x1b[1 ",
    b"\x1b[1 ?",
    b"\x1b]0;title",
    b"\x1b_body",
    b"\x1bP",
    b"\x1bP12;",
    b"\x1bP1 ",
    b"\x1bP:",
]


@pytest.mark.parametrize("pending", CANCELLABLE)
@pytest.mark.parametrize("cancel", [b"\x18", b"\x1a", b"\x1b[0m"])
def test_cancel_releases_boundary_and_inner_parser_at_every_byte_split(pending, cancel):
    data = b"abc" + pending + cancel
    for split in range(len(data) + 1):
        projection = NativeTerminalProjection(24, 8)
        first, second = projection.feed(data[:split]), projection.feed(data[split:])
        assert first.native + second.native == data
        assert projection.at_boundary
        assert projection.screen.display[0].rstrip() == "abc"
        assert projection.feed(b"X\x1b[6n").replies == b"\x1b[1;5R"
        assert projection.screen.display[0].rstrip() == "abcX"


@pytest.mark.parametrize("pending", CANCELLABLE)
@pytest.mark.parametrize("cancel", [b"\x18", b"\x1a", b"\x1b[0m"])
def test_cancellation_and_continuation_match_native_tmux(tmp_path, pending, cancel):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        data = b"abc" + pending + cancel + b"X"
        projection = NativeTerminalProjection(24, 8)
        output = projection.feed(data)
        assert output.native == data
        assert terminal.write(data) == projection.feed(b"\x1b[6n").replies
        assert terminal.screen() == ([row.rstrip() for row in projection.screen.display], (4, 0))


@pytest.mark.parametrize(
    "body", [b"payload\x18X", b"payload\x1aX", b"payload\x07X", b"payload\x1b[0mX", b"payload\x1b\x1b\\X"]
)
def test_dcs_body_is_opaque_until_actual_st(tmp_path, body):
    data = b"abc\x1bPq" + body
    for split in range(len(data) + 1):
        projection = NativeTerminalProjection(24, 8)
        first, second = projection.feed(data[:split]), projection.feed(data[split:])
        assert first.native + second.native == data
        assert not projection.at_boundary
        assert projection.screen.display[0].rstrip() == "abc"
        projection.feed(b"\x1b\\Y")
        assert projection.at_boundary
        assert projection.screen.display[0].rstrip() == "abcY"
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        assert terminal.write(data + b"\x1b\\Y") == b"\x1b[1;5R"
        assert terminal.screen()[0][0] == "abcY"


@pytest.mark.parametrize("prefix", [b"\x1b", b"\x1b%", b"\x1b[31", b"\x1b[1 ", b"\x1b[" + b"1;" * 3000])
def test_embedded_c0_effects_survive_cancelled_and_oversize_instructions(prefix):
    data = b"abc" + prefix + b"\r\n\t\x07\x18"
    for split in [0, 3, len(data) - 2, len(data) - 1, len(data)]:
        projection = NativeTerminalProjection(24, 8)
        first, second = projection.feed(data[:split]), projection.feed(data[split:])
        assert first.native + second.native == data
        assert projection.at_boundary and not projection._oversize_csi
        assert projection.feed(b"X\x1b[6n").replies == b"\x1b[2;10R"
        assert projection.screen.display[1].rstrip() == "        X"


def test_cancel_releases_pending_presentation_without_cancelling_synchronized_update():
    renderer = TerminalSurfaceRenderer(24, 8)
    renderer.native_output(b"abc\x1b[31")
    snapshot = PresentationSnapshot(1, "light", PresentationSurface.SEMANTIC, "heading", (), ())
    assert renderer.present(snapshot) == b""
    assert renderer.native_output(b"\x18").frame
    renderer.native_output(b"\x1b[?2026h\x1b[31\x18")
    assert renderer.native.synchronized_update and not renderer.native.at_boundary
    renderer.native_output(b"\x1b[?2026l")
    assert renderer.native.at_boundary


def test_cancel_does_not_reset_valid_utf8_continuation():
    data = "世界".encode() + b"\x1b[31\x18" + "猫".encode()
    for split in range(len(data) + 1):
        projection = NativeTerminalProjection(24, 8)
        projection.feed(data[:split])
        projection.feed(data[split:])
        assert projection.at_boundary
        assert projection.screen.display[0].rstrip() == "世界猫"


@pytest.mark.parametrize("pending", [b"\x1b[31", b"\x1b]0;title", b"\x1bP12;", b"\x1b_body"])
@pytest.mark.parametrize("embedded", [b"", b"\r\n"])
def test_intercepted_query_still_cancels_forwarded_fragment_before_embedded_c0(tmp_path, pending, embedded):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        projection = NativeTerminalProjection(24, 8)
        data = b"abc" + pending + b"\x1b[" + embedded + b"6nX"
        output = projection.feed(data)
        native_cpr = terminal.write(output.native)
        assert native_cpr == projection.feed(b"\x1b[6n").replies
        assert terminal.screen() == (
            [row.rstrip() for row in projection.screen.display],
            (projection.screen.cursor.x, projection.screen.cursor.y),
        )


@pytest.mark.parametrize("middle,expected", [(b"\x1b[\x18", "AZ"), (b"\x1b[\x1a", "AZ"), (b"\x1b[\x1b[0m", "A世Z")])
def test_utf8_pending_state_follows_c0_cancellation_but_survives_escape_restart(tmp_path, middle, expected):
    data = b"A\xe4" + middle + b"\xb8\x96Z"
    for split in range(len(data) + 1):
        projection = NativeTerminalProjection(24, 8)
        projection.feed(data[:split])
        projection.feed(data[split:])
        assert projection.screen.display[0].rstrip() == expected
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        assert terminal.write(data) == projection.feed(b"\x1b[6n").replies
        assert terminal.screen()[0][0] == expected


@pytest.mark.parametrize("instruction", [b"\x1b##8", b"\x1b((B", b"\x1b[1;2:3m", b"\x1b[1:2m", b"\x1b[1 A"])
def test_complete_unsupported_grammar_does_not_leak_into_text(tmp_path, instruction):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        data = b"abc" + instruction + b"X"
        projection = NativeTerminalProjection(24, 8)
        projection.feed(data)
        assert terminal.write(data) == projection.feed(b"\x1b[6n").replies
        assert terminal.screen()[0][0] == projection.screen.display[0].rstrip() == "abcX"


@pytest.mark.parametrize("byte", [b"\x00", b"\xff", b"1"])
def test_ignored_bytes_cannot_bypass_sequence_bound(byte):
    projection = NativeTerminalProjection(24, 8)
    data = b"\x1b[" + byte * 20000
    output = projection.feed(data)
    assert len(projection._sequence) <= 4096 and len(projection._instruction) <= 4096
    assert not projection.at_boundary
    cancelled = projection.feed(b"\x18")
    assert output.native + cancelled.native == data + b"\x18"
    assert projection.at_boundary


@pytest.mark.parametrize(
    "pending",
    [b"\x1b[31", b"\x1b]0;t", b"\x1bP12;", b"\x1b_body", b"\x1b[" + b"1" * 4095, b"\x1b[" + b"1" * 8192],
    ids=["csi", "osc", "dcs-header", "apc", "exact-flush", "second-flush"],
)
def test_intercepted_restart_preserves_pending_utf8_and_exact_oversize_boundary(tmp_path, pending):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        projection = NativeTerminalProjection(24, 8)
        output = projection.feed(b"A\xe4" + pending + b"\x1b[6n\xb8\x96Z")
        assert terminal.write(output.native) == projection.feed(b"\x1b[6n").replies
        assert terminal.screen()[0][0] == projection.screen.display[0].rstrip() == "A世Z"
