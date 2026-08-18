from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.client import unix_connect

from rodex.app_server_contract import require_supported_app_server
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
        [codex_binary, "app-server", "--listen", f"unix://{socket_path}"],
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
            uri="ws://localhost/rpc",
            compression=None,
            open_timeout=2,
            close_timeout=1,
            max_size=None,
        ) as websocket:
            initialize_id = "rodex:integration:initialize"
            websocket.send(
                json.dumps(
                    {
                        "id": initialize_id,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {
                                "name": "rodex-integration",
                                "title": "Rodex Integration",
                                "version": "1",
                            }
                        },
                    }
                )
            )
            initialized = _response_for(websocket, initialize_id)
            assert initialized["id"] == initialize_id
            require_supported_app_server(initialized["result"])
            websocket.send(json.dumps({"method": "initialized", "params": {}}))

            loaded_id = "rodex:integration:loaded"
            websocket.send(
                json.dumps({"id": loaded_id, "method": "thread/loaded/list", "params": {}})
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
    unexpected_server_requests: tuple[str, ...]


@pytest.mark.evolutionary_regression
def test_live_turn_survives_initiator_disconnect_and_streams_to_subscriber() -> None:
    """Current 0.147 evidence: the subscribed primary owns lifecycle and approvals."""
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
        [codex_binary, "app-server", "--listen", f"unix://{socket_path}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
                    {"ephemeral": True, "cwd": str(workspace)},
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
                                    "Reply exactly RODEX_FIDELITY_OK. Do not use tools."
                                ),
                            }
                        ],
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "readOnly"},
                    },
                    timeout=30,
                )
                turn = turn_response["result"]["turn"]
                assert turn["status"] == "inProgress"
                turn_id = turn["id"]
                initiator.close()

            lifecycle = _exact_turn_lifecycle(
                subscriber,
                thread_id=thread_id,
                turn_id=turn_id,
                timeout=120,
            )

        assert lifecycle.started
        assert lifecycle.completed_status == "completed"
        assert lifecycle.final_text == "RODEX_FIDELITY_OK"
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
        uri="ws://localhost/rpc",
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


def _initialize(websocket: Any, name: str) -> None:
    response = _request(
        websocket,
        f"{name}:initialize",
        "initialize",
        {
            "clientInfo": {
                "name": "rodex-live-integration",
                "title": name,
                "version": "1",
            }
        },
    )
    require_supported_app_server(response["result"])
    websocket.send(json.dumps({"method": "initialized", "params": {}}))


def _request(
    websocket: Any,
    request_id: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 5,
) -> dict[str, Any]:
    websocket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    response = _response_for(websocket, request_id, timeout=timeout)
    assert "error" not in response, response.get("error")
    return response


def _exact_turn_lifecycle(
    websocket: Any,
    *,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> _LiveTurnLifecycle:
    deadline = time.monotonic() + timeout
    started = False
    final_text: str | None = None
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
                return _LiveTurnLifecycle(
                    started,
                    str(turn.get("status")),
                    final_text,
                    tuple(unexpected_server_requests),
                )


def _response_for(websocket: Any, request_id: str, *, timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for App Server request {request_id}")
        payload = json.loads(websocket.recv(timeout=remaining))
        if (
            isinstance(payload, dict)
            and payload.get("id") == request_id
            and ("result" in payload or "error" in payload)
        ):
            return payload
