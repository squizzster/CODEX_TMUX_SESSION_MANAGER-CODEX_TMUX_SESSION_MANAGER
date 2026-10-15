from __future__ import annotations

import json
from contextlib import contextmanager, suppress
from threading import Event, Thread

import pytest
from runtime_peer_fixtures import TEST_PEER, LiveTestProcess
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRejectionReason,
    InteractionRequest,
    InteractionResult,
)
from rodex.interaction_transport import publish_session_interaction
from rodex.primary_thread_binding import MAX_PENDING_THREAD_SELECTIONS, PrimaryThreadBinding
from rodex.protocol_proxy import CONTROL_CONNECTION_PATH, CodexProtocolProxy, ToolCallCounter


@contextmanager
def proxy_with_upstream(tmp_path, upstream, *, observer=None):
    server = unix_serve(upstream, path=str(tmp_path / "app.sock"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    delivered = []

    def deliver(request):
        delivered.append(request)
        return InteractionResult(DeliveryStatus.COMPLETED)

    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        ToolCallCounter(lambda _: None),
        on_primary_server_message=observer,
        peer_identity=TEST_PEER,
        app_server_process=LiveTestProcess(),
        native_tui_process=LiveTestProcess(),
        model_message_sender=deliver,
    )
    try:
        with proxy:
            yield proxy, delivered
    finally:
        server.shutdown(close_connections=True)
        thread.join(timeout=3)
        assert not thread.is_alive()


def steer(proxy, thread_id="resumed-root"):
    return proxy.interactions.execute(
        InteractionRequest(
            "main",
            InteractionOperation.STEER,
            "test",
            text="reminder",
            expected_thread_id=thread_id,
            expected_turn_id="active-turn",
        )
    )


def test_resumed_primary_binds_from_response_without_thread_started(tmp_path):
    def upstream(connection):
        with suppress(ConnectionClosed):
            for message in connection:
                request = json.loads(message)
                connection.send(json.dumps({"id": request["id"], "result": {"thread": {"id": "resumed-root"}}}))

    with (
        proxy_with_upstream(tmp_path, upstream) as (proxy, delivered),
        unix_connect(str(tmp_path / "proxy.sock"), additional_headers=TEST_PEER.headers()) as client,
    ):
        client.send(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": "resumed-root"}}))
        assert json.loads(client.recv(timeout=2))["result"]["thread"]["id"] == "resumed-root"
        outcome = steer(proxy)
        assert outcome.status is DeliveryStatus.COMPLETED, outcome.detail
        assert len(delivered) == 1
        proxy.validate_primary_model_binding(delivered[0])


@pytest.mark.parametrize("method", ["thread/start", "thread/resume", "thread/fork"])
@pytest.mark.parametrize("request_id", [1, "1"])
def test_correlated_lifecycle_response_selects_root(method, request_id):
    binding = PrimaryThreadBinding()
    source = "selected" if method == "thread/resume" else "fork-source"
    binding.observe_request({"id": request_id, "method": method, "params": {"threadId": source}})
    binding.observe_response({"method": "thread/started", "params": {"thread": {"id": "child"}}})
    assert binding.thread_id is None
    binding.observe_response({"id": request_id, "result": {"thread": {"id": "selected"}}})
    assert binding.thread_id == "selected"


@pytest.mark.parametrize("wrong_id", [True, False, 1.0, "1", None, {}, []])
def test_response_identity_is_type_exact(wrong_id):
    binding = PrimaryThreadBinding()
    binding.observe_request({"id": 1, "method": "thread/start", "params": {}})
    binding.observe_response({"id": wrong_id, "result": {"thread": {"id": "wrong"}}})
    assert binding.thread_id is None
    binding.observe_response({"id": 1, "result": {"thread": {"id": "root"}}})
    assert binding.thread_id == "root"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": None},
        {"result": []},
        {"result": {"thread": {"id": ""}}},
        {"result": {"thread": {"id": True}}},
        {"result": {"thread": {"id": "other-root"}}},
        {"error": None},
        {"error": "error"},
        {"error": {"code": True, "message": "invalid"}},
        {"error": {"code": 1, "message": "invalid"}, "result": {"thread": {"id": "root"}}},
    ],
)
def test_malformed_or_conflicting_acknowledgement_cannot_restore_old_authority(response):
    binding = PrimaryThreadBinding()
    binding.observe_request({"id": 1, "method": "thread/start", "params": {}})
    binding.observe_response({"id": 1, "result": {"thread": {"id": "root"}}})
    binding.observe_request({"id": 2, "method": "thread/resume", "params": {"threadId": "root"}})
    assert binding.thread_id is None
    binding.observe_response({"id": 2, **response})
    assert binding.thread_id is None
    binding.observe_response({"id": 2, "result": {"thread": {"id": "root"}}})
    assert binding.thread_id is None


