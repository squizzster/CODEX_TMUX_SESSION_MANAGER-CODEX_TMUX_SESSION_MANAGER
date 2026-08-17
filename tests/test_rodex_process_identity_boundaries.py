from __future__ import annotations

from pathlib import Path

import pytest

from rodex.analytics import (
    AnalyticsWorkerConfig,
    analytics_worker_command,
    analytics_worker_main,
)
from rodex.session_host import _build_parser as build_session_host_parser
from rodex_registry import RodexSessionId

LEADING_ZERO_SESSION_ID = RodexSessionId.parse("0000000000000001")


def _session_host_arguments(rodex_session_id: str) -> list[str]:
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
        "--rodex-session-id",
        rodex_session_id,
    ]


def test_session_host_preserves_a_leading_zero_session_id_as_a_domain_value() -> None:
    options = build_session_host_parser().parse_args(
        _session_host_arguments(str(LEADING_ZERO_SESSION_ID))
    )

    assert options.rodex_session_id == LEADING_ZERO_SESSION_ID
    assert str(options.rodex_session_id) == "0000000000000001"


def test_analytics_worker_command_preserves_the_exact_string_wire_form() -> None:
    command = analytics_worker_command(
        "/venv/bin/python",
        AnalyticsWorkerConfig(
            rodex_database_path=Path("/tmp/rodex.sqlite3"),
            codex_sessions_root=Path("/tmp/sessions"),
            rodex_session_id=LEADING_ZERO_SESSION_ID,
        ),
    )

    assert command[-2:] == ["--rodex-session-id", "0000000000000001"]


@pytest.mark.parametrize(
    "invalid_session_id",
    ["000000000000001", "000000000000000A", "00000000-00000000"],
)
def test_process_entry_points_reject_noncanonical_session_ids_before_work_starts(
    invalid_session_id: str,
) -> None:
    with pytest.raises(SystemExit):
        build_session_host_parser().parse_args(_session_host_arguments(invalid_session_id))
    with pytest.raises(SystemExit):
        analytics_worker_main(
            [
                "--rodex-database",
                "/tmp/rodex.sqlite3",
                "--codex-sessions-root",
                "/tmp/sessions",
                "--rodex-session-id",
                invalid_session_id,
            ]
        )
