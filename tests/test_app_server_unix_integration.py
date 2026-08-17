from __future__ import annotations

import json
import shutil
import stat
import subprocess
import tempfile
import time
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


def _response_for(websocket: Any, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for App Server request {request_id}")
        payload = json.loads(websocket.recv(timeout=remaining))
        if isinstance(payload, dict) and payload.get("id") == request_id:
            return payload
