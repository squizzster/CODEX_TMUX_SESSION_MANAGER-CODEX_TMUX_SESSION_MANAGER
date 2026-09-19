from __future__ import annotations

from pathlib import Path

import pytest

from rodex.process_contracts import AnalyticsRuntimeConfig, RuntimeServiceConfig
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId

LEADING_ZERO_SESSION_ID = RodexSessionId.parse("0000000000000001")
REGISTRY_ID = RodexRegistryId.parse("0000000000000001")
RUNTIME_ID = RodexRuntimeId.parse("0000000000000001")
CODEX_SESSION_ID = "01a00654-f2bc-7a30-834a-a5f886a65f82"
SERVER_ID = "0123456789abcdef0123456789abcdef"


def _analytics(root: Path, *, rodex_session_id: RodexSessionId = LEADING_ZERO_SESSION_ID) -> AnalyticsRuntimeConfig:
    return AnalyticsRuntimeConfig(
        tmux_server_id=SERVER_ID,
        rodex_database_path=root / "rodex database.sqlite3",
        codex_sessions_root=root / "codex sessions",
        rodex_session_id=rodex_session_id,
        rodex_registry_id=REGISTRY_ID,
        runtime_id=RUNTIME_ID,
        protocol_event_socket_path=root / "events-0000000000000001.sock",
        rodex_sessions_id=1,
        codex_session_id=CODEX_SESSION_ID,
    )


def _service(root: Path) -> RuntimeServiceConfig:
    return RuntimeServiceConfig(
        workspace=root,
        codex_binary="/opt/Codex CLI/codex",
        app_server_socket_path=root / "app-0000000000000001.sock",
        app_server_log_path=root / "app-0000000000000001.log",
        protocol_proxy_socket_path=root / "proxy-0000000000000001.sock",
        protocol_event_socket_path=root / "events-0000000000000001.sock",
        tmux_binary="/opt/tmux bin/tmux",
        tmux_server_socket_path=root / "tmux-v4-0000000000000001.sock",
        tmux_pane_target="%7",
        runtime_id=RUNTIME_ID,
        codex_arguments=("resume", "thread with spaces"),
        analytics=_analytics(root),
        user_environment=(("EMPTY", ""), ("PATH", "/usr/bin")),
    )


def test_daemon_contract_preserves_a_leading_zero_session_id_as_a_domain_value(tmp_path: Path) -> None:
    config = RuntimeServiceConfig.from_payload(_service(tmp_path).to_payload())

    assert config.analytics.rodex_session_id == LEADING_ZERO_SESSION_ID
    assert str(config.analytics.rodex_session_id) == "0000000000000001"


def test_daemon_runtime_configs_own_round_trippable_private_payloads(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert AnalyticsRuntimeConfig.from_payload(service.analytics.to_payload()) == service.analytics
    assert RuntimeServiceConfig.from_payload(service.to_payload()) == service
    assert service.to_payload()["user_environment"] == [["EMPTY", ""], ["PATH", "/usr/bin"]]


@pytest.mark.parametrize(
    "missing_field",
    ["rodex_database_path", "codex_sessions_root", "rodex_session_id", "rodex_registry_id"],
)
def test_analytics_runtime_payload_requires_complete_managed_identity(tmp_path: Path, missing_field: str) -> None:
    payload = _analytics(tmp_path).to_payload()
    del payload[missing_field]

    with pytest.raises(ValueError, match="fields do not match"):
        AnalyticsRuntimeConfig.from_payload(payload)


@pytest.mark.parametrize(
    "invalid_session_id",
    ["000000000000001", "000000000000000A", "00000000-00000000"],
)
def test_daemon_payload_rejects_noncanonical_session_ids_before_runtime_work(
    tmp_path: Path,
    invalid_session_id: str,
) -> None:
    payload = _analytics(tmp_path).to_payload()
    payload["rodex_session_id"] = invalid_session_id

    with pytest.raises(ValueError):
        AnalyticsRuntimeConfig.from_payload(payload)
