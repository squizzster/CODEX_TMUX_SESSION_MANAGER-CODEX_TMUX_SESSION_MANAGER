from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

import rodex.agent_observer as observer_module
from rodex.agent_observer import _ObserverEventDispatcher
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRejected,
    InteractionRequest,
    InteractionResult,
    SessionInteractionPipeline,
)
from rodex.interaction_transport import (
    SESSION_INTERACTION_CONNECTION_PATH,
    SESSION_INTERACTION_METHOD,
    publish_session_interaction,
)
from rodex.observer_pane import ObserverPaneController
from rodex.protocol_proxy import CONTROL_CONNECTION_PATH, CodexProtocolProxy, ToolCallCounter
from rodex.tmux_session_capability import (
    RODEX_PRIMARY_PANE_ID_OPTION,
    RODEX_RUNTIME_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    TmuxRuntimeCapability,
)
from rodex_registry.identity import RodexRuntimeId


@pytest.mark.parametrize("connection_path", ["/", CONTROL_CONNECTION_PATH])
def test_real_proxy_hooks_cover_both_directions_and_control_connections(tmp_path, connection_path):
    received = []
    projected = []
    hooks = []

    def upstream(connection):
        try:
            for message in connection:
                received.append(message)
                connection.send('{"id":1,"result":{"text":"output"}}')
        except ConnectionClosed:
            pass

    def hook(request):
        hooks.append(request)
        if request.operation in {InteractionOperation.PROTOCOL_INPUT, InteractionOperation.PROTOCOL_OUTPUT}:
            return replace(
                request,
                payload=request.payload.replace('"input"', '"accepted-input"').replace(
                    '"output"',
                    '"accepted-output"',
                ),
            )
        return request

    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    server = unix_serve(upstream, path=str(app_socket))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pipeline = SessionInteractionPipeline(hooks=(hook,))
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(lambda _: None),
        lambda message, event: projected.append((message, event)),
        interaction_pipeline=pipeline,
    )
    try:
        proxy.start()
        with unix_connect(str(proxy_socket), uri=f"ws://localhost{connection_path}", close_timeout=1) as client:
            client.send('{"id":1,"params":{"text":"input"}}')
            response = client.recv(timeout=2)
            assert json.loads(response)["result"]["text"] == "accepted-output"
        assert json.loads(received[0])["params"]["text"] == "accepted-input"
        assert [request.operation for request in hooks] == [
            InteractionOperation.PROTOCOL_INPUT,
            InteractionOperation.PROTOCOL_OUTPUT,
        ]
        if connection_path == "/":
            assert projected[0][0] == response
            assert projected[0][1] == json.loads(response)
        else:
            assert projected == []
    finally:
        proxy.close()
        server.shutdown(close_connections=True)
        thread.join(timeout=3)


def test_protocol_rejection_closes_connection_without_forwarding_the_request(tmp_path):
    received = []

    def upstream(connection):
        with suppress(ConnectionClosed):
            received.extend(connection)

    def reject(request):
        if request.operation == InteractionOperation.PROTOCOL_INPUT:
            raise InteractionRejected("intent rejected")
        return request

    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    server = unix_serve(upstream, path=str(app_socket))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(lambda _: None),
        interaction_pipeline=SessionInteractionPipeline(hooks=(reject,)),
    )
    try:
        proxy.start()
        with unix_connect(str(proxy_socket), close_timeout=1) as client:
            client.send('{"id":1,"method":"turn/start"}')
            with pytest.raises(ConnectionClosed):
                client.recv(timeout=2)
        assert received == []
    finally:
        proxy.close()
        server.shutdown(close_connections=True)
        thread.join(timeout=3)


