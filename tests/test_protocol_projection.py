from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from test_context_threshold_steering import ROOT_THREAD, SteeringHarness
from test_primary_event_continuity import connect, running_proxy
from test_rodex_protocol_proxy import token_usage_updated
from test_rodex_runtime import RUNTIME_ID, _create_socket_path, _mock_terminal_gateway
from websockets.exceptions import ConnectionClosed

import rodex.runtime as runtime_module
from rodex.interaction_pipeline import InteractionOperation, InteractionRejected, SessionInteractionPipeline
from rodex.process_contracts import AnalyticsRuntimeConfig, RuntimeServiceConfig
from rodex.protocol_projection import ProtocolProjectionFailure, ProtocolProjectionGuard
from rodex.protocol_proxy import (
    EVENT_STREAM_READY_MESSAGE,
    CodexContextStatusObserver,
    CodexProtocolProxy,
    ToolCallCounter,
)
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import RodexRegistryId, RodexSessionId


def test_projection_health_counts_failures_without_retaining_content():
    guard = ProtocolProjectionGuard()

    def fail(_payload):
        raise RuntimeError("private command or conversation")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: guard.observe("presentation-output", fail, "private body"), range(32)))
    snapshot = guard.failures
    assert guard.observe("presentation-output", lambda: False)
    assert not guard.observe("working-status", fail, "private body")
    assert snapshot == (ProtocolProjectionFailure("presentation-output", 32, "RuntimeError"),)
    assert guard.failures == (*snapshot, ProtocolProjectionFailure("working-status", 1, "RuntimeError"))
    assert "private" not in repr(guard.failures)


@pytest.mark.parametrize("source", ["protocol", "rollout"])
def test_context_render_failure_does_not_preserve_stale_steering_authority(source):
    harness = SteeringHarness()
    guard = ProtocolProjectionGuard()
    render_fails = False

    def render(_status):
        if render_fails:
            raise RuntimeError("injected rendering failure")

    observer = CodexContextStatusObserver(
        lambda status: guard.observe("context-status-render", render, status),
        on_context_percent=lambda thread, percent, generation: harness.controller.observe_context_percent(
            thread, percent, connection_generation=generation
        ),
    )
    observer.bind_primary_thread(ROOT_THREAD, "fixture-connection")

    def usage(percent):
        if source == "protocol":
            observer.observe_protocol_event(
                json.loads(token_usage_updated(percent * 10, thread_id=ROOT_THREAD, context_window=1000)),
                connection_generation="fixture-connection",
            )
        else:
            observer.observe_rollout_context_percent(ROOT_THREAD, percent, connection_generation="fixture-connection")

    try:
        harness.start_turn()
        usage(75)
        pending = harness.deferred_calls[0]
        render_fails = True
        usage(10)
        assert pending.cancelled
        pending.fire(even_if_cancelled=True)
        assert harness.requests == []
        assert guard.failures == (ProtocolProjectionFailure("context-status-render", 1, "RuntimeError"),)
    finally:
        observer.close()
        harness.close()


def test_failed_control_observation_revokes_authority_even_when_cancellation_raises():
    harness = SteeringHarness()
    guard = ProtocolProjectionGuard()

    def fail():
        raise RuntimeError("injected control observation or cancellation failure")

    try:
        harness.start_turn()
        harness.controller.observe_context_percent(ROOT_THREAD, 75, connection_generation="fixture-connection")
        pending = harness.deferred_calls[0]
        pending.cancel = fail
        if not guard.observe("context-status", fail):
            guard.observe("context-status-reset", harness.controller.reset_after_disconnect)
        pending.fire(even_if_cancelled=True)
        assert harness.requests == []
        assert guard.failures == (
            ProtocolProjectionFailure("context-status", 1, "RuntimeError"),
            ProtocolProjectionFailure("context-status-reset", 1, "RuntimeError"),
        )
    finally:
        harness.close()


