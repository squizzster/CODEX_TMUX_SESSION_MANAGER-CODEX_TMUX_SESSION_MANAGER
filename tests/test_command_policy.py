"""The private policy path cannot execute, log, or approve native commands."""

from __future__ import annotations

import fcntl
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

import rodex.command_policy as policy_module
from rodex.command_policy import (
    POLICY_MESSAGE_LIMIT,
    POLICY_PROTOCOL,
    POLICY_SOCKET_ENV,
    SHORT_SLEEP_DENIAL_REASON,
    CommandPolicyBroker,
    CommandPolicyDecision,
    CommandPolicyRequest,
    allow_response,
    encode_policy_message,
    evaluate_command_policy,
    receive_policy_message,
)
from rodex.command_policy_configuration import caller_bypasses_hook_trust, policy_helper_command, policy_hook_override
from rodex.command_policy_hook import native_hook_response, run_hook
from rodex.runtime_peer import CurrentProcessOwner

RUNTIME = "0000000000000001"


def hook_event(command="printf unchanged", *, cwd="/workspace"):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "cwd": cwd,
        "permission_mode": "default",
        "tool_name": "Bash",
        "tool_input": {
            "command": command,
            "cwd": "/workspace",
            "timeout_ms": 123,
            "sandbox_permissions": "require_escalated",
            "justification": "caller intent",
        },
    }


@pytest.fixture
def policy_root():
    with tempfile.TemporaryDirectory(prefix="rdx-policy-") as directory:
        yield Path(directory)


def exchange(path, runtime=RUNTIME, command="printf unchanged", *, cwd="/workspace"):
    request = CommandPolicyRequest.from_hook(runtime, hook_event(command, cwd=cwd))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        connection.sendall(encode_policy_message(request.to_payload()))
        return receive_policy_message(connection)


def test_request_preserves_all_input_and_context_without_shell_parsing():
    event = hook_event("find . && printf '%s' \"#{host}\"\nsha512sum --version")
    request = CommandPolicyRequest.from_hook(RUNTIME, event)
    assert CommandPolicyRequest.from_payload(request.to_payload()) == request
    assert request.tool_input == event["tool_input"]
    assert request.to_payload()["command"] == {"kind": "shell", "text": event["tool_input"]["command"]}


@pytest.mark.parametrize(
    "command",
    [
        "sleep 0",
        "sleep 00",
        "sleep 1",
        "sleep 09",
        "sleep 9",
        "sleep 5;",
        "  sleep 5  ;",
        "\tsleep\t5\t;\t",
    ],
)
def test_policy_denies_standalone_integer_sleeps_below_ten_with_feedback(command):
    request = CommandPolicyRequest.from_hook(RUNTIME, hook_event(command))

    assert evaluate_command_policy(request) == CommandPolicyDecision("deny", reason=SHORT_SLEEP_DENIAL_REASON)


def test_find_rg_customer_policy_rejects_before_later_policy_rules(monkeypatch):
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    request = CommandPolicyRequest.from_hook(RUNTIME, hook_event("find /; sleep 5", cwd="/"))

    decision = evaluate_command_policy(request)

    assert decision.action == "deny"
    assert decision.reason is not None
    assert decision.reason.startswith("Rejected: Stage 1: Directory input '/'")


def test_non_search_continues_to_the_existing_sleep_policy(monkeypatch):
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    request = CommandPolicyRequest.from_hook(RUNTIME, hook_event("sleep 5"))

    assert evaluate_command_policy(request) == CommandPolicyDecision("deny", reason=SHORT_SLEEP_DENIAL_REASON)


def test_allowed_search_continues_through_the_policy_pipeline(monkeypatch):
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    request = CommandPolicyRequest.from_hook(RUNTIME, hook_event("rg --version"))

    assert evaluate_command_policy(request) == CommandPolicyDecision("allow")


def test_search_policy_verifies_a_bounded_regular_file(monkeypatch, tmp_path):
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    (tmp_path / "input.txt").write_text("needle\n")
    request = CommandPolicyRequest.from_hook(
        RUNTIME,
        hook_event("rg needle input.txt", cwd=str(tmp_path)),
    )

    assert evaluate_command_policy(request) == CommandPolicyDecision("allow")


