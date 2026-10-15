"""Native startup identity with simultaneous persistent and temporary threads.

The model and Codex home are isolated. Native RPC is driven explicitly so the
helper is guaranteed loaded before launcher discovery, independent of timing.
"""

import shutil
from types import SimpleNamespace
from uuid import UUID

import pytest
from command_policy_native_fixture import NativeClient, native_policy_fixture
from runtime_peer_fixtures import TEST_PEER, LiveTestProcess
from websockets.sync.client import unix_connect

from rodex.interaction_transport import read_primary_thread_id
from rodex.protocol_proxy import CodexProtocolProxy, ToolCallCounter
from rodex.runtime import RodexRuntimeLauncher


@pytest.mark.skipif(shutil.which("codex") is None, reason="installed native Codex is required")
@pytest.mark.parametrize("lifecycle", ["start", "resume", "fork"])
def test_native_discovery_identifies_main_while_helper_remains_loaded(monkeypatch, lifecycle):
    with native_policy_fixture() as native:
        saved_root = None
        if lifecycle != "start":
            with native.app_server(injection=False) as (seed, *_):
                saved_root = seed.rpc("thread/start", {"cwd": str(native.workspace)})["thread"]["id"]
                seed.rpc("turn/start", {"threadId": saved_root, "input": [{"type": "text", "text": "Seed history."}]})
                seed.complete_turn()
        with native.app_server(injection=False) as (_unused, _observed, process, app):
            path = native.root / "proxy.sock"
            with (
                CodexProtocolProxy(
                    path,
                    app,
                    ToolCallCounter(lambda _: None),
                    peer_identity=TEST_PEER,
                    app_server_process=process,
                    native_tui_process=LiveTestProcess(),
                ),
                unix_connect(str(path), additional_headers=TEST_PEER.headers()) as connection,
            ):
                client = NativeClient(connection)
                client.rpc(
                    "initialize",
                    {
                        "clientInfo": {"name": "rodex-startup-test", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                params = {"cwd": str(native.workspace)} if lifecycle == "start" else {"threadId": saved_root}
                root = client.rpc(f"thread/{lifecycle}", params)["thread"]["id"]
                helper = client.rpc("thread/start", {"cwd": str(native.workspace), "ephemeral": True})["thread"]["id"]
                assert root != helper
                assert {root, helper} <= set(client.rpc("thread/loaded/list", {})["data"])
                launcher = RodexRuntimeLauncher("codex", "tmux")
                monkeypatch.setattr(launcher, "_bootstrap_session_exists", lambda _runtime: True)
                runtime = SimpleNamespace(
                    protocol_proxy_socket_path=path,
                    runtime_id=TEST_PEER.runtime_id,
                    creation_server_id=TEST_PEER.tmux_server_id,
                    app_server_log_path=native.root / "unused.log",
                )
                assert launcher._wait_for_primary_codex_session_id(runtime) == UUID(root)
                assert read_primary_thread_id(path, peer_identity=TEST_PEER) == root
                if lifecycle == "resume":
                    assert root == saved_root
                elif lifecycle == "fork":
                    assert root != saved_root
