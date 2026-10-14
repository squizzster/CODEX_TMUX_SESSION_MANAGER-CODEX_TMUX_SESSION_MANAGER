"""Real Codex/tmux submission through Rodex; isolated local model, no credentials."""

import json
import os
import shutil
import time
from pathlib import Path

import pyte
import pytest
from command_policy_native_fixture import native_policy_fixture
from test_command_policy_native import await_screen, native_key
from test_managed_startup import RodexTerminalClient, _stop_fixture_daemon

from rodex.native_composer import matches_composer


def await_native_draft(client, expected):
    """Observe the active cursor row, never a matching earlier transcript message."""
    screen = pyte.Screen(120, 36)
    stream = pyte.ByteStream(screen)
    offset = 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        client.poll()
        stream.feed(client.output[offset:])
        offset = len(client.output)
        if matches_composer((screen.display[screen.cursor.y],), screen.cursor.x, expected, screen.columns):
            return
        assert client.exit_code is None
    pytest.fail(f"native editor did not restore {expected!r}:\n" + "\n".join(screen.display))


@pytest.mark.parametrize("route,enter", [("terminal", b"\r"), ("terminal", b"\r\n"), ("initial_argv", None)])
def test_native_submission_transforms_once_and_preserves_original_recall(route, enter):
    if not shutil.which("codex") or not shutil.which("tmux"):
        pytest.skip("installed Codex and tmux are required")
    project = Path(__file__).parents[1]
    with native_policy_fixture() as native:
        (native.root / "state/rodex").mkdir(mode=0o700, parents=True)
        rules = native.root / "state/rodex/conf/hooks/user_prompt_substitutions.yaml"
        rules.parent.mkdir(parents=True)
        # Override the shipped named rule: applying twice produces Hello!!.
        rules.write_text("- name: Add enthusiasm to Hello\n  match: '^Hello!*$'\n  replace: '\\g<0>!'\n")
        native.model.tool_input = {"cmd": "true", "yield_time_ms": 1000}
        environment = {**native.environment, "RODEX_PROJECT_DIR": str(project)}
        runtime_root = Path(environment["RODEX_RUNTIME_DIR"])
        command = [str(project / "rodex")]
        if route == "initial_argv":
            command.append("Hello")
        try:
            with RodexTerminalClient(command, environment, native.workspace) as client:
                client.wait_for_attach()
                if route == "terminal":
                    await_screen(client, "\u203a ")  # Empty composer includes a native placeholder.
                    native_key(client, b"Hello")
                    await_native_draft(client, "Hello")
                    os.write(client.terminal, enter)

                def request_user_texts(request):
                    return [
                        part.get("text")
                        for item in request.get("input", [])
                        if item.get("role") == "user"
                        for part in item.get("content", [])
                        if part.get("type") == "input_text"
                    ]

                def submitted_texts():
                    # Title-generation/internal requests have different text;
                    # only the exact fixture prompt is relevant to this assertion.
                    return [text for request in tuple(native.model.requests) for text in request_user_texts(request)]

                deadline = time.monotonic() + 20
                while "Hello!" not in submitted_texts() and time.monotonic() < deadline:
                    client.poll()
                    assert client.exit_code is None
                assert "Hello!" in submitted_texts()
                assert "Hello!!" not in submitted_texts()
                assert "Hello" not in submitted_texts()
                await_screen(client, "Fixture command completed.")
                history = [
                    json.loads(line)["text"] for line in (native.codex_home / "history.jsonl").read_text().splitlines()
                ]
                assert history == ["Hello"]
                if route == "terminal":
                    # Native recall is original input, so repeating it does not
                    # apply a non-idempotent rule to an earlier transformed value.
                    native_key(client, b"\x1b[A")
                    await_native_draft(client, "Hello")
                    os.write(client.terminal, b"\r")
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        client.poll()
                        if any(request_user_texts(req).count("Hello!") == 2 for req in tuple(native.model.requests)):
                            break
                    screen = pyte.Screen(120, 36)
                    pyte.ByteStream(screen).feed(client.output)
                    assert any(request_user_texts(req).count("Hello!") == 2 for req in tuple(native.model.requests)), (
                        "\n".join(screen.display)
                    )
                    history = [
                        json.loads(line)["text"]
                        for line in (native.codex_home / "history.jsonl").read_text().splitlines()
                    ]
                    assert history and set(history) == {"Hello"}  # Native may coalesce identical recall entries.
                    assert "Hello!!" not in submitted_texts()
        finally:
            _stop_fixture_daemon(runtime_root)