def test_search_denial_reason_is_bounded_before_native_encoding(monkeypatch):
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    request = CommandPolicyRequest.from_hook(RUNTIME, hook_event("rg needle /" + "x" * 9000, cwd="/"))

    decision = evaluate_command_policy(request)

    assert decision.action == "deny"
    assert decision.reason is not None
    assert len(decision.reason) == 8192
    assert decision.reason.endswith("...")


@pytest.mark.parametrize(
    "command",
    [
        "sleep 10",
        "sleep 010",
        " sleep 10 ; ",
        "sleep 232;",
        "sleep 9999999999999999999999999999999999999999",
        "sleep 9.0",
        "sleep 9s",
        "sleep 9 && printf done",
        "printf before; sleep 9",
        "/bin/sleep 9;",
        "sleep 9;;",
        "sleep 9; # comment",
    ],
)
def test_policy_allows_ten_or_more_and_commands_outside_the_standalone_integer_sleep_shape(command):
    request = CommandPolicyRequest.from_hook(RUNTIME, hook_event(command))

    assert evaluate_command_policy(request) == CommandPolicyDecision("allow")


def test_default_broker_returns_short_sleep_denial_without_an_allowed_status(policy_root):
    path = policy_root / f"policy-{RUNTIME}.sock"
    broker = CommandPolicyBroker(path, RUNTIME, lambda _command: pytest.fail("denied command published as allowed"))
    broker.start()
    broker.bind_app_server(CurrentProcessOwner())
    try:
        assert exchange(path, command="sleep 9") == CommandPolicyDecision(
            "deny", reason=SHORT_SLEEP_DENIAL_REASON
        ).to_payload(RUNTIME)
    finally:
        broker.close()


def test_broker_accepts_and_stops_with_high_numbered_listener(policy_root, monkeypatch):
    path = policy_root / "high-fd.sock"
    broker = CommandPolicyBroker(path, RUNTIME, lambda _command: None, environment={})
    open_endpoint = broker._endpoint.open
    retained = []

    def open_high_listener():
        listener = open_endpoint()
        high_listener = socket.socket(fileno=fcntl.fcntl(listener.fileno(), fcntl.F_DUPFD_CLOEXEC, 1100))
        retained.append(high_listener)
        return high_listener

    monkeypatch.setattr(broker._endpoint, "open", open_high_listener)
    try:
        broker.start()
        broker.bind_app_server(CurrentProcessOwner())
        assert exchange(path, command="sleep 9")["decision"] == "deny"
    finally:
        broker.close()
        for listener in retained:
            listener.close()
    assert broker._thread is not None and not broker._thread.is_alive()


@pytest.mark.parametrize("config", [None, "", "/private/config"])
@pytest.mark.parametrize("no_config", [False, True])
def test_brokers_keep_independent_reservation_environments(policy_root, monkeypatch, config, no_config):
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", "/daemon/config")
    (policy_root / "input.txt").write_text("needle\n")
    environment = {} if config is None else {"RIPGREP_CONFIG_PATH": config}
    paths = [policy_root / "first.sock", policy_root / "second.sock"]
    brokers = [
        CommandPolicyBroker(paths[0], RUNTIME, lambda _command: None, environment=environment),
        CommandPolicyBroker(paths[1], RUNTIME, lambda _command: None, environment={}),
    ]
    # A caller's later mutation cannot alter either runtime's baseline.
    environment["RIPGREP_CONFIG_PATH"] = "/changed/config"
    command = "rg " + ("--no-config " if no_config else "") + "needle input.txt"
    try:
        for broker in brokers:
            broker.start()
            broker.bind_app_server(CurrentProcessOwner())
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(exchange, path, command=command, cwd=str(policy_root)) for path in paths]
            results = [future.result() for future in futures]
        assert results[0]["decision"] == ("deny" if config and not no_config else "allow")
        assert results[1]["decision"] == "allow"
    finally:
        for broker in brokers:
            broker.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", "future"),
        ("tool_name", "write_stdin"),
        ("runtime_id", "another-runtime"),
        ("turn_id", ""),
        ("cwd", "\x00"),
        ("permission_mode", None),
        ("tool_input", {"command": ["find"]}),
        ("command", {"kind": "shell", "text": "different"}),
        ("unexpected", True),
    ],
)
def test_request_rejects_alternate_or_inconsistent_contracts(field, value):
    payload = CommandPolicyRequest.from_hook(RUNTIME, hook_event()).to_payload()
    payload[field] = value
    with pytest.raises(ValueError):
        CommandPolicyRequest.from_payload(payload)


