"""Real native hook/sandbox behavior, driven by an offline deterministic model."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import pyte
import pytest
from command_policy_native_fixture import native_policy_fixture
from test_managed_startup import RodexTerminalClient, _stop_fixture_daemon
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from rodex.command_policy import SHORT_SLEEP_DENIAL_REASON, CommandPolicyDecision
from rodex.command_policy_configuration import (
    POLICY_INACTIVE_SETUP,
    policy_hook_admission,
    policy_hook_inactive_reason,
)
from rodex.command_policy_status import POLICY_INACTIVE_OPTION, POLICY_STATUS_SECONDS

USER_HOOK_CONFIGURATION = (
    '\n[hooks]\nPreToolUse=[{matcher="^Bash$",hooks=[{type="command",command="/bin/true",timeout=3}]}]\n'
)


@pytest.fixture
def native(request):
    if not shutil.which("codex"):
        if request.config.getoption("--require-live-startup"):
            pytest.fail("installed Codex is required")
        pytest.skip("installed Codex is required")
    with native_policy_fixture() as fixture:
        yield fixture


def trust_hook(client, hook):
    client.rpc(
        "config/batchWrite",
        {
            "edits": [
                {
                    "keyPath": "hooks.state",
                    "value": {hook["key"]: {"trusted_hash": hook["currentHash"]}},
                    "mergeStrategy": "upsert",
                }
            ],
            "reloadUserConfig": True,
        },
    )
    return hook


def trust_fixture_hook(client, workspace):
    hooks = client.rpc("hooks/list", {"cwds": [str(workspace)]})["data"][0]["hooks"]
    return trust_hook(client, next(hook for hook in hooks if hook["source"] == "sessionFlags"))


def install_user_hook(native):
    config_path = native.codex_home / "config.toml"
    config_path.write_text(config_path.read_text() + USER_HOOK_CONFIGURATION)


@pytest.mark.live_startup
@pytest.mark.parametrize(
    "sandbox,approval",
    [
        ("read-only", "never"),
        ("workspace-write", "on-request"),
        ("danger-full-access", "never"),
    ],
)
def test_native_allow_uses_original_security_and_no_model_context(native, sandbox, approval):
    with native.app_server() as (client, observed, process, path):
        admission = policy_hook_admission(path, process, native.workspace, ())
        assert admission.inactive_reason is None
        assert admission.add_hook_trust_bypass
        first = trust_fixture_hook(client, native.workspace)
    # A distinct App Server/socket/runtime must reuse native trust with no bypass.
    with native.app_server() as (client, observed, process, path):
        current = client.rpc("hooks/list", {"cwds": [str(native.workspace)]})["data"][0]["hooks"][0]
        assert current["currentHash"] == first["currentHash"] and current["trustStatus"] == "trusted"
        assert policy_hook_inactive_reason(path, process, native.workspace, ()) is None
        started = client.rpc(
            "thread/start", {"cwd": str(native.workspace), "sandbox": sandbox, "approvalPolicy": approval}
        )
        assert started["approvalPolicy"] == approval
        assert (
            started["sandbox"]["type"]
            == {"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}[
                sandbox
            ]
        )
        client.rpc(
            "turn/start",
            {"threadId": started["thread"]["id"], "input": [{"type": "text", "text": "Run the fixture command."}]},
        )
        client.complete_turn()
        assert observed == ["find . -maxdepth 0 -print"]
        commands = [
            e["params"]["item"]
            for e in client.events
            if e.get("method") == "item/completed"
            and e.get("params", {}).get("item", {}).get("type") == "commandExecution"
        ]
        assert commands and commands[0]["exitCode"] == 0
        hooks = [e["params"]["run"] for e in client.events if e.get("method") == "hook/completed"]
        assert len(hooks) == 1 and hooks[0]["entries"] == [] and hooks[0]["status"] == "completed"
        assert "Rodex policy allowed" not in json.dumps(native.model.requests)


@pytest.mark.live_startup
def test_native_extra_enabled_user_hook_keeps_manual_policy_setup(native):
    install_user_hook(native)

    with native.app_server() as (client, _observed, process, path):
        hooks = client.rpc("hooks/list", {"cwds": [str(native.workspace)]})["data"][0]["hooks"]
        assert {hook["source"] for hook in hooks} == {"sessionFlags", "user"}
        admission = policy_hook_admission(path, process, native.workspace, ())

    assert admission.inactive_reason == POLICY_INACTIVE_SETUP
    assert not admission.add_hook_trust_bypass


@pytest.mark.live_startup
def test_extra_user_hook_without_rodex_trust_shows_bounded_prefix_aware_notice(native):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    install_user_hook(native)
    with native.app_server() as (client, _observed, _process, _path):
        hooks = client.rpc("hooks/list", {"cwds": [str(native.workspace)]})["data"][0]["hooks"]
        trust_hook(client, next(hook for hook in hooks if hook["source"] == "user"))
        assert next(hook for hook in hooks if hook["source"] == "sessionFlags")["trustStatus"] == "untrusted"

    rodex = str(Path(__file__).parents[1] / "rodex")
    runtime_root = Path(native.environment["RODEX_RUNTIME_DIR"])
    try:
        with RodexTerminalClient([rodex], native.environment, native.workspace) as terminal:
            name = terminal.wait_for_attach()
            await_screen(terminal, POLICY_INACTIVE_SETUP)
            inspected = subprocess.run(
                [rodex, "_inspect", name, "--json"],
                env=native.environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            identity = json.loads(inspected.stdout)
            socket_path = runtime_root / f"tmux-v5-{identity['runtime']['runtime_id']}.sock"

            native_key(terminal, b"\x02")
            await_screen(terminal, "CTRL-B MODE")
            native_key(terminal, b"\x1b")

            deadline = time.monotonic() + POLICY_STATUS_SECONDS + 3
            status_format = POLICY_INACTIVE_SETUP
            while POLICY_INACTIVE_SETUP in status_format and time.monotonic() < deadline:
                terminal.poll()
                status_format = subprocess.run(
                    ["tmux", "-S", str(socket_path), "show-options", "-v", "-t", "%0", "status-format[0]"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout
                if POLICY_INACTIVE_SETUP in status_format:
                    time.sleep(0.05)
            assert POLICY_INACTIVE_SETUP not in status_format
            rendered = await_screen(terminal, f"Rodex: {name}")
            assert "#[" not in rendered.splitlines()[-1]
            inactive = subprocess.run(
                ["tmux", "-S", str(socket_path), "show-options", "-q", "-v", "-t", "%0", POLICY_INACTIVE_OPTION],
                capture_output=True,
                text=True,
                timeout=3,
            )
            assert inactive.stdout.strip() == POLICY_INACTIVE_SETUP
    finally:
        _stop_fixture_daemon(runtime_root)


@pytest.mark.live_startup
def test_native_short_sleep_policy_denies_with_model_visible_feedback(native):
    native.model.tool_input = {"cmd": "sleep 9"}
    with native.app_server() as (client, observed, _process, _path):
        started = client.rpc(
            "thread/start",
            {
                "cwd": str(native.workspace),
                "sandbox": "danger-full-access",
                "approvalPolicy": "never",
                "config": {"bypass_hook_trust": True},
            },
        )
        client.rpc(
            "turn/start",
            {"threadId": started["thread"]["id"], "input": [{"type": "text", "text": "Run the fixture command."}]},
        )
        client.complete_turn()

        assert observed == []
        assert SHORT_SLEEP_DENIAL_REASON in json.dumps(native.model.requests)


@pytest.mark.live_startup
@pytest.mark.parametrize("injection,disabled", [(False, False), (True, True)])
def test_unmanaged_and_explicitly_disabled_hooks_do_not_intercept(native, injection, disabled):
    with native.app_server(injection=injection) as (client, observed, _process, _path):
        started = client.rpc(
            "thread/start",
            {
                "cwd": str(native.workspace),
                "sandbox": "danger-full-access",
                "approvalPolicy": "never",
                "config": {"bypass_hook_trust": True, "features.hooks": not disabled},
            },
        )
        client.rpc(
            "turn/start",
            {"threadId": started["thread"]["id"], "input": [{"type": "text", "text": "Run the fixture command."}]},
        )
        client.complete_turn()
        assert observed == []


@pytest.mark.live_startup
def test_hook_allow_cannot_grant_a_read_only_write(native):
    marker = native.workspace / "native-policy-write-marker"
    native.model.tool_input = {"cmd": f"printf denied > {marker}"}
    with native.app_server() as (client, observed, _process, _path):
        started = client.rpc(
            "thread/start",
            {
                "cwd": str(native.workspace),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "config": {"bypass_hook_trust": True},
            },
        )
        client.rpc(
            "turn/start",
            {"threadId": started["thread"]["id"], "input": [{"type": "text", "text": "Run the fixture command."}]},
        )
        client.complete_turn()
        assert observed == [native.model.tool_input["cmd"]]
        assert not marker.exists()


@pytest.mark.live_startup
@pytest.mark.parametrize(
    "action,sandbox", [("deny", "danger-full-access"), ("rewrite", "danger-full-access"), ("rewrite", "read-only")]
)
def test_native_future_decisions_return_to_codex_execution(native, action, sandbox):
    original = native.workspace / "original-marker"
    rewritten = native.workspace / "rewritten-marker"
    native.model.tool_input = {"cmd": f"printf original > {original}"}
    decision = (
        CommandPolicyDecision("deny", reason="Fixture policy denied this command")
        if action == "deny"
        else CommandPolicyDecision("rewrite", command=f"printf rewritten > {rewritten}")
    )
    with native.app_server(policy=lambda _request: decision) as (client, _observed, _process, _path):
        started = client.rpc(
            "thread/start",
            {
                "cwd": str(native.workspace),
                "sandbox": sandbox,
                "approvalPolicy": "never",
                "config": {"bypass_hook_trust": True},
            },
        )
        client.rpc(
            "turn/start",
            {"threadId": started["thread"]["id"], "input": [{"type": "text", "text": "Run the fixture command."}]},
        )
        client.complete_turn()
        assert not original.exists()
        assert rewritten.exists() is (action == "rewrite" and sandbox == "danger-full-access")
        if action == "deny":
            assert decision.reason in json.dumps(native.model.requests)


@pytest.mark.live_startup
def test_native_allow_does_not_suppress_approval(native):
    native.model.tool_input = {
        "cmd": "printf approval-required",
        "sandbox_permissions": "require_escalated",
        "justification": "Fixture requests native approval",
    }
    with native.app_server() as (client, observed, _process, _path):
        started = client.rpc(
            "thread/start",
            {
                "cwd": str(native.workspace),
                "sandbox": "read-only",
                "approvalPolicy": "on-request",
                "config": {"bypass_hook_trust": True},
            },
        )
        client.rpc(
            "turn/start",
            {"threadId": started["thread"]["id"], "input": [{"type": "text", "text": "Run the fixture command."}]},
        )
        client.complete_turn(approval_decision="decline")
        assert observed == [native.model.tool_input["cmd"]]
        assert any(event.get("method") == "item/commandExecution/requestApproval" for event in client.events)


def await_screen(client, expected, timeout=15):
    deadline = time.monotonic() + timeout
    screen = pyte.Screen(120, 36)
    stream = pyte.ByteStream(screen)
    offset = 0
    while time.monotonic() < deadline:
        client.poll()
        stream.feed(client.output[offset:])
        offset = len(client.output)
        rendered = "\n".join(screen.display)
        if expected in rendered:
            return rendered
        assert client.exit_code is None, rendered
    pytest.fail(f"native UI did not display {expected!r}: {rendered}")


def native_key(client, key):
    # A rendered menu can precede installation of its native input receiver.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        client.poll()
    os.write(client.terminal, key)


@pytest.mark.live_startup
def test_managed_trust_once_two_isolated_sessions_and_private_status(native):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    project = Path(__file__).parents[1]
    rodex = str(project / "rodex")
    environment = native.environment
    runtime_root = Path(environment["RODEX_RUNTIME_DIR"])
    sentinel = f"RODEX_POLICY_CONTENT_{uuid.uuid4().hex}"
    native.model.tool_input = {"cmd": f"find . -maxdepth 0 -print; printf {sentinel}"}
    try:
        # First-use trust is performed through native /hooks, only in the fixture's
        # private Codex home. No programmatic trust or bypass in Rodex itself.
        with RodexTerminalClient([rodex, "_policy-setup"], environment, native.workspace) as setup:
            await_screen(setup, "Hooks need review")
            # The minimal PTY fixture does not implement Codex's startup terminal
            # capability negotiation. Let that native timeout finish before keys.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                setup.poll()
            native_key(setup, b"\r")
            await_screen(setup, "Lifecycle hooks")
            native_key(setup, b"\r")
            await_screen(setup, "PreToolUse hooks")
            native_key(setup, b"t")
            await_screen(setup, "Trusted")
        with ExitStack() as stack:
            sessions = []
            for _ in range(2):
                client = stack.enter_context(RodexTerminalClient([rodex], environment, native.workspace))
                name = client.wait_for_attach()
                await_screen(client, "OpenAI Codex")
                inspected = subprocess.run(
                    [rodex, "_inspect", name, "--json"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                identity = json.loads(inspected.stdout)
                assert identity["ok"]
                sessions.append((client, name, identity))
            assert sessions[0][2]["runtime"]["runtime_id"] != sessions[1][2]["runtime"]["runtime_id"]
            first, name, identity = sessions[0]
            dispatched = subprocess.run(
                [rodex, "_start", name, "--dispatch", f"rodex:dispatch:{uuid.uuid4()}", "--stdin", "--json"],
                input="Run the fixture command.",
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            dispatch = json.loads(dispatched.stdout)
            assert dispatch["ok"], dispatch
            await_screen(first, "Rodex policy allowed:")
            first_socket = runtime_root / f"tmux-v5-{identity['runtime']['runtime_id']}.sock"
            second_socket = runtime_root / f"tmux-v5-{sessions[1][2]['runtime']['runtime_id']}.sock"

            def status(path):
                return subprocess.run(
                    ["tmux", "-S", str(path), "show-options", "-v", "status-format[0]"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=3,
                ).stdout

            assert sentinel in status(first_socket)
            assert sentinel not in status(second_socket)
            completed = subprocess.run(
                [rodex, "_wait", name, "--turn", dispatch["codex"]["turn_id"], "--timeout", "10s", "--json"],
                env=environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert json.loads(completed.stdout)["ok"]
            # Command text naturally belongs to Codex's native transcript. Rodex's
            # SQL, runtime logs/receipts and diagnostic files must not duplicate it.
            for directory in (runtime_root, native.root / "state/rodex"):
                for path in directory.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        assert sentinel.encode() not in path.read_bytes(), path
            assert "Rodex policy allowed" not in json.dumps(native.model.requests)
    finally:
        _stop_fixture_daemon(runtime_root)


@pytest.mark.live_startup
def test_fresh_managed_session_auto_bypasses_only_rodex_hook_and_denies_short_sleep(native):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    rodex = str(Path(__file__).parents[1] / "rodex")
    runtime_root = Path(native.environment["RODEX_RUNTIME_DIR"])
    native.model.tool_input = {"cmd": "sleep 5"}
    try:
        with RodexTerminalClient([rodex], native.environment, native.workspace) as client:
            name = client.wait_for_attach()
            await_screen(client, "OpenAI Codex")
            inspected = subprocess.run(
                [rodex, "_inspect", name, "--json"],
                env=native.environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            identity = json.loads(inspected.stdout)
            assert identity["ok"]
            dispatched = subprocess.run(
                [rodex, "_start", name, "--dispatch", f"rodex:dispatch:{uuid.uuid4()}", "--stdin", "--json"],
                input="Run the fixture command.",
                env=native.environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            dispatch = json.loads(dispatched.stdout)
            assert dispatch["ok"], dispatch
            completed = subprocess.run(
                [rodex, "_wait", name, "--turn", dispatch["codex"]["turn_id"], "--timeout", "4s", "--json"],
                env=native.environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert json.loads(completed.stdout)["ok"]
            assert SHORT_SLEEP_DENIAL_REASON in json.dumps(native.model.requests)
            socket_path = runtime_root / f"tmux-v5-{identity['runtime']['runtime_id']}.sock"
            inactive = subprocess.run(
                ["tmux", "-S", str(socket_path), "show-options", "-q", "-v", "-t", "%0", POLICY_INACTIVE_OPTION],
                capture_output=True,
                text=True,
                timeout=3,
            )
            assert inactive.stdout == ""
    finally:
        _stop_fixture_daemon(runtime_root)


@pytest.mark.live_startup
def test_remote_tui_transports_explicit_hook_bypass_without_an_argv_adapter(native):
    captured = []
    with native.app_server() as (_client, _observed, _process, app):

        def relay(downstream):
            with unix_connect(str(app), uri="ws://localhost/rpc", close_timeout=1) as upstream:

                def receive():
                    try:
                        for frame in upstream:
                            downstream.send(frame)
                    except ConnectionClosed:
                        pass
                    finally:
                        downstream.close()

                worker = threading.Thread(target=receive, daemon=True)
                worker.start()
                try:
                    for frame in downstream:
                        captured.append(json.loads(frame))
                        upstream.send(frame)
                except ConnectionClosed:
                    pass
                finally:
                    upstream.close()
                    worker.join(timeout=3)

        proxy = native.root / "capture.sock"
        with unix_serve(relay, str(proxy), close_timeout=1) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                command = [
                    native.environment["RODEX_CODEX_BINARY"],
                    "--remote",
                    f"unix://{proxy}",
                    "--dangerously-bypass-hook-trust",
                    "--sandbox",
                    "workspace-write",
                    "--ask-for-approval",
                    "on-request",
                ]
                with RodexTerminalClient(command, native.environment, native.workspace) as tui:
                    deadline = time.monotonic() + 15
                    while not any(frame.get("method") == "thread/start" for frame in captured):
                        tui.poll()
                        assert time.monotonic() < deadline, tui.output.decode(errors="replace")
                    params = next(frame["params"] for frame in captured if frame.get("method") == "thread/start")
                    assert params["config"]["bypass_hook_trust"] is True
                    assert params["sandbox"] == "workspace-write"
                    assert params["approvalPolicy"] == "on-request"
            finally:
                server.shutdown()
                worker.join(timeout=3)


@pytest.mark.live_startup
def test_named_permissions_and_turn_selection_remain_native(native):
    marker = native.workspace / "turn-profile-must-block-write"
    native.model.tool_input = {"cmd": f"printf blocked > {marker}"}
    profiles = (
        "-c",
        'permissions.fixture_workspace.extends=":workspace"',
        "-c",
        'permissions.fixture_readonly.extends=":read-only"',
    )
    with native.app_server(extra=profiles) as (client, observed, _process, _path):
        started = client.rpc(
            "thread/start",
            {
                "cwd": str(native.workspace),
                "permissions": "fixture_workspace",
                "approvalPolicy": "never",
                "config": {"bypass_hook_trust": True},
            },
        )
        assert started["activePermissionProfile"] == {"id": "fixture_workspace", "extends": ":workspace"}
        assert started["sandbox"]["type"] == "workspaceWrite"
        client.rpc(
            "turn/start",
            {
                "threadId": started["thread"]["id"],
                "permissions": "fixture_readonly",
                "input": [{"type": "text", "text": "Run the fixture command."}],
            },
        )
        client.complete_turn()
        assert observed == [native.model.tool_input["cmd"]]
        assert not marker.exists()
