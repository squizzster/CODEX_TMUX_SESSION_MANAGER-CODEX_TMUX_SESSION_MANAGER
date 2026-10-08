from __future__ import annotations

import pytest

from rodex.terminal_title import (
    TERMINAL_TITLE_ANIMATION_FRAMES,
    format_working_elapsed,
    terminal_title_pane_escape,
    working_terminal_title_field,
)


def test_working_title_breathes_every_quarter_second_between_elapsed_windows() -> None:
    assert tuple(working_terminal_title_field(index * 0.25) for index in range(9)) == (
        *TERMINAL_TITLE_ANIMATION_FRAMES,
        TERMINAL_TITLE_ANIMATION_FRAMES[0],
    )
    assert working_terminal_title_field(4.99) in TERMINAL_TITLE_ANIMATION_FRAMES
    assert working_terminal_title_field(5.0) == "0m05s"
    assert working_terminal_title_field(5.99) == "0m05s"
    assert working_terminal_title_field(6.0) in TERMINAL_TITLE_ANIMATION_FRAMES
    assert working_terminal_title_field(10.0) == "0m10s"


@pytest.mark.parametrize(
    ("total_seconds", "expected"),
    [
        (5, "0m05s"),
        (59 * 60 + 23, "59m23s"),
        (23 * 60 * 60 + 20 * 60, "23h20m"),
        (10 * 24 * 60 * 60 + 23 * 60 * 60 + 20 * 60, "10d23h20m"),
    ],
)
def test_working_elapsed_uses_the_requested_minute_hour_and_day_shapes(
    total_seconds: int,
    expected: str,
) -> None:
    assert format_working_elapsed(total_seconds) == expected


@pytest.mark.parametrize("invalid", [-1, True, 1.5])
def test_working_elapsed_rejects_invalid_values(invalid: object) -> None:
    with pytest.raises(ValueError):
        format_working_elapsed(invalid)  # type: ignore[arg-type]


def test_pane_title_escape_accepts_only_the_owned_field_alphabet() -> None:
    assert terminal_title_pane_escape("10d23h20m") == b"\x1b]2;10d23h20m\x07"
    with pytest.raises(ValueError):
        terminal_title_pane_escape("name\x07spoofed")
