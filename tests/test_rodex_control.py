from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from rodex.control import (
    CodexControlClient,
    LiveRodexControl,
    RodexControlError,
    RodexDispatchIndeterminateError,
    RodexWaitTimeoutError,
    format_protocol_log_event,
)
from rodex.protocol_proxy import EVENT_STREAM_READY_MESSAGE

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


class FakeWebSocket:
    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.responses = iter(json.dumps(response) for response in (responses or []))
        self.events = [json.dumps(event) for event in (events or [])]
        self.sent: list[dict[str, Any]] = []

    def __enter__(self) -> FakeWebSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.events)

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float | None) -> str:
        assert timeout is None or timeout > 0
        try:
            return next(self.responses)
        except StopIteration as error:
            raise TimeoutError from error


class RoutingConnector:
    def __init__(
        self, protocol: FakeWebSocket, events: FakeWebSocket | None = None
    ) -> None:
        self.protocol = protocol
        self.events = events

    def __call__(self, _path: str, *, uri: str, **_options: object) -> FakeWebSocket:
        if uri.endswith("/events"):
            assert self.events is not None
            return self.events
        return self.protocol


def control(tmp_path: Path) -> LiveRodexControl:
    return LiveRodexControl(
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        CODEX_SESSION_ID,
    )


def verified_responses(
    *, status: str, turns: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    return [
        {"id": 0, "result": {"userAgent": "rodex-control/0.147.0 (Linux)"}},
        {"id": 1, "result": {"data": [str(CODEX_SESSION_ID)]}},
        {
            "id": 2,
            "result": {
                "thread": {
                    "id": str(CODEX_SESSION_ID),
                    "sessionId": str(CODEX_SESSION_ID),
                    "cwd": "/workspace/project",
                    "status": {
                        "type": status,
                        **({"activeFlags": []} if status == "active" else {}),
                    },
                    "turns": turns or [],
                    "canAcceptDirectInput": True,
                }
            },
        },
    ]


def test_send_starts_an_idle_turn_after_codex_session_id_verification(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="idle"),
            {"id": "rodex:test", "result": {"turn": {"id": "turn-new"}}},
        ]
    )
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:test",
        dispatch_id_factory=lambda: "rodex:dispatch:test",
    )
    revalidated: list[bool] = []

    dispatched = client.send_prompt(
        control(tmp_path), "run tests", revalidate=lambda: revalidated.append(True)
    )

    assert dispatched.action == "started"
    assert dispatched.turn_id == "turn-new"
    assert protocol.sent[-1] == {
        "method": "turn/start",
        "id": "rodex:test",
        "params": {
            "threadId": str(CODEX_SESSION_ID),
            "input": [{"type": "text", "text": "run tests"}],
            "clientUserMessageId": "rodex:dispatch:test",
        },
    }
    assert revalidated == [True]


def test_send_steers_the_exact_active_turn(tmp_path: Path) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active"),
            {"id": "rodex:test", "result": {"turnId": "turn-active"}},
        ]
    )
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-active"}},
            }
        ]
    )
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:test",
    )

    dispatched = client.send_prompt(control(tmp_path), "also lint")

    assert dispatched.action == "steered"
    assert protocol.sent[-1]["method"] == "turn/steer"
    assert protocol.sent[-1]["params"]["expectedTurnId"] == "turn-active"


def test_thread_inspection_does_not_require_rollout_history(tmp_path: Path) -> None:
    protocol = FakeWebSocket(verified_responses(status="idle"))
    client = CodexControlClient(connector=RoutingConnector(protocol))

    state = client.inspect(control(tmp_path))

    assert state.status == "idle"
    assert state.cwd == "/workspace/project"
    assert protocol.sent[-1]["params"]["includeTurns"] is False


def test_live_inspection_includes_the_event_taps_exact_active_turn(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(verified_responses(status="active"))
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-active"}},
            }
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    state = client.inspect_live(control(tmp_path))

    assert state.status == "active"
    assert state.active_turn_id == "turn-active"


def test_exact_control_version_uses_the_live_initialize_contract(tmp_path: Path) -> None:
    protocol = FakeWebSocket(
        [{"id": 0, "result": {"userAgent": "rodex-control/0.147.0 (Linux)"}}]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol))

    assert client.exact_control_version(control(tmp_path)) == "0.147.0"
    assert protocol.sent[-1] == {"method": "initialized", "params": {}}