@pytest.mark.parametrize("content", [b"{}\ntrailing", b"[]\n", b"invalid\n", b"{}", b"x" * POLICY_MESSAGE_LIMIT])
def test_frames_require_bounded_complete_object(content):
    sender, receiver = socket.socketpair()

    def send():
        with sender:
            try:
                sender.sendall(content)
                sender.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    thread = Thread(target=send)
    thread.start()
    try:
        with receiver, pytest.raises(ValueError):
            receive_policy_message(receiver)
    finally:
        thread.join(timeout=2)


def test_broker_replies_before_slow_status_and_never_executes_command(policy_root):
    entered, release = Event(), Event()

    def status(_command):
        entered.set()
        assert release.wait(3)

    path = policy_root / f"policy-{RUNTIME}.sock"
    broker = CommandPolicyBroker(path, RUNTIME, status)
    broker.start()
    broker.bind_app_server(CurrentProcessOwner())
    marker = policy_root / "not-executed"
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert exchange(path, command=f"touch {marker}") == allow_response(RUNTIME)
        assert entered.wait(2)
        assert not marker.exists()
    finally:
        release.set()
        broker.close()
    assert not path.exists()


def test_concurrent_brokers_reject_other_runtime_and_stop_workers(policy_root):
    observed = [[], []]
    brokers = []
    for index in range(2):
        runtime = f"{index + 1:016x}"
        broker = CommandPolicyBroker(policy_root / f"policy-{runtime}.sock", runtime, observed[index].append)
        broker.start()
        broker.bind_app_server(CurrentProcessOwner())
        brokers.append(broker)
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    exchange,
                    policy_root / f"policy-{index % 2 + 1:016x}.sock",
                    f"{index % 2 + 1:016x}",
                    f"command-{index}",
                )
                for index in range(12)
            ]
            assert all(future.result()["decision"] == "allow" for future in futures)
        with pytest.raises((ValueError, OSError)):
            exchange(policy_root / f"policy-{RUNTIME}.sock", "0000000000000002", "wrong-runtime")
        deadline = time.monotonic() + 2
        while sum(map(len, observed)) < 12 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert set(observed[0]) == {f"command-{index}" for index in range(0, 12, 2)}
        assert set(observed[1]) == {f"command-{index}" for index in range(1, 12, 2)}
    finally:
        for broker in brokers:
            broker.close()
    assert all(not broker._workers for broker in brokers)


def test_broker_close_shares_one_deadline_across_listener_and_all_workers(policy_root, monkeypatch, caplog):
    clock = [100.0]
    joins = []

    class StalledThread:
        ident = 1

        def __init__(self, *, exit_after=None):
            self.exit_after = exit_after

        def join(self, timeout):
            joins.append(timeout)
            clock[0] += timeout if self.exit_after is None else min(timeout, self.exit_after)

        def is_alive(self):
            return self.exit_after is None

    broker = CommandPolicyBroker(policy_root / "deadline.sock", RUNTIME, lambda _command: None)
    broker._thread = StalledThread(exit_after=0.4)
    broker._workers = {StalledThread() for _ in range(policy_module.POLICY_MAX_WORKERS)}
    monkeypatch.setattr(policy_module.time, "monotonic", lambda: clock[0])
    stopped = broker.close()

    assert clock[0] == pytest.approx(102.0)
    assert len(joins) == policy_module.POLICY_MAX_WORKERS + 1
    assert all(timeout >= 0 for timeout in joins)
    assert stopped is False
    assert len(broker._workers) == policy_module.POLICY_MAX_WORKERS
    assert "remaining_workers=16" in caplog.text


