"""A redirected pre-retention hook can still reach only its exact live daemon."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from rodex.legacy_runtime_compat import (
    LEGACY_DAEMON_PROTOCOL,
    LEGACY_PROCESS_RECEIPT_PROTOCOL,
    LegacyRuntimeCompatibilityError,
    notify_legacy_runtime_resize,
)

RUNTIME_ID = "0123456789abcdef"
IMPLEMENTATION_DIGEST = "a" * 64
IMPLEMENTATION_ID = f"0.14.0a2+sha256.{IMPLEMENTATION_DIGEST}"


def _process_state(pid: int) -> tuple[int, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[2]), int(fields[19])


def _write_receipt(runtime_root: Path, process: subprocess.Popen[bytes]) -> None:
    process_group_id, start_time_ticks = _process_state(process.pid)
    receipt = runtime_root / f"{IMPLEMENTATION_DIGEST}.process-{RUNTIME_ID}-app-server.json"
    receipt.write_text(
        json.dumps(
            {
                "kind": "app-server",
                "operation_id": "0" * 32,
                "pid": process.pid,
                "process_group_id": process_group_id,
                "protocol": LEGACY_PROCESS_RECEIPT_PROTOCOL,
                "runtime_id": RUNTIME_ID,
                "start_time_ticks": start_time_ticks,
                "uid": os.getuid(),
            }
        )
        + "\n"
    )
    receipt.chmod(0o600)


def _serve_legacy_daemon(
    listener: socket.socket,
    requests: list[dict[str, object]],
    *,
    implementation_id: str = IMPLEMENTATION_ID,
    request_count: int = 2,
) -> None:
    for index in range(request_count):
        connection, _address = listener.accept()
        with connection:
            request = json.loads(connection.makefile("rb").readline())
            requests.append(request)
            response = {
                "protocol": LEGACY_DAEMON_PROTOCOL,
                "implementation_id": implementation_id,
                "ok": index == 1,
            }
            if index == 0:
                response["error"] = "daemon implementation does not match this generation"
            connection.sendall(json.dumps(response).encode() + b"\n")


@pytest.fixture
def legacy_runtime():
    runtime_root = Path(tempfile.mkdtemp(prefix="rodex-lr-", dir="/tmp"))
    runtime_root.chmod(0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(runtime_root / f"{IMPLEMENTATION_DIGEST}.sock"))
    listener.listen()
    Path(listener.getsockname()).chmod(0o600)
    process = subprocess.Popen(["/usr/bin/sleep", "30"], start_new_session=True)
    _write_receipt(runtime_root, process)
    try:
        yield runtime_root, process, listener
    finally:
        listener.close()
        process.terminate()
        process.wait(timeout=5)
        shutil.rmtree(runtime_root)


def test_resize_discovers_and_wakes_exact_receipt_bound_legacy_daemon(legacy_runtime) -> None:
    runtime_root, _process, listener = legacy_runtime
    requests: list[dict[str, object]] = []
    server = threading.Thread(target=_serve_legacy_daemon, args=(listener, requests), daemon=True)
    server.start()

    notify_legacy_runtime_resize(runtime_root, RUNTIME_ID)

    server.join(timeout=5)
    assert not server.is_alive()
    assert requests == [
        {
            "protocol": LEGACY_DAEMON_PROTOCOL,
            "implementation_id": "legacy-runtime-owner-discovery",
            "operation": "ping",
        },
        {
            "protocol": LEGACY_DAEMON_PROTOCOL,
            "implementation_id": IMPLEMENTATION_ID,
            "operation": "wake_runtime",
            "runtime_id": RUNTIME_ID,
            "cause": "terminal_resize",
        },
    ]


def test_resize_rejects_daemon_identity_not_bound_to_receipt(legacy_runtime) -> None:
    runtime_root, _process, listener = legacy_runtime
    requests: list[dict[str, object]] = []
    wrong_identity = f"0.14.0a2+sha256.{'b' * 64}"
    server = threading.Thread(
        target=_serve_legacy_daemon,
        args=(listener, requests),
        kwargs={"implementation_id": wrong_identity, "request_count": 1},
        daemon=True,
    )
    server.start()

    with pytest.raises(LegacyRuntimeCompatibilityError, match="receipt-bound implementation"):
        notify_legacy_runtime_resize(runtime_root, RUNTIME_ID)

    assert requests == [
        {
            "protocol": LEGACY_DAEMON_PROTOCOL,
            "implementation_id": "legacy-runtime-owner-discovery",
            "operation": "ping",
        }
    ]
