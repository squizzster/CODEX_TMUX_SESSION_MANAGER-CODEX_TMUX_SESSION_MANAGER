from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.client import unix_connect

from rodex.app_server_contract import CODEX_APP_SERVER, AppServerClientInfo
from rodex.runtime import default_runtime_root


def test_real_app_server_accepts_string_ids_on_a_private_unix_socket() -> None:
    codex_binary = shutil.which("codex")
    if codex_binary is None:
        pytest.skip("Codex CLI is not installed")
    # The project path can exceed sockaddr_un's hard path limit.
    integration_root = Path(tempfile.mkdtemp(prefix="it-", dir=default_runtime_root()))
    integration_root.chmod(0o700)
    socket_path = integration_root / "app.sock"
    process = subprocess.Popen(
        CODEX_APP_SERVER.command(codex_binary, socket_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not socket_path.exists():
            if process.poll() is not None:
                pytest.fail(f"Codex App Server exited with status {process.returncode}")
            if time.monotonic() >= deadline:
                pytest.fail("Codex App Server did not bind its Unix socket")
            time.sleep(0.02)
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        with unix_connect(
            str(socket_path),
            uri=f"ws://localhost{CODEX_APP_SERVER.rpc_connection_path}",
            compression=None,
            open_timeout=2,
            close_timeout=1,
            max_size=None,
        ) as websocket:
            initialize_id = "rodex:integration:initialize"
            websocket.send(
                json.dumps(
                    CODEX_APP_SERVER.initialize_request(
                        initialize_id,
                        AppServerClientInfo("rodex-integration", "Rodex Integration", "1"),
                    )
                )
            )
            initialized = _response_for(websocket, initialize_id)
            assert initialized["id"] == initialize_id
            CODEX_APP_SERVER.require_minimum_version(initialized["result"])
            websocket.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))

            loaded_id = "rodex:integration:loaded"
            websocket.send(
                json.dumps(
                    CODEX_APP_SERVER.request(
                        loaded_id, CODEX_APP_SERVER.thread_loaded_list_method, {}
                    )
                )
            )
            loaded = _response_for(websocket, loaded_id)
            assert isinstance(loaded["result"]["data"], list)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for child in integration_root.iterdir():
            child.unlink()
        integration_root.rmdir()


@dataclass(frozen=True, slots=True)
class _LiveTurnLifecycle:
    started: bool
    completed_status: str
    final_text: str | None
    observed_dispatch_ids: tuple[str, ...]
    unexpected_server_requests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LiveUserInputLifecycle:
    request_received: bool
    request_resolved: bool
    selected_answer: str | None
    completed_status: str
    final_text: str | None
    unexpected_server_requests: tuple[str, ...]