def test_broker_close_bounds_wait_but_retains_all_blocked_workers_until_release(policy_root, monkeypatch, caplog):
    entered, release = Event(), Event()
    admission_lock = Lock()
    admissions = []
    statuses = []

    def stalled_policy(request):
        with admission_lock:
            admissions.append(request)
            if len(admissions) == policy_module.POLICY_MAX_WORKERS:
                entered.set()
        assert release.wait(5)
        return CommandPolicyDecision("allow")

    monkeypatch.setattr(policy_module, "POLICY_CLOSE_WAIT_SECONDS", 0.1)
    path = policy_root / "stalled.sock"
    broker = CommandPolicyBroker(path, RUNTIME, statuses.append, policy=stalled_policy)
    clients = []
    workers = ()
    broker.start()
    broker.bind_app_server(CurrentProcessOwner())
    try:
        request = CommandPolicyRequest.from_hook(RUNTIME, hook_event("private-command-must-not-be-logged"))
        for _ in range(policy_module.POLICY_MAX_WORKERS):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            clients.append(connection)
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(encode_policy_message(request.to_payload()))
        assert entered.wait(2)
        # Client abandonment does not cancel an in-process policy/filesystem call.
        for connection in clients:
            connection.close()
        with broker._lock:
            workers = tuple(broker._workers)
        assert len(workers) == policy_module.POLICY_MAX_WORKERS
        with pytest.raises((ValueError, OSError)):
            exchange(path)
        started = time.monotonic()
        assert broker.close() is False
        assert time.monotonic() - started < 1.0
        assert not path.exists()
        assert broker._thread is not None and not broker._thread.is_alive()
        assert all(worker.is_alive() for worker in workers)
        assert len(broker._workers) == policy_module.POLICY_MAX_WORKERS
        assert "remaining_workers=16" in caplog.text
        assert "private-command-must-not-be-logged" not in caplog.text
    finally:
        release.set()
        for connection in clients:
            connection.close()
        for worker in workers:
            worker.join(timeout=2)
        assert broker.close() is True
    assert not broker._workers
    assert statuses == []
    replacement = CommandPolicyBroker(path, RUNTIME, lambda _command: None, environment={})
    try:
        replacement.start()
        replacement.bind_app_server(CurrentProcessOwner())
        assert exchange(path) == allow_response(RUNTIME)
    finally:
        assert replacement.close() is True


def test_broker_close_wakes_incomplete_requests_within_shared_budget(policy_root, monkeypatch):
    monkeypatch.setattr(policy_module, "POLICY_CLOSE_WAIT_SECONDS", 0.1)
    path = policy_root / "incomplete.sock"
    broker = CommandPolicyBroker(path, RUNTIME, lambda _command: pytest.fail("incomplete request must not publish"))
    admitted = Event()
    original_serve = broker._serve

    def serve(connection):
        admitted.set()
        original_serve(connection)

    monkeypatch.setattr(broker, "_serve", serve)
    broker.start()
    broker.bind_app_server(CurrentProcessOwner())
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(path))
            connection.sendall(b'{"incomplete":')
            assert admitted.wait(2)
            started = time.monotonic()
            assert broker.close() is True
            assert time.monotonic() - started < 1.0
    finally:
        broker.close()
    assert not broker._workers


def test_broker_requires_exact_app_server_descendant(policy_root):
    path = policy_root / f"policy-{RUNTIME}.sock"
    broker = CommandPolicyBroker(path, RUNTIME, lambda _command: pytest.fail("foreign process admitted"))
    broker.start()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    broker.bind_app_server(process)
    try:
        with pytest.raises((ValueError, OSError)):
            exchange(path)
    finally:
        process.terminate()
        process.wait(timeout=3)
        broker.close()


def test_retained_module_helper_is_empty_and_does_not_execute_or_persist_input(policy_root, capsys):
    path = policy_root / f"policy-{RUNTIME}.sock"
    seen = []
    broker = CommandPolicyBroker(path, RUNTIME, seen.append)
    broker.start()
    broker.bind_app_server(CurrentProcessOwner())
    sentinel = "RODEX_COMMAND_CONTENT_PRIVACY_18ea5c"
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "rodex.command_policy_hook"],
            input=json.dumps(hook_event(f"touch {policy_root / sentinel}")),
            env={**os.environ, POLICY_SOCKET_ENV: str(path)},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == 0
        assert completed.stdout == completed.stderr == ""
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(seen) == 1 and sentinel in seen[0]
        assert not (policy_root / sentinel).exists()
    finally:
        broker.close()
    assert capsys.readouterr().out == ""
    assert not any(sentinel.encode() in path.read_bytes() for path in policy_root.iterdir() if path.is_file())