@pytest.mark.parametrize("participant", ["primary-input", "tool-count", "primary-output"])
def test_projection_exceptions_preserve_current_and_following_frames(tmp_path, monkeypatch, participant):
    observations = []
    second_output_observed = Event()

    def observe(name, event):
        observations.append((name, event["method"]))
        if name == participant:
            raise RuntimeError("private injected observer failure")

    def observe_output(_message, event):
        try:
            observe("primary-output", event)
        finally:
            if event["method"] == "turn/completed":
                second_output_observed.set()

    counter = ToolCallCounter(lambda _count: None)
    monkeypatch.setattr(counter, "observe_protocol_event", lambda event: observe("tool-count", event))
    with running_proxy(
        tmp_path,
        on_client_message=lambda _message, event: observe("primary-input", event),
        on_server_message=observe_output,
        tool_counter=counter,
    ) as (proxy, tap):
        source_active = Event()
        activate = tap.activate_primary_source

        def activate_source():
            epoch = activate()
            source_active.set()
            return epoch

        monkeypatch.setattr(tap, "activate_primary_source", activate_source)
        with connect(tmp_path / "proxy.sock", "/rpc") as tui:
            assert source_active.wait(1)
            with connect(tmp_path / "events.sock") as subscriber:
                assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
                for method in ("turn/started", "turn/completed"):
                    message = json.dumps({"method": method, "params": {"threadId": "main", "turn": {"id": "target"}}})
                    tui.send(message)
                    assert tui.recv(timeout=1) == message
                    assert subscriber.recv(timeout=1) == message
                assert second_output_observed.wait(1)
                # A round trip orders the completed callback and its failure record.
                tui.send(json.dumps({"method": "barrier"}))
                assert json.loads(tui.recv(timeout=1))["method"] == "barrier"
                for name in ("primary-input", "tool-count", "primary-output"):
                    assert [
                        method for observed_name, method in observations if observed_name == name and method != "barrier"
                    ] == ["turn/started", "turn/completed"]
                failures = proxy.projection_failures
                assert len(failures) == 1
                assert failures[0].participant_name == participant
                assert failures[0].failure_count >= 2
                assert failures[0].last_exception_type == "RuntimeError"
                assert "private" not in repr(failures)


def test_projection_guard_does_not_swallow_intentional_input_rejection(tmp_path):
    observed_input = []

    def reject(request):
        if request.operation is InteractionOperation.PROTOCOL_INPUT:
            raise InteractionRejected("intentional prompt rejection")
        return request

    pipeline = SessionInteractionPipeline(hooks=(reject,))
    with (
        running_proxy(tmp_path, on_client_message=lambda *_args: observed_input.append(True), interactions=pipeline) as (
            proxy,
            _tap,
        ),
        connect(tmp_path / "proxy.sock", "/rpc") as tui,
    ):
        tui.send("not admitted")
        with pytest.raises(ConnectionClosed):
            tui.recv(timeout=1)
        assert observed_input == []
        assert proxy.projection_failures == ()


RUNTIME_PROJECTIONS = (
    ("presentation-input", "SessionPresentationPipeline", "observe_protocol_input"),
    ("presentation-output", "SessionPresentationPipeline", "observe_protocol_output"),
    ("context-steering", "ContextThresholdSteeringController", "observe_protocol_event"),
    ("context-status", "CodexContextStatusObserver", "observe_protocol_event"),
    ("working-status", "CodexWorkingStatusObserver", "observe_protocol_event"),
    ("overload-recovery", "ServerOverloadedRecoveryController", "observe_protocol_event"),
    ("agent-observer", "AgentObserverCoordinator", "observe_protocol_event"),
)


