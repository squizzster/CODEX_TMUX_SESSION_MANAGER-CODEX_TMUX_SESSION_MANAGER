"""A subscription attests one verified primary lifetime, never a vacant slot."""

from __future__ import annotations

import json
from contextlib import contextmanager, suppress
from dataclasses import replace
from threading import Event, Thread

import pytest
from runtime_peer_fixtures import TEST_PEER, LiveTestProcess
from test_rodex_control import CODEX_SESSION_ID, FakeWebSocket, control, verified_responses
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

import rodex.protocol_proxy as proxy_module
from rodex.control import CodexControlClient, RodexEventStreamContinuityError
from rodex.protocol_proxy import (
    CONTROL_CONNECTION_PATH,
    EVENT_STREAM_READY_MESSAGE,
    CodexProtocolEventTap,
    CodexProtocolProxy,
    ToolCallCounter,
)


def connect(path, route="/events"):
    return unix_connect(
        str(path), uri=f"ws://localhost{route}", additional_headers=TEST_PEER.headers(), close_timeout=0.2
    )


@contextmanager
def running_proxy(
    tmp_path, *, on_disconnect=None, on_server_message=None, on_client_message=None, tool_counter=None, interactions=None
):
    def echo(connection):
        with suppress(ConnectionClosed):
            for message in connection:
                connection.send(message)

    upstream = unix_serve(echo, path=str(tmp_path / "app.sock"))
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    tap = CodexProtocolEventTap(tmp_path / "events.sock", peer_identity=TEST_PEER)
    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        tool_counter if tool_counter is not None else ToolCallCounter(lambda _count: None),
        on_primary_server_message=on_server_message,
        on_primary_disconnect=on_disconnect,
        on_primary_client_message=on_client_message,
        interaction_pipeline=interactions,
        peer_identity=TEST_PEER,
        app_server_process=LiveTestProcess(),
        native_tui_process=LiveTestProcess(),
        event_tap=tap,
    )
    try:
        tap.start()
        proxy.start()
        yield proxy, tap
    finally:
        proxy.close()
        tap.close()
        upstream.shutdown(close_connections=True)
        upstream_thread.join(2)
        assert not upstream_thread.is_alive()


def assert_source_unavailable(path):
    with connect(path) as subscriber, pytest.raises(ConnectionClosed):
        subscriber.recv(timeout=1)


@pytest.mark.parametrize("verification_fails", [False, True])
def test_subscription_requires_verified_primary_upstream(tmp_path, monkeypatch, verification_fails):
    verifying = Event()
    release_verification = Event()
    verify = proxy_module.require_unix_peer_process

    def paused_verification(*args):
        verifying.set()
        assert release_verification.wait(3)
        if verification_fails:
            raise OSError("injected upstream verification failure")
        verify(*args)

    monkeypatch.setattr(proxy_module, "require_unix_peer_process", paused_verification)
    with running_proxy(tmp_path) as (proxy, _tap):
        assert_source_unavailable(tmp_path / "events.sock")
        with connect(tmp_path / "proxy.sock", "/rpc") as tui:
            try:
                assert verifying.wait(1)
                assert proxy._primary_connection_claimed
                assert_source_unavailable(tmp_path / "events.sock")
            finally:
                release_verification.set()
            if verification_fails:
                with pytest.raises(ConnectionClosed):
                    tui.recv(timeout=1)
            else:
                tui.send("verified")
                assert tui.recv(timeout=1) == "verified"
                with connect(tmp_path / "events.sock") as subscriber:
                    assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
        proxy.wait_for_primary_connection_release(1)
        assert_source_unavailable(tmp_path / "events.sock")


def test_control_connection_cannot_activate_primary_event_source(tmp_path):
    with running_proxy(tmp_path), connect(tmp_path / "proxy.sock", CONTROL_CONNECTION_PATH) as machine:
        machine.send("read")
        assert machine.recv(timeout=1) == "read"
        assert_source_unavailable(tmp_path / "events.sock")


def test_disconnect_invalidates_before_blocked_auxiliary_reset(tmp_path):
    resetting = Event()
    release_reset = Event()

    def blocked_reset():
        resetting.set()
        assert release_reset.wait(3)

    with running_proxy(tmp_path, on_disconnect=blocked_reset) as (proxy, _tap):
        try:
            with connect(tmp_path / "proxy.sock", "/rpc") as tui:
                tui.send("ready")
                assert tui.recv(timeout=1) == "ready"
                with connect(tmp_path / "events.sock") as subscriber:
                    assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
                    tui.close()
                    assert resetting.wait(1)
                    with pytest.raises(ConnectionClosed):
                        subscriber.recv(timeout=1)
                    assert_source_unavailable(tmp_path / "events.sock")
        finally:
            release_reset.set()
        proxy.wait_for_primary_connection_release(1)
        with connect(tmp_path / "proxy.sock", "/rpc") as replacement:
            replacement.send("rebound")
            assert replacement.recv(timeout=1) == "rebound"
            with connect(tmp_path / "events.sock") as subscriber:
                assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE


