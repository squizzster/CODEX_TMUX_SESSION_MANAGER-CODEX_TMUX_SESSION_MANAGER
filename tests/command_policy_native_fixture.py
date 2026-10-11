"""Installed Codex with a local deterministic Responses fixture; no model quota."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.sync.client import unix_connect

from rodex.command_policy import POLICY_SOCKET_ENV, CommandPolicyBroker, evaluate_command_policy
from rodex.command_policy_configuration import policy_hook_override


class FixtureModel(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        turn_input = request.get("input", [])
        current_turn_start = 0
        for index, item in enumerate(turn_input):
            if item.get("role") == "user":
                current_turn_start = index + 1
        turn_input = turn_input[current_turn_start:]
        completed = any(item.get("type") == "function_call_output" for item in turn_input)
        if completed:
            item = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Fixture command completed.", "annotations": []}],
            }
        else:
            item = {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "exec_command",
                "arguments": json.dumps(self.server.tool_input),
            }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = {
            "id": "resp_1",
            "object": "response",
            "model": "gpt-5.4",
            "status": "completed",
            "output": [item],
            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
        }
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        for event in events:
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()


class NativePolicyFixture:
    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.codex_home = root / "codex"
        self.codex_home.mkdir(mode=0o700)
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
        self.model.requests = []
        self.model.tool_input = {"cmd": "find . -maxdepth 0 -print", "yield_time_ms": 1000}
        self.worker = threading.Thread(target=self.model.serve_forever, daemon=True)
        self.worker.start()
        self.configuration = (
            'model_provider="rodex_test"\nmodel="gpt-5.4"\nmodel_reasoning_effort="low"\n'
            'sandbox_mode="read-only"\napproval_policy="never"\ncheck_for_update_on_startup=false\n'
            "[features]\ncode_mode=false\nhooks=true\n"
            f'[projects.{json.dumps(str(self.workspace))}]\ntrust_level="trusted"\n'
            '[model_providers.rodex_test]\nname="rodex_test"\nwire_api="responses"\n'
            f'base_url="http://127.0.0.1:{self.model.server_port}/v1"\n'
        )
        (self.codex_home / "config.toml").write_text(self.configuration)
        self.environment = {
            **os.environ,
            "CODEX_HOME": str(self.codex_home),
            "TERM": "xterm-256color",
            "RODEX_RUNTIME_DIR": str(root / "runtime"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "RODEX_CODEX_BINARY": shutil.which("codex"),
            "RODEX_TMUX_BINARY": shutil.which("tmux"),
        }
        self.environment.pop("TMUX", None)
        self.environment.pop("TMUX_PANE", None)
        self.sequence = 0

    def close(self):
        self.model.shutdown()
        self.model.server_close()
        self.worker.join(timeout=2)

    @contextmanager
    def app_server(self, *, injection=True, extra=(), policy=evaluate_command_policy):
        self.sequence += 1
        runtime_id = f"{self.sequence:016x}"
        app = self.root / f"app-{self.sequence}.sock"
        path = self.root / f"policy-{runtime_id}.sock"
        observed = []
        broker = CommandPolicyBroker(path, runtime_id, observed.append, policy=policy)
        broker.start()
        command = [self.environment["RODEX_CODEX_BINARY"], "app-server", "--listen", f"unix://{app}"]
        if injection:
            command += ["-c", policy_hook_override()]
        command += list(extra)
        process = subprocess.Popen(
            command,
            env={**self.environment, POLICY_SOCKET_ENV: str(path)},
            cwd=self.workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        broker.bind_app_server(process)
        try:
            deadline = time.monotonic() + 15
            while not app.exists():
                assert process.poll() is None, "native App Server exited"
                assert time.monotonic() < deadline, "native App Server did not bind"
                time.sleep(0.01)
            with unix_connect(str(app), uri="ws://localhost/rpc", close_timeout=1, compression=None) as connection:
                client = NativeClient(connection)
                client.rpc(
                    "initialize",
                    {
                        "clientInfo": {"name": "rodex-policy-test", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                connection.send(json.dumps({"method": "initialized", "params": {}}))
                yield client, observed, process, app
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            broker.close()


class NativeClient:
    def __init__(self, connection):
        self.connection = connection
        self.events = []
        self.counter = 0

    def rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + 20
        while True:
            event = json.loads(self.connection.recv(timeout=max(0, deadline - time.monotonic())))
            if event.get("id") == request_id and "method" not in event:
                assert "error" not in event, event
                return event["result"]
            self.events.append(event)

    def complete_turn(self, *, approval_decision=None):
        deadline = time.monotonic() + 30
        while True:
            event = json.loads(self.connection.recv(timeout=max(0, deadline - time.monotonic())))
            self.events.append(event)
            if event.get("method") == "item/commandExecution/requestApproval" and approval_decision is not None:
                self.connection.send(json.dumps({"id": event["id"], "result": {"decision": approval_decision}}))
            if event.get("method") == "turn/completed":
                assert event["params"]["turn"]["status"] == "completed", event
                return


@contextmanager
def native_policy_fixture():
    with tempfile.TemporaryDirectory(prefix="rp-native-") as directory:
        fixture = NativePolicyFixture(Path(directory))
        try:
            yield fixture
        finally:
            fixture.close()
