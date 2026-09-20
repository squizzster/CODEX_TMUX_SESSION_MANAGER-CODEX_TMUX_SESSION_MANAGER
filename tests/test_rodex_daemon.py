from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread

import pytest

import rodex.daemon as daemon_module
from rodex.analytics import SharedAnalyticsCoordinator
from rodex.daemon import RODEX_DAEMON_PROCESS_NAME, DaemonRuntimeManager, RodexDaemonServerError
from rodex.daemon_client import (
    RODEX_DAEMON_IMPLEMENTATION_FIELD,
    RODEX_DAEMON_PROTOCOL,
    RodexDaemonClient,
    RodexDaemonError,
    daemon_socket_path,
    encode_daemon_message,
    receive_daemon_message,
)
from rodex.implementation_identity import RODEX_IMPLEMENTATION_ID, RODEX_IMPLEMENTATION_SHA256
from rodex.process_contracts import AnalyticsRuntimeConfig, RuntimeServiceConfig
from rodex.process_guard import set_current_linux_task_name
from rodex.process_receipts import PROCESS_RECEIPT_PATTERN, RuntimeProcessReceipts
from rodex.tmux_session_capability import TmuxRuntimeCapability, runtime_tmux_socket_name
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId


@pytest.fixture
def short_runtime_root() -> Iterator[Path]:
    """Keep the full implementation-hash socket below Linux's AF_UNIX path limit."""
    with tempfile.TemporaryDirectory(prefix="rodex-test-", dir="/tmp") as directory:
        yield Path(directory)


def _config(root: Path, value: int) -> RuntimeServiceConfig:
    runtime_id = RodexRuntimeId(value)
    token = str(runtime_id)
    event_socket = root / f"events-{token}.sock"
    return RuntimeServiceConfig(
        workspace=root,
        codex_binary="/usr/bin/codex",
        app_server_socket_path=root / f"app-{token}.sock",
        app_server_log_path=root / f"app-{token}.log",
        protocol_proxy_socket_path=root / f"proxy-{token}.sock",
        protocol_event_socket_path=event_socket,
        tmux_binary="/usr/bin/tmux",
        tmux_server_socket_path=root / runtime_tmux_socket_name(runtime_id),
        tmux_pane_target=f"%{value}",
        runtime_id=runtime_id,
        analytics=AnalyticsRuntimeConfig(
            rodex_database_path=root / "rodex.sqlite3",
            codex_sessions_root=root / "sessions",
            rodex_session_id=RodexSessionId(value),
            rodex_registry_id=RodexRegistryId(1),
            runtime_id=runtime_id,
            tmux_server_id=f"{value:032x}",
            protocol_event_socket_path=event_socket,
        ),
        user_environment=(("PATH", "/usr/bin"),),
    )


class RecordingAnalytics:
    def __init__(self) -> None:
        self.started = 0
        self.reserved: list[AnalyticsRuntimeConfig] = []
        self.activated: list[AnalyticsRuntimeConfig] = []
        self.events: list[tuple[str, object]] = []
        self.retired: list[str] = []
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def reserve(self, config: AnalyticsRuntimeConfig) -> None:
        self.reserved.append(config)

    def activate(self, config: AnalyticsRuntimeConfig) -> None:
        self.activated.append(config)

    def observe_protocol_event(self, runtime_id: str, event: object) -> None:
        self.events.append((runtime_id, event))

    def retire(self, runtime_id: str) -> None:
        self.retired.append(runtime_id)

    def close(self) -> None:
        self.closed += 1


class RecordingReceipts:
    def __init__(self) -> None:
        self.reconciled = 0

    def reconcile(self) -> None:
        self.reconciled += 1

    def record(self, *_args: object) -> None:
        return None

    def release(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize("name", ["", "Rodex_Daemon", "rodex daemon", "rodex-daemon", "r" * 16])
def test_linux_task_name_contract_rejects_ambiguous_or_truncated_names(name: str) -> None:
    with pytest.raises(ValueError, match="Linux task name"):
        set_current_linux_task_name(name)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.14.0a1", "rodexd_v0_14a1"),
        ("0.15.0b2", "rodexd_v0_15b2"),
        ("1.2.3", "rodexd_v1_2_3"),
        ("0.14.1a1", "rodexd_v0141a1"),
    ],
)
def test_daemon_process_name_follows_release_version(version: str, expected: str) -> None:
    assert daemon_module._versioned_daemon_process_name(version) == expected