def test_control_rejects_an_endpoint_without_the_expected_codex_session_id(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [
            {"id": 0, "result": {}},
            {"id": 1, "result": {"data": [str(uuid.uuid4())]}},
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol))

    with pytest.raises(RodexControlError, match="does not contain expected"):
        client.inspect(control(tmp_path))


def test_wait_subscribes_before_inspection_and_returns_on_turn_completion(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [*verified_responses(status="active"), *verified_responses(status="idle")]
    )
    events = FakeWebSocket(
        responses=[json.loads(EVENT_STREAM_READY_MESSAGE)],
        events=[
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": {"id": "turn-active", "status": "completed"},
                },
            }
        ],
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    client.wait_until_idle(control(tmp_path))


def test_wait_does_not_accept_a_queued_completion_from_the_previous_turn(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active"),
            *verified_responses(status="active"),
            *verified_responses(status="idle"),
        ]
    )
    events = FakeWebSocket(
        responses=[json.loads(EVENT_STREAM_READY_MESSAGE)],
        events=[
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": {"id": "turn-a", "status": "completed"},
                },
            },
            {
                "method": "turn/started",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": {"id": "turn-b", "status": "inProgress"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": {"id": "turn-b", "status": "completed"},
                },
            },
        ],
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    client.wait_until_idle(control(tmp_path))

    assert [message["method"] for message in protocol.sent].count("thread/read") == 3


def test_event_stream_emits_structured_collaboration_events_but_not_token_deltas(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(verified_responses(status="idle"))
    collab_event = {
        "method": "item/started",
        "params": {
            "threadId": str(CODEX_SESSION_ID),
            "turnId": "turn-1",
            "item": {
                "type": "collabAgentToolCall",
                "id": "collab-1",
                "tool": "spawnAgent",
            },
        },
    }
    events = FakeWebSocket(
        responses=[json.loads(EVENT_STREAM_READY_MESSAGE)],
        events=[
            {
                "method": "agentMessage/delta",
                "params": {"threadId": str(CODEX_SESSION_ID), "delta": "noise"},
            },
            collab_event,
        ],
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))
    observed: list[str] = []

    client.stream_events(control(tmp_path), observed.append)

    assert observed == [
        json.dumps(
            collab_event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    ]
    assert format_protocol_log_event({"method": "agentMessage/delta"}) is None
    assert (
        format_protocol_log_event({"method": "item/commandExecution/outputDelta"}) is None
    )
    assert format_protocol_log_event({"method": "item/completed", "params": {}}) == (
        '{"method":"item/completed","params":{}}'
    )


def test_exact_start_uses_a_string_request_id_and_returns_both_codex_identities(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="idle"),
            {"id": "rodex:request", "result": {"turn": {"id": "turn-new"}}},
        ]
    )
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:request",
        dispatch_id_factory=lambda: "rodex:dispatch:test",
    )

    dispatch = client.start_turn(control(tmp_path), "run tests")

    assert dispatch.turn_id == "turn-new"
    assert dispatch.thread_id == str(CODEX_SESSION_ID)
    assert dispatch.session_id == str(CODEX_SESSION_ID)
    assert dispatch.dispatch_id == "rodex:dispatch:test"
    assert protocol.sent[-1]["id"] == "rodex:request"
    assert protocol.sent[-1]["method"] == "turn/start"
    assert protocol.sent[-1]["params"]["clientUserMessageId"] == "rodex:dispatch:test"


def test_exact_start_refuses_to_steer_an_observed_active_turn(tmp_path: Path) -> None:
    protocol = FakeWebSocket(verified_responses(status="active"))
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-active"}},
            }
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    with pytest.raises(RodexControlError, match="use _steer"):
        client.start_turn(control(tmp_path), "new work")

    assert all(message.get("method") != "turn/start" for message in protocol.sent)


@pytest.mark.evolutionary_regression
def test_exact_start_uses_fresh_thread_state_when_ready_snapshot_is_stale(
    tmp_path: Path,
) -> None:
    """Current evidence: the event-ready snapshot may precede the authoritative read."""
    protocol = FakeWebSocket(
        [
            *verified_responses(status="idle"),
            {"id": "rodex:test", "result": {"turn": {"id": "turn-new"}}},
        ]
    )
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-old"}},
            }
        ]
    )
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:test",
    )

    dispatch = client.start_turn(control(tmp_path), "new work")

    assert dispatch.turn_id == "turn-new"
    assert protocol.sent[-1]["method"] == "turn/start"


