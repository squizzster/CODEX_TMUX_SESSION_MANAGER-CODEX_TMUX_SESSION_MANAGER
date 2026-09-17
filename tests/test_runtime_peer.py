from __future__ import annotations

import os
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from runtime_peer_fixtures import TEST_PEER, LiveTestProcess
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from rodex.control import CodexControlClient, LiveRodexControl, RodexControlError
from rodex.interaction_pipeline import DeliveryStatus, InteractionOperation, InteractionRequest
from rodex.interaction_transport import publish_session_interaction
from rodex.protocol_proxy import CodexProtocolEventTap, CodexProtocolProxy, ToolCallCounter
from rodex.runtime_peer import (
    RuntimePeerIdentityError,
    require_unix_peer_process,
    verified_runtime_connection,
)
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId

OTHER_RUNTIME = replace(TEST_PEER, runtime_id=RodexRuntimeId.parse("c" * 16))
OTHER_SERVER = replace(TEST_PEER, tmux_server_id="d" * 32)


def _control(path: Path) -> LiveRodexControl:
    codex_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    capability = TmuxSessionCapability(
        path.parent / "tmux.sock",
        TEST_PEER.tmux_server_id,
        "$0",
        "%0",
        TEST_PEER.runtime_id,
        RodexSessionId.parse("1" * 16),
        RodexRegistryId.parse("2" * 16),
        1,
        codex_id,
    )
    return LiveRodexControl(path, path, codex_id, runtime_id=TEST_PEER.runtime_id, tmux_capability=capability)


@contextmanager
def _server(path: Path, handler):
    server = unix_serve(handler, path=str(path), close_timeout=1)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown(close_connections=True)
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize("path", ["/rodex-control", "/rodex-interaction"])
@pytest.mark.parametrize("claimed", [None, OTHER_RUNTIME, OTHER_SERVER])
def test_wrong_proxy_identity_is_rejected_before_opening_upstream(tmp_path: Path, path: str, claimed) -> None:
    upstream_frames = []

    def upstream(connection):
        upstream_frames.append("connected")

    app = tmp_path / "app.sock"
    proxy_path = tmp_path / "proxy.sock"
    with (
        _server(app, upstream),
        CodexProtocolProxy(
            proxy_path,
            app,
            ToolCallCounter(lambda _: None),
            peer_identity=TEST_PEER,
            app_server_process=LiveTestProcess(),
        ) as proxy,
    ):
        with (
            pytest.raises(InvalidHandshake),
            unix_connect(
                str(proxy_path),
                uri=f"ws://localhost{path}",
                additional_headers={} if claimed is None else claimed.headers(),
            ),
        ):
            pytest.fail("mismatched connection was admitted")
        assert proxy.interactions.records == ()
    assert upstream_frames == []


@pytest.mark.parametrize("claimed", [None, OTHER_RUNTIME, OTHER_SERVER])
def test_event_tap_does_not_publish_ready_to_an_unverified_subscriber(tmp_path: Path, claimed) -> None:
    path = tmp_path / "events.sock"
    tap = CodexProtocolEventTap(path, peer_identity=TEST_PEER)
    tap.start()
    try:
        with (
            pytest.raises(InvalidHandshake),
            unix_connect(
                str(path), uri="ws://localhost/events", additional_headers={} if claimed is None else claimed.headers()
            ),
        ):
            pytest.fail("mismatched subscriber was admitted")
        assert tap._subscribers == {}
    finally:
        tap.close()


@pytest.mark.parametrize("operation", ["inspect", "start", "steer", "interrupt"])
def test_same_opened_connection_must_echo_expected_server_incarnation(tmp_path: Path, operation: str) -> None:
    frames = []

    def substituted(connection):
        with suppress(ConnectionClosed):
            frames.extend(connection)

    path = tmp_path / "replaced.sock"
    with _server(path, substituted):
        client = CodexControlClient()
        with pytest.raises(RodexControlError, match="expected runtime and tmux server"):
            control = _control(path)
            if operation == "inspect":
                client.inspect(control)
            elif operation == "start":
                client._start_turn(control, "must not dispatch", revalidate=lambda: None)
            elif operation == "steer":
                client._steer_turn(control, "turn-1", "must not dispatch", revalidate=lambda: None)
            else:
                client._interrupt_turn(control, "turn-1", revalidate=lambda: None)
        result = publish_session_interaction(
            path,
            InteractionRequest("main", InteractionOperation.MESSAGE, "test", text="must not dispatch"),
            peer_identity=TEST_PEER,
        )
        assert result.status == DeliveryStatus.REJECTED
    assert frames == []


@pytest.mark.parametrize("identity", [OTHER_RUNTIME, OTHER_SERVER])
def test_connected_different_runtime_receives_no_control_or_interaction_request(tmp_path: Path, identity) -> None:
    path = tmp_path / "proxy.sock"
    with CodexProtocolProxy(
        path,
        tmp_path / "never-opened.sock",
        ToolCallCounter(lambda _: None),
        peer_identity=identity,
        app_server_process=LiveTestProcess(),
    ) as proxy:
        with pytest.raises(RodexControlError):
            CodexControlClient().inspect(_control(path))
        result = publish_session_interaction(
            path,
            InteractionRequest("main", InteractionOperation.MESSAGE, "test", text="must not dispatch"),
            peer_identity=TEST_PEER,
        )
        assert result.status == DeliveryStatus.REJECTED
        assert proxy.interactions.records == ()