@pytest.mark.evolutionary_regression
def test_live_turn_survives_initiator_disconnect_and_streams_to_subscriber() -> None:
    """Current contract: subscriber owns lifecycle and persisted correlation."""
    live_turn_environment = "RODEX_RUN_LIVE_TURN_INTEGRATION"
    if os.environ.get(live_turn_environment) != "1":
        pytest.skip(
            f"set {live_turn_environment}=1 to run the authenticated model-backed test"
        )
    codex_binary = shutil.which("codex")
    if codex_binary is None:
        pytest.fail("opted-in live test requires Codex CLI")
    integration_root = Path(tempfile.mkdtemp(prefix="turn-it-", dir=default_runtime_root()))
    integration_root.chmod(0o700)
    workspace = integration_root / "workspace"
    workspace.mkdir(mode=0o700)
    socket_path = integration_root / "app.sock"
    process = subprocess.Popen(
        CODEX_APP_SERVER.command(codex_binary, socket_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    thread_id: str | None = None
    try:
        _wait_for_socket(process, socket_path)
        with _connect(socket_path) as subscriber:
            _initialize(subscriber, "subscriber")
            with _connect(socket_path) as initiator:
                _initialize(initiator, "initiator")
                thread_response = _request(
                    subscriber,
                    "thread:start",
                    "thread/start",
                    {"ephemeral": False, "cwd": str(workspace)},
                )
                thread_id = thread_response["result"]["thread"]["id"]
                _request(
                    initiator,
                    "thread:read",
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": False},
                )
                turn_response = _request(
                    initiator,
                    "turn:start",
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": (
                                    "Use the shell tool to run `sleep 5`. After it "
                                    "finishes, wait for one steering message before "
                                    "answering. Do not modify files."
                                ),
                            }
                        ],
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "readOnly"},
                        "clientUserMessageId": "rodex:live:start-fidelity",
                    },
                    timeout=30,
                )
                turn = turn_response["result"]["turn"]
                assert turn["status"] == "inProgress"
                turn_id = turn["id"]
                _wait_for_exact_turn_started(
                    subscriber,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    timeout=10,
                )
                steer_response = _request(
                    initiator,
                    "turn:steer",
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": turn_id,
                        "input": [
                            {
                                "type": "text",
                                "text": (
                                    "Reply exactly RODEX_FIDELITY_OK. Do not use tools."
                                ),
                            }
                        ],
                        "clientUserMessageId": "rodex:live:steer-fidelity",
                    },
                )
                assert steer_response["result"]["turnId"] == turn_id
                initiator.close()

            lifecycle = _exact_turn_lifecycle(
                subscriber,
                thread_id=thread_id,
                turn_id=turn_id,
                timeout=120,
                already_started=True,
            )
            assert lifecycle.started
            assert lifecycle.completed_status == "completed"
            assert lifecycle.final_text == "RODEX_FIDELITY_OK"
            assert set(lifecycle.observed_dispatch_ids) == {
                "rodex:live:start-fidelity",
                "rodex:live:steer-fidelity",
            }
            assert lifecycle.unexpected_server_requests == ()

            read_response = _request(
                subscriber,
                "thread:read:dispatch-history",
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            persisted_thread = read_response["result"]["thread"]
            persisted_turn = next(
                candidate
                for candidate in persisted_thread["turns"]
                if candidate.get("id") == turn_id
            )
            persisted_dispatch_ids = {
                item.get("clientId")
                for item in persisted_turn["items"]
                if item.get("type") == "userMessage"
            }
            assert persisted_dispatch_ids == {
                "rodex:live:start-fidelity",
                "rodex:live:steer-fidelity",
            }
            _request(
                subscriber,
                "thread:delete:test-created",
                "thread/delete",
                {"threadId": thread_id},
            )
            thread_id = None
    finally:
        if thread_id is not None and process.poll() is None:
            with suppress(Exception), _connect(socket_path) as cleanup:
                _initialize(cleanup, "cleanup")
                _request(
                    cleanup,
                    "thread:delete:test-created",
                    "thread/delete",
                    {"threadId": thread_id},
                )
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(integration_root)


@pytest.mark.evolutionary_regression
def test_live_user_input_routes_to_subscriber_after_initiator_disconnect() -> None:
    """Current contract: subscribed primary owns request_user_input."""
    live_user_input_environment = "RODEX_RUN_LIVE_USER_INPUT_INTEGRATION"
    if os.environ.get(live_user_input_environment) != "1":
        pytest.skip(
            f"set {live_user_input_environment}=1 to run the authenticated "
            "model-backed test"
        )
    codex_binary = shutil.which("codex")
    if codex_binary is None:
        pytest.fail("opted-in live test requires Codex CLI")
    integration_root = Path(
        tempfile.mkdtemp(prefix="input-it-", dir=default_runtime_root())
    )
    integration_root.chmod(0o700)
    workspace = integration_root / "workspace"
    workspace.mkdir(mode=0o700)
    socket_path = integration_root / "app.sock"
    process = subprocess.Popen(
        CODEX_APP_SERVER.command(codex_binary, socket_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_socket(process, socket_path)
        with _connect(socket_path) as subscriber:
            _initialize(subscriber, "user-input-subscriber", experimental_api=True)
            model_response = _request(
                subscriber,
                "model:list",
                "model/list",
                {},
            )
            models = model_response["result"].get("data", [])
            default_model = next(
                model["model"]
                for model in models
                if isinstance(model, dict) and model.get("isDefault") is True
            )
            with _connect(socket_path) as initiator:
                _initialize(initiator, "user-input-initiator", experimental_api=True)
                initiator_server_requests: list[str] = []
                thread_response = _request(
                    subscriber,
                    "user-input:thread:start",
                    "thread/start",
                    {"ephemeral": True, "cwd": str(workspace)},
                )
                thread_id = thread_response["result"]["thread"]["id"]
                _request(
                    initiator,
                    "user-input:thread:read",
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": False},
                )
                turn_response = _request(
                    initiator,
                    "user-input:turn:start",
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": (
                                    "Call request_user_input exactly once before "
                                    "answering. Ask one question with id fidelity_choice, "
                                    "header Fidelity, question 'Choose the fidelity "
                                    "path.', and options Alpha and Beta. After receiving "
                                    "the answer, reply exactly "
                                    "RODEX_USER_INPUT_OK:<selected label>. Do not use "
                                    "other tools."
                                ),
                            }
                        ],
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "readOnly"},
                        "collaborationMode": {
                            "mode": "plan",
                            "settings": {
                                "model": default_model,
                                "developer_instructions": None,
                            },
                        },
                    },
                    timeout=30,
                    observed_server_requests=initiator_server_requests,
                )
                turn = turn_response["result"]["turn"]
                assert turn["status"] == "inProgress"
                turn_id = turn["id"]
                initiator.close()

            lifecycle = _answer_exact_user_input(
                subscriber,
                thread_id=thread_id,
                turn_id=turn_id,
                timeout=120,
            )

        assert lifecycle.request_received
        assert initiator_server_requests == []
        assert lifecycle.request_resolved
        assert lifecycle.selected_answer is not None
        assert lifecycle.completed_status == "completed"
        assert lifecycle.final_text == (f"RODEX_USER_INPUT_OK:{lifecycle.selected_answer}")
        assert lifecycle.unexpected_server_requests == ()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(integration_root)