@pytest.mark.parametrize("version", ["not-a-version", "999.999.999rc999"])
def test_daemon_process_name_rejects_invalid_or_truncated_versions(version: str) -> None:
    with pytest.raises(ValueError, match="daemon task name"):
        daemon_module._versioned_daemon_process_name(version)


def test_current_daemon_process_name_includes_current_release() -> None:
    assert RODEX_DAEMON_PROCESS_NAME == "rodexd_v0_15a1"


def test_server_claims_single_socket_before_constructing_multi_runtime_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FailingEndpoint:
        def open(self) -> socket.socket:
            events.append("endpoint-claim")
            raise OSError("daemon endpoint is already owned")

    class ForbiddenManager:
        def __init__(self, _runtime_root: Path) -> None:
            events.append("manager-created")

    server = daemon_module.RodexDaemonServer(tmp_path)
    server._endpoint = FailingEndpoint()  # type: ignore[assignment]
    monkeypatch.setattr(daemon_module, "DaemonRuntimeManager", ForbiddenManager)

    with pytest.raises(OSError, match="already owned"):
        server.run()

    assert events == ["endpoint-claim"]


def test_idle_server_blocks_on_control_event_until_stop(
    short_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_entered = Event()
    select_timeouts: list[float | None] = []
    failures: list[BaseException] = []
    real_select = daemon_module.select.select

    class EmptyManager:
        def __init__(self, _runtime_root: Path) -> None:
            return None

        def close(self) -> None:
            return None

    def observed_select(reads, writes, exceptional, timeout=None):
        select_timeouts.append(timeout)
        select_entered.set()
        return real_select(reads, writes, exceptional, timeout)

    server = daemon_module.RodexDaemonServer(short_runtime_root)
    monkeypatch.setattr(daemon_module, "DaemonRuntimeManager", EmptyManager)
    monkeypatch.setattr(daemon_module.select, "select", observed_select)

    def run_server() -> None:
        try:
            server.run()
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run_server)
    thread.start()
    assert select_entered.wait(1)
    time.sleep(0.1)
    assert select_timeouts == [None]

    server.stop()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert failures == []
    assert select_timeouts == [None]


@pytest.mark.parametrize(
    ("request_payload", "expected_error"),
    [
        ({"protocol": "rodex-daemon-v1", "operation": "ping"}, "protocol does not match"),
        ({"protocol": RODEX_DAEMON_PROTOCOL, "operation": "ping"}, "implementation does not match"),
    ],
)
def test_daemon_rejects_prior_protocol_and_missing_implementation_identity(
    short_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_payload: dict[str, object],
    expected_error: str,
) -> None:
    class EmptyManager:
        def __init__(self, _runtime_root: Path) -> None:
            return None

        def close(self) -> None:
            return None

    server = daemon_module.RodexDaemonServer(short_runtime_root)
    monkeypatch.setattr(daemon_module, "DaemonRuntimeManager", EmptyManager)
    failures: list[BaseException] = []

    def run_server() -> None:
        try:
            server.run()
        except BaseException as error:
            failures.append(error)

    server_thread = Thread(target=run_server)
    server_thread.start()
    socket_path = daemon_socket_path(short_runtime_root)
    deadline = time.monotonic() + 1
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            connection.sendall(encode_daemon_message(request_payload))
            response = receive_daemon_message(connection)

        assert set(response) == {"protocol", RODEX_DAEMON_IMPLEMENTATION_FIELD, "ok", "error"}
        assert response["protocol"] == RODEX_DAEMON_PROTOCOL
        assert response[RODEX_DAEMON_IMPLEMENTATION_FIELD] == RODEX_IMPLEMENTATION_ID
        assert response["ok"] is False
        assert expected_error in response["error"]
    finally:
        server.stop()
        server_thread.join(timeout=1)

    assert not server_thread.is_alive()
    assert failures == []


