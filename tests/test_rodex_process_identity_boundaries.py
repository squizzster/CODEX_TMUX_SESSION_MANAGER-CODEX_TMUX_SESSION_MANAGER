from __future__ import annotations

from pathlib import Path

import pytest

from rodex.analytics import (
    AnalyticsWorkerConfig,
    analytics_worker_command,
    analytics_worker_main,
)
from rodex.session_host import _build_parser as build_session_host_parser
from rodex_registry import RodexSessionIdentifier

LEADING_ZERO_IDENTIFIER = RodexSessionIdentifier.parse("0000000000000001")


def _session_host_arguments(identifier: str) -> list[str]:
    return [
        "--codex-binary",
        "/usr/bin/codex",
        "--app-server-socket",
        "/tmp/app.sock",
        "--app-server-log",
        "/tmp/app.log",
        "--protocol-proxy-socket",
        "/tmp/proxy.sock",
        "--protocol-event-socket",
        "/tmp/events.sock",
        "--tmux-binary",
        "/usr/bin/tmux",
        "--tmux-server-socket",
        "/tmp/tmux.sock",
        "--rodex-database",
        "/tmp/rodex.sqlite3",
        "--codex-sessions-root",
        "/tmp/sessions",
        "--rodex-session-identifier",
        identifier,
    ]


def test_session_host_preserves_a_leading_zero_identifier_as_a_domain_value() -> None:
    options = build_session_host_parser().parse_args(
        _session_host_arguments(str(LEADING_ZERO_IDENTIFIER))
    )

    assert options.rodex_session_identifier == LEADING_ZERO_IDENTIFIER
    assert str(options.rodex_session_identifier) == "0000000000000001"


def test_analytics_worker_command_preserves_the_exact_string_wire_form() -> None:
    command = analytics_worker_command(
        "/venv/bin/python",
        AnalyticsWorkerConfig(
            rodex_database_path=Path("/tmp/rodex.sqlite3"),
            codex_sessions_root=Path("/tmp/sessions"),
            rodex_session_identifier=LEADING_ZERO_IDENTIFIER,
        ),
    )

    assert command[-2:] == ["--rodex-session-identifier", "0000000000000001"]


@pytest.mark.parametrize(
    "invalid_identifier",
    ["000000000000001", "000000000000000A", "00000000-00000000"],
)
def test_process_entry_points_reject_noncanonical_identifiers_before_work_starts(
    invalid_identifier: str,
) -> None:
    with pytest.raises(SystemExit):
        build_session_host_parser().parse_args(_session_host_arguments(invalid_identifier))
    with pytest.raises(SystemExit):
        analytics_worker_main(
            [
                "--rodex-database",
                "/tmp/rodex.sqlite3",
                "--codex-sessions-root",
                "/tmp/sessions",
                "--rodex-session-identifier",
                invalid_identifier,
            ]
        )