def test_definitive_lifecycle_rejection_preserves_previous_selection():
    binding = PrimaryThreadBinding()
    binding.observe_request({"id": 1, "method": "thread/start", "params": {}})
    binding.observe_response({"id": 1, "result": {"thread": {"id": "root"}}})
    binding.observe_request({"id": 2, "method": "thread/resume", "params": {"threadId": "other"}})
    assert binding.thread_id is None
    binding.observe_response({"id": 2, "error": {"code": -1, "message": "rejected"}})
    assert binding.thread_id == "root"


def test_successful_selection_of_different_root_invalidates_instead_of_rebinding():
    binding = PrimaryThreadBinding()
    for request_id, root in [(1, "root"), (2, "replacement")]:
        binding.observe_request({"id": request_id, "method": "thread/start", "params": {}})
        binding.observe_response({"id": request_id, "result": {"thread": {"id": root}}})
    assert binding.thread_id is None


def test_duplicate_inflight_id_and_overflow_fail_closed():
    for count in [2, MAX_PENDING_THREAD_SELECTIONS + 1]:
        binding = PrimaryThreadBinding()
        for index in range(count):
            binding.observe_request({"id": 1 if count == 2 else index, "method": "thread/start", "params": {}})
        for index in range(count):
            binding.observe_response({"id": index, "result": {"thread": {"id": "root"}}})
        assert binding.thread_id is None
        assert not binding._pending


@pytest.mark.parametrize("method", ["thread/read", "turn/start", "unrecognized"])
def test_non_selection_requests_cannot_bind(method):
    binding = PrimaryThreadBinding()
    binding.observe_request({"id": 1, "method": method, "params": {}})
    binding.observe_response({"id": 1, "result": {"thread": {"id": "root"}}})
    assert binding.thread_id is None


def test_disconnect_invalidates_inflight_reply_and_old_connection_cannot_rebind(tmp_path):
    proxy = CodexProtocolProxy(
        tmp_path / "unused-proxy.sock",
        tmp_path / "unused-app.sock",
        ToolCallCounter(lambda _: None),
        peer_identity=TEST_PEER,
        app_server_process=LiveTestProcess(),
        native_tui_process=LiveTestProcess(),
        model_message_sender=lambda _: InteractionResult(DeliveryStatus.COMPLETED),
    )
    try:
        old, new = object(), object()
        assert proxy._claim_primary_connection(old)
        request = {"id": 1, "method": "thread/start", "params": {}}
        reply = {"id": 1, "result": {"thread": {"id": "root"}}}
        proxy._observe_primary_input(request, old)
        proxy._primary_thread_binding.invalidate_after_disconnect()
        proxy._observe_primary_response(reply, old)
        assert proxy._primary_model_thread_id() is None
        proxy._release_primary_connection(old)
        assert proxy._claim_primary_connection(new)
        proxy._observe_primary_input(request, new)
        proxy._observe_primary_response(reply, old)
        proxy._observe_primary_input({**request, "id": 2}, old)
        assert proxy._primary_model_thread_id() is None
        proxy._observe_primary_response(reply, new)
        assert proxy._primary_model_thread_id() == "root"
    finally:
        proxy._release_primary_connection(old)
        proxy._release_primary_connection(new)


