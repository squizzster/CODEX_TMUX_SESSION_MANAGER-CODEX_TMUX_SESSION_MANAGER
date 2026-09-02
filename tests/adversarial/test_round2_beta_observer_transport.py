from __future__ import annotations

import inspect
import queue
import time
import tracemalloc
import uuid
from pathlib import Path
from threading import Event

import pytest

import rodex.agent_observer as observer_module
from rodex.observer_contract import (
    OBSERVER_PROJECTED_FIELD_MAX_CHARS,
    OBSERVER_PROJECTED_LIST_ITEM_LIMIT,
    OBSERVER_PROJECTED_TEXT_MAX_CHARS,
    OBSERVER_SNAPSHOT_EVENT_LIMIT,
)
from rodex.observer_state import ObserverStateReducer

MAX_PENDING_OBSERVER_EVENTS = 64
MAX_SEND_ATTEMPTS_PER_SNAPSHOT = 8
MAX_OBSERVER_FRAME_BYTES = 256 * 1024


def _snapshot(revision: int) -> dict[str, object]:
    return {
        "schema": observer_module.OBSERVER_SCHEMA,
        "kind": "observer_state_snapshot",
        "revision": revision,
        "state": {"active_agent_ids": [f"agent-{revision}"]},
    }


def _semantic_event(revision: int) -> dict[str, object]:
    return {
        "schema": observer_module.OBSERVER_SCHEMA,
        "kind": "app_server_agent_message",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item": {
            "type": "agentMessage",
            "id": f"message-{revision}",
            "phase": "commentary",
            "text": f"revision {revision}",
        },
    }


def test_round2_observer_dispatch_queue_is_bounded_while_disconnected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = Event()

    def unavailable(_path: Path, _event: dict[str, object]) -> None:
        attempted.set()
        raise OSError("observer unavailable")

    monkeypatch.setattr(observer_module, "_send_observer_event_frame", unavailable)
    dispatcher = observer_module._ObserverEventDispatcher()
    try:
        dispatcher.send(tmp_path / "observer.sock", _snapshot(0))
        assert attempted.wait(1), "dispatcher worker did not make its first send attempt"
        for revision in range(1, 513):
            dispatcher.send(tmp_path / "observer.sock", _snapshot(revision))

        assert dispatcher._events.qsize() <= MAX_PENDING_OBSERVER_EVENTS, (
            "a disconnected observer must have a fixed memory bound; "
            f"queued {dispatcher._events.qsize()} events"
        )
    finally:
        dispatcher.close()


def test_round2_observer_reconnect_delivers_only_the_latest_coalesced_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = Event()
    first_attempt = Event()
    newest_delivered = Event()
    delivered: list[dict[str, object]] = []
    newest_revision = 200

    def conditional_send(_path: Path, event: dict[str, object]) -> None:
        first_attempt.set()
        if not connected.is_set():
            raise OSError("observer unavailable")
        delivered.append(event)
        if event.get("revision") == newest_revision:
            newest_delivered.set()

    monkeypatch.setattr(
        observer_module,
        "_send_observer_event_frame",
        conditional_send,
    )
    dispatcher = observer_module._ObserverEventDispatcher()
    try:
        socket_path = tmp_path / "observer.sock"
        dispatcher.send(socket_path, _snapshot(0))
        assert first_attempt.wait(1), "dispatcher worker did not enter retry state"
        for revision in range(1, newest_revision + 1):
            dispatcher.send(socket_path, _snapshot(revision))
        connected.set()
        assert newest_delivered.wait(2), "latest observer snapshot was not delivered"
    finally:
        dispatcher.close()

    assert delivered == [_snapshot(newest_revision)], (
        "reconnect must publish one current snapshot, not replay stale deltas; "
        f"delivered {len(delivered)} frames"
    )


