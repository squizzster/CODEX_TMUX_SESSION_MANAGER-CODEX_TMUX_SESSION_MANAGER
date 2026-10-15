"""Real native resume/counters/steer, isolated home and local model (no quota).

SQL/tmux discovery is fixture-provided; proxy, threshold, event tap and exact
native control execute production code over real authenticated Unix sockets.
"""

from __future__ import annotations

import json
import shutil
from http.server import BaseHTTPRequestHandler
from threading import Event

import pytest
from command_policy_native_fixture import NativeClient, native_policy_fixture
from runtime_peer_fixtures import TEST_PEER, LiveTestProcess
from websockets.sync.client import unix_connect

from rodex.context_threshold_steering import CONTEXT_STEER_MESSAGE, ContextThresholdSteeringController
from rodex.control import CodexControlClient, LiveRodexControl
from rodex.interaction_pipeline import DeliveryStatus, InteractionOperation, InteractionResult, SessionInteractionPipeline
from rodex.protocol_proxy import CodexContextStatusObserver, CodexProtocolEventTap, CodexProtocolProxy, ToolCallCounter
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex_registry.identity import CodexSessionId, RodexRegistryId, RodexSessionId


@pytest.mark.skipif(shutil.which("codex") is None, reason="installed native Codex is required")
def test_resumed_native_high_context_steers_running_turn_without_interrupting():
    model_waiting, release_model, dispatch_finished = Event(), Event(), Event()
    logs, percentages, wire_requests, dispatches = [], [], [], []

    class HighContextModel(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append(request)
            if len(self.server.requests) == 1:
                item = {
                    "id": "fc_context",
                    "type": "function_call",
                    "call_id": "call_context",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "true", "yield_time_ms": 1000}),
                }
            else:
                model_waiting.set()
                assert release_model.wait(20), "native model barrier was not released"
                item = {
                    "id": "msg_context",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Synthetic completion.", "annotations": []}],
                }
            response = {
                "id": "resp_context",
                "object": "response",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 7500, "output_tokens": 1, "total_tokens": 7501},
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

    def capture(request):
        if request.operation is InteractionOperation.PROTOCOL_INPUT:
            wire_requests.append(json.loads(request.payload))
        return request

    def log(message):
        logs.append(message)
        if any(outcome in message for outcome in ("dispatchCompleted", "dispatchRejected", "dispatchFailed")):
            dispatch_finished.set()

    with native_policy_fixture() as native:
        # Persist a genuine native thread, then restart the server before resume.
        with native.app_server(injection=False) as (seed, *_):
            root = seed.rpc("thread/start", {"cwd": str(native.workspace)})["thread"]["id"]
            seed.rpc("turn/start", {"threadId": root, "input": [{"type": "text", "text": "Seed fixture history."}]})
            seed.complete_turn()
        native.model.requests.clear()
        native.model.RequestHandlerClass = HighContextModel
        with native.app_server(injection=False, extra=("-c", "model_context_window=10000")) as (
            _,
            _observed,
            process,
            app,
        ):
            pipeline = SessionInteractionPipeline(hooks=(capture,))
            controller = ContextThresholdSteeringController(pipeline, logger=log)
            controller.bind_root_thread(root)

            def percentage(thread_id, percent):
                percentages.append(percent)
                controller.observe_context_percent(thread_id, percent)

            context = CodexContextStatusObserver(lambda _: None, on_context_percent=percentage)
            tap = CodexProtocolEventTap(native.root / "events.sock", peer_identity=TEST_PEER)
            codex_id = CodexSessionId(root)
            capability = TmuxSessionCapability(
                native.root / "fixture-tmux.sock",
                TEST_PEER.tmux_server_id,
                "$1",
                "%1",
                TEST_PEER.runtime_id,
                RodexSessionId.parse("a" * 16),
                RodexRegistryId.parse("b" * 16),
                1,
                codex_id,
            )
            control = LiveRodexControl(
                native.root / "proxy.sock",
                native.root / "events.sock",
                codex_id,
                runtime_id=TEST_PEER.runtime_id,
                tmux_capability=capability,
            )

            def deliver(request):
                def admit():
                    proxy.validate_primary_model_binding(request)
                    controller.admit_dispatch(request)

                dispatch = CodexControlClient()._steer_turn(
                    control,
                    request.expected_turn_id,
                    request.text,
                    dispatch_id=request.dispatch_id,
                    revalidate=admit,
                )
                dispatches.append(dispatch)
                return InteractionResult(DeliveryStatus.COMPLETED)

            def observe(_message, event):
                controller.observe_protocol_event(event)
                context.observe_protocol_event(event)

            proxy = CodexProtocolProxy(
                control.protocol_proxy_socket_path,
                app,
                ToolCallCounter(lambda _: None),
                observe,
                interaction_pipeline=pipeline,
                peer_identity=TEST_PEER,
                app_server_process=process,
                native_tui_process=LiveTestProcess(),
                model_message_sender=deliver,
                event_tap=tap,
            )
            try:
                tap.start()
                with (
                    proxy,
                    unix_connect(
                        str(control.protocol_proxy_socket_path), additional_headers=TEST_PEER.headers()
                    ) as connection,
                ):
                    client = NativeClient(connection)
                    client.rpc(
                        "initialize",
                        {
                            "clientInfo": {"name": "rodex-context-test", "version": "1"},
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                    connection.send(json.dumps({"method": "initialized", "params": {}}))
                    assert client.rpc("thread/resume", {"threadId": root})["thread"]["id"] == root
                    assert not any(event.get("method") == "thread/started" for event in client.events)
                    turn = client.rpc(
                        "turn/start", {"threadId": root, "input": [{"type": "text", "text": "Exercise high context."}]}
                    )["turn"]["id"]
                    assert model_waiting.wait(10), logs
                    assert dispatch_finished.wait(10), logs
                    assert len(dispatches) == 1, logs
                    assert dispatches[0].turn_id == turn
                    assert any(percent >= 70 for percent in percentages)
                    assert not release_model.is_set()
                    release_model.set()
                    client.complete_turn()
                    history = client.rpc("thread/read", {"threadId": root, "includeTurns": True})
                    current_turn = next(item for item in history["thread"]["turns"] if item["id"] == turn)
                    assert current_turn["status"] == "completed"
                    assert json.dumps(current_turn).count(CONTEXT_STEER_MESSAGE) == 1
                    assert any(CONTEXT_STEER_MESSAGE in json.dumps(request["input"]) for request in native.model.requests)
                    assert sum(request.get("method") == "turn/steer" for request in wire_requests) == 1
                    assert not any(request.get("method") == "turn/interrupt" for request in wire_requests)
                    assert sum(request.get("method") == "turn/start" for request in wire_requests) == 1
            finally:
                release_model.set()
                controller.close()
                context.close()
                tap.close()
