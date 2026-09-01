from __future__ import annotations

import json
import socket as socket_module
import subprocess
import time
import uuid
from pathlib import Path
from threading import Barrier, Event, Lock, Thread

import pytest

import rodex.control as control_module
from rodex.control import CodexControlClient, LiveRodexControl, RodexControlError
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex.tmux_shared_ctrl_c import handle_shared_ctrl_c
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CONFIRMATION_OPTION = "@rodex_shared_ctrl_c_confirmation"


def _capability(socket_path: Path) -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0123456789abcdef"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        CODEX_SESSION_ID,
    )


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
        return json.dumps(
            {"id": 0, "result": {"userAgent": "rodex-control/0.147.0 (Linux)"}}
        )


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
    context = control_module._MutationDispatchContext(
        "dispatch-one", "thread-one", "turn-one"
    )

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
    context = control_module._MutationDispatchContext(
        "dispatch-one", "thread-one", "turn-one"
    )
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
            require_compatible=True,
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
    def __init__(self) -> None:
        self.recv_calls = 0

    def __enter__(self) -> ChatteringReadSocket:
        return self

    def __exit__(self, *_error: object) -> None:
        return None

    def send(self, _message: str) -> None:
        return

    def recv(self, _timeout: float | None = None, *, timeout: float | None = None) -> str:
        self.recv_calls += 1
        if self.recv_calls == 1:
            return json.dumps(
                {"id": 0, "result": {"userAgent": "rodex-control/0.147.0 (Linux)"}}
            )
        if self.recv_calls > 32:
            raise AssertionError("read RPC consumed unbounded unrelated frames")
        return json.dumps({"method": "unrelated/notification", "params": {}})


def test_round1_read_control_rpc_has_an_absolute_deadline(tmp_path: Path) -> None:
    socket = ChatteringReadSocket()
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
    )

    with pytest.raises(RodexControlError, match=r"deadline|timed out"):
        client.inspect(control)

    assert socket.recv_calls < 32


def test_round1_shared_ctrl_c_rechecks_current_attachment_count(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        arguments = command[3:]
        if "display-message" in arguments and any(
            "@rodex_registration_state" in argument for argument in arguments
        ):
            return subprocess.CompletedProcess(command, 0, stdout="1\t%9\n", stderr="")
        if "display-message" in arguments or any(
            "session_attached" in argument for argument in arguments
        ):
            return subprocess.CompletedProcess(command, 0, stdout="2\n", stderr="")
        if arguments[:2] == ["show-options", "-v"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    handle_shared_ctrl_c(
        "tmux",
        _capability(tmp_path / "tmux.sock"),
        "%9",
        "client-one",
        monotonic_nanoseconds=lambda: 10_000_000_000,
        confirmation_token=lambda: "token-one",
        expiry_scheduler=lambda _callback: None,
        runner=runner,
    )

    assert any("session_attached" in " ".join(command) for command in commands)
    assert not any("send-keys" in command for command in commands)


def test_round1_shared_ctrl_c_confirmation_is_atomic_across_callers(
    tmp_path: Path,
) -> None:
    initial_confirmation = json.dumps(
        {
            "armed_at_monotonic_ns": 10_000_000_000,
            "client_name": "client-one",
            "status_token": "warning-token",
        },
        separators=(",", ":"),
    )
    confirmation = initial_confirmation
    readers = Barrier(2)
    state_lock = Lock()
    send_count = 0
    errors: list[BaseException] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal confirmation, send_count
        arguments = command[3:]
        joined = " ".join(arguments)
        if "display-message" in arguments and "@rodex_registration_state" in joined:
            return subprocess.CompletedProcess(command, 0, stdout="1\t%9\n", stderr="")
        if arguments[:1] == ["if-shell"] and "show-options" in joined:
            if CONFIRMATION_OPTION not in joined:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            with state_lock:
                observed = confirmation
            readers.wait(timeout=2)
            return subprocess.CompletedProcess(command, 0, stdout=observed, stderr="")
        if "display-message" in arguments or "session_attached" in joined:
            return subprocess.CompletedProcess(command, 0, stdout="2\n", stderr="")
        if arguments[:2] == ["show-options", "-v"]:
            if arguments[-1] != CONFIRMATION_OPTION:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            with state_lock:
                observed = confirmation
            readers.wait(timeout=2)
            return subprocess.CompletedProcess(command, 0, stdout=observed, stderr="")
        if arguments[:2] == ["set-option", "-u"]:
            with state_lock:
                confirmation = ""
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if arguments[:1] == ["if-shell"] and "send-keys" in joined:
            with state_lock:
                if confirmation == initial_confirmation:
                    confirmation = ""
                    send_count += 1
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if arguments[:1] == ["send-keys"]:
            with state_lock:
                send_count += 1
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def confirm() -> None:
        try:
            handle_shared_ctrl_c(
                "tmux",
                _capability(tmp_path / "tmux.sock"),
                "%9",
                "client-one",
                monotonic_nanoseconds=lambda: 11_000_000_000,
                confirmation_token=lambda: "unused-token",
                expiry_scheduler=lambda _callback: None,
                runner=runner,
            )
        except BaseException as error:
            errors.append(error)

    callers = [Thread(target=confirm) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(3)

    assert all(not caller.is_alive() for caller in callers)
    assert errors == []
    assert send_count == 1