def test_one_manager_owns_two_runtime_socket_sets_and_one_analytics_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analytics = RecordingAnalytics()
    receipts = RecordingReceipts()
    services_started: list[str] = []
    supervisor_wakes: list[str] = []
    resize_wakes: list[str] = []
    release_services = Event()

    def admit(config: RuntimeServiceConfig, _fd: int, *, peer_pid: int):
        assert peer_pid == os.getpid()
        return (
            TmuxRuntimeCapability(
                config.tmux_server_socket_path,
                config.tmux_server_id,
                f"${config.runtime_id.value}",
                config.tmux_pane_target,
                config.runtime_id,
            ),
            {
                "TERM": "xterm-256color",
                "TMUX": f"{config.tmux_server_socket_path},1,0",
                "TMUX_PANE": config.tmux_pane_target,
            },
        )

    def run_service(config: RuntimeServiceConfig, **callbacks: object) -> int:
        services_started.append(str(config.runtime_id))
        callbacks["on_terminal_gateway_ready"](  # type: ignore[operator]
            lambda: supervisor_wakes.append(str(config.runtime_id)),
            lambda: resize_wakes.append(str(config.runtime_id)),
        )
        activated = config.analytics.activate(
            rodex_sessions_id=config.runtime_id.value,
            codex_session_id=uuid.UUID(int=config.runtime_id.value),
        )
        callbacks["on_analytics_activated"](activated)  # type: ignore[operator]
        callbacks["on_analytics_event"](  # type: ignore[operator]
            {"method": "turn/completed", "params": {"threadId": str(activated.codex_session_id)}}
        )
        callbacks["on_started"]()  # type: ignore[operator]
        release_services.wait(2)
        return 0

    monkeypatch.setattr(daemon_module, "admit_runtime_terminal_fd", admit)
    monkeypatch.setattr(daemon_module, "run_runtime_service", run_service)
    manager = DaemonRuntimeManager(
        tmp_path,
        analytics=analytics,  # type: ignore[arg-type]
        process_receipts=receipts,  # type: ignore[arg-type]
    )
    configs = (_config(tmp_path, 1), _config(tmp_path, 2))
    bridges: list[socket.socket] = []
    for index, config in enumerate(configs, 1):
        operation_id = f"{index:032x}"
        manager.reserve(operation_id, config)
        bridge, daemon_end = socket.socketpair()
        bridges.append(bridge)
        context = manager.bind_terminal(
            operation_id,
            str(config.runtime_id),
            config.tmux_server_id,
            config.tmux_pane_target,
            os.dup(0),
            daemon_end,
            peer_pid=os.getpid(),
        )
        manager.start_bound_runtime(context)
        manager.await_ready(operation_id, str(config.runtime_id), 1)

    assert receipts.reconciled == 1
    assert analytics.started == 1
    assert analytics.reserved == [config.analytics for config in configs]
    assert set(services_started) == {str(config.runtime_id) for config in configs}
    assert len(analytics.activated) == 2
    assert len(analytics.events) == 2
    # Initial geometry is reconciled after callback registration, before any
    # external wake, closing the initial-size-read/subscription gap.
    assert sorted(resize_wakes) == sorted(str(config.runtime_id) for config in configs)
    for config in configs:
        manager.wake_runtime(str(config.runtime_id), "registration")
        manager.wake_runtime(str(config.runtime_id), "terminal_resize")
    assert set(supervisor_wakes) == {str(config.runtime_id) for config in configs}
    assert set(resize_wakes) == {str(config.runtime_id) for config in configs}

    release_services.set()
    manager.close()
    for bridge in bridges:
        bridge.close()

    assert set(analytics.retired) == {str(config.runtime_id) for config in configs}
    assert analytics.closed == 1


def test_runtime_reservation_rejects_cross_operation_ownership(tmp_path: Path) -> None:
    analytics = RecordingAnalytics()
    manager = DaemonRuntimeManager(
        tmp_path,
        analytics=analytics,  # type: ignore[arg-type]
        process_receipts=RecordingReceipts(),  # type: ignore[arg-type]
    )
    config = _config(tmp_path, 1)
    manager.reserve("1" * 32, config)
    manager.reserve("1" * 32, config)

    with pytest.raises(RodexDaemonServerError, match="another operation"):
        manager.reserve("2" * 32, config)

    manager.close()


def test_shared_coordinator_has_one_worker_thread_for_all_reserved_runtimes(tmp_path: Path) -> None:
    thread_count_before = sum(
        thread.name == "rodex-analytics-coordinator" for thread in __import__("threading").enumerate()
    )
    coordinator = SharedAnalyticsCoordinator()
    coordinator.start()
    coordinator.reserve(_config(tmp_path, 1).analytics)
    coordinator.reserve(_config(tmp_path, 2).analytics)
    thread_names_after = [thread.name for thread in __import__("threading").enumerate()]
    coordinator.close()

    assert thread_names_after.count("rodex-analytics-coordinator") == thread_count_before + 1


