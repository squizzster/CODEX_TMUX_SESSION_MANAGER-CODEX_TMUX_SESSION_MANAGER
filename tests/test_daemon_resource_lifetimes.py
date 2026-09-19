"""Real descriptors and explicit barriers guard daemon ownership boundaries."""

import array
import os
import socket
from contextlib import suppress
from threading import Event, Thread

import pytest
from test_rodex_daemon import RecordingAnalytics, RecordingReceipts, _config

import rodex.daemon as daemon
import rodex.runtime as runtime


class ReceivingSocket:
    def __init__(self, connection):
        self.connection = connection
        self.received = []

    def recvmsg(self, *args):
        result = self.connection.recvmsg(*args)
        for level, kind, data in result[1]:
            if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                values = array.array("i")
                values.frombytes(data)
                self.received.extend(values)
        return result

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize(
    "payload", [b"broken\n", b"\xff\n", b"[]\n", b"{}\ntrailing", b"{", b"x" * 130, b"{" + b" " * 128 + b"}\n"]
)
def test_failed_request_reclaims_received_descriptors(monkeypatch, payload):
    monkeypatch.setattr(daemon, "DAEMON_MESSAGE_LIMIT_BYTES", 128)
    sender, receiver = socket.socketpair()
    observed = ReceivingSocket(receiver)
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        sender.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))])
        sender.close()
        with pytest.raises(daemon.RodexDaemonServerError):
            daemon._receive_request(observed)
        assert observed.received
        for received in observed.received:
            with pytest.raises(OSError):
                os.fstat(received)
    finally:
        for received in observed.received:
            with suppress(OSError):
                os.close(received)
        os.close(descriptor)
        sender.close()
        receiver.close()


def test_valid_request_transfers_descriptor_to_caller():
    sender, receiver = socket.socketpair()
    observed = ReceivingSocket(receiver)
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        sender.sendmsg([b"{}\n"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))])
        payload, descriptors = daemon._receive_request(observed)
        assert payload == {}
        assert descriptors == tuple(observed.received)
        assert os.fstat(descriptors[0]) == os.fstat(descriptor)
    finally:
        for received in observed.received:
            os.close(received)
        os.close(descriptor)
        sender.close()
        receiver.close()


def manager_fixture(tmp_path, monkeypatch):
    analytics = RecordingAnalytics()
    manager = daemon.DaemonRuntimeManager(tmp_path, analytics=analytics, process_receipts=RecordingReceipts())
    config = _config(tmp_path, 1)
    manager.reserve("1" * 32, config)
    monkeypatch.setattr(daemon, "admit_runtime_terminal_fd", lambda *args, **kwargs: (None, {}))
    return manager, config, analytics


def bind(manager, config, descriptor, connection):
    return manager.bind_terminal(
        "1" * 32,
        str(config.runtime_id),
        config.tmux_server_id,
        config.tmux_pane_target,
        descriptor,
        connection,
        peer_pid=os.getpid(),
    )


def test_cancelled_reservation_retires_and_rejects_late_bridge(tmp_path, monkeypatch):
    manager, config, analytics = manager_fixture(tmp_path, monkeypatch)
    context = manager._runtimes[str(config.runtime_id)]
    sender, receiver = socket.socketpair()
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        manager.stop_runtime("1" * 32, str(config.runtime_id))
        assert context.ready.is_set() and context.done.is_set()
        assert analytics.retired == [str(config.runtime_id)]
        with pytest.raises(daemon.RodexDaemonServerError):
            bind(manager, config, descriptor, receiver)
        with pytest.raises(daemon.RodexDaemonServerError):
            manager.await_ready("1" * 32, str(config.runtime_id), 0.1)
        manager.stop_runtime("1" * 32, str(config.runtime_id))
        assert analytics.retired == [str(config.runtime_id)]
    finally:
        manager.close()
        os.close(descriptor)
        sender.close()
        receiver.close()


