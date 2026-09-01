"""Explicit authority for one session on Rodex's shared tmux server."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rodex_registry.identity import (
    CodexSessionId,
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
    parse_codex_session_id,
    parse_rodex_registry_id,
    parse_rodex_runtime_id,
)

RODEX_CODEX_SESSION_ID_OPTION: Final = "@rodex_codex_session_id"
RODEX_INTERNAL_SESSION_ID_OPTION: Final = "@rodex_sessions_id"
RODEX_PRIMARY_PANE_ID_OPTION: Final = "@rodex_primary_pane_id"
RODEX_PROTOCOL_EVENT_SOCKET_OPTION: Final = "@rodex_protocol_event_socket_path"
RODEX_PROTOCOL_PROXY_SOCKET_OPTION: Final = "@rodex_protocol_proxy_socket_path"
RODEX_REGISTRATION_STATE_OPTION: Final = "@rodex_registration_state"
RODEX_REGISTRY_ID_OPTION: Final = "@rodex_registry_id"
RODEX_RUNTIME_ID_OPTION: Final = "@rodex_runtime_id"
RODEX_SESSION_ID_OPTION: Final = "@rodex_session_id"
RODEX_REGISTRATION_PENDING: Final = "pending"
RODEX_REGISTRATION_REGISTERED: Final = "registered"
RODEX_SHARED_TMUX_PROTOCOL_OPTION: Final = "@rodex_shared_tmux_protocol"
RODEX_SHARED_TMUX_PROTOCOL: Final = "rodex-shared-tmux-v1"
RODEX_SHARED_TMUX_SERVER_ID_OPTION: Final = "@rodex_shared_tmux_server_id"
RODEX_SHARED_TMUX_SOCKET_NAME: Final = "tmux-shared-v1.sock"

_TMUX_SESSION_ID_PATTERN: Final = re.compile(r"\$[0-9]+")
_TMUX_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")
_TMUX_SERVER_ID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class TmuxRuntimeCapability:
    """Authority for the session host's own pane throughout one runtime incarnation."""

    tmux_server_socket_path: Path
    tmux_server_id: str
    tmux_session_id: str
    tmux_primary_pane_id: str
    runtime_id: RodexRuntimeId

    def __post_init__(self) -> None:
        _validate_tmux_runtime_identity(
            self.tmux_server_socket_path,
            self.tmux_server_id,
            self.tmux_session_id,
            self.tmux_primary_pane_id,
        )

    @property
    def session_target(self) -> str:
        return self.tmux_session_id

    @property
    def pane_target(self) -> str:
        return self.tmux_primary_pane_id


@dataclass(frozen=True, slots=True)
class TmuxSessionCapability:
    """The non-ambient authority required for one registered session operation."""

    tmux_server_socket_path: Path
    tmux_server_id: str
    tmux_session_id: str
    tmux_primary_pane_id: str
    runtime_id: RodexRuntimeId
    rodex_session_id: RodexSessionId
    registry_id: RodexRegistryId
    internal_session_id: int
    codex_session_id: CodexSessionId

    def __post_init__(self) -> None:
        _validate_tmux_runtime_identity(
            self.tmux_server_socket_path,
            self.tmux_server_id,
            self.tmux_session_id,
            self.tmux_primary_pane_id,
        )
        if (
            not isinstance(self.internal_session_id, int)
            or isinstance(self.internal_session_id, bool)
            or self.internal_session_id <= 0
        ):
            raise ValueError("tmux session capability requires a positive SQL row ID")

    @property
    def session_target(self) -> str:
        return self.tmux_session_id

    @property
    def pane_target(self) -> str:
        return self.tmux_primary_pane_id

    @property
    def runtime_capability(self) -> TmuxRuntimeCapability:
        return TmuxRuntimeCapability(
            self.tmux_server_socket_path,
            self.tmux_server_id,
            self.tmux_session_id,
            self.tmux_primary_pane_id,
            self.runtime_id,
        )


def _validate_tmux_runtime_identity(
    tmux_server_socket_path: Path,
    tmux_server_id: str,
    tmux_session_id: str,
    tmux_primary_pane_id: str,
) -> None:
    if not tmux_server_socket_path.is_absolute():
        raise ValueError("tmux server socket path must be absolute")
    if _TMUX_SERVER_ID_PATTERN.fullmatch(tmux_server_id) is None:
        raise ValueError("tmux capability requires an exact server ID")
    if _TMUX_SESSION_ID_PATTERN.fullmatch(tmux_session_id) is None:
        raise ValueError("tmux capability requires an exact $session_id")
    if _TMUX_PANE_ID_PATTERN.fullmatch(tmux_primary_pane_id) is None:
        raise ValueError("tmux capability requires an exact primary %pane_id")