def _connect(socket_path: Path) -> Any:
    return unix_connect(
        str(socket_path),
        uri=f"ws://localhost{CODEX_APP_SERVER.rpc_connection_path}",
        compression=None,
        open_timeout=2,
        close_timeout=1,
        max_size=None,
    )


def _wait_for_socket(process: subprocess.Popen[bytes], socket_path: Path) -> None:
    deadline = time.monotonic() + 10
    while not socket_path.exists():
        if process.poll() is not None:
            pytest.fail(f"Codex App Server exited with status {process.returncode}")
        if time.monotonic() >= deadline:
            pytest.fail("Codex App Server did not bind its Unix socket")
        time.sleep(0.02)


def _initialize(
    websocket: Any,
    name: str,
    *,
    experimental_api: bool = False,
) -> None:
    client = AppServerClientInfo("rodex-live-integration", name, "1")
    response = _request(
        websocket,
        f"{name}:initialize",
        CODEX_APP_SERVER.initialize_method,
        CODEX_APP_SERVER.initialize_params(
            client,
            experimental_api=experimental_api,
        ),
    )
    CODEX_APP_SERVER.require_minimum_version(response["result"])
    websocket.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))


def _request(
    websocket: Any,
    request_id: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 5,
    observed_server_requests: list[str] | None = None,
) -> dict[str, Any]:
    websocket.send(json.dumps(CODEX_APP_SERVER.request(request_id, method, params)))
    response = _response_for(
        websocket,
        request_id,
        timeout=timeout,
        observed_server_requests=observed_server_requests,
    )
    assert "error" not in response, response.get("error")
    return response


