from __future__ import annotations

import pytest

from rodex.human_messages import rodex_session_message
from rodex.statistics_commands import _print_human_statistics


def test_session_message_uses_action_name_and_terminal_punctuation() -> None:
    assert (
        rodex_session_message("attach", "puzzling-dogfish")
        == "Rodex attach [puzzling-dogfish]."
    )
    assert (
        rodex_session_message("mouse", "puzzling-dogfish", detail="on")
        == "Rodex mouse [puzzling-dogfish]: on."
    )


def test_human_statistics_header_uses_the_session_message_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_human_statistics(
        {
            "rodex_session_name": "puzzling-dogfish",
            "statistics_publication_sequence": 7,
            "worker_state": "up_to_date",
            "statistics": {},
        }
    )

    assert capsys.readouterr().out == (
        "Rodex statistics [puzzling-dogfish]: publication sequence 7; up_to_date.\n"
    )
