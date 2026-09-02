from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from rodex.tmux_session_capability import (
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
)
from rodex.tmux_sharing_coordinator import (
    reconcile_sharing_state,
    sharing_coordinator_hook_command,
)

SERVER_ID = "0123456789abcdef0123456789abcdef"


def _registered_row(
    *,
    tmux_session_id: str = "$1",
    tmux_primary_pane_id: str = "%3",
    attached: str = "1",
    previous: str = "1",
    runtime_id: str = "0123456789abcdef",
    rodex_session_id: str = "1111111111111111",
    internal_session_id: str = "1",
    codex_session_id: str = "01a00654-f2bc-7a30-834a-a5f886a65f82",
) -> str:
    return "\t".join(
        (
            tmux_session_id,
            tmux_primary_pane_id,
            attached,
            previous,
            runtime_id,
            "registered",
            rodex_session_id,
            "2222222222222222",
            internal_session_id,
            codex_session_id,
        )
    )


class _SharingRunner:
    def __init__(
        self,
        roster: str,
        *,
        protocol: str = RODEX_SHARED_TMUX_PROTOCOL,
        mutation_returncode: int = 0,
    ) -> None:
        self.roster = roster
        self.protocol = protocol
        self.mutation_returncode = mutation_returncode
        self.calls: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        **_options: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        operation = command[3]
        if operation == "show-options":
            output = f"{self.protocol}\n{SERVER_ID}\n"
        elif operation == "list-sessions":
            output = self.roster
        else:
            output = ""
        return subprocess.CompletedProcess(
            command,
            self.mutation_returncode if operation == "if-shell" else 0,
            stdout=output,
            stderr="",
        )

    @property
    def mutations(self) -> list[list[str]]:
        return [command for command in self.calls if command[3] == "if-shell"]


def test_unchanged_survivor_produces_no_mutation_after_source_destruction(
    tmp_path: Path,
) -> None:
    runner = _SharingRunner(f"{_registered_row()}\n")

    result = reconcile_sharing_state(
        "tmux",
        tmp_path / "tmux.sock",
        SERVER_ID,
        runner=runner,
    )

    assert result == 0
    assert runner.mutations == []


def test_changed_count_is_admitted_only_through_the_exact_full_capability(
    tmp_path: Path,
) -> None:
    runner = _SharingRunner(f"{_registered_row(attached='2', previous='1')}\n")

    result = reconcile_sharing_state(
        "tmux",
        tmp_path / "tmux.sock",
        SERVER_ID,
        python_executable="/venv/bin/python",
        runner=runner,
    )

    assert result == 0
    assert len(runner.mutations) == 1
    mutation = runner.mutations[0]
    assert mutation[4:6] == ["-t", "%3"]
    condition = mutation[-2]
    action = mutation[-1]
    for expected in (
        RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        SERVER_ID,
        "$1",
        "%3",
        "0123456789abcdef",
        "1111111111111111",
        "2222222222222222",
        str(uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")),
    ):
        assert expected in condition
    assert "set-option -t %3 @rodex_sharing_attached_count 2" in action
    assert "--tmux-session-id" in action
    assert action.count("$1") >= 3
    assert "--tmux-primary-pane-id %3" in action
    assert "--expected-runtime-id 0123456789abcdef" in action
    assert "#{session_id}" not in action.split("--tmux-session-id", maxsplit=1)[1]


def test_malformed_registered_roster_aborts_before_any_mutation(tmp_path: Path) -> None:
    runner = _SharingRunner(
        f"{_registered_row(attached='2', previous='1')}\n"
        f"{_registered_row(tmux_session_id='$2', runtime_id='invalid')}\n"
    )

    result = reconcile_sharing_state(
        "tmux",
        tmp_path / "tmux.sock",
        SERVER_ID,
        runner=runner,
    )

    assert result == 1
    assert runner.mutations == []


def test_mutation_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    runner = _SharingRunner(
        f"{_registered_row(attached='2', previous='1')}\n",
        mutation_returncode=7,
    )

    result = reconcile_sharing_state(
        "tmux",
        tmp_path / "tmux.sock",
        SERVER_ID,
        runner=runner,
    )

    assert result == 7
    assert len(runner.mutations) == 1


def test_duplicate_registered_identity_aborts_before_any_mutation(tmp_path: Path) -> None:
    runner = _SharingRunner(
        f"{_registered_row(attached='2', previous='1')}\n"
        f"{_registered_row(tmux_session_id='$2', internal_session_id='2')}\n"
    )

    result = reconcile_sharing_state(
        "tmux",
        tmp_path / "tmux.sock",
        SERVER_ID,
        runner=runner,
    )

    assert result == 1
    assert runner.mutations == []


def test_server_protocol_or_incarnation_mismatch_fails_before_roster_read(
    tmp_path: Path,
) -> None:
    runner = _SharingRunner(_registered_row(), protocol="unexpected-protocol")

    result = reconcile_sharing_state(
        "tmux",
        tmp_path / "tmux.sock",
        SERVER_ID,
        runner=runner,
    )

    assert result == 1
    assert [command[3] for command in runner.calls] == ["show-options"]


def test_hook_command_escapes_static_tmux_format_syntax() -> None:
    command = sharing_coordinator_hook_command(
        "/opt/rodex/#{client_name}/#(python)",
        "/opt/tmux/#{session_id}/#(tmux)",
        Path("/tmp/rodex/#{pane_id}/#(socket)"),
        SERVER_ID,
    )

    assert "##{client_name}" in command
    assert "##(python)" in command
    assert "##{session_id}" in command
    assert "##(tmux)" in command
    assert "##{pane_id}" in command
    assert "##(socket)" in command
    assert "#{client_name}" not in command.replace("##{client_name}", "")