def test_process_receipt_reconciliation_terminates_exact_orphan_group(tmp_path: Path) -> None:
    process = subprocess.Popen(["/usr/bin/sleep", "30"], start_new_session=True)
    receipts = RuntimeProcessReceipts(tmp_path)
    receipts.record("app-server", RodexRuntimeId(1), "1" * 32, process)

    RuntimeProcessReceipts(tmp_path).reconcile()
    process.wait(timeout=3)

    assert process.returncode == -15
    assert list(tmp_path.glob(PROCESS_RECEIPT_PATTERN)) == []


def test_process_receipt_reconciliation_escalates_for_a_surviving_group_child(tmp_path: Path) -> None:
    child_ready = tmp_path / "child-ready"
    parent_source = """
import os
import subprocess
import sys
import time
child_source = '''
import pathlib
import signal
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text('ready')
time.sleep(30)
'''
child = subprocess.Popen([sys.executable, '-c', child_source, sys.argv[1]])
while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
print(child.pid, flush=True)
time.sleep(30)
"""
    leader = subprocess.Popen(
        [sys.executable, "-c", parent_source, os.fspath(child_ready)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    child_pid = int(leader.stdout.readline())
    receipts = RuntimeProcessReceipts(tmp_path)
    receipts.record("app-server", RodexRuntimeId(1), "1" * 32, leader)

    RuntimeProcessReceipts(tmp_path).reconcile()
    leader.wait(timeout=3)

    try:
        child_state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        child_state = "gone"
    assert child_state in {"gone", "Z"}
    assert list(tmp_path.glob(PROCESS_RECEIPT_PATTERN)) == []


def test_process_guard_rejects_a_parent_identity_that_already_changed() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "rodex.process_guard",
            "--parent-pid",
            str(os.getpid() + 1_000_000),
            "--",
            "/usr/bin/sleep",
            "30",
        ]
    )

    assert process.wait(timeout=3) == -15