def test_old_source_cannot_publish_or_reset_replacement_epoch(tmp_path):
    tap = CodexProtocolEventTap(tmp_path / "events.sock", peer_identity=TEST_PEER)
    tap.start()
    old_epoch = tap.activate_primary_source()
    old_event = json.dumps({"method": "turn/started", "params": {"threadId": "old", "turn": {"id": "old"}}})
    try:
        with connect(tmp_path / "events.sock") as old_subscriber:
            assert old_subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
            tap.publish(old_event, source_epoch=old_epoch)
            assert old_subscriber.recv(timeout=1) == old_event
            tap.reset_after_disconnect(source_epoch=old_epoch)
            with pytest.raises(ConnectionClosed):
                old_subscriber.recv(timeout=1)
        assert_source_unavailable(tmp_path / "events.sock")
        new_epoch = tap.activate_primary_source()
        tap.publish(old_event, source_epoch=old_epoch)
        tap.reset_after_disconnect(source_epoch=old_epoch)
        with connect(tmp_path / "events.sock") as replacement:
            assert replacement.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
            tap.publish(old_event, source_epoch=old_epoch)
            tap.publish("new-source", source_epoch=new_epoch)
            assert replacement.recv(timeout=1) == "new-source"
    finally:
        tap.close()


def test_input_disconnect_invalidates_while_output_projection_is_blocked(tmp_path):
    projecting = Event()
    release_projection = Event()

    def blocked_projection(message, _event):
        if message == "block":
            projecting.set()
            assert release_projection.wait(3)

    with running_proxy(tmp_path, on_server_message=blocked_projection):
        try:
            with connect(tmp_path / "proxy.sock", "/rpc") as tui:
                tui.send("ready")
                assert tui.recv(timeout=1) == "ready"
                with connect(tmp_path / "events.sock") as subscriber:
                    assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
                    tui.send("block")
                    assert tui.recv(timeout=1) == "block"
                    assert subscriber.recv(timeout=1) == "block"
                    assert projecting.wait(1)
                    tui.close()
                    with pytest.raises(ConnectionClosed):
                        subscriber.recv(timeout=1)
                    assert_source_unavailable(tmp_path / "events.sock")
        finally:
            release_projection.set()


def test_mandatory_publication_failure_ends_continuity(tmp_path, monkeypatch):
    with running_proxy(tmp_path) as (proxy, tap), connect(tmp_path / "proxy.sock", "/rpc") as tui:
        tui.send("ready")
        assert tui.recv(timeout=1) == "ready"
        with connect(tmp_path / "events.sock") as subscriber:
            assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE

            def fail_publication(*_args, **_kwargs):
                raise RuntimeError("injected mandatory publication failure")

            monkeypatch.setattr(tap, "publish_protocol_event", fail_publication)
            tui.send("forwarded-before-observation")
            assert tui.recv(timeout=1) == "forwarded-before-observation"
            with pytest.raises(ConnectionClosed):
                subscriber.recv(timeout=1)
            proxy.wait_for_primary_connection_release(1)
            assert_source_unavailable(tmp_path / "events.sock")


@pytest.mark.parametrize("loss_during_initial_read", [False, True])
def test_unbounded_exact_wait_reports_source_loss_without_resubmitting(tmp_path, loss_during_initial_read):
    tap = CodexProtocolEventTap(tmp_path / "events.sock", peer_identity=TEST_PEER)
    source_epoch = tap.activate_primary_source()
    tap.start()
    state_read = Event()
    outcome = []
    protocol = FakeWebSocket(
        verified_responses(status="active", turns=[{"id": "target", "status": "inProgress", "items": []}])
    )

    def connector(path, **options):
        return unix_connect(path, **options) if options["uri"].endswith("/events") else protocol

    def revalidate():
        if loss_during_initial_read:
            tap.reset_after_disconnect(source_epoch=source_epoch)
        state_read.set()

    def wait():
        try:
            CodexControlClient(connector=connector).wait_for_turn(
                replace(control(tmp_path), protocol_event_socket_path=tmp_path / "events.sock"),
                "target",
                revalidate=revalidate,
            )
        except Exception as error:
            outcome.append(error)

    waiter = Thread(target=wait, daemon=True)
    waiter.start()
    try:
        assert state_read.wait(1)
        tap.reset_after_disconnect(source_epoch=source_epoch)
        # The native turn can finish during the gap without a deliverable event.
        tap.publish(
            json.dumps(
                {"method": "turn/completed", "params": {"threadId": str(CODEX_SESSION_ID), "turn": {"id": "target"}}}
            ),
            source_epoch=source_epoch,
        )
        waiter.join(1)
        assert not waiter.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], RodexEventStreamContinuityError)
        assert "do not resubmit" in str(outcome[0])
        assert {frame.get("method") for frame in protocol.sent} <= {
            "initialize",
            "initialized",
            "thread/loaded/list",
            "thread/read",
        }
    finally:
        tap.close()
        waiter.join(1)


def test_exact_wait_during_source_gap_fails_before_history_read(tmp_path):
    tap = CodexProtocolEventTap(tmp_path / "events.sock", peer_identity=TEST_PEER)
    tap.start()
    try:
        with pytest.raises(RodexEventStreamContinuityError, match="unavailable"):
            CodexControlClient().wait_for_turn(
                replace(control(tmp_path), protocol_event_socket_path=tmp_path / "events.sock"), "target"
            )
    finally:
        tap.close()