def _wait_for_exact_turn_started(
    websocket: Any,
    *,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for exact turn {turn_id} to start")
        payload = json.loads(websocket.recv(timeout=remaining))
        params = payload.get("params")
        if (
            payload.get("method") != "turn/started"
            or not isinstance(params, dict)
            or params.get("threadId") != thread_id
        ):
            continue
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return


def _exact_turn_lifecycle(
    websocket: Any,
    *,
    thread_id: str,
    turn_id: str,
    timeout: float,
    already_started: bool = False,
) -> _LiveTurnLifecycle:
    deadline = time.monotonic() + timeout
    started = already_started
    final_text: str | None = None
    observed_dispatch_ids: list[str] = []
    unexpected_server_requests: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for exact turn {turn_id}")
        payload = json.loads(websocket.recv(timeout=remaining))
        if "method" in payload and "id" in payload:
            method = str(payload["method"])
            unexpected_server_requests.append(method)
            websocket.send(
                json.dumps(
                    {
                        "id": payload["id"],
                        "error": {
                            "code": -32601,
                            "message": "unsupported in live lifecycle test",
                        },
                    }
                )
            )
            continue
        method = payload.get("method")
        params = payload.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            continue
        if method == "turn/started":
            turn = params.get("turn", {})
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                started = True
        elif method == "item/completed" and params.get("turnId") == turn_id:
            item = params.get("item", {})
            if isinstance(item, dict) and item.get("type") == "userMessage":
                client_id = item.get("clientId")
                if isinstance(client_id, str):
                    observed_dispatch_ids.append(client_id)
            elif (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("phase") == "final_answer"
            ):
                text = item.get("text")
                final_text = text if isinstance(text, str) else None
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return _LiveTurnLifecycle(
                    started,
                    str(turn.get("status")),
                    final_text,
                    tuple(observed_dispatch_ids),
                    tuple(unexpected_server_requests),
                )


def _answer_exact_user_input(
    websocket: Any,
    *,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> _LiveUserInputLifecycle:
    deadline = time.monotonic() + timeout
    request_received = False
    request_resolved = False
    selected_answer: str | None = None
    final_text: str | None = None
    request_id: object | None = None
    unexpected_server_requests: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for exact turn {turn_id}")
        payload = json.loads(websocket.recv(timeout=remaining))
        if "method" in payload and "id" in payload:
            method = str(payload["method"])
            params = payload.get("params")
            if method != "item/tool/requestUserInput":
                unexpected_server_requests.append(method)
                websocket.send(
                    json.dumps(
                        {
                            "id": payload["id"],
                            "error": {
                                "code": -32601,
                                "message": "unsupported in live user-input test",
                            },
                        }
                    )
                )
                continue
            assert isinstance(params, dict)
            assert params.get("threadId") == thread_id
            assert params.get("turnId") == turn_id
            questions = params.get("questions")
            assert isinstance(questions, list) and len(questions) == 1
            question = questions[0]
            assert isinstance(question, dict)
            question_id = question.get("id")
            assert isinstance(question_id, str)
            options = question.get("options")
            assert isinstance(options, list) and options
            first_option = options[0]
            assert isinstance(first_option, dict)
            option_label = first_option.get("label")
            assert isinstance(option_label, str)
            selected_answer = option_label
            request_id = payload["id"]
            request_received = True
            websocket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "result": {
                            "answers": {
                                question_id: {"answers": [selected_answer]},
                            }
                        },
                    }
                )
            )
            continue
        method = payload.get("method")
        params = payload.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            continue
        if method == "serverRequest/resolved" and params.get("requestId") == request_id:
            request_resolved = True
        elif method == "item/completed" and params.get("turnId") == turn_id:
            item = params.get("item", {})
            if (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("phase") == "final_answer"
            ):
                text = item.get("text")
                final_text = text if isinstance(text, str) else None
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return _LiveUserInputLifecycle(
                    request_received,
                    request_resolved,
                    selected_answer,
                    str(turn.get("status")),
                    final_text,
                    tuple(unexpected_server_requests),
                )


def _response_for(
    websocket: Any,
    request_id: str,
    *,
    timeout: float = 5,
    observed_server_requests: list[str] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for App Server request {request_id}")
        payload = json.loads(websocket.recv(timeout=remaining))
        if (
            observed_server_requests is not None
            and isinstance(payload, dict)
            and isinstance(payload.get("method"), str)
            and "id" in payload
        ):
            observed_server_requests.append(payload["method"])
            websocket.send(
                json.dumps(
                    {
                        "id": payload["id"],
                        "error": {
                            "code": -32601,
                            "message": "unexpected request on mutation initiator",
                        },
                    }
                )
            )
            continue
        if (
            isinstance(payload, dict)
            and payload.get("id") == request_id
            and ("result" in payload or "error" in payload)
        ):
            return payload