def test_round2_observer_snapshot_makes_capacity_overflow_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = Event()
    connected = Event()
    delivered = Event()
    snapshots: list[dict[str, object]] = []

    def conditional_send(_path: Path, event: dict[str, object]) -> None:
        attempted.set()
        if not connected.is_set():
            raise OSError("observer unavailable")
        snapshots.append(event)
        delivered.set()

    monkeypatch.setattr(observer_module, "_send_observer_event_frame", conditional_send)
    dispatcher = observer_module._ObserverEventDispatcher()
    reducer = ObserverStateReducer.producer()
    try:
        socket_path = tmp_path / "observer.sock"
        dispatcher.send(socket_path, reducer.observe(_semantic_event(0)))
        assert attempted.wait(1)
        for revision in range(1, 100):
            dispatcher.send(socket_path, reducer.observe(_semantic_event(revision)))
        connected.set()
        assert delivered.wait(2)
    finally:
        dispatcher.close()

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    state = snapshot["state"]
    overflow = snapshot["overflow"]
    assert isinstance(state, dict)
    assert isinstance(overflow, dict)
    events = state["events"]
    assert isinstance(events, list)
    assert len(events) == OBSERVER_SNAPSHOT_EVENT_LIMIT
    assert overflow == {"dropped_event_count": 36, "state_complete": False}
    assert events[-1]["item"]["id"] == "message-99"  # type: ignore[index]