def test_process_guard_terminates_the_native_child_when_its_daemon_parent_dies() -> None:
    parent_source = """
import os
import subprocess
import sys
import time
child = subprocess.Popen([
    sys.executable, '-I', '-m', 'rodex.process_guard',
    '--parent-pid', str(os.getpid()), '--', '/usr/bin/sleep', '30',
])
print(child.pid, flush=True)
time.sleep(30)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_source],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    assert Path(f"/proc/{child_pid}").exists()

    parent.kill()
    parent.wait(timeout=3)
    deadline = time.monotonic() + 3
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert not Path(f"/proc/{child_pid}").exists()


def test_concurrent_first_clients_converge_on_one_private_daemon_socket(short_runtime_root: Path) -> None:
    clients = (
        RodexDaemonClient(short_runtime_root, sys.executable),
        RodexDaemonClient(short_runtime_root, sys.executable),
    )
    failures: list[BaseException] = []

    def ensure(client: RodexDaemonClient) -> None:
        try:
            client.ensure_running()
        except BaseException as error:
            failures.append(error)

    threads = [Thread(target=ensure, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    daemon_pids: list[int] = []
    for process_path in Path("/proc").glob("[0-9]*"):
        try:
            command = (process_path / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"rodex.daemon" in command and os.fsencode(short_runtime_root) in command:
            daemon_pids.append(int(process_path.name))
    try:
        assert failures == []
        assert all(not thread.is_alive() for thread in threads)
        assert len(daemon_pids) == 1
        assert Path(f"/proc/{daemon_pids[0]}/comm").read_text().strip() == RODEX_DAEMON_PROCESS_NAME
        assert clients[0].socket_path == clients[1].socket_path
        assert clients[0].socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        for pid in daemon_pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)


def test_current_client_starts_its_exact_daemon_alongside_an_older_daemon_socket(
    short_runtime_root: Path,
) -> None:
    legacy_path = short_runtime_root / "rodexd-v2.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(legacy_path))
    listener.listen()
    client = RodexDaemonClient(short_runtime_root, sys.executable)
    daemon_pids: list[int] = []
    try:
        client.ensure_running()
        assert client.socket_path == short_runtime_root / f"{RODEX_IMPLEMENTATION_SHA256}.sock"
        assert client.socket_path != legacy_path
        assert client.socket_path.is_socket()
        assert legacy_path.is_socket()
        for process_path in Path("/proc").glob("[0-9]*"):
            try:
                command = (process_path / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if b"rodex.daemon" in command and os.fsencode(short_runtime_root) in command:
                daemon_pids.append(int(process_path.name))
        assert len(daemon_pids) == 1
    finally:
        for pid in daemon_pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        listener.close()


def test_current_receipt_reconciliation_ignores_an_older_implementation_namespace(tmp_path: Path) -> None:
    older_receipt = tmp_path / "rodexd-v2-process-0000000000000001-app-server.json"
    older_receipt.write_text("owned by the still-running older daemon\n")

    RuntimeProcessReceipts(tmp_path).reconcile()

    assert older_receipt.read_text() == "owned by the still-running older daemon\n"


def test_implementation_daemon_socket_rejects_an_overlong_runtime_root_cleanly(tmp_path: Path) -> None:
    long_root = tmp_path / ("x" * 100)

    with pytest.raises(RodexDaemonError, match="daemon Unix socket path is too long"):
        RodexDaemonClient(long_root, sys.executable)


def test_current_client_rejects_same_protocol_daemon_with_different_loaded_code(short_runtime_root: Path) -> None:
    socket_path = daemon_socket_path(short_runtime_root)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()

    def respond_once() -> None:
        connection, _address = listener.accept()
        try:
            request = receive_daemon_message(connection)
            assert request[RODEX_DAEMON_IMPLEMENTATION_FIELD] == RODEX_IMPLEMENTATION_ID
            connection.sendall(
                encode_daemon_message(
                    {
                        "protocol": RODEX_DAEMON_PROTOCOL,
                        RODEX_DAEMON_IMPLEMENTATION_FIELD: "different-loaded-code",
                        "ok": True,
                    }
                )
            )
        finally:
            connection.close()

    server = Thread(target=respond_once)
    server.start()
    spawns: list[object] = []
    client = RodexDaemonClient(
        short_runtime_root,
        sys.executable,
        process_spawner=lambda *_args, **_kwargs: spawns.append(object()),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(RodexDaemonError) as raised:
            client.ensure_running()
        diagnostic = str(raised.value)
        assert f"incompatible at {socket_path}" in diagnostic
        assert "running daemon implementation: 'different-loaded-code'" in diagnostic
        assert "current client: Rodex 0.15.0a1 (0.15.0a1+sha256." in diagnostic
        assert "daemon protocol: rodex-daemon-v3" in diagnostic
        assert "SQL catalog: not checked; this client expects generation 21 (rodex-v21.sqlite3)" in diagnostic
        assert "does not migrate earlier runtimes or catalogs" in diagnostic
        assert spawns == []
    finally:
        listener.close()
        server.join(timeout=1)


def test_current_client_reports_both_sides_of_a_daemon_protocol_mismatch(short_runtime_root: Path) -> None:
    socket_path = daemon_socket_path(short_runtime_root)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()

    def respond_once() -> None:
        connection, _address = listener.accept()
        try:
            receive_daemon_message(connection)
            connection.sendall(
                encode_daemon_message(
                    {
                        "protocol": "rodex-daemon-v1",
                        RODEX_DAEMON_IMPLEMENTATION_FIELD: "0.13.0+sha256.previous",
                        "ok": True,
                    }
                )
            )
        finally:
            connection.close()

    server = Thread(target=respond_once)
    server.start()
    client = RodexDaemonClient(short_runtime_root, sys.executable)
    try:
        with pytest.raises(RodexDaemonError) as raised:
            client.ensure_running()
        diagnostic = str(raised.value)
        assert f"protocol is incompatible at {socket_path}" in diagnostic
        assert "running daemon protocol: 'rodex-daemon-v1'" in diagnostic
        assert "current client protocol: 'rodex-daemon-v3' (Rodex 0.15.0a1)" in diagnostic
        assert "SQL catalog: not checked; this client expects generation 21 (rodex-v21.sqlite3)" in diagnostic
        assert "does not translate earlier wire protocols or migrate earlier catalogs" in diagnostic
    finally:
        listener.close()
        server.join(timeout=1)