def test_exact_steer_defers_the_turn_id_precondition_to_app_server(tmp_path: Path) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active"),
            {
                "id": "rodex:test",
                "error": {"code": -32602, "message": "expected turn id mismatch"},
            },
        ]
    )
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-active"}},
            }
        ]
    )
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:test",
    )

    with pytest.raises(RodexControlError, match="expected turn id mismatch"):
        client.steer_turn(control(tmp_path), "turn-other", "more work")

    assert protocol.sent[-1]["params"]["expectedTurnId"] == "turn-other"


@pytest.mark.evolutionary_regression
def test_exact_steer_succeeds_before_event_tap_observes_turn_started(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active"),
            {"id": "rodex:test", "result": {"turnId": "turn-target"}},
        ]
    )
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:test",
    )

    dispatch = client.steer_turn(control(tmp_path), "turn-target", "more work")

    assert dispatch.turn_id == "turn-target"
    assert protocol.sent[-1]["params"]["expectedTurnId"] == "turn-target"


@pytest.mark.evolutionary_regression
def test_exact_interrupt_succeeds_before_event_tap_observes_turn_started(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(
        [*verified_responses(status="active"), {"id": "rodex:test", "result": {}}]
    )
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:test",
    )

    state = client.interrupt_turn(control(tmp_path), "turn-target")

    assert state.active_turn_id == "turn-target"
    assert protocol.sent[-1]["params"] == {
        "threadId": str(CODEX_SESSION_ID),
        "turnId": "turn-target",
    }


def test_exact_result_reads_live_turn_content_without_persisting_it(tmp_path: Path) -> None:
    turn = {
        "id": "turn-1",
        "status": "completed",
        "startedAt": 10,
        "completedAt": 12,
        "durationMs": 2000,
        "error": None,
        "items": [
            {
                "id": "message-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": '{"ok":true}',
            },
            {
                "id": "change-1",
                "type": "fileChange",
                "status": "completed",
                "changes": [
                    {"path": "src/b.py", "kind": {"type": "update"}},
                    {"path": "src/a.py", "kind": {"type": "create"}},
                ],
            },
        ],
    }
    protocol = FakeWebSocket(verified_responses(status="idle", turns=[turn]))
    client = CodexControlClient(connector=RoutingConnector(protocol))

    state, result = client.result(control(tmp_path), "turn-1")

    assert state.thread_id == str(CODEX_SESSION_ID)
    assert result.status == "completed"
    assert result.final_agent_message == '{"ok":true}'
    assert result.structured_output == {"ok": True}
    assert result.changed_paths == ("src/a.py", "src/b.py")
    assert protocol.sent[-1]["params"]["includeTurns"] is True


@pytest.mark.evolutionary_regression
def test_dispatch_status_attributes_client_message_id_from_exact_thread_history(
    tmp_path: Path,
) -> None:
    """Current 0.147 evidence: clientUserMessageId is exposed as userMessage.clientId."""
    turn = {
        "id": "turn-target",
        "status": "inProgress",
        "items": [
            {
                "type": "userMessage",
                "id": "message-target",
                "clientId": "controller:dispatch:42",
                "content": [{"type": "text", "text": "run tests"}],
            }
        ],
    }
    protocol = FakeWebSocket(verified_responses(status="active", turns=[turn]))
    client = CodexControlClient(connector=RoutingConnector(protocol))

    state, status = client.dispatch_status(
        control(tmp_path),
        "controller:dispatch:42",
    )

    assert state.thread_id == str(CODEX_SESSION_ID)
    assert status.observation == "accepted"
    assert status.matches[0].turn_id == "turn-target"
    assert status.matches[0].turn_status == "inProgress"
    assert status.matches[0].user_message_item_id == "message-target"
    assert protocol.sent[-1]["params"]["includeTurns"] is True


def test_dispatch_status_keeps_absence_and_duplicate_observations_explicit(
    tmp_path: Path,
) -> None:
    turns = [
        {
            "id": f"turn-{index}",
            "status": "completed",
            "items": [
                {
                    "type": "userMessage",
                    "id": f"message-{index}",
                    "clientId": "duplicate-dispatch",
                    "content": [],
                }
            ],
        }
        for index in (1, 2)
    ]
    ambiguous_protocol = FakeWebSocket(verified_responses(status="idle", turns=turns))
    ambiguous_client = CodexControlClient(connector=RoutingConnector(ambiguous_protocol))

    _state, ambiguous = ambiguous_client.dispatch_status(
        control(tmp_path),
        "duplicate-dispatch",
    )

    assert ambiguous.observation == "ambiguous"
    assert [match.turn_id for match in ambiguous.matches] == ["turn-1", "turn-2"]

    absent_protocol = FakeWebSocket(verified_responses(status="idle", turns=turns))
    absent_client = CodexControlClient(connector=RoutingConnector(absent_protocol))

    _state, absent = absent_client.dispatch_status(control(tmp_path), "not-present")

    assert absent.observation == "not_observed"
    assert absent.matches == ()