@pytest.mark.parametrize(
    "content", [b"", b"not-json", b"[]", b"x" * (POLICY_MESSAGE_LIMIT + 1), json.dumps(hook_event()).encode()]
)
def test_helper_fails_open_without_output_on_bad_or_unavailable_broker(policy_root, content, capsys):
    run_hook(io.BytesIO(content), str(policy_root / f"policy-{RUNTIME}.sock"))
    run_hook(io.BytesIO(content), None)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "arguments,bypass",
    [
        ((), None),
        (("--dangerously-bypass-hook-trust",), True),
        (("--", "--dangerously-bypass-hook-trust"), None),
        (("-c", "bypass_hook_trust=true"), True),
        (("--config=bypass_hook_trust=false",), False),
        (("-cbypass_hook_trust=true",), True),
        (("--model", "example", "--dangerously-bypass-hook-trust", "prompt"), True),
        (("--config", "bypass_hook_trust=true", "--config", "bypass_hook_trust=false"), False),
        (("resume", "fixture-thread", "--dangerously-bypass-hook-trust"), True),
        (("resume", "fixture-thread", "--", "--dangerously-bypass-hook-trust"), None),
    ],
)
def test_bypass_is_only_explicit_native_intent(arguments, bypass):
    assert caller_bypasses_hook_trust(arguments) is bypass


def test_hook_definition_uses_retained_interpreter_and_no_session_values():
    definition = policy_hook_override("/retained/python/bin/python")
    assert (
        policy_helper_command("/retained/python/bin/python")
        == "/retained/python/bin/python -I -m rodex.command_policy_hook"
    )
    assert 'matcher="^Bash$"' in definition
    assert "timeout=3" in definition
    assert "policy-" not in definition
    assert "bypass" not in definition
    assert POLICY_PROTOCOL == "rodex-command-policy-v1"


@pytest.mark.parametrize(
    "decision",
    [
        CommandPolicyDecision("allow"),
        CommandPolicyDecision("deny", reason="fixture denied"),
        CommandPolicyDecision("rewrite", command="printf rewritten"),
    ],
)
def test_decision_contract_and_real_helper_encoding(policy_root, decision):
    assert CommandPolicyDecision.from_payload(decision.to_payload(RUNTIME), RUNTIME) == decision
    original = hook_event()
    request = CommandPolicyRequest.from_hook(RUNTIME, original)
    path = policy_root / f"policy-{RUNTIME}.sock"
    broker = CommandPolicyBroker(path, RUNTIME, lambda _command: None, policy=lambda _request: decision)
    broker.start()
    broker.bind_app_server(CurrentProcessOwner())
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "rodex.command_policy_hook"],
            input=json.dumps(original),
            env={**os.environ, POLICY_SOCKET_ENV: str(path)},
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == 0 and completed.stderr == ""
        expected = native_hook_response(request, decision)
        if expected is None:
            assert completed.stdout == ""
        else:
            assert json.loads(completed.stdout) == expected
        if decision.action == "rewrite":
            rewritten = expected["hookSpecificOutput"]["updatedInput"]
            assert rewritten == {**original["tool_input"], "command": "printf rewritten"}
            assert request.tool_input == original["tool_input"]
        elif decision.action == "deny":
            assert expected == {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "fixture denied",
                }
            }
    finally:
        broker.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": "future"},
        {"runtime_id": "0000000000000002"},
        {"decision": "ask"},
        {"decision": "deny"},
        {"decision": "rewrite", "command": None},
        {"extra": True},
        {"decision": "rewrite", "command": "x", "tool_input": {"sandbox_permissions": "require_escalated"}},
        {"decision": "deny", "reason": "no", "command": "printf bypass"},
        {"reason": "not-an-allow-field"},
    ],
)
def test_response_rejects_wrong_runtime_shape_and_permission_mutation(changes):
    with pytest.raises(ValueError):
        CommandPolicyDecision.from_payload({**allow_response(RUNTIME), **changes}, RUNTIME)
