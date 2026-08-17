from __future__ import annotations

import json
from pathlib import Path

import pytest

from rodex.app_server_contract import (
    SUPPORTED_CODEX_APP_SERVER_VERSION,
    RodexAppServerCompatibilityError,
    require_supported_app_server,
)


def test_checked_in_contract_matches_the_exact_supported_version() -> None:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "codex_app_server_0_147_contract.json"
    )
    contract = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert contract == {
        "codex_cli_version": SUPPORTED_CODEX_APP_SERVER_VERSION,
        "initialize_user_agent": "rodex-control/0.147.0 (platform metadata)",
        "request_id_types": ["integer", "string"],
        "thread_required_fields": ["id", "sessionId", "status", "turns"],
        "turn_statuses": ["completed", "failed", "inProgress", "interrupted"],
        "turn_interrupt_required_params": ["threadId", "turnId"],
        "turn_steer_required_params": ["expectedTurnId", "input", "threadId"],
        "turn_start_optional_params": ["clientUserMessageId"],
    }


def test_live_initialize_metadata_is_the_exact_control_compatibility_gate() -> None:
    assert (
        require_supported_app_server({"userAgent": "rodex-control/0.147.0 (Linux; x86_64)"})
        == "0.147.0"
    )

    with pytest.raises(RodexAppServerCompatibilityError, match=r"live server is 0\.148\.0"):
        require_supported_app_server({"userAgent": "rodex-control/0.148.0 (Linux)"})

    with pytest.raises(RodexAppServerCompatibilityError, match="no recognized"):
        require_supported_app_server({})