def test_one_remote_interface_routes_display_and_explicit_model_intent_without_duplicate_echo(tmp_path):
    displayed = []
    model_inputs = []
    pipeline = SessionInteractionPipeline()
    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "unused.sock",
        ToolCallCounter(lambda _: None),
        interaction_pipeline=pipeline,
        model_message_sender=lambda request: (
            model_inputs.append(request)
            or InteractionResult(
                DeliveryStatus.MODEL_TURN_STARTED,
                value={"turn_id": "turn-1"},
            )
        ),
    )

    class Primary:
        def send(self, message):
            displayed.append(json.loads(message))

    primary = Primary()
    assert proxy._claim_primary_connection(primary)
    proxy._primary_thread_id = "thread-1"
    proxy.start()
    try:
        displayed_result = publish_session_interaction(
            tmp_path / "proxy.sock",
            InteractionRequest("main", InteractionOperation.MESSAGE, "test", text="display"),
        )
        model_result = publish_session_interaction(
            tmp_path / "proxy.sock",
            InteractionRequest("main", InteractionOperation.MESSAGE, "test", text="input", start_model_turn=True),
        )
        assert displayed_result.status == DeliveryStatus.DELIVERED
        assert model_result.status == DeliveryStatus.MODEL_TURN_STARTED
        assert [message["params"]["message"] for message in displayed] == ["display"]
        assert [request.text for request in model_inputs] == ["input"]
        assert model_inputs[0].dispatch_id.startswith("rodex:dispatch:")
        stale = publish_session_interaction(
            tmp_path / "proxy.sock",
            InteractionRequest(
                "main",
                InteractionOperation.MESSAGE,
                "test",
                text="stale",
                expected_binding="old-connection",
            ),
        )
        assert stale.status == DeliveryStatus.REJECTED
        with unix_connect(
            str(tmp_path / "proxy.sock"), uri=f"ws://localhost{SESSION_INTERACTION_CONNECTION_PATH}"
        ) as client:
            client.send(
                json.dumps(
                    {"id": 7, "method": SESSION_INTERACTION_METHOD, "params": {"target": "main", "operation": "made-up"}}
                )
            )
            assert json.loads(client.recv(timeout=1))["result"]["status"] == "rejected"
    finally:
        proxy._release_primary_connection(primary)
        proxy.close()


def test_closed_observer_dispatcher_rejects_admission():
    dispatcher = _ObserverEventDispatcher()
    dispatcher.close()
    with pytest.raises(OSError, match="closed"):
        dispatcher.send(Path("/unused.sock"), {"kind": "observer_state_snapshot"})


