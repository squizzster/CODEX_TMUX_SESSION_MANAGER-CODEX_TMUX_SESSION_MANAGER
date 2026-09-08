"""Current release boundaries reject missing authority and alternate wire shapes."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from rodex.agent_trace import _canonical_record_turn_id, normalize_rollout_trace
from rodex.control import RodexControlError, _started_turn_id, _steered_turn_id
from rodex.protocol_proxy import _context_percent, _rollout_context_percent, _started_thread_id
from rodex.tmux_session_capability import RODEX_SHARED_TMUX_PROTOCOL, RODEX_SHARED_TMUX_SOCKET_NAME
from rodex.version import RODEX_VERSION
from rodex_registry import parse_codex_thread_id
from rodex_sql import RODEX_DATABASE_FILENAME, RODEX_DATABASE_SCHEMA_GENERATION


def test_current_release_declares_matching_package_and_process_versions() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["version"] == RODEX_VERSION == "0.7.0a1"
    assert RODEX_DATABASE_SCHEMA_GENERATION == 19
    assert RODEX_DATABASE_FILENAME == "rodex-v19.sqlite3"
    assert RODEX_SHARED_TMUX_PROTOCOL == "rodex-shared-tmux-v2"
    assert RODEX_SHARED_TMUX_SOCKET_NAME == "tmux-shared-v2.sock"


@pytest.mark.parametrize("turn_id", [None, "", " ", 1])
def test_mutation_response_parsers_require_their_own_nonempty_turn_identity(turn_id: object) -> None:
    with pytest.raises(RodexControlError):
        _started_turn_id({"turn": {"id": turn_id}})
    with pytest.raises(RodexControlError):
        _steered_turn_id({"turnId": turn_id})


def test_mutation_response_parsers_do_not_guess_another_methods_shape() -> None:
    start_response = {"turn": {"id": "started-turn"}}
    steer_response = {"turnId": "steered-turn"}
    assert _started_turn_id(start_response) == "started-turn"
    assert _steered_turn_id(steer_response) == "steered-turn"
    with pytest.raises(RodexControlError):
        _started_turn_id(steer_response)
    with pytest.raises(RodexControlError):
        _steered_turn_id(start_response)


@pytest.mark.parametrize("alternate_last_key", ["lastTokenUsage", "last_token_usage"])
def test_app_server_context_ignores_alternate_usage_fields(alternate_last_key: str) -> None:
    assert _context_percent({"tokenUsage": {alternate_last_key: {"totalTokens": 50}, "modelContextWindow": 100}}) is None


def test_rollout_and_app_server_context_each_require_their_own_field_contract() -> None:
    assert _context_percent({"tokenUsage": {"last": {"totalTokens": 50}, "modelContextWindow": 100}}) == 50
    assert _context_percent({"tokenUsage": {"last": {"total_tokens": 50}, "model_context_window": 100}}) is None
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": 50},
                "model_context_window": 100,
            },
        },
    }
    assert _rollout_context_percent(json.dumps(record).encode()) == 50
    record["payload"]["info"] = {"last_token_usage": {"totalTokens": 50}, "modelContextWindow": 100}
    assert _rollout_context_percent(json.dumps(record).encode()) is None


def test_thread_started_requires_nested_thread_identity() -> None:
    assert _started_thread_id({"thread": {"id": "current-thread"}}) == "current-thread"
    assert _started_thread_id({"threadId": "alternate-thread"}) is None


def test_rollout_turn_identity_is_read_from_its_record_types_authoritative_location() -> None:
    turn_id = "00000000-0000-7000-8000-00000000000a"
    direct_identity = {"turn_id": turn_id}
    response_identity = {"internal_chat_message_metadata_passthrough": direct_identity}
    assert _canonical_record_turn_id("response_item", response_identity) == turn_id
    assert _canonical_record_turn_id("turn_context", direct_identity) == turn_id
    assert _canonical_record_turn_id("response_item", direct_identity) is None
    assert _canonical_record_turn_id("turn_context", response_identity) is None


def test_trace_does_not_infer_turn_or_tool_identity_from_alternate_spellings() -> None:
    publication = normalize_rollout_trace(
        (
            (
                parse_codex_thread_id("01a00654-f2bc-7a30-834a-a5f886a65f82"),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "tool": "invented",
                            "turnId": "00000000-0000-7000-8000-00000000000a",
                            "input": "invented request",
                        },
                    }
                ).encode(),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-09-08T18:00:00Z",
    )
    event = publication.events[0]
    assert event.codex_turn_id is None
    assert event.detail.tool_name == "unknown"
    assert event.detail.request_utf8_bytes == 0