@pytest.mark.evolutionary_regression
def test_result_reports_only_final_answer_and_completed_file_changes(
    tmp_path: Path,
) -> None:
    turn = {
        "id": "turn-1",
        "status": "completed",
        "items": [
            {
                "id": "commentary",
                "type": "agentMessage",
                "phase": "commentary",
                "text": "still working",
            },
            {
                "id": "failed-change",
                "type": "fileChange",
                "status": "failed",
                "changes": [{"path": "not-changed.py"}],
            },
            {
                "id": "completed-change",
                "type": "fileChange",
                "status": "completed",
                "changes": [{"path": "changed.py"}],
            },
        ],
    }
    protocol = FakeWebSocket(verified_responses(status="idle", turns=[turn]))
    client = CodexControlClient(connector=RoutingConnector(protocol))

    _state, result = client.result(control(tmp_path), "turn-1")

    assert result.final_agent_message is None
    assert result.changed_paths == ("changed.py",)


@pytest.mark.evolutionary_regression
def test_result_uses_phase_null_message_when_current_schema_has_no_explicit_final(
    tmp_path: Path,
) -> None:
    turn = {
        "id": "turn-1",
        "status": "completed",
        "items": [
            {
                "id": "phase-unknown",
                "type": "agentMessage",
                "phase": None,
                "text": "schema-valid final",
            }
        ],
    }
    protocol = FakeWebSocket(verified_responses(status="idle", turns=[turn]))
    client = CodexControlClient(connector=RoutingConnector(protocol))

    _state, result = client.result(control(tmp_path), "turn-1")

    assert result.final_agent_message == "schema-valid final"


@pytest.mark.evolutionary_regression
def test_result_bounds_final_text_and_rejects_nonstandard_structured_json(
    tmp_path: Path,
) -> None:
    oversized = "x" * (64 * 1024 + 1)
    turns = [
        {
            "id": "turn-large",
            "status": "completed",
            "items": [
                {
                    "id": "large",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": oversized,
                }
            ],
        },
        {
            "id": "turn-nan",
            "status": "completed",
            "items": [
                {
                    "id": "nan",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"value":NaN}',
                }
            ],
        },
    ]
    protocol = FakeWebSocket(
        [
            *verified_responses(status="idle", turns=turns),
            *verified_responses(status="idle", turns=turns),
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol))

    _state, large = client.result(control(tmp_path), "turn-large")
    _state, nonstandard = client.result(control(tmp_path), "turn-nan")

    assert large.final_agent_message_bytes == len(oversized)
    assert large.final_agent_message_truncated is True
    assert len(large.final_agent_message or "") == 64 * 1024
    assert large.structured_output is None
    assert nonstandard.final_agent_message_truncated is False
    assert nonstandard.structured_output is None


def test_exact_wait_ignores_another_turn_and_returns_its_target(tmp_path: Path) -> None:
    in_progress = {"id": "turn-target", "status": "inProgress", "items": []}
    completed = {
        "id": "turn-target",
        "status": "completed",
        "items": [
            {
                "id": "message-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": "finished",
            }
        ],
    }
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active", turns=[in_progress]),
            *verified_responses(status="idle", turns=[completed]),
        ]
    )
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-target"}},
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": {"id": "turn-other", "status": "completed", "items": []},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": completed,
                },
            },
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    state, result = client.wait_for_turn(control(tmp_path), "turn-target")

    assert state.status == "idle"
    assert result.status == "completed"
    assert result.final_agent_message == "finished"


def test_exact_wait_timeout_never_sends_an_interrupt(tmp_path: Path) -> None:
    in_progress = {"id": "turn-target", "status": "inProgress", "items": []}
    protocol = FakeWebSocket(verified_responses(status="active", turns=[in_progress]))
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_SESSION_ID): "turn-target"}},
            }
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    with pytest.raises(RodexWaitTimeoutError, match="timed out"):
        client.wait_for_turn(control(tmp_path), "turn-target", timeout_seconds=0.01)

    assert all(message.get("method") != "turn/interrupt" for message in protocol.sent)


