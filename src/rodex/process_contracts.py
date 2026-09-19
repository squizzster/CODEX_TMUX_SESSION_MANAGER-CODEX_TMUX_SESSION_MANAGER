"""Typed, round-trippable contracts crossing into the shared Rodex daemon."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from rodex_registry.identity import (
    CodexSessionId,
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
    parse_codex_session_id,
    parse_rodex_registry_id,
    parse_rodex_runtime_id,
    parse_rodex_session_id,
)

from .tmux_session_capability import parse_tmux_server_id

_TMUX_OWNED_ENVIRONMENT_NAMES = frozenset({"SHELL", "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TMUX", "TMUX_PANE"})


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    return value


def _optional_positive_integer(payload: Mapping[str, Any], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class AnalyticsRuntimeConfig:
    """Stable identity and source inputs for one daemon-owned analytics runtime."""

    rodex_database_path: Path
    codex_sessions_root: Path
    rodex_session_id: RodexSessionId
    rodex_registry_id: RodexRegistryId
    runtime_id: RodexRuntimeId
    tmux_server_id: str
    protocol_event_socket_path: Path
    rodex_sessions_id: int | None = None
    codex_session_id: CodexSessionId | None = None

    def __post_init__(self) -> None:
        parse_tmux_server_id(self.tmux_server_id)
        object.__setattr__(self, "rodex_database_path", _absolute_path(self.rodex_database_path))
        object.__setattr__(self, "codex_sessions_root", self.codex_sessions_root.expanduser().resolve())
        object.__setattr__(self, "protocol_event_socket_path", _absolute_path(self.protocol_event_socket_path))
        object.__setattr__(self, "rodex_session_id", parse_rodex_session_id(self.rodex_session_id))
        object.__setattr__(self, "rodex_registry_id", parse_rodex_registry_id(self.rodex_registry_id))
        object.__setattr__(self, "runtime_id", parse_rodex_runtime_id(self.runtime_id))
        if self.codex_session_id is not None:
            object.__setattr__(self, "codex_session_id", parse_codex_session_id(self.codex_session_id))
        activated = (self.rodex_sessions_id, self.codex_session_id)
        if any(value is not None for value in activated) and not all(value is not None for value in activated):
            raise ValueError("analytics activation identity must be supplied together")
        if self.rodex_sessions_id is not None and (
            not isinstance(self.rodex_sessions_id, int)
            or isinstance(self.rodex_sessions_id, bool)
            or self.rodex_sessions_id <= 0
        ):
            raise ValueError("rodex_sessions_id must be a positive integer")

    @property
    def is_activated(self) -> bool:
        return self.rodex_sessions_id is not None

    def activate(
        self,
        *,
        rodex_sessions_id: int,
        codex_session_id: CodexSessionId | str,
    ) -> Self:
        activated = type(self)(
            rodex_database_path=self.rodex_database_path,
            codex_sessions_root=self.codex_sessions_root,
            rodex_session_id=self.rodex_session_id,
            rodex_registry_id=self.rodex_registry_id,
            runtime_id=self.runtime_id,
            tmux_server_id=self.tmux_server_id,
            protocol_event_socket_path=self.protocol_event_socket_path,
            rodex_sessions_id=rodex_sessions_id,
            codex_session_id=parse_codex_session_id(codex_session_id),
        )
        if self.is_activated and activated != self:
            raise ValueError("analytics runtime is already activated for another identity")
        return activated

    def to_payload(self) -> dict[str, object]:
        return {
            "rodex_database_path": os.fspath(self.rodex_database_path),
            "codex_sessions_root": os.fspath(self.codex_sessions_root),
            "rodex_session_id": str(self.rodex_session_id),
            "rodex_registry_id": str(self.rodex_registry_id),
            "runtime_id": str(self.runtime_id),
            "tmux_server_id": self.tmux_server_id,
            "protocol_event_socket_path": os.fspath(self.protocol_event_socket_path),
            "rodex_sessions_id": self.rodex_sessions_id,
            "codex_session_id": None if self.codex_session_id is None else str(self.codex_session_id),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        expected = {
            "rodex_database_path",
            "codex_sessions_root",
            "rodex_session_id",
            "rodex_registry_id",
            "runtime_id",
            "tmux_server_id",
            "protocol_event_socket_path",
            "rodex_sessions_id",
            "codex_session_id",
        }
        if set(payload) != expected:
            raise ValueError("analytics runtime payload fields do not match the current contract")
        codex_session_id = payload.get("codex_session_id")
        return cls(
            rodex_database_path=Path(_required_text(payload, "rodex_database_path")),
            codex_sessions_root=Path(_required_text(payload, "codex_sessions_root")),
            rodex_session_id=RodexSessionId.parse(_required_text(payload, "rodex_session_id")),
            rodex_registry_id=parse_rodex_registry_id(_required_text(payload, "rodex_registry_id")),
            runtime_id=parse_rodex_runtime_id(_required_text(payload, "runtime_id")),
            tmux_server_id=parse_tmux_server_id(_required_text(payload, "tmux_server_id")),
            protocol_event_socket_path=Path(_required_text(payload, "protocol_event_socket_path")),
            rodex_sessions_id=_optional_positive_integer(payload, "rodex_sessions_id"),
            codex_session_id=(None if codex_session_id is None else parse_codex_session_id(codex_session_id)),
        )


@dataclass(frozen=True, slots=True)
class RuntimeServiceConfig:
    """Complete configuration for one runtime managed inside ``rodexd``."""

    codex_binary: str
    workspace: Path
    app_server_socket_path: Path
    app_server_log_path: Path
    protocol_proxy_socket_path: Path
    protocol_event_socket_path: Path
    tmux_binary: str
    tmux_server_socket_path: Path
    tmux_pane_target: str
    runtime_id: RodexRuntimeId
    analytics: AnalyticsRuntimeConfig
    user_environment: tuple[tuple[str, str], ...]
    codex_arguments: tuple[str, ...] = ()

    @property
    def tmux_server_id(self) -> str:
        return self.analytics.tmux_server_id

    def __post_init__(self) -> None:
        if not self.workspace.is_absolute():
            raise ValueError("runtime workspace must be absolute")
        object.__setattr__(self, "workspace", self.workspace.resolve())
        if not self.codex_binary or not self.tmux_binary:
            raise ValueError("runtime service binaries must be non-empty")
        if not self.tmux_pane_target.startswith("%") or not self.tmux_pane_target[1:].isdigit():
            raise ValueError("runtime service requires an exact tmux pane target")
        for field_name in (
            "app_server_socket_path",
            "app_server_log_path",
            "protocol_proxy_socket_path",
            "protocol_event_socket_path",
            "tmux_server_socket_path",
        ):
            object.__setattr__(self, field_name, _absolute_path(getattr(self, field_name)))
        object.__setattr__(self, "runtime_id", parse_rodex_runtime_id(self.runtime_id))
        if self.analytics.protocol_event_socket_path != self.protocol_event_socket_path:
            raise ValueError("analytics must use the runtime service event socket")
        if self.analytics.runtime_id != self.runtime_id:
            raise ValueError("analytics must use the runtime service identity")
        if self.tmux_server_socket_path.parent != self.protocol_event_socket_path.parent:
            raise ValueError("runtime service endpoints must share one runtime root")
        environment_names: set[str] = set()
        for entry in self.user_environment:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not all(isinstance(value, str) for value in entry)
                or not entry[0]
                or "=" in entry[0]
                or "\x00" in entry[0]
                or "\x00" in entry[1]
            ):
                raise ValueError("runtime service environment entries must be valid text pairs")
            if entry[0] in environment_names:
                raise ValueError("runtime service environment names must be unique")
            if entry[0] in _TMUX_OWNED_ENVIRONMENT_NAMES:
                raise ValueError("runtime service user environment cannot claim tmux-owned names")
            environment_names.add(entry[0])
        if tuple(sorted(self.user_environment)) != self.user_environment:
            raise ValueError("runtime service environment must use canonical name order")
        if any(not isinstance(argument, str) or "\x00" in argument for argument in self.codex_arguments):
            raise ValueError("Codex arguments must be text without NUL bytes")

    @property
    def environment(self) -> dict[str, str]:
        return dict(self.user_environment)

    def to_payload(self) -> dict[str, object]:
        return {
            "codex_binary": self.codex_binary,
            "workspace": os.fspath(self.workspace),
            "app_server_socket_path": os.fspath(self.app_server_socket_path),
            "app_server_log_path": os.fspath(self.app_server_log_path),
            "protocol_proxy_socket_path": os.fspath(self.protocol_proxy_socket_path),
            "protocol_event_socket_path": os.fspath(self.protocol_event_socket_path),
            "tmux_binary": self.tmux_binary,
            "tmux_server_socket_path": os.fspath(self.tmux_server_socket_path),
            "tmux_pane_target": self.tmux_pane_target,
            "runtime_id": str(self.runtime_id),
            "analytics": self.analytics.to_payload(),
            "user_environment": [[name, value] for name, value in self.user_environment],
            "codex_arguments": list(self.codex_arguments),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        expected = {
            "codex_binary",
            "workspace",
            "app_server_socket_path",
            "app_server_log_path",
            "protocol_proxy_socket_path",
            "protocol_event_socket_path",
            "tmux_binary",
            "tmux_server_socket_path",
            "tmux_pane_target",
            "runtime_id",
            "analytics",
            "user_environment",
            "codex_arguments",
        }
        if set(payload) != expected:
            raise ValueError("runtime service payload fields do not match the current contract")
        analytics = payload.get("analytics")
        environment = payload.get("user_environment")
        arguments = payload.get("codex_arguments")
        if not isinstance(analytics, dict):
            raise ValueError("runtime analytics payload must be an object")
        if not isinstance(environment, list) or any(
            not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(value, str) for value in entry)
            for entry in environment
        ):
            raise ValueError("runtime environment payload must contain text pairs")
        if not isinstance(arguments, list) or not all(isinstance(argument, str) for argument in arguments):
            raise ValueError("runtime Codex arguments payload must contain text")
        return cls(
            codex_binary=_required_text(payload, "codex_binary"),
            workspace=Path(_required_text(payload, "workspace")),
            app_server_socket_path=Path(_required_text(payload, "app_server_socket_path")),
            app_server_log_path=Path(_required_text(payload, "app_server_log_path")),
            protocol_proxy_socket_path=Path(_required_text(payload, "protocol_proxy_socket_path")),
            protocol_event_socket_path=Path(_required_text(payload, "protocol_event_socket_path")),
            tmux_binary=_required_text(payload, "tmux_binary"),
            tmux_server_socket_path=Path(_required_text(payload, "tmux_server_socket_path")),
            tmux_pane_target=_required_text(payload, "tmux_pane_target"),
            runtime_id=parse_rodex_runtime_id(_required_text(payload, "runtime_id")),
            analytics=AnalyticsRuntimeConfig.from_payload(analytics),
            user_environment=tuple((entry[0], entry[1]) for entry in environment),
            codex_arguments=tuple(arguments),
        )
