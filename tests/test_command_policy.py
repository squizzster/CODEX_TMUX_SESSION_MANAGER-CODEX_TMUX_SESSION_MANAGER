"""The private allow-all path cannot execute, log, or approve native commands."""

from __future__ import annotations

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
from threading import Event, Thread

import pytest

from rodex.command_policy import (
    POLICY_MESSAGE_LIMIT,
    POLICY_PROTOCOL,
    POLICY_SOCKET_ENV,
    CommandPolicyBroker,
    CommandPolicyDecision,
    CommandPolicyRequest,
    allow_response,
    encode_policy_message,
    receive_policy_message,
)
from rodex.command_policy_configuration import caller_bypasses_hook_trust, policy_helper_command, policy_hook_override
from rodex.command_policy_hook import native_hook_response, run_hook
from rodex.runtime_peer import CurrentProcessOwner

RUNTIME = "0000000000000001"


def hook_event(command="find . -maxdepth 0 -print"):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "cwd": "/workspace",
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


def exchange(path, runtime=RUNTIME, command="find . -maxdepth 0 -print"):
    request = CommandPolicyRequest.from_hook(runtime, hook_event(command))
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
