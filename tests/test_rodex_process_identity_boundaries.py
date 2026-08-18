from __future__ import annotations

from pathlib import Path

import pytest

from rodex.analytics import analytics_worker_main
from rodex.process_contracts import AnalyticsWorkerConfig, SessionHostConfig
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
    config = SessionHostConfig.parse(_session_host_arguments(str(LEADING_ZERO_SESSION_ID)))

    assert config.analytics is not None
    assert config.analytics.rodex_session_id == LEADING_ZERO_SESSION_ID
    assert str(config.analytics.rodex_session_id) == "0000000000000001"


def test_analytics_worker_command_preserves_the_exact_string_wire_form() -> None:
    config = AnalyticsWorkerConfig(
        rodex_database_path=Path("/tmp/rodex.sqlite3"),
        codex_sessions_root=Path("/tmp/sessions"),
        rodex_session_id=LEADING_ZERO_SESSION_ID,
    )
    command = config.command("/venv/bin/python")

    assert command[-2:] == ["--rodex-session-id", "0000000000000001"]


def test_process_configs_own_round_trippable_wire_contracts() -> None:
    analytics = AnalyticsWorkerConfig(
        rodex_database_path=Path("/tmp/rodex database.sqlite3"),
        codex_sessions_root=Path("/tmp/codex sessions"),
        rodex_session_id=LEADING_ZERO_SESSION_ID,
    )
    host = SessionHostConfig(
        codex_binary="/opt/Codex CLI/codex",
        app_server_socket_path=Path("/tmp/app socket.sock"),
        app_server_log_path=Path("/tmp/app log.log"),
        protocol_proxy_socket_path=Path("/tmp/proxy socket.sock"),
        protocol_event_socket_path=Path("/tmp/event socket.sock"),
        tmux_binary="/opt/tmux bin/tmux",
        tmux_server_socket_path=Path("/tmp/tmux socket.sock"),
        codex_arguments=("resume", "thread with spaces"),
        analytics=analytics,
    )

    assert AnalyticsWorkerConfig.parse(analytics.to_argv()) == analytics
    assert SessionHostConfig.parse(host.to_argv()) == host


@pytest.mark.parametrize(
    "invalid_session_id",
    ["000000000000001", "000000000000000A", "00000000-00000000"],
)
def test_process_entry_points_reject_noncanonical_session_ids_before_work_starts(
    invalid_session_id: str,
) -> None:
    with pytest.raises(SystemExit):
        SessionHostConfig.parse(_session_host_arguments(invalid_session_id))
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
