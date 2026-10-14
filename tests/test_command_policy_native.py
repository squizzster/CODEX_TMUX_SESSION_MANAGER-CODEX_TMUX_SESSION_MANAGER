"""Real native hook/sandbox behavior, driven by an offline deterministic model."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
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
    policy_hook_override,
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


def trust_previous_rodex_installation(native):
    # Review only: the previous helper path is never executed by this fixture.
    previous = policy_hook_override(str(native.root / "previous-installation" / "bin" / "python"))
    with native.app_server(injection=False, extra=("-c", previous)) as (client, _observed, _process, _path):
        trust_fixture_hook(client, native.workspace)


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
        assert admission.temporary_trust is not None
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
@pytest.mark.parametrize("prior_installation_trusted", [False, True])
def test_native_extra_enabled_user_hook_keeps_manual_policy_setup(native, prior_installation_trusted):
    install_user_hook(native)
    if prior_installation_trusted:
        trust_previous_rodex_installation(native)

    with native.app_server() as (client, _observed, process, path):
        hooks = client.rpc("hooks/list", {"cwds": [str(native.workspace)]})["data"][0]["hooks"]
        assert {hook["source"] for hook in hooks} == {"sessionFlags", "user"}
        rodex_hook = next(hook for hook in hooks if hook["source"] == "sessionFlags")
        assert rodex_hook["trustStatus"] == ("modified" if prior_installation_trusted else "untrusted")
        admission = policy_hook_admission(path, process, native.workspace, ())

    assert admission.inactive_reason == POLICY_INACTIVE_SETUP
    assert admission.temporary_trust is None


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
@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("sleep 9", SHORT_SLEEP_DENIAL_REASON),
        (
            "find /",
            "Rejected: Stage 1: Directory input '/' has no maximum depth.",
        ),
    ],
)
def test_native_command_policy_denies_with_model_visible_feedback(native, command, reason):
    native.model.tool_input = {"cmd": command}
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
        assert reason in json.dumps(native.model.requests)


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
@pytest.mark.parametrize("prior_installation_trusted", [False, True])
def test_managed_automatic_hook_admission_survives_saved_session_resume(native, prior_installation_trusted):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    if prior_installation_trusted:
        trust_previous_rodex_installation(native)
    with native.app_server() as (client, _observed, _process, _path):
        hook = client.rpc("hooks/list", {"cwds": [str(native.workspace)]})["data"][0]["hooks"][0]
        assert hook["trustStatus"] == ("modified" if prior_installation_trusted else "untrusted")
    rodex = str(Path(__file__).parents[1] / "rodex")
    runtime_root = Path(native.environment["RODEX_RUNTIME_DIR"])
    native.model.tool_input = {"cmd": "sleep 5"}
    original_state = tomllib.loads((native.codex_home / "config.toml").read_text()).get("hooks", {}).get("state", {})
    previous_identity = None
    name = None
    try:
        for resumed in (False, True):
            command = [rodex, name] if resumed else [rodex]
            with RodexTerminalClient(command, native.environment, native.workspace) as client:
                opened_name = client.wait_for_attach()
                if resumed:
                    assert opened_name == name
                name = opened_name
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
                if resumed:
                    assert identity["codex"]["session_id"] == previous_identity["codex"]["session_id"]
                    assert identity["runtime"]["runtime_id"] != previous_identity["runtime"]["runtime_id"]
                previous_identity = identity
                requests_before_turn = len(native.model.requests)
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
                current_requests = native.model.requests[requests_before_turn:]
                assert len(current_requests) >= 2
                final_input = current_requests[-1]["input"]
                last_user = max(index for index, item in enumerate(final_input) if item.get("role") == "user")
                current_tool_outputs = [
                    item for item in final_input[last_user + 1 :] if item.get("type") == "function_call_output"
                ]
                assert SHORT_SLEEP_DENIAL_REASON in json.dumps(current_tool_outputs)
                socket_path = runtime_root / f"tmux-v5-{identity['runtime']['runtime_id']}.sock"
                inactive = subprocess.run(
                    ["tmux", "-S", str(socket_path), "show-options", "-q", "-v", "-t", "%0", POLICY_INACTIVE_OPTION],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                assert inactive.stdout == ""
                client.detach()
            _stop_fixture_daemon(runtime_root)
        assert tomllib.loads((native.codex_home / "config.toml").read_text()).get("hooks", {}).get("state", {}) == (
            original_state
        )
    finally:
        _stop_fixture_daemon(runtime_root)


@pytest.mark.live_startup
@pytest.mark.parametrize("lifecycle", ["start", "resume", "fork"])
@pytest.mark.parametrize("prior_installation_trusted", [False, True])
def test_remote_tui_uses_exact_temporary_hook_trust_for_every_lifecycle(native, lifecycle, prior_installation_trusted):
    if prior_installation_trusted:
        trust_previous_rodex_installation(native)
    with (native.codex_home / "config.toml").open("a") as config:
        config.write('\n[hooks.state.unrelated]\nenabled=false\ntrusted_hash="sha256:sentinel"\n')
    original_state = tomllib.loads((native.codex_home / "config.toml").read_text())["hooks"]["state"]
    thread_id = None
    if lifecycle != "start":
        native.model.tool_input = {"cmd": "pwd"}
        with native.app_server(injection=False) as (client, _observed, _process, _path):
            thread_id = client.rpc("thread/start", {"cwd": str(native.workspace)})["thread"]["id"]
            client.rpc(
                "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "Run the fixture command."}]}
            )
            client.complete_turn()
    with native.app_server() as (client, _observed, process, path):
        admission = policy_hook_admission(path, process, native.workspace, ())
        trust = admission.temporary_trust
        assert trust is not None
    captured = []
    responses = []
    with native.app_server(extra=("-c", trust.configuration_override())) as (client, _observed, process, app):
        verified = policy_hook_admission(app, process, native.workspace, (), expected_trust=trust)
        assert verified.inactive_reason is None and verified.temporary_trust is None
        effective_state = client.rpc("config/read", {"cwd": str(native.workspace)})["config"]["hooks"]["state"]
        assert effective_state["unrelated"] == original_state["unrelated"]
        assert effective_state[trust.key]["trusted_hash"] == trust.current_hash

        def relay(downstream):
            with unix_connect(str(app), uri="ws://localhost/rpc", close_timeout=1) as upstream:

                def receive():
                    try:
                        for frame in upstream:
                            responses.append(json.loads(frame))
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
                    *(
                        ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
                        if lifecycle == "start"
                        else [lifecycle, thread_id]
                    ),
                ]
                with RodexTerminalClient(command, native.environment, native.workspace) as tui:
                    deadline = time.monotonic() + 15
                    method = f"thread/{lifecycle}"
                    while not any(frame.get("method") == method for frame in captured):
                        tui.poll()
                        assert tui.exit_code is None, tui.output.decode(errors="replace")
                        assert time.monotonic() < deadline, tui.output.decode(errors="replace")
                    request = next(frame for frame in captured if frame.get("method") == method)
                    params = request["params"]
                    assert not (params.get("config") or {}).get("bypass_hook_trust")
                    assert params["sandbox"] == ("workspace-write" if lifecycle == "start" else None)
                    assert params["approvalPolicy"] == ("on-request" if lifecycle == "start" else None)
                    while not any(frame.get("id") == request["id"] for frame in responses):
                        tui.poll()
                        assert time.monotonic() < deadline, tui.output.decode(errors="replace")
                    response = next(frame for frame in responses if frame.get("id") == request["id"])
                    assert "error" not in response, response
                    # Remote resume/fork reject TUI permission overrides and use
                    # native server configuration; trust must not broaden it.
                    assert response["result"]["sandbox"]["type"] == (
                        "workspaceWrite" if lifecycle == "start" else "readOnly"
                    )
                    assert response["result"]["approvalPolicy"] == ("on-request" if lifecycle == "start" else "never")
                    if lifecycle == "resume":
                        assert response["result"]["thread"]["id"] == thread_id
                    elif lifecycle == "fork":
                        assert response["result"]["thread"]["id"] != thread_id
            finally:
                server.shutdown()
                worker.join(timeout=3)
    assert tomllib.loads((native.codex_home / "config.toml").read_text())["hooks"]["state"] == original_state


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