@pytest.mark.parametrize("failing_participant", [name for name, *_ in RUNTIME_PROJECTIONS] + ["analytics"])
def test_runtime_projection_faults(tmp_path, monkeypatch, failing_participant):
    """Exercise the production composition, with native processes and effects replaced."""
    _mock_terminal_gateway(monkeypatch)
    observations = []
    health_snapshots = []
    failure_resets = []

    def observe(name):
        observations.append(name)
        if name == failing_participant:
            raise RuntimeError("private injected runtime content")

    for name, component, method in RUNTIME_PROJECTIONS:
        monkeypatch.setattr(
            getattr(runtime_module, component),
            method,
            lambda _self, _event, participant=name, **_kwargs: observe(participant),
        )
    for component in ("ContextThresholdSteeringController", "ServerOverloadedRecoveryController"):
        original_reset = getattr(runtime_module, component).reset_after_disconnect

        def reset(self, participant=component, reset_method=original_reset):
            failure_resets.append(participant)
            reset_method(self)

        monkeypatch.setattr(getattr(runtime_module, component), "reset_after_disconnect", reset)

    class Process:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = 0

    class PassiveComponent:
        failure = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def close(self):
            pass

        def update(self, _value):
            pass

    class Proxy(PassiveComponent):
        def __init__(
            self,
            _path,
            _upstream,
            _counter,
            on_output,
            _reset,
            *,
            on_primary_client_message,
            projection_guard,
            context_observer,
            context_steering,
            **_options,
        ):
            self.on_output = on_output
            self.on_input = on_primary_client_message
            self.guard = projection_guard
            self._projection_guard = projection_guard
            self._context_observer = context_observer
            self._context_steering = context_steering
            self._connection_lock = Lock()
            self._primary_tui_connection = object()
            self._primary_context_generation = "fixture-connection"

        def start(self):
            for _ in range(2):
                self.on_input("{}", {})
                CodexProtocolProxy._project_primary_context(
                    self, {}, self._primary_tui_connection, connection_generation=self._primary_context_generation
                )
                self.on_output("{}", {})
            health_snapshots.append(self.guard.failures)

    monkeypatch.setattr(runtime_module.subprocess, "Popen", Process)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", Proxy)
    for name in ("TmuxToolCallStatus", "TmuxContextStatus", "CodexProtocolEventTap", "_RuntimePathKeepalive"):
        monkeypatch.setattr(runtime_module, name, PassiveComponent)
    monkeypatch.setattr(
        runtime_module, "_wait_for_app_server_socket", lambda *_args: _create_socket_path(tmp_path / "app.sock")
    )
    monkeypatch.setattr(runtime_module, "_registered_analytics_runtime_config", lambda *_args: None)
    monkeypatch.setattr(
        runtime_module,
        "_resolve_runtime_service_tmux_capability",
        lambda *_args: TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$7", "%9", RUNTIME_ID),
    )
    config = RuntimeServiceConfig(
        workspace=tmp_path,
        codex_binary="/usr/bin/codex",
        app_server_socket_path=tmp_path / "app.sock",
        app_server_log_path=tmp_path / "app.log",
        command_policy_socket_path=tmp_path / "policy.sock",
        protocol_proxy_socket_path=tmp_path / "proxy.sock",
        protocol_event_socket_path=tmp_path / "events.sock",
        tmux_binary="/usr/bin/tmux",
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_pane_target="%9",
        runtime_id=RUNTIME_ID,
        codex_arguments=(),
        analytics=AnalyticsRuntimeConfig(
            tmux_server_id="a" * 32,
            rodex_database_path=tmp_path / "rodex.sqlite3",
            codex_sessions_root=tmp_path / "sessions",
            rodex_session_id=RodexSessionId(1),
            rodex_registry_id=RodexRegistryId(1),
            runtime_id=RUNTIME_ID,
            protocol_event_socket_path=tmp_path / "events.sock",
        ),
        user_environment=(("PATH", "/usr/bin"),),
    )
    terminal_fd = os.open(os.devnull, os.O_RDONLY)
    assert (
        runtime_module.run_runtime_service(
            config,
            terminal_fd=terminal_fd,
            terminal_environment={},
            stop=Event(),
            on_analytics_event=lambda _event: observe("analytics"),
        )
        == 0
    )
    assert (
        observations
        == (
            [
                "presentation-input",
                "context-steering",
                "context-status",
                "presentation-output",
                "working-status",
                "overload-recovery",
                "agent-observer",
                "analytics",
            ]
        )
        * 2
    )
    assert health_snapshots == [(ProtocolProjectionFailure(failing_participant, 2, "RuntimeError"),)]
    assert "private" not in repr(health_snapshots)
    if failing_participant in {"context-steering", "context-status"}:
        assert failure_resets == ["ContextThresholdSteeringController"] * 2
    elif failing_participant == "overload-recovery":
        assert failure_resets == ["ServerOverloadedRecoveryController"] * 2
    else:
        assert failure_resets == []
