"""Typed, round-trippable wire contracts for Rodex-owned subprocesses."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from rodex_registry.identity import RodexSessionId


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


@dataclass(frozen=True, slots=True)
class AnalyticsWorkerConfig:
    """Stable identity and source inputs for one runtime's analytics worker."""

    rodex_database_path: Path
    codex_sessions_root: Path
    rodex_session_id: RodexSessionId

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rodex_database_path",
            _absolute_path(self.rodex_database_path),
        )
        object.__setattr__(
            self,
            "codex_sessions_root",
            self.codex_sessions_root.expanduser().resolve(),
        )

    @classmethod
    def add_arguments(
        cls,
        parser: argparse.ArgumentParser,
        *,
        required: bool,
    ) -> None:
        parser.add_argument("--rodex-database", required=required, type=Path)
        parser.add_argument("--codex-sessions-root", required=required, type=Path)
        parser.add_argument(
            "--rodex-session-id",
            required=required,
            type=RodexSessionId.parse,
        )

    @classmethod
    def from_namespace(
        cls,
        namespace: argparse.Namespace,
        *,
        optional_group: bool,
    ) -> Self | None:
        values = (
            namespace.rodex_database,
            namespace.codex_sessions_root,
            namespace.rodex_session_id,
        )
        if optional_group and not any(value is not None for value in values):
            return None
        if not all(value is not None for value in values):
            raise ValueError("analytics arguments must be supplied together")
        return cls(
            rodex_database_path=namespace.rodex_database,
            codex_sessions_root=namespace.codex_sessions_root,
            rodex_session_id=namespace.rodex_session_id,
        )

    def to_argv(self) -> list[str]:
        return [
            "--rodex-database",
            str(self.rodex_database_path),
            "--codex-sessions-root",
            str(self.codex_sessions_root),
            "--rodex-session-id",
            str(self.rodex_session_id),
        ]

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="python -m rodex.analytics_worker")
        cls.add_arguments(parser, required=True)
        return parser

    @classmethod
    def parse(cls, arguments: list[str] | None = None) -> Self:
        config = cls.from_namespace(
            cls.parser().parse_args(arguments),
            optional_group=False,
        )
        assert config is not None
        return config

    def command(self, python_executable: str) -> list[str]:
        return [
            python_executable,
            "-m",
            "rodex.analytics_worker",
            *self.to_argv(),
        ]


@dataclass(frozen=True, slots=True)
class SessionHostConfig:
    """Complete configuration crossing from the launcher into the session host."""

    codex_binary: str
    app_server_socket_path: Path
    app_server_log_path: Path
    protocol_proxy_socket_path: Path
    protocol_event_socket_path: Path
    tmux_binary: str
    tmux_server_socket_path: Path
    codex_arguments: tuple[str, ...] = ()
    analytics: AnalyticsWorkerConfig | None = None

    def __post_init__(self) -> None:
        if not self.codex_binary or not self.tmux_binary:
            raise ValueError("session host binaries must be non-empty")
        for field_name in (
            "app_server_socket_path",
            "app_server_log_path",
            "protocol_proxy_socket_path",
            "protocol_event_socket_path",
            "tmux_server_socket_path",
        ):
            object.__setattr__(self, field_name, _absolute_path(getattr(self, field_name)))

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="python -m rodex.session_host")
        parser.add_argument("--codex-binary", required=True)
        parser.add_argument("--app-server-socket", required=True, type=Path)
        parser.add_argument("--app-server-log", required=True, type=Path)
        parser.add_argument("--protocol-proxy-socket", required=True, type=Path)
        parser.add_argument("--protocol-event-socket", required=True, type=Path)
        parser.add_argument("--tmux-binary", required=True)
        parser.add_argument("--tmux-server-socket", required=True, type=Path)
        AnalyticsWorkerConfig.add_arguments(parser, required=False)
        parser.add_argument("codex_arguments", nargs=argparse.REMAINDER)
        return parser

    @classmethod
    def parse(cls, arguments: list[str] | None = None) -> Self:
        namespace = cls.parser().parse_args(arguments)
        codex_arguments = tuple(namespace.codex_arguments)
        if codex_arguments[:1] == ("--",):
            codex_arguments = codex_arguments[1:]
        try:
            analytics = AnalyticsWorkerConfig.from_namespace(
                namespace,
                optional_group=True,
            )
        except ValueError as error:
            cls.parser().error(str(error))
        return cls(
            codex_binary=namespace.codex_binary,
            app_server_socket_path=namespace.app_server_socket,
            app_server_log_path=namespace.app_server_log,
            protocol_proxy_socket_path=namespace.protocol_proxy_socket,
            protocol_event_socket_path=namespace.protocol_event_socket,
            tmux_binary=namespace.tmux_binary,
            tmux_server_socket_path=namespace.tmux_server_socket,
            codex_arguments=codex_arguments,
            analytics=analytics,
        )

    def to_argv(self) -> list[str]:
        arguments = [
            "--codex-binary",
            self.codex_binary,
            "--app-server-socket",
            str(self.app_server_socket_path),
            "--app-server-log",
            str(self.app_server_log_path),
            "--protocol-proxy-socket",
            str(self.protocol_proxy_socket_path),
            "--protocol-event-socket",
            str(self.protocol_event_socket_path),
            "--tmux-binary",
            self.tmux_binary,
            "--tmux-server-socket",
            str(self.tmux_server_socket_path),
        ]
        if self.analytics is not None:
            arguments.extend(self.analytics.to_argv())
        return [*arguments, "--", *self.codex_arguments]

    def command(self, python_executable: str) -> list[str]:
        return [python_executable, "-m", "rodex.session_host", *self.to_argv()]
