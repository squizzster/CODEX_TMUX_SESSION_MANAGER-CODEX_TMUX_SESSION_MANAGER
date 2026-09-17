from __future__ import annotations

import json
import socket as socket_module
import time
import uuid
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import rodex.control as control_module
from rodex.control import CodexControlClient, LiveRodexControl, RodexControlError
from rodex.runtime_peer import RuntimePeerIdentity
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex_registry.identity import RodexRegistryId, RodexRuntimeId, RodexSessionId

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


class _BlockingTransport:
    def __init__(self, release: Event) -> None:
        self.release = release
        self.shutdown_calls: list[int] = []
        self.close_calls = 0

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)
        self.release.set()

    def close(self) -> None:
        self.close_calls += 1
        self.release.set()


class _BlockingSendSocket:
    def __init__(self, *, block_on_call: int) -> None:
        self.block_on_call = block_on_call
        self.send_calls = 0
        self.entered = Event()
        self.release = Event()
        self.socket = _BlockingTransport(self.release)

    def send(self, _message: str) -> None:
        self.send_calls += 1
        if self.send_calls == self.block_on_call:
            self.entered.set()
            self.release.wait(5)

    def recv(self, timeout: float | None = None) -> str:
        return json.dumps({"id": 0, "result": {"userAgent": "rodex-control/0.151.0 (Linux)"}})


def _run_in_daemon(call: object) -> tuple[Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            call()  # type: ignore[operator]
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=invoke, daemon=True)
    thread.start()
    return thread, errors


def test_round1_expired_mutation_deadline_sends_no_frame() -> None:
    socket = _BlockingSendSocket(block_on_call=99)
    context = control_module._MutationDispatchContext("dispatch-one", "thread-one", "turn-one")

    with pytest.raises(RodexControlError) as raised:
        control_module._request(
            socket,
            "request-one",
            "turn/start",
            {},
            indeterminate_context=context,
            deadline=1.0,
            monotonic=lambda: 2.0,
        )

    assert not isinstance(raised.value, control_module.RodexDispatchIndeterminateError)
    assert socket.send_calls == 0


def test_round1_blocked_request_send_tears_down_transport_at_deadline() -> None:
    socket = _BlockingSendSocket(block_on_call=1)
    context = control_module._MutationDispatchContext("dispatch-one", "thread-one", "turn-one")
    deadline = time.monotonic() + 0.02
    caller, errors = _run_in_daemon(
        lambda: control_module._request(
            socket,
            "request-one",
            "turn/start",
            {},
            indeterminate_context=context,
            deadline=deadline,
        )
    )
    assert socket.entered.wait(1)
    caller.join(1)
    completed_before_cleanup = not caller.is_alive()
    socket.release.set()
    caller.join(1)

    assert completed_before_cleanup
    assert socket.socket.shutdown_calls == [socket_module.SHUT_RDWR]
    assert len(errors) == 1
    assert isinstance(errors[0], control_module.RodexDispatchIndeterminateError)
    assert errors[0].dispatch_id == "dispatch-one"


def test_round1_blocked_initialized_notification_obeys_chain_deadline() -> None:
    socket = _BlockingSendSocket(block_on_call=2)
    client = CodexControlClient()
    deadline = time.monotonic() + 0.02
    caller, errors = _run_in_daemon(
        lambda: client._initialize_protocol(
            socket,
            deadline=deadline,
        )
    )
    assert socket.entered.wait(1)
    caller.join(1)
    completed_before_cleanup = not caller.is_alive()
    socket.release.set()
    caller.join(1)

    assert completed_before_cleanup
    assert socket.socket.shutdown_calls == [socket_module.SHUT_RDWR]
    assert len(errors) == 1 and isinstance(errors[0], RodexControlError)


class ChatteringReadSocket:
    def __init__(self, peer_identity: RuntimePeerIdentity) -> None:
        self.recv_calls = 0
        self.response = SimpleNamespace(headers=peer_identity.headers())

    def __enter__(self) -> ChatteringReadSocket:
        return self

    def __exit__(self, *_error: object) -> None:
        return None

    def send(self, _message: str) -> None:
        return

    def recv(self, _timeout: float | None = None, *, timeout: float | None = None) -> str:
        self.recv_calls += 1
        if self.recv_calls == 1:
            return json.dumps({"id": 0, "result": {"userAgent": "rodex-control/0.151.0 (Linux)"}})
        if self.recv_calls > 32:
            raise AssertionError("read RPC consumed unbounded unrelated frames")
        return json.dumps({"method": "unrelated/notification", "params": {}})


def test_round1_read_control_rpc_has_an_absolute_deadline(tmp_path: Path) -> None:
    capability = TmuxSessionCapability(
        tmp_path / "runtime.sock",
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0123456789abcdef"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        CODEX_SESSION_ID,
    )
    socket = ChatteringReadSocket(RuntimePeerIdentity(capability.runtime_id, capability.tmux_server_id))
    clock = 0.0

    def monotonic() -> float:
        nonlocal clock
        clock += 1.0
        return clock

    client = CodexControlClient(
        connector=lambda *_args, **_kwargs: socket,
        monotonic=monotonic,
    )
    control = LiveRodexControl(
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        CODEX_SESSION_ID,
        runtime_id=capability.runtime_id,
        tmux_capability=capability,
    )

    with pytest.raises(RodexControlError, match=r"deadline|timed out"):
        client.inspect(control)

    assert socket.recv_calls < 32


# Native Ctrl-C lifecycle and adversarial client schedules are exercised against
# disposable real tmux servers in tests/test_tmux_shared_ctrl_c.py.