def test_control_traffic_and_child_notification_cannot_select_primary(tmp_path):
    def upstream(connection):
        with suppress(ConnectionClosed):
            for message in connection:
                request = json.loads(message)
                connection.send(json.dumps({"method": "thread/started", "params": {"thread": {"id": "child"}}}))
                connection.send(
                    json.dumps({"id": request["id"], "result": {"thread": {"id": request["params"]["threadId"]}}})
                )

    with (
        proxy_with_upstream(tmp_path, upstream) as (proxy, delivered),
        unix_connect(str(tmp_path / "proxy.sock"), additional_headers=TEST_PEER.headers()) as primary,
    ):
        with unix_connect(
            str(tmp_path / "proxy.sock"),
            uri=f"ws://localhost{CONTROL_CONNECTION_PATH}",
            additional_headers=TEST_PEER.headers(),
        ) as control:
            control.send(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": "control-root"}}))
            control.recv(timeout=2)
            control.recv(timeout=2)
            assert steer(proxy).status is DeliveryStatus.REJECTED
        primary.send(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": "resumed-root"}}))
        primary.recv(timeout=2)
        primary.recv(timeout=2)
        assert steer(proxy).status is DeliveryStatus.COMPLETED
        assert len(delivered) == 1


def test_input_eof_revokes_binding_before_blocked_output_projection_releases(tmp_path):
    blocked, release, invalidated = Event(), Event(), Event()

    def observe(_message, event):
        if event.get("method") == "fixture/block":
            blocked.set()
            assert release.wait(5)

    def upstream(connection):
        with suppress(ConnectionClosed):
            request = json.loads(connection.recv(timeout=2))
            connection.send(json.dumps({"id": request["id"], "result": {"thread": {"id": "resumed-root"}}}))
            connection.send(json.dumps({"method": "fixture/block", "params": {}}))
            for _ in connection:
                pass

    with (
        proxy_with_upstream(tmp_path, upstream, observer=observe) as (proxy, _delivered),
        unix_connect(str(tmp_path / "proxy.sock"), additional_headers=TEST_PEER.headers()) as client,
    ):
        try:
            client.send(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": "resumed-root"}}))
            client.recv(timeout=2)
            client.recv(timeout=2)
            assert blocked.wait(2)
            assert proxy._primary_model_thread_id() == "resumed-root"
            original = proxy._primary_thread_binding.invalidate_after_disconnect

            def invalidate():
                original()
                invalidated.set()

            proxy._primary_thread_binding.invalidate_after_disconnect = invalidate
            client.close()
            assert invalidated.wait(2)
            assert proxy._primary_tui_connection is not None  # Release still blocked in projection.
            proxy._observe_primary_response(
                {"id": 1, "result": {"thread": {"id": "resumed-root"}}}, proxy._primary_tui_connection
            )
            assert proxy._primary_model_thread_id() is None
        finally:
            release.set()


def test_missing_binding_reason_survives_interaction_transport(tmp_path):
    with proxy_with_upstream(tmp_path, lambda _: None):
        result = publish_session_interaction(
            tmp_path / "proxy.sock",
            InteractionRequest("main", InteractionOperation.MESSAGE, "test", text="fixture", start_model_turn=True),
            peer_identity=TEST_PEER,
        )
        assert result.status is DeliveryStatus.REJECTED
        assert result.rejection_reason is InteractionRejectionReason.MODEL_THREAD_UNBOUND


@pytest.mark.parametrize(
    "lifecycle_request",
    [
        {"id": True, "params": {}},
        {"id": 1.0, "params": {}},
        {"id": None, "params": {}},
        {"id": 1, "params": []},
        {"id": 1, "params": {"threadId": ""}},
        {"id": 1, "params": {"threadId": True}},
    ],
)
def test_malformed_selection_cannot_authorize_following_response(lifecycle_request):
    binding = PrimaryThreadBinding()
    binding.observe_request({"method": "thread/resume", **lifecycle_request})
    binding.observe_response({"id": lifecycle_request["id"], "result": {"thread": {"id": "root"}}})
    assert binding.thread_id is None