def test_duplicate_listener_and_its_cleanup_preserve_the_original_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "events.sock"
    owner = CodexProtocolEventTap(path, peer_identity=TEST_PEER)
    duplicate = CodexProtocolEventTap(path, peer_identity=OTHER_SERVER)
    owner.start()
    try:
        with pytest.raises(RuntimeError, match="could not bind"):
            duplicate.start()
        duplicate.close()
        assert path.exists()
        with verified_runtime_connection(
            unix_connect, path, peer_identity=TEST_PEER, uri="ws://localhost/events"
        ) as connection:
            assert "rodex/event-stream/ready" in connection.recv(timeout=1)
    finally:
        owner.close()
    assert not path.exists()


def test_old_listener_cleanup_does_not_unlink_a_replaced_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "events.sock"
    owner = CodexProtocolEventTap(path, peer_identity=TEST_PEER)
    owner.start()
    path.rename(tmp_path / "retained.sock")
    with socket.socket(socket.AF_UNIX) as replacement:
        replacement.bind(str(path))
        replacement.listen()
        owner.close()
        assert path.exists()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))


_CHILD_SERVER = """
import socket, sys
with socket.socket(socket.AF_UNIX) as listener:
    listener.bind(sys.argv[1])
    listener.listen()
    print('ready', flush=True)
    with listener.accept()[0] as connection:
        connection.recv(1)
"""


@pytest.mark.parametrize("wrapped", [False, True])
def test_process_ownership_accepts_exact_child_and_live_wrapper_descendant(tmp_path: Path, wrapped: bool) -> None:
    path = tmp_path / "native.sock"
    arguments = [sys.executable, "-B", "-c", _CHILD_SERVER, str(path)]
    if wrapped:
        arguments = [
            sys.executable,
            "-B",
            "-c",
            "import subprocess,sys; subprocess.run(sys.argv[1:], check=True)",
            *arguments,
        ]
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            require_unix_peer_process(SimpleNamespace(socket=client), process)
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=3)


def test_same_user_unrelated_process_does_not_satisfy_retained_child_identity(tmp_path: Path) -> None:
    path = tmp_path / "native.sock"
    process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"])
    try:
        with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
            server.bind(str(path))
            server.listen()
            client.connect(str(path))
            with pytest.raises(RuntimePeerIdentityError, match="retained live App Server"):
                require_unix_peer_process(SimpleNamespace(socket=client), process)
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_exited_retained_process_cannot_authorize_a_reused_pid() -> None:
    left, right = socket.socketpair()
    with left, right, pytest.raises(RuntimePeerIdentityError, match="retained live App Server"):
        require_unix_peer_process(SimpleNamespace(socket=left), SimpleNamespace(pid=os.getpid(), poll=lambda: 0))


def test_another_host_tree_cannot_use_native_path_but_exact_control_can_connect(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from threading import Thread
from websockets.sync.server import unix_serve
from rodex.protocol_proxy import CodexProtocolProxy, ToolCallCounter
from rodex.runtime_peer import CurrentProcessOwner, RuntimePeerIdentity
from rodex_registry import RodexRuntimeId
root = Path(sys.argv[1])
identity = RuntimePeerIdentity(RodexRuntimeId.parse('a' * 16), 'b' * 32)
def echo(connection):
    for message in connection:
        connection.send(message)
app = unix_serve(echo, path=str(root / 'app.sock'), close_timeout=1)
thread = Thread(target=app.serve_forever, daemon=True)
thread.start()
try:
    with CodexProtocolProxy(root / 'proxy.sock', root / 'app.sock', ToolCallCounter(lambda _: None),
            peer_identity=identity, app_server_process=CurrentProcessOwner()):
        print('ready', flush=True)
        sys.stdin.read(1)
finally:
    app.shutdown(close_connections=True)
    thread.join(3)
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        for native_path in ("/", "/rpc"):
            with (
                pytest.raises(InvalidHandshake),
                unix_connect(
                    str(tmp_path / "proxy.sock"),
                    uri=f"ws://localhost{native_path}",
                ),
            ):
                pytest.fail("another host's native client was admitted")
        with verified_runtime_connection(
            unix_connect, tmp_path / "proxy.sock", peer_identity=TEST_PEER, uri="ws://localhost/rodex-control"
        ) as connection:
            connection.send("exact control")
            assert connection.recv(timeout=2) == "exact control"
    finally:
        _output, errors = process.communicate(input="q", timeout=5)
    assert process.returncode == 0, errors


def test_native_client_from_own_host_descendant_uses_unmodified_codex_protocol(tmp_path: Path) -> None:
    def echo(connection):
        for message in connection:
            connection.send(message)

    app = tmp_path / "app.sock"
    proxy_path = tmp_path / "proxy.sock"
    with (
        _server(app, echo),
        CodexProtocolProxy(
            proxy_path,
            app,
            ToolCallCounter(lambda _: None),
            peer_identity=TEST_PEER,
            app_server_process=LiveTestProcess(),
        ),
    ):
        script = """
import sys
from websockets.sync.client import unix_connect
with unix_connect(sys.argv[1], uri='ws://localhost/rpc') as connection:
    connection.send('native request')
    print(connection.recv(timeout=2), flush=True)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(proxy_path)], capture_output=True, text=True, timeout=5, check=True
        )
        assert result.stdout.strip() == "native request"
