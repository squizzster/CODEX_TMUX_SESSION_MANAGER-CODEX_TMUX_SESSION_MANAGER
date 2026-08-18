from __future__ import annotations

import pytest

from rodex.command_contract import (
    COMMAND_SPECS,
    COMMANDS_BY_TOKEN,
    HELP_TEXT,
    MACHINE_COMMAND_SPECS,
    RODEX_COMMANDS,
    WAIT_COMMAND,
    CommandRoute,
    MachineUsageError,
    machine_spec_for_arguments,
    parse_machine_invocation,
)


def test_command_specs_are_the_complete_unique_rodex_vocabulary() -> None:
    tokens = [spec.token for spec in COMMAND_SPECS]

    assert len(tokens) == len(set(tokens))
    assert frozenset(tokens) == RODEX_COMMANDS
    assert {spec.token: spec for spec in COMMAND_SPECS} == COMMANDS_BY_TOKEN
    assert set(MACHINE_COMMAND_SPECS) <= {
        spec.token for spec in COMMAND_SPECS if spec.route is CommandRoute.CONTROL
    }


def test_help_is_generated_from_every_declared_command() -> None:
    for spec in COMMAND_SPECS:
        assert len(spec.help_lines) % 2 == 0
        assert spec.token in HELP_TEXT
        for usage, description in zip(
            spec.help_lines[::2], spec.help_lines[1::2], strict=True
        ):
            assert usage in HELP_TEXT
            assert description in HELP_TEXT


@pytest.mark.parametrize(
    ("arguments", "turn_id", "dispatch_id", "timeout_seconds"),
    [
        (["_inspect", "session", "--json"], None, None, None),
        (
            ["_start", "session", "--dispatch", "dispatch-1", "--stdin", "--json"],
            None,
            "dispatch-1",
            None,
        ),
        (
            [
                "_steer",
                "session",
                "--turn",
                "turn-1",
                "--stdin",
                "--json",
            ],
            "turn-1",
            None,
            None,
        ),
        (
            ["_wait", "session", "--turn", "turn-1", "--timeout", "2m", "--json"],
            "turn-1",
            None,
            120.0,
        ),
        (
            ["_dispatch-status", "session", "--dispatch", "dispatch-1", "--json"],
            None,
            "dispatch-1",
            None,
        ),
        (["_interrupt", "session", "--turn", "turn-1", "--json"], "turn-1", None, None),
        (["_result", "session", "--turn", "turn-1", "--json"], "turn-1", None, None),
    ],
)
def test_machine_invocation_parser_owns_the_exact_control_grammar(
    arguments: list[str],
    turn_id: str | None,
    dispatch_id: str | None,
    timeout_seconds: float | None,
) -> None:
    invocation = parse_machine_invocation(arguments)

    assert invocation.session_name == "session"
    assert invocation.turn_id == turn_id
    assert invocation.dispatch_id == dispatch_id
    assert invocation.timeout_seconds == timeout_seconds


@pytest.mark.parametrize(
    "arguments",
    [
        ["_inspect", "session"],
        ["_inspect", "session", "--stdin", "--json"],
        ["_start", "session", "--json"],
        ["_steer", "session", "--turn", "turn-1", "--json"],
        ["_wait", "session", "--turn", "", "--json"],
        ["_wait", "session", "--turn", "turn-1", "--timeout", "0", "--json"],
        ["_interrupt", "session", "--turn", "turn-1", "--json", "--json"],
        ["_result", "--json"],
        ["_dispatch-status", "session", "--json"],
    ],
)
def test_machine_invocation_parser_rejects_contract_violations(
    arguments: list[str],
) -> None:
    with pytest.raises(MachineUsageError):
        parse_machine_invocation(arguments)


def test_wait_route_is_disambiguated_by_exact_control_arguments() -> None:
    assert machine_spec_for_arguments([WAIT_COMMAND, "session"]) is None
    assert (
        machine_spec_for_arguments([WAIT_COMMAND, "session", "--turn", "turn-1", "--json"])
        is MACHINE_COMMAND_SPECS[WAIT_COMMAND]
    )
