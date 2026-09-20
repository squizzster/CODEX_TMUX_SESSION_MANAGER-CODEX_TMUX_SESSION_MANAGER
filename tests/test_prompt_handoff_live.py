"""Real installed Rodex, tmux and Codex: one greeting submitted through a client PTY."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import yaml
from test_managed_startup import RodexTerminalClient, _require_startup_prerequisite, _stop_fixture_daemon
from websockets.sync.client import unix_connect

from rodex.runtime_peer import RuntimePeerIdentity, verified_runtime_connection
from rodex.tmux_session_capability import RODEX_TMUX_SOCKET_PATTERN


@pytest.mark.live_startup
@pytest.mark.parametrize(
    "effort,glyph,replacement",
    [
        ("xhigh", "\u203a", "Hello!"),
        ("ultra", "»", "Hello!"),
        ("ultra", "»", "Hello! " + "This is a greeting for the terminal rendering test. " * 3 + "\nHello again!"),
    ],
)
def test_real_codex_greeting_matches_terminal_and_persisted_input(tmp_path, request, effort, glyph, replacement):
    codex, tmux = shutil.which("codex"), shutil.which("tmux")
    _require_startup_prerequisite(request, bool(codex and tmux), "Codex and tmux are required")
    project = Path(__file__).parents[1]
    isolated_home = tmp_path / "codex"
    isolated_home.mkdir(mode=0o700)
    installed_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    (tmp_path / "state/rodex").mkdir(mode=0o700, parents=True)
    hook_path = tmp_path / "state/rodex/conf/hooks/user_prompt_substitutions.yaml"
    hook_path.parent.mkdir(parents=True)
    # A second application changes Hello! to Hello!!; receipt mistakes are visible.
    hook_path.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Add enthusiasm to Hello",
                    "match": "^Hello!*$",
                    "replace": r"\g<0>!" if replacement == "Hello!" else replacement,
                }
            ]
        )
    )
    for filename in ("auth.json", "config.toml"):
        source = installed_home / filename
        if source.is_file():
            shutil.copy2(source, isolated_home / filename)
    with tempfile.TemporaryDirectory(prefix="rdx-prompt-") as runtime_directory:
        runtime_root = Path(runtime_directory)
        environment = {
            **os.environ,
            "TERM": "xterm-256color",
            "RODEX_PROJECT_DIR": str(project),
            "RODEX_CODEX_BINARY": codex,
            "RODEX_TMUX_BINARY": tmux,
            "RODEX_RUNTIME_DIR": runtime_directory,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "CODEX_HOME": str(isolated_home),
        }
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        try:
            command = [str(project / "rodex"), "-c", f'model_reasoning_effort="{effort}"']
            with RodexTerminalClient(command, environment, project) as client:
                name = client.wait_for_attach()
                sockets = list(runtime_root.glob(RODEX_TMUX_SOCKET_PATTERN))
                assert len(sockets) == 1

                def capture():
                    return subprocess.run(
                        [tmux, "-N", "-S", str(sockets[0]), "capture-pane", "-p", "-S", "-100", "-t", f"={name}:"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=True,
                    ).stdout

                def await_screen(text):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        client.poll()
                        screen = capture()
                        if text in screen:
                            return screen
                    pytest.fail(f"Missing {text!r} in tmux screen: {screen}")

                await_screen("OpenAI Codex")
                # Human typing, then Enter: actual client PTY, never tmux key injection.
                for character in "Hello":
                    os.write(client.terminal, character.encode())
                    client.poll()
                await_screen(f"{glyph} Hello")
                identity = (
                    subprocess.run(
                        [
                            tmux,
                            "-N",
                            "-S",
                            str(sockets[0]),
                            "display-message",
                            "-p",
                            "-t",
                            f"={name}:",
                            "#{@rodex_runtime_id}|#{@rodex_shared_tmux_server_id}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=True,
                    )
                    .stdout.strip()
                    .split("|")
                )
                peer = RuntimePeerIdentity(*identity)
                with verified_runtime_connection(
                    unix_connect,
                    next(runtime_root.glob("events-*.sock")),
                    peer_identity=peer,
                    uri="ws://localhost",
                    compression=None,
                ) as connection:
                    connection.recv(timeout=5)  # Ready: subscription precedes Enter.
                    os.write(client.terminal, b"\r")
                    deadline = time.monotonic() + 15
                    texts = []
                    while time.monotonic() < deadline:
                        client.poll()
                        try:
                            frame = json.loads(connection.recv(timeout=0.1))
                        except TimeoutError:
                            continue
                        item = frame.get("params", {}).get("item", {})
                        if item.get("type") == "userMessage":
                            texts = [part["text"] for part in item["content"] if part["type"] == "text"]
                            break
                screen = capture()
                (tmp_path / "submitted-screen.txt").write_text(screen)
                assert texts == [replacement], (texts, screen)
                assert "\u203a Hello!" in screen or "» Hello!" in screen, screen
                assert "\u203a Hello\n" not in screen, screen
                history = [
                    json.loads(line)["text"] for line in (isolated_home / "history.jsonl").read_text().splitlines()
                ]
                assert history == [replacement]
        finally:
            for socket in runtime_root.glob(RODEX_TMUX_SOCKET_PATTERN):
                subprocess.run([tmux, "-N", "-S", str(socket), "kill-server"], capture_output=True, timeout=3)
            _stop_fixture_daemon(runtime_root)