@pytest.mark.evolutionary_regression
def test_exact_wait_timeout_includes_event_and_protocol_setup(tmp_path: Path) -> None:
    clock = [0.0]

    class SlowReadyWebSocket(FakeWebSocket):
        def recv(self, timeout: float | None) -> str:
            clock[0] = 2.0
            return super().recv(timeout)

    protocol = FakeWebSocket([])
    events = SlowReadyWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        monotonic=lambda: clock[0],
    )

    with pytest.raises(RodexWaitTimeoutError, match="timed out"):
        client.wait_for_turn(control(tmp_path), "turn-target", timeout_seconds=1)

    assert protocol.sent == []


@pytest.mark.evolutionary_regression
def test_exact_wait_ready_recv_timeout_is_classified_as_wait_timeout(
    tmp_path: Path,
) -> None:
    clock = [0.0]

    class TimedOutReadyWebSocket(FakeWebSocket):
        def recv(self, timeout: float | None) -> str:
            assert timeout == 1
            clock[0] = 1.0
            raise TimeoutError

    protocol = FakeWebSocket([])
    events = TimedOutReadyWebSocket()
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        monotonic=lambda: clock[0],
    )

    with pytest.raises(RodexWaitTimeoutError, match="timed out"):
        client.wait_for_turn(control(tmp_path), "turn-target", timeout_seconds=1)

    assert protocol.sent == []


@pytest.mark.evolutionary_regression
def test_exact_wait_accepts_in_progress_thread_before_ready_snapshot_catches_up(
    tmp_path: Path,
) -> None:
    in_progress = {"id": "turn-target", "status": "inProgress", "items": []}
    completed = {"id": "turn-target", "status": "completed", "items": []}
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active", turns=[in_progress]),
            *verified_responses(status="idle", turns=[completed]),
        ]
    )
    events = FakeWebSocket(
        responses=[
            json.loads(EVENT_STREAM_READY_MESSAGE),
            {
                "method": "turn/started",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": in_progress,
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_SESSION_ID),
                    "turn": completed,
                },
            },
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    state, result = client.wait_for_turn(control(tmp_path), "turn-target")

    assert state.status == "idle"
    assert result.status == "completed"


@pytest.mark.evolutionary_regression
def test_lost_mutation_response_preserves_dispatch_status_hook(tmp_path: Path) -> None:
    """Current evidence: lost responses keep caller-owned correlation for lookup."""
    protocol = FakeWebSocket(verified_responses(status="idle"))
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:request",
        dispatch_id_factory=lambda: "controller:dispatch:42",
    )

    with pytest.raises(
        RodexDispatchIndeterminateError, match="acceptance is unknown"
    ) as raised:
        client.start_turn(control(tmp_path), "run tests")

    assert raised.value.dispatch_id == "controller:dispatch:42"
    assert raised.value.thread_id == str(CODEX_SESSION_ID)
    assert raised.value.method == "turn/start"
    assert protocol.sent[-1]["method"] == "turn/start"


@pytest.mark.evolutionary_regression
def test_mutation_response_has_a_total_deadline_despite_unrelated_frames(
    tmp_path: Path,
) -> None:
    """Current evidence: legal unrelated frames cannot suppress recovery forever."""
    protocol = FakeWebSocket(
        [
            *verified_responses(status="idle"),
            {"method": "unrelated/notification", "params": {}},
        ]
    )
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    clock_values = iter((0.0, 1.0, 6.0))
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:request",
        dispatch_id_factory=lambda: "controller:dispatch:deadline",
        monotonic=lambda: next(clock_values),
    )

    with pytest.raises(
        RodexDispatchIndeterminateError, match="acceptance is unknown"
    ) as raised:
        client.start_turn(control(tmp_path), "run tests")

    assert raised.value.dispatch_id == "controller:dispatch:deadline"


@pytest.mark.evolutionary_regression
def test_failed_mutation_send_is_explicitly_indeterminate(tmp_path: Path) -> None:
    class FailingMutationWebSocket(FakeWebSocket):
        def send(self, message: str) -> None:
            if json.loads(message).get("method") == "turn/start":
                raise OSError("connection lost during send")
            super().send(message)

    protocol = FailingMutationWebSocket(verified_responses(status="idle"))
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(
        connector=RoutingConnector(protocol, events),
        request_id_factory=lambda: "rodex:request",
    )

    with pytest.raises(RodexDispatchIndeterminateError, match="before acceptance"):
        client.start_turn(control(tmp_path), "run tests")
