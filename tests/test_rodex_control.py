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
    format_protocol_log_event,
)
from rodex.protocol_proxy import EVENT_STREAM_READY_MESSAGE

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


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

    def recv(self, timeout: float) -> str:
        assert timeout > 0
        return next(self.responses)


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
        CODEX_UUID,
    )


def verified_responses(
    *, status: str, turns: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    return [
        {"id": 0, "result": {}},
        {"id": 1, "result": {"data": [str(CODEX_UUID)]}},
        {
            "id": 2,
            "result": {
                "thread": {
                    "id": str(CODEX_UUID),
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


def test_send_starts_an_idle_turn_after_uuid_verification(tmp_path: Path) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="idle"),
            {"id": 3, "result": {"turn": {"id": "turn-new"}}},
        ]
    )
    events = FakeWebSocket(responses=[json.loads(EVENT_STREAM_READY_MESSAGE)])
    client = CodexControlClient(connector=RoutingConnector(protocol, events))
    revalidated: list[bool] = []

    dispatched = client.send_prompt(
        control(tmp_path), "run tests", revalidate=lambda: revalidated.append(True)
    )

    assert dispatched.action == "started"
    assert dispatched.turn_id == "turn-new"
    assert protocol.sent[-1] == {
        "method": "turn/start",
        "id": 3,
        "params": {
            "threadId": str(CODEX_UUID),
            "input": [{"type": "text", "text": "run tests"}],
        },
    }
    assert revalidated == [True]


def test_send_steers_the_exact_active_turn(tmp_path: Path) -> None:
    protocol = FakeWebSocket(
        [
            *verified_responses(status="active"),
            {"id": 3, "result": {"turnId": "turn-active"}},
        ]
    )
    events = FakeWebSocket(
        responses=[
            {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {str(CODEX_UUID): "turn-active"}},
            }
        ]
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    dispatched = client.send_prompt(control(tmp_path), "also lint")

    assert dispatched.action == "steered"
    assert protocol.sent[-1]["method"] == "turn/steer"
    assert protocol.sent[-1]["params"]["expectedTurnId"] == "turn-active"


def test_thread_inspection_does_not_require_rollout_history(tmp_path: Path) -> None:
    protocol = FakeWebSocket(verified_responses(status="idle"))
    client = CodexControlClient(connector=RoutingConnector(protocol))

    state = client.inspect(control(tmp_path))

    assert state.status == "idle"
    assert protocol.sent[-1]["params"]["includeTurns"] is False


def test_control_rejects_an_endpoint_without_the_expected_codex_uuid(
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
                    "threadId": str(CODEX_UUID),
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
                    "threadId": str(CODEX_UUID),
                    "turn": {"id": "turn-a", "status": "completed"},
                },
            },
            {
                "method": "turn/started",
                "params": {
                    "threadId": str(CODEX_UUID),
                    "turn": {"id": "turn-b", "status": "inProgress"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": str(CODEX_UUID),
                    "turn": {"id": "turn-b", "status": "completed"},
                },
            },
        ],
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))

    client.wait_until_idle(control(tmp_path))

    assert [message["method"] for message in protocol.sent].count("thread/read") == 3


def test_tail_emits_structured_collaboration_events_but_not_token_deltas(
    tmp_path: Path,
) -> None:
    protocol = FakeWebSocket(verified_responses(status="idle"))
    collab_event = {
        "method": "item/started",
        "params": {
            "threadId": str(CODEX_UUID),
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
                "params": {"threadId": str(CODEX_UUID), "delta": "noise"},
            },
            collab_event,
        ],
    )
    client = CodexControlClient(connector=RoutingConnector(protocol, events))
    observed: list[str] = []

    client.tail(control(tmp_path), observed.append)

    assert observed == [
        json.dumps(
            collab_event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    ]
    assert format_protocol_log_event({"method": "agentMessage/delta"}) is None