def test_round2_observer_retains_latest_after_initial_retry_budget_without_new_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    initial_budget_exhausted = Event()
    ready = Event()
    delivered = Event()

    def unavailable_until_ready(
        _path: Path,
        event: dict[str, object],
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= MAX_SEND_ATTEMPTS_PER_SNAPSHOT:
            initial_budget_exhausted.set()
        if not ready.is_set():
            raise OSError("observer unavailable")
        assert event == _snapshot(1)
        delivered.set()

    monkeypatch.setattr(
        observer_module,
        "_send_observer_event_frame",
        unavailable_until_ready,
    )
    dispatcher = observer_module._ObserverEventDispatcher(parked_retry_seconds=0.01)
    try:
        dispatcher.send(tmp_path / "observer.sock", _snapshot(1))
        assert initial_budget_exhausted.wait(2)
        ready.set()
        assert delivered.wait(1), (
            "the retained current snapshot must be delivered when the receiver becomes "
            "ready even if no later event wakes the dispatcher"
        )
    finally:
        dispatcher.close()

    assert attempts > MAX_SEND_ATTEMPTS_PER_SNAPSHOT


def test_round2_observer_parked_retry_has_a_low_capped_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    initial_budget_exhausted = Event()

    def unavailable(_path: Path, _event: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= MAX_SEND_ATTEMPTS_PER_SNAPSHOT:
            initial_budget_exhausted.set()
        raise OSError("observer unavailable")

    monkeypatch.setattr(observer_module, "_send_observer_event_frame", unavailable)
    parked_interval = 0.05
    dispatcher = observer_module._ObserverEventDispatcher(
        parked_retry_seconds=parked_interval
    )
    try:
        dispatcher.send(tmp_path / "observer.sock", _snapshot(1))
        assert initial_budget_exhausted.wait(2)
        attempts_after_initial_budget = attempts
        time.sleep(parked_interval * 3.5)
    finally:
        dispatcher.close()

    parked_attempts = attempts - attempts_after_initial_budget
    assert 2 <= parked_attempts <= 4, (
        "a parked retained snapshot should retry once per low-cadence interval, "
        f"observed {parked_attempts} attempts"
    )


def test_round2_dispatch_snapshot_keeps_same_item_id_from_two_child_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = Event()
    attempted = Event()
    delivered = Event()
    snapshots: list[dict[str, object]] = []

    def conditional_send(_path: Path, event: dict[str, object]) -> None:
        attempted.set()
        if not ready.is_set():
            raise OSError("observer unavailable")
        snapshots.append(event)
        delivered.set()

    monkeypatch.setattr(observer_module, "_send_observer_event_frame", conditional_send)
    dispatcher = observer_module._ObserverEventDispatcher(parked_retry_seconds=0.01)
    reducer = ObserverStateReducer.producer()
    try:
        socket_path = tmp_path / "observer.sock"
        first = _semantic_event(1)
        second = _semantic_event(2)
        first["thread_id"] = "child-a"
        second["thread_id"] = "child-b"
        first["item"]["id"] = "shared-message-id"  # type: ignore[index]
        second["item"]["id"] = "shared-message-id"  # type: ignore[index]
        dispatcher.send(socket_path, reducer.observe(first))
        assert attempted.wait(1)
        dispatcher.send(socket_path, reducer.observe(second))
        ready.set()
        assert delivered.wait(1)
    finally:
        dispatcher.close()

    assert len(snapshots) == 1
    state = snapshots[0]["state"]
    assert isinstance(state, dict)
    events = state["events"]
    assert isinstance(events, list)
    assert {event["thread_id"] for event in events} == {"child-a", "child-b"}


def test_round2_projection_bounds_multi_megabyte_fields_before_json_encoding() -> None:
    huge_text = "\x01" * (4 * 1024 * 1024)
    raw_event = {
        "method": "item/completed",
        "params": {
            "threadId": "01a00654-f2bc-7a30-834a-a5f886a65f83",
            "turnId": "turn-1",
            "item": {
                "type": "agentMessage",
                "id": "message-1",
                "phase": "final_answer",
                "text": huge_text,
            },
        },
    }

    tracemalloc.start()
    try:
        projected = observer_module.project_agent_message_event(raw_event)
        assert projected is not None
        frame = observer_module._observer_event_frame(projected)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    item = projected["item"]
    assert isinstance(item, dict)
    assert len(item["text"]) <= OBSERVER_PROJECTED_TEXT_MAX_CHARS
    assert projected["projection_overflow"] == {
        "truncated_fields": ["item.text"],
        "omitted_list_items": {},
    }
    assert len(frame) - observer_module._OBSERVER_FRAME_LENGTH.size <= (
        observer_module.OBSERVER_MAX_FRAME_BYTES
    )
    assert peak < 1024 * 1024, (
        "projection and encoding allocated in proportion to the multi-megabyte input: "
        f"peak={peak}"
    )


def test_round2_all_projected_free_text_and_lists_have_explicit_bounds() -> None:
    huge_text = "x" * (2 * 1024 * 1024)
    root_thread_id = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    child_thread_id = "01a00654-f2bc-7a30-834a-a5f886a65f83"
    activity = observer_module.project_subagent_activity_event(
        {
            "method": "item/started",
            "params": {
                "threadId": root_thread_id,
                "turnId": "turn-1",
                "item": {
                    "type": "subAgentActivity",
                    "id": "activity-1",
                    "agentThreadId": child_thread_id,
                    "agentPath": huge_text,
                    "kind": "started",
                },
            },
        }
    )
    receivers = [str(uuid.UUID(int=index + 1)) for index in range(100)]
    invocation = observer_module.project_collaboration_invocation_event(
        {
            "method": "item/started",
            "params": {
                "threadId": root_thread_id,
                "turnId": "turn-1",
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "activity-1",
                    "tool": "spawnAgent",
                    "status": "completed",
                    "prompt": huge_text,
                    "senderThreadId": root_thread_id,
                    "receiverThreadIds": receivers,
                },
            },
        }
    )
    user_message = observer_module.project_user_message_event(
        {
            "method": "item/completed",
            "params": {
                "threadId": root_thread_id,
                "turnId": "turn-1",
                "item": {
                    "type": "userMessage",
                    "id": "user-1",
                    "content": [
                        {"type": "text", "text": huge_text} for _index in range(100)
                    ],
                },
            },
        }
    )

    assert activity is not None
    assert invocation is not None
    assert user_message is not None
    assert len(activity["item"]["agent_path"]) == (  # type: ignore[index]
        OBSERVER_PROJECTED_FIELD_MAX_CHARS
    )
    assert len(invocation["item"]["prompt"]) == (  # type: ignore[index]
        OBSERVER_PROJECTED_TEXT_MAX_CHARS
    )
    assert len(invocation["item"]["receiver_thread_ids"]) == (  # type: ignore[index]
        OBSERVER_PROJECTED_LIST_ITEM_LIMIT
    )
    assert sum(len(block) for block in user_message["item"]["text_blocks"]) <= (  # type: ignore[index]
        OBSERVER_PROJECTED_TEXT_MAX_CHARS
    )
    for projected in (activity, invocation, user_message):
        assert "projection_overflow" in projected
        assert len(observer_module._observer_event_frame(projected)) <= (
            observer_module.OBSERVER_MAX_FRAME_BYTES
            + observer_module._OBSERVER_FRAME_LENGTH.size
        )


def test_round2_closed_observer_dispatcher_reclaims_worker_and_rejects_late_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = Event()
    attempts = 0

    def unavailable(_path: Path, _event: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        attempted.set()
        raise OSError("observer unavailable")

    monkeypatch.setattr(observer_module, "_send_observer_event_frame", unavailable)
    dispatcher = observer_module._ObserverEventDispatcher()
    dispatcher.send(tmp_path / "observer.sock", _snapshot(1))
    assert attempted.wait(1)
    worker = dispatcher._thread
    assert worker is not None

    dispatcher.close()
    attempts_after_close = attempts
    dispatcher.send(tmp_path / "observer.sock", _snapshot(2))

    assert not worker.is_alive()
    assert dispatcher._thread is worker
    assert attempts == attempts_after_close


class _MemoryConnection:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.bytes_read = 0

    def __enter__(self) -> _MemoryConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def recv(self, size: int) -> bytes:
        chunk = self._payload[:size]
        self._payload = self._payload[len(chunk) :]
        self.bytes_read += len(chunk)
        return chunk


class _OneConnectionListener:
    def __init__(self, connection: _MemoryConnection) -> None:
        self._connection = connection
        self._accepted = False

    def accept(self) -> tuple[_MemoryConnection, object]:
        if self._accepted:
            raise OSError("listener exhausted")
        self._accepted = True
        return self._connection, object()


def test_round2_observer_receiver_rejects_oversize_frame_before_reading_payload() -> None:
    event = {
        "schema": observer_module.OBSERVER_SCHEMA,
        "kind": "probe",
        "payload": "x" * MAX_OBSERVER_FRAME_BYTES,
    }
    frame = observer_module._observer_event_frame(event)
    connection = _MemoryConnection(frame)
    listener = _OneConnectionListener(connection)
    events: queue.Queue[dict[str, object]] = queue.Queue()

    observer_module._observer_control_receiver(listener, events, Event())  # type: ignore[arg-type]

    assert events.empty(), "oversize observer frame reached the application queue"
    assert connection.bytes_read == observer_module._OBSERVER_FRAME_LENGTH.size, (
        "oversize frames must be rejected from their header without allocating or "
        f"reading the {len(frame)}-byte payload"
    )


def test_round2_observer_receiver_accepts_exact_frame_limit_before_json_validation() -> (
    None
):
    payload = b"x" * observer_module.OBSERVER_MAX_FRAME_BYTES
    frame = observer_module._OBSERVER_FRAME_LENGTH.pack(len(payload)) + payload
    connection = _MemoryConnection(frame)
    listener = _OneConnectionListener(connection)
    events: queue.Queue[dict[str, object]] = queue.Queue()

    observer_module._observer_control_receiver(listener, events, Event())  # type: ignore[arg-type]

    assert events.empty()
    assert connection.bytes_read == len(frame)


def test_round2_receive_exactly_requires_an_absolute_deadline_contract() -> None:
    parameters = inspect.signature(observer_module._receive_exactly).parameters

    assert "deadline" in parameters and "monotonic" in parameters, (
        "receive_exactly needs an absolute deadline and an injectable monotonic clock "
        "so a peer cannot keep a frame alive with an endless byte trickle"
    )

    class DripConnection:
        def __init__(self) -> None:
            self.recv_calls = 0
            self.timeouts: list[float] = []

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def recv(self, _size: int) -> bytes:
            self.recv_calls += 1
            return b"x"

    timestamps = iter((0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0))

    def monotonic() -> float:
        return next(timestamps)

    connection = DripConnection()

    with pytest.raises(TimeoutError):
        observer_module._receive_exactly(  # type: ignore[call-arg]
            connection, 100, deadline=2.0, monotonic=monotonic
        )
    assert connection.recv_calls <= 5
    assert connection.timeouts