def test_stop_cannot_close_worker_owned_descriptor(tmp_path, monkeypatch):
    manager, config, _analytics = manager_fixture(tmp_path, monkeypatch)
    entered, release = Event(), Event()

    def delayed_service(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return 0

    monkeypatch.setattr(runtime, "_run_runtime_service", delayed_service)
    sender, receiver = socket.socketpair()
    descriptor = os.open(os.devnull, os.O_RDONLY)
    context = bind(manager, config, descriptor, receiver)
    try:
        manager.start_bound_runtime(context)
        assert entered.wait(1)
        # Exercise an expired join without making correctness depend on ten seconds.
        monkeypatch.setattr(context.thread, "join", lambda timeout=None: None)
        manager.stop_runtime("1" * 32, str(config.runtime_id))
        assert not context.done.is_set()
        os.fstat(descriptor)
        assert context.terminal_fd is None
        release.set()
        assert context.done.wait(1)
        with pytest.raises(OSError):
            os.fstat(descriptor)
        replacement = os.open(os.devnull, os.O_RDONLY)
        try:
            manager.stop_runtime("1" * 32, str(config.runtime_id))
            os.fstat(replacement)
        finally:
            os.close(replacement)
    finally:
        release.set()
        context.done.wait(1)
        manager.close()
        sender.close()
        receiver.close()


def test_thread_start_failure_reclaims_untransferred_terminal(tmp_path, monkeypatch):
    manager, config, analytics = manager_fixture(tmp_path, monkeypatch)
    sender, receiver = socket.socketpair()
    descriptor = os.open(os.devnull, os.O_RDONLY)
    context = bind(manager, config, descriptor, receiver)

    def fail_start(_thread):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(daemon.Thread, "start", fail_start)
    try:
        with pytest.raises(RuntimeError, match="cannot start thread"):
            manager.start_bound_runtime(context)
        assert context.done.is_set()
        assert analytics.retired == [str(config.runtime_id)]
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        # Old implementation leaves a never-started Thread; avoid masking the assertion.
        if context.thread is not None and context.thread.ident is None:
            context.thread = None
        manager.close()
        sender.close()
        receiver.close()


def test_cancel_during_terminal_admission_never_takes_descriptor(tmp_path, monkeypatch):
    manager, config, analytics = manager_fixture(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    errors = []

    def admit(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return None, {}

    monkeypatch.setattr(daemon, "admit_runtime_terminal_fd", admit)
    sender, receiver = socket.socketpair()
    descriptor = os.open(os.devnull, os.O_RDONLY)

    def bind_after_wait():
        try:
            bind(manager, config, descriptor, receiver)
        except daemon.RodexDaemonServerError as error:
            errors.append(error)

    thread = Thread(target=bind_after_wait)
    thread.start()
    try:
        assert entered.wait(1)
        assert manager.stop_runtime("1" * 32, str(config.runtime_id)) == "terminal"
        release.set()
        thread.join(1)
        assert len(errors) == 1
        os.fstat(descriptor)  # Rejected handoff remains the caller's responsibility.
        assert analytics.retired == [str(config.runtime_id)]
    finally:
        release.set()
        thread.join(2)
        manager.close()
        os.close(descriptor)
        sender.close()
        receiver.close()


def test_cancel_between_bind_and_start_closes_once(tmp_path, monkeypatch):
    manager, config, _ = manager_fixture(tmp_path, monkeypatch)
    sender, receiver = socket.socketpair()
    descriptor = os.open(os.devnull, os.O_RDONLY)
    context = bind(manager, config, descriptor, receiver)
    try:
        manager.stop_runtime("1" * 32, str(config.runtime_id))
        with pytest.raises(OSError):
            os.fstat(descriptor)
        with pytest.raises(daemon.RodexDaemonServerError):
            manager.start_bound_runtime(context)
        assert context.thread is None and context.done.is_set()
    finally:
        manager.close()
        sender.close()
        receiver.close()


def test_completion_records_are_bounded_and_evicted_operations_cannot_restart(tmp_path):
    manager = daemon.DaemonRuntimeManager(tmp_path, analytics=RecordingAnalytics(), process_receipts=RecordingReceipts())
    try:
        for index in range(1, 1201):
            config = _config(tmp_path, index)
            operation = f"{index:032x}"
            manager.reserve(operation, config)
            assert manager.stop_runtime(operation, str(config.runtime_id)) == "terminal"
        assert not manager._runtimes
        assert len(manager._completed) == daemon.COMPLETION_RECORD_LIMIT
        assert all(not hasattr(record, "config") for record in manager._completed.values())
        with pytest.raises(daemon.RodexDaemonServerError, match="expired"):
            manager.reserve(f"{1:032x}", _config(tmp_path, 1))
        assert manager.stop_runtime(f"{1200:032x}", str(_config(tmp_path, 1200).runtime_id)) == "terminal"
    finally:
        manager.close()


def test_ancillary_truncation_closes_every_delivered_descriptor():
    sender, receiver = socket.socketpair()
    observed = ReceivingSocket(receiver)
    descriptors = [os.open(os.devnull, os.O_RDONLY) for _ in range(8)]
    try:
        sender.sendmsg([b"{}\n"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))])
        with pytest.raises(daemon.RodexDaemonServerError, match="truncated"):
            daemon._receive_request(observed)
        for descriptor in observed.received:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        for descriptor in observed.received:
            with suppress(OSError):
                os.close(descriptor)
        sender.close()
        receiver.close()


def test_partial_request_deadline_releases_descriptors_and_restores_timeout(monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT_SECONDS", 0.05)
    sender, receiver = socket.socketpair()
    observed = ReceivingSocket(receiver)
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        sender.sendmsg([b"{"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))])
        with pytest.raises(daemon.RodexDaemonServerError, match="timed out"):
            daemon._receive_request(observed)
        assert receiver.gettimeout() is None
        for received in observed.received:
            with pytest.raises(OSError):
                os.fstat(received)
    finally:
        os.close(descriptor)
        sender.close()
        receiver.close()


def test_startup_cancellation_precedes_child_readiness_checks(tmp_path):
    stop = Event()
    stop.set()
    with pytest.raises(runtime.RodexRuntimeError, match="cancelled"):
        runtime._wait_for_app_server_socket(object(), tmp_path / "absent.sock", stop)
