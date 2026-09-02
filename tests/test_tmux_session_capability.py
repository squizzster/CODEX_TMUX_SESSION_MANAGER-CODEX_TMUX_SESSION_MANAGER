from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from rodex.tmux_session_capability import (
    RODEX_CODEX_SESSION_ID_OPTION,
    RODEX_INTERNAL_SESSION_ID_OPTION,
    RODEX_PRIMARY_PANE_ID_OPTION,
    RODEX_REGISTRATION_REGISTERED,
    RODEX_REGISTRATION_STATE_OPTION,
    RODEX_REGISTRY_ID_OPTION,
    RODEX_RUNTIME_ID_OPTION,
    RODEX_SESSION_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    TmuxSessionCapability,
    capability_identity_if_shell_condition,
    primary_pane_capability_if_shell_condition,
    registered_capability_if_shell_condition,
    registered_primary_pane_if_shell_condition,
    registered_primary_pane_read_arguments,
)
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId


def _registered_capability(socket_path: Path) -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0c01ee2ead7240e1"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )


def test_session_identity_accepts_owned_observer_but_primary_identity_does_not(
    tmp_path: Path,
) -> None:
    capability = _registered_capability(tmp_path / "tmux.sock")
    exact_current_pane = f"#{{==:#{{pane_id}},#{{l:{capability.tmux_primary_pane_id}}}}}"
    stored_primary_pane = (
        f"#{{==:#{{{RODEX_PRIMARY_PANE_ID_OPTION}}},"
        f"#{{l:{capability.tmux_primary_pane_id}}}}}"
    )

    session_identity = capability_identity_if_shell_condition(capability)
    primary_identity = primary_pane_capability_if_shell_condition(capability)

    assert stored_primary_pane in session_identity
    assert exact_current_pane not in session_identity
    assert session_identity in primary_identity
    assert exact_current_pane in primary_identity


def test_registration_and_primary_target_are_independent_capability_layers(
    tmp_path: Path,
) -> None:
    capability = _registered_capability(tmp_path / "tmux.sock")
    exact_current_pane = f"#{{==:#{{pane_id}},#{{l:{capability.tmux_primary_pane_id}}}}}"

    registered_session = registered_capability_if_shell_condition(capability)
    registered_primary = registered_primary_pane_if_shell_condition(capability)

    assert "@rodex_registration_state" in registered_session
    assert exact_current_pane not in registered_session
    assert registered_session in registered_primary
    assert exact_current_pane in registered_primary


@pytest.mark.parametrize("expected_pane_id", ("%4", "%9"))
def test_registered_primary_read_owns_condition_context_at_high_pane_ids(
    tmp_path: Path,
    expected_pane_id: str,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"

    def tmux(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    tmux("new-session", "-d", "-s", "pane-id-anchor", "sleep 30")
    for pane_number in range(1, int(expected_pane_id[1:])):
        dummy_name = f"pane-id-{pane_number}"
        tmux("new-session", "-d", "-s", dummy_name, "sleep 30")
        tmux("kill-session", "-t", f"={dummy_name}")
    tmux("new-session", "-d", "-s", "managed", "sleep 30")
    try:
        session_id, pane_id = tmux(
            "display-message",
            "-p",
            "-t",
            "=managed:",
            "-F",
            "#{session_id}\t#{pane_id}",
        ).stdout.strip().split("\t")
        assert pane_id == expected_pane_id
        template = _registered_capability(socket_path)
        capability = TmuxSessionCapability(
            socket_path,
            template.tmux_server_id,
            session_id,
            pane_id,
            template.runtime_id,
            template.rodex_session_id,
            template.registry_id,
            template.internal_session_id,
            template.codex_session_id,
        )
        tmux(
            "set-option",
            "-s",
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
            RODEX_SHARED_TMUX_PROTOCOL,
        )
        tmux(
            "set-option",
            "-s",
            RODEX_SHARED_TMUX_SERVER_ID_OPTION,
            capability.tmux_server_id,
        )
        for option, value in (
            (RODEX_PRIMARY_PANE_ID_OPTION, pane_id),
            (RODEX_RUNTIME_ID_OPTION, str(capability.runtime_id)),
            (RODEX_REGISTRATION_STATE_OPTION, RODEX_REGISTRATION_REGISTERED),
            (RODEX_SESSION_ID_OPTION, str(capability.rodex_session_id)),
            (RODEX_REGISTRY_ID_OPTION, str(capability.registry_id)),
            (RODEX_INTERNAL_SESSION_ID_OPTION, str(capability.internal_session_id)),
            (RODEX_CODEX_SESSION_ID_OPTION, str(capability.codex_session_id)),
        ):
            tmux("set-option", "-t", "=managed:", option, value)

        admitted = tmux(
            *registered_primary_pane_read_arguments(
                capability,
                "#{pane_id}|#{session_attached}",
            )
        )
        assert admitted.stdout.strip() == f"{pane_id}|0"

        stale_capability = TmuxSessionCapability(
            socket_path,
            capability.tmux_server_id,
            capability.tmux_session_id,
            capability.tmux_primary_pane_id,
            RodexRuntimeId.parse("ffffffffffffffff"),
            capability.rodex_session_id,
            capability.registry_id,
            capability.internal_session_id,
            capability.codex_session_id,
        )
        rejected = tmux(
            *registered_primary_pane_read_arguments(stale_capability, "#{pane_id}"),
            check=False,
        )
        assert rejected.returncode != 0
        assert pane_id not in rejected.stdout
    finally:
        tmux("kill-server", check=False)