def parse_tmux_session_capability(
    tmux_server_socket_path: Path,
    tmux_server_id: str,
    tmux_session_id: str,
    tmux_primary_pane_id: str,
    runtime_id: str,
    rodex_session_id: str,
    registry_id: str,
    internal_session_id: str,
    codex_session_id: str,
) -> TmuxSessionCapability:
    """Parse an externally observed capability without accepting fuzzy identities."""
    return TmuxSessionCapability(
        tmux_server_socket_path=tmux_server_socket_path,
        tmux_server_id=tmux_server_id,
        tmux_session_id=tmux_session_id,
        tmux_primary_pane_id=tmux_primary_pane_id,
        runtime_id=parse_rodex_runtime_id(runtime_id),
        rodex_session_id=RodexSessionId.parse(rodex_session_id),
        registry_id=parse_rodex_registry_id(registry_id),
        internal_session_id=int(internal_session_id),
        codex_session_id=parse_codex_session_id(codex_session_id),
    )


def parse_tmux_server_id(value: str) -> str:
    """Reject absent, malformed, or noncanonical shared-server identities."""
    if _TMUX_SERVER_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("shared tmux server ID is invalid")
    return value


def server_identity_condition(tmux_server_id: str) -> str:
    """Fence a server-global action to one shared-server incarnation."""
    parse_tmux_server_id(tmux_server_id)
    return combine_tmux_conditions(
        _literal_comparison(
            f"#{{{RODEX_SHARED_TMUX_PROTOCOL_OPTION}}}",
            RODEX_SHARED_TMUX_PROTOCOL,
        ),
        _literal_comparison(
            f"#{{{RODEX_SHARED_TMUX_SERVER_ID_OPTION}}}",
            tmux_server_id,
        ),
    )


def capability_identity_condition(
    capability: TmuxRuntimeCapability | TmuxSessionCapability,
) -> str:
    """Fence any pane in the exact session and runtime incarnation."""
    return combine_tmux_conditions(
        server_identity_condition(capability.tmux_server_id),
        _literal_comparison("#{session_id}", capability.tmux_session_id),
        _literal_comparison(
            f"#{{{RODEX_PRIMARY_PANE_ID_OPTION}}}",
            capability.tmux_primary_pane_id,
        ),
        _literal_comparison(
            f"#{{{RODEX_RUNTIME_ID_OPTION}}}",
            str(capability.runtime_id),
        ),
    )


def registered_capability_condition(capability: TmuxSessionCapability) -> str:
    """Fence any pane in an exact, SQL-confirmed Rodex runtime incarnation."""
    return combine_tmux_conditions(
        capability_identity_condition(capability),
        _literal_comparison(
            f"#{{{RODEX_REGISTRATION_STATE_OPTION}}}",
            RODEX_REGISTRATION_REGISTERED,
        ),
        _literal_comparison(
            f"#{{{RODEX_SESSION_ID_OPTION}}}",
            str(capability.rodex_session_id),
        ),
        _literal_comparison(
            f"#{{{RODEX_REGISTRY_ID_OPTION}}}",
            str(capability.registry_id),
        ),
        _literal_comparison(
            f"#{{{RODEX_INTERNAL_SESSION_ID_OPTION}}}",
            str(capability.internal_session_id),
        ),
        _literal_comparison(
            f"#{{{RODEX_CODEX_SESSION_ID_OPTION}}}",
            str(capability.codex_session_id),
        ),
    )


def primary_pane_capability_condition(
    capability: TmuxRuntimeCapability | TmuxSessionCapability,
) -> str:
    """Fence an operation to the immutable primary pane carried by a capability."""
    return combine_tmux_conditions(
        capability_identity_condition(capability),
        _literal_comparison("#{pane_id}", capability.tmux_primary_pane_id),
    )


def registered_primary_pane_condition(capability: TmuxSessionCapability) -> str:
    """Fence an operation to one registered runtime's immutable primary pane."""
    return combine_tmux_conditions(
        registered_capability_condition(capability),
        _literal_comparison("#{pane_id}", capability.tmux_primary_pane_id),
    )


def combine_tmux_conditions(*conditions: str) -> str:
    """Combine already-validated tmux format predicates without ambient authority."""
    if not conditions or any(not condition for condition in conditions):
        raise ValueError("at least one non-empty tmux condition is required")
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = f"#{{&&:{combined},{condition}}}"
    return combined


def _literal_comparison(actual_format: str, expected: str) -> str:
    """Compare a tmux format value without reinterpreting the expected identity."""
    return f"#{{==:{actual_format},#{{l:{tmux_format_literal(expected)}}}}}"


def tmux_format_literal(value: str) -> str:
    """Escape static text before tmux performs its earlier format-expansion pass."""
    if not isinstance(value, str):
        raise TypeError("tmux format literal must be text")
    return value.replace("#", "##")