def test_real_tmux_pane_operations_use_the_same_pipeline_and_exact_targets(tmp_path):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux unavailable")
    socket_path = tmp_path / "owned-tmux.sock"

    def tmux_command(*arguments):
        return subprocess.run(
            [tmux, "-S", str(socket_path), *arguments], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()

    server_id = "1234567890abcdef1234567890abcdef"
    runtime_id = RodexRuntimeId.parse("0123456789abcdef")
    try:
        identity = tmux_command(
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "fixture",
            "-x",
            "120",
            "-y",
            "36",
            "-P",
            "-F",
            "#{session_id}|#{pane_id}",
            "sleep 120",
        )
        session_id, primary_id = identity.split("|")
        tmux_command("set-option", "-g", RODEX_SHARED_TMUX_PROTOCOL_OPTION, RODEX_SHARED_TMUX_PROTOCOL)
        tmux_command("set-option", "-g", RODEX_SHARED_TMUX_SERVER_ID_OPTION, server_id)
        tmux_command("set-option", "-t", session_id, RODEX_RUNTIME_ID_OPTION, str(runtime_id))
        tmux_command("set-option", "-t", session_id, RODEX_PRIMARY_PANE_ID_OPTION, primary_id)
        capability = TmuxRuntimeCapability(socket_path, server_id, session_id, primary_id, runtime_id)
        pipeline = SessionInteractionPipeline()
        notices = []
        controller = ObserverPaneController(
            tmux,
            capability,
            primary_id,
            runner=subprocess.run,
            pipeline=pipeline,
            message_sender=lambda text, pane: notices.append((text, pane)),
        )
        open_request = InteractionRequest(
            "agent-observer", InteractionOperation.OPEN, "fixture", payload=json.dumps(["sleep", "120"])
        )
        opened = pipeline.execute(open_request)
        assert opened.accepted
        pane_id = opened.value
        assert pane_id != primary_id
        assert pipeline.execute(open_request).value == pane_id
        assert tmux_command("list-panes", "-t", session_id, "-F", "#{pane_id}").splitlines() == [pane_id, primary_id]
        assert pipeline.send_message(target="agent-observer", text="visible").accepted
        assert notices == [("visible", pane_id)]
        assert (
            pipeline.send_message(target="agent-observer", text="not a model chat", start_model_turn=True).status
            == DeliveryStatus.REJECTED
        )
        assert pipeline.execute(InteractionRequest("agent-observer", InteractionOperation.FOCUS, "fixture")).accepted
        assert tmux_command("display-message", "-p", "-t", pane_id, "#{pane_active}") == "1"
        assert pipeline.execute(
            InteractionRequest("agent-observer", InteractionOperation.RESIZE, "fixture", size_percent=40)
        ).accepted
        assert pipeline.execute(InteractionRequest("agent-observer", InteractionOperation.CLOSE, "fixture")).accepted
        assert pipeline.send_message(target="agent-observer", text="missing").status == DeliveryStatus.REJECTED
        assert pipeline.send_message(target="agent-observer", text="reopened", open_if_missing=True).accepted
        assert notices[-1][1] != pane_id
        assert not controller._pane.close(pane_id)  # A stale CLOSE cannot resolve to the replacement pane.
        assert len(tmux_command("list-panes", "-t", session_id).splitlines()) == 2
        assert any(record.operation == InteractionOperation.OPEN for record in pipeline.records[-3:])
        tmux_command("set-option", "-t", session_id, RODEX_RUNTIME_ID_OPTION, "ffffffffffffffff")
        assert not pipeline.execute(InteractionRequest("agent-observer", InteractionOperation.CLOSE, "fixture")).accepted
        assert len(tmux_command("list-panes", "-t", session_id).splitlines()) == 2
    finally:
        subprocess.run([tmux, "-S", str(socket_path), "kill-server"], capture_output=True, timeout=5)


def test_observer_lookup_and_state_delivery_have_bounded_tmux_probe_count(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[3] == "show-options":
            output = "%9\n"
        else:
            assert "display-message" in shlex.split(command[-2])
            output = "%9|%7|0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    capability = TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$7", "%7", RodexRuntimeId.parse("a" * 16))
    pipeline = SessionInteractionPipeline()
    controller = ObserverPaneController(
        "tmux", capability, "%7", runner=runner, pipeline=pipeline, state_sender=lambda _: None
    )
    assert controller.locate() == "%9"
    assert len(calls) == 2  # Initial role lookup plus one exact capability read.
    calls.clear()
    for _ in range(20):
        assert controller.send_state({"kind": "observer_state_snapshot"}).accepted
    assert len(calls) == 20  # Not multiple blocking subprocesses per streamed delta.


def test_every_observer_render_origin_uses_the_terminal_pipeline(monkeypatch, capsys):
    import rodex.terminal_presentation as terminal_module

    seen = []
    pipeline = SessionInteractionPipeline(hooks=(lambda request: seen.append(request) or request,))
    from rodex.interaction_pipeline import InteractionTarget

    pipeline.register(
        InteractionTarget(
            "agent-observer",
            "test-process",
            frozenset({InteractionOperation.MESSAGE}),
            lambda: True,
            terminal_module._write_observer_terminal,
        )
    )
    monkeypatch.setattr(terminal_module, "_OBSERVER_TERMINAL", pipeline)
    observer_module._print_lines(["bootstrap"])
    observer_module._print_lines(["live state"])
    observer_module._print_lines(["SQL recovery"])
    assert capsys.readouterr().out == "bootstrap\nlive state\nSQL recovery\n"
    assert len(seen) == 3
    assert all(not request.start_model_turn for request in seen)


@pytest.mark.parametrize(
    "selected_thread,changes_during_wait", [("registered", False), ("other", False), ("registered", True)]
)
def test_registered_main_adapter_preserves_displayed_thread_and_rechecks_after_wait(
    tmp_path,
    monkeypatch,
    selected_thread,
    changes_during_wait,
):
    import rodex.exact_turn_mutation as mutation_module
    import rodex.runtime as runtime_module
    import rodex_registry
    from rodex.control import PromptDispatch

    sent = []
    connection = {"binding": "connection-1"}
    request = InteractionRequest(
        "main",
        InteractionOperation.MESSAGE,
        "test",
        text="hello",
        start_model_turn=True,
        expected_thread_id=selected_thread,
        expected_binding="connection-1",
        dispatch_id="dispatch-1",
    )
    context = SimpleNamespace(rodex_sessions_id=1, codex_session_id="registered", rodex_database_path=tmp_path / "db")
    config = SimpleNamespace(codex_binary="codex", tmux_binary="tmux", runtime_id="runtime-1")

    def validate_binding(observed):
        if observed.expected_binding != connection["binding"]:
            raise InteractionRejected("main conversation changed before model dispatch")

    def start(selector, text, **kwargs):
        assert selector == "worker" and kwargs["expected_thread_id"] == "registered"
        assert kwargs["expected_runtime_id"] == "runtime-1"
        if changes_during_wait:
            connection["binding"] = "connection-2"
        kwargs["before_dispatch"]()
        sent.append(text)
        return object(), PromptDispatch("started", "turn-1", "dispatch-1")

    monkeypatch.setattr(
        rodex_registry, "lookup_rodex_session_names", lambda *_args: SimpleNamespace(display_name="worker")
    )
    monkeypatch.setattr(runtime_module, "RodexRuntimeLauncher", lambda *_args: object())
    monkeypatch.setattr(mutation_module, "ExactTurnMutationCoordinator", lambda *_args: SimpleNamespace(start=start))
    proxy = SimpleNamespace(validate_primary_model_binding=validate_binding)
    if changes_during_wait:
        with pytest.raises(InteractionRejected, match="changed"):
            runtime_module._start_registered_main_model_message(request, context, config, proxy)
        assert sent == []
    else:
        result = runtime_module._start_registered_main_model_message(request, context, config, proxy)
        assert result.accepted is (selected_thread == "registered")
        assert sent == (["hello"] if selected_thread == "registered" else [])


def test_reopen_publishes_current_state_after_creation_not_a_preopen_snapshot(tmp_path):
    current_revision = 1
    published = []
    capability = TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$7", "%7", RodexRuntimeId.parse("a" * 16))
    pipeline = SessionInteractionPipeline()
    controller = ObserverPaneController(
        "tmux",
        capability,
        "%7",
        runner=lambda *_args, **_kwargs: None,
        pipeline=pipeline,
        snapshot_publisher=lambda: published.append(current_revision),
    )

    class Pane:
        known_pane_id = None

        def locate(self):
            return self.known_pane_id

        def create(self, _command):
            nonlocal current_revision
            current_revision = 2  # A normal live update while pane creation is in progress.
            self.known_pane_id = "%9"
            return self.known_pane_id

    controller._pane = Pane()
    controller._open_request = InteractionRequest(
        "agent-observer", InteractionOperation.OPEN, "observer", payload=json.dumps(["sleep", "30"])
    )
    assert pipeline.execute(InteractionRequest("agent-observer", InteractionOperation.OPEN, "test")).accepted
    assert published == [2]


@pytest.mark.parametrize(
    "target,open_if_missing,timeout", [("main", False, 1), ("agent-observer", False, 10), ("agent-observer", True, 10)]
)
def test_external_message_timeout_accommodates_observer_readiness(target, open_if_missing, timeout):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def send(self, _message):
            pass

        def recv(self, **kwargs):
            assert kwargs["timeout"] == timeout
            return '{"id":0,"result":{"status":"delivered"}}'

    result = publish_session_interaction(
        Path("/unused.sock"),
        InteractionRequest(target, InteractionOperation.MESSAGE, "test", text="hello", open_if_missing=open_if_missing),
        connector=lambda *_args, **_kwargs: Connection(),
    )
    assert result.accepted
