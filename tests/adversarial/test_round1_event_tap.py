from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread

import pytest
from runtime_peer_fixtures import TEST_PEER

from rodex.protocol_proxy import CodexProtocolEventTap


class ReadyThenBlockingConnection:
    def __init__(self) -> None:
        self.request = None
        self.ready_sent = Event()
        self.blocked_send_entered = Event()
        self.release_send = Event()
        self.closed = Event()
        self._send_count = 0
        self._lock = Lock()

    def send(self, _message: str | bytes) -> None:
        with self._lock:
            self._send_count += 1
            send_count = self._send_count
        if send_count == 1:
            self.ready_sent.set()
            return
        self.blocked_send_entered.set()
        assert self.release_send.wait(2)

    def close(self) -> None:
        self.closed.set()
        self.release_send.set()

    def close_socket(self) -> None:
        self.close()


class ClosingConnection:
    def __init__(self) -> None:
        self.request = None
        self.closed = Event()

    def send(self, _message: str | bytes) -> None:
        return

    def close(self) -> None:
        self.closed.set()

    def close_socket(self) -> None:
        self.close()


def test_round1_event_tap_rejects_registration_after_shutdown(tmp_path: Path) -> None:
    tap = CodexProtocolEventTap(tmp_path / "events.sock", queue_size=1, peer_identity=TEST_PEER)
    tap.close()
    connection = ClosingConnection()
    invoked = Event()
    done = Event()

    def register_after_close() -> None:
        invoked.set()
        tap._handle_subscriber(connection)
        done.set()

    handler = Thread(target=register_after_close)
    handler.start()
    assert invoked.wait(1)
    terminated_without_second_close = done.wait(0.2)
    if not terminated_without_second_close:
        tap.close()
    handler.join(2)

    assert not handler.is_alive()
    assert terminated_without_second_close
    assert connection.closed.is_set()


def test_round1_slow_event_subscriber_is_closed_and_reclaimed_on_overflow(
    tmp_path: Path,
) -> None:
    tap = CodexProtocolEventTap(tmp_path / "events.sock", queue_size=1, peer_identity=TEST_PEER)
    connection = ReadyThenBlockingConnection()
    source_epoch = tap.activate_primary_source()
    done = Event()

    def handle() -> None:
        tap._handle_subscriber(connection)
        done.set()

    handler = Thread(target=handle)
    handler.start()
    assert connection.ready_sent.wait(1)
    tap.publish("first", source_epoch=source_epoch)
    assert connection.blocked_send_entered.wait(1)
    tap.publish("queued", source_epoch=source_epoch)
    tap.publish("overflow", source_epoch=source_epoch)

    reclaimed_on_overflow = done.wait(0.2) and connection.closed.is_set()
    if not reclaimed_on_overflow:
        connection.release_send.set()
        tap.close()
    handler.join(2)

    assert not handler.is_alive()
    assert reclaimed_on_overflow


@pytest.mark.parametrize("shutdown", [False, True])
def test_primary_source_loss_drains_queued_events_and_interrupts_blocked_writer(tmp_path, shutdown):
    tap = CodexProtocolEventTap(tmp_path / "events.sock", queue_size=2, peer_identity=TEST_PEER)
    source_epoch = tap.activate_primary_source()
    connection = ReadyThenBlockingConnection()
    handler = Thread(target=tap._handle_subscriber, args=(connection,), daemon=True)
    handler.start()
    try:
        assert connection.ready_sent.wait(1)
        subscriber_queue = next(iter(tap._subscribers))
        tap.publish("in-flight", source_epoch=source_epoch)
        assert connection.blocked_send_entered.wait(1)
        tap.publish("queued-old-completion", source_epoch=source_epoch)
        if shutdown:
            tap.close()
        else:
            tap.reset_after_disconnect(source_epoch=source_epoch)
        handler.join(1)
        assert not handler.is_alive()
        assert connection.closed.is_set()
        assert not tap._subscribers
        assert subscriber_queue.empty()
        assert connection._send_count == 2  # Ready and in-flight; queued completion was discarded.
    finally:
        connection.release_send.set()
        tap.close()
        handler.join(1)
