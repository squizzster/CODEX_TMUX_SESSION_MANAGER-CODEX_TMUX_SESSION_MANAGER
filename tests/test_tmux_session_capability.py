from __future__ import annotations

import uuid
from pathlib import Path

from rodex.tmux_session_capability import (
    RODEX_PRIMARY_PANE_ID_OPTION,
    TmuxSessionCapability,
    capability_identity_condition,
    primary_pane_capability_condition,
    registered_capability_condition,
    registered_primary_pane_condition,
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
    exact_current_pane = f"#{{==:#{{pane_id}},{capability.tmux_primary_pane_id}}}"
    stored_primary_pane = (
        f"#{{==:#{{{RODEX_PRIMARY_PANE_ID_OPTION}}},{capability.tmux_primary_pane_id}}}"
    )

    session_identity = capability_identity_condition(capability)
    primary_identity = primary_pane_capability_condition(capability)

    assert stored_primary_pane in session_identity
    assert exact_current_pane not in session_identity
    assert session_identity in primary_identity
    assert exact_current_pane in primary_identity


def test_registration_and_primary_target_are_independent_capability_layers(
    tmp_path: Path,
) -> None:
    capability = _registered_capability(tmp_path / "tmux.sock")
    exact_current_pane = f"#{{==:#{{pane_id}},{capability.tmux_primary_pane_id}}}"

    registered_session = registered_capability_condition(capability)
    registered_primary = registered_primary_pane_condition(capability)

    assert "@rodex_registration_state" in registered_session
    assert exact_current_pane not in registered_session
    assert registered_session in registered_primary
    assert exact_current_pane in registered_primary
