"""Running helpers retain their version when an editable checkout is replaced."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sysconfig
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.implementation_identity import FIRST_PARTY_PACKAGES
from rodex.installation import RodexInstallationError, prepare_installation

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def development_installation(tmp_path):
    source = tmp_path / "checkout/src"
    for package in FIRST_PARTY_PACKAGES:
        shutil.copytree(PROJECT / "src" / package, source / package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PROJECT / "conf", source.parent / "conf")
    library = tmp_path / "dependencies"
    shutil.copytree(Path(sysconfig.get_path("purelib")), library, ignore=shutil.ignore_patterns("__pycache__"))
    (library / "installation_probe_dependency.py").write_text("VERSION = 'A'\n")
    return source, library


def publish(tmp_path, development):
    source, library = development
    return prepare_installation(tmp_path / "installations", source_root=source, site_packages=library)


def info(interpreter, *, cwd=None, environment=None):
    code = """
import json, sys
import installation_probe_dependency as dependency
from rodex.implementation_identity import RODEX_IMPLEMENTATION_ID
from rodex.daemon_client import RODEX_DAEMON_PROTOCOL
from rodex.tmux_session_capability import RODEX_SHARED_TMUX_PROTOCOL
from rodex.user_prompt_hook import USER_PROMPT_SUBSTITUTIONS_PATH
from rodex_sql import RODEX_DATABASE_FILENAME
print(json.dumps(dict(identity=RODEX_IMPLEMENTATION_ID, daemon=RODEX_DAEMON_PROTOCOL,
    tmux=RODEX_SHARED_TMUX_PROTOCOL, database=RODEX_DATABASE_FILENAME,
    dependency=dependency.VERSION, configuration=USER_PROMPT_SUBSTITUTIONS_PATH.read_text(),
    python=sys.executable)))
"""
    completed = subprocess.run(
        [interpreter, "-I", "-c", code], cwd=cwd, env=environment, capture_output=True, text=True, check=True, timeout=15
    )
    return json.loads(completed.stdout)


def test_published_installation_keeps_code_dependencies_and_defaults_after_replacement(
    tmp_path, development_installation
):
    source, library = development_installation
    first = publish(tmp_path, development_installation)
    before = info(first)
    edits = (
        (source / "rodex/version.py", "0.15.0a1", "0.16.0a1"),
        (source / "rodex/daemon_client.py", "rodex-daemon-v3", "rodex-daemon-v4"),
        (source / "rodex/tmux_session_capability.py", "PROTOCOL_GENERATION: Final = 5", "PROTOCOL_GENERATION: Final = 6"),
        (source / "rodex_sql/private_database_path.py", "Final = 21", "Final = 22"),
        (library / "installation_probe_dependency.py", "'A'", "'B'"),
    )
    for path, old, new in edits:
        original = path.read_text()
        assert old in original
        path.write_text(original.replace(old, new))
    configuration = source.parent / "conf/hooks/user_prompt_substitutions.yaml"
    configuration.write_text(configuration.read_text() + "\n# replacement defaults\n")
    second = publish(tmp_path, development_installation)
    after = info(second)
    assert first != second
    assert after["dependency"] == "B"
    assert after["database"] == "rodex-v22.sqlite3"
    assert after["daemon"] == "rodex-daemon-v4"
    assert after["tmux"] == "rodex-isolated-tmux-v6"
    assert after["configuration"].endswith("# replacement defaults\n")
    assert info(first, cwd=source, environment={**os.environ, "PYTHONPATH": str(source)}) == before
    # Removing the mutable bootstrap checkout and dependencies cannot break old helpers.
    shutil.rmtree(source.parent)
    shutil.rmtree(library)
    assert info(first) == before
    assert info(second) == after


def test_configuration_only_change_publishes_new_defaults(tmp_path, development_installation):
    first = publish(tmp_path, development_installation)
    source, _library = development_installation
    (source.parent / "conf/hooks/user_prompt_substitutions.yaml").write_text("[]\n")
    second = publish(tmp_path, development_installation)
    assert first != second
    assert info(second)["configuration"] == "[]\n"
    assert info(first)["configuration"] != "[]\n"


def test_concurrent_launches_publish_one_complete_installation(tmp_path, development_installation):
    with ThreadPoolExecutor(max_workers=3) as executor:
        interpreters = list(executor.map(lambda _: publish(tmp_path, development_installation), range(3)))
    assert len(set(interpreters)) == 1
    assert info(interpreters[0])["dependency"] == "A"
    assert not list((tmp_path / "installations").glob(".preparing-*"))


def test_external_editable_dependency_is_rejected(tmp_path, development_installation):
    _source, library = development_installation
    (library / "external.pth").write_text(str(tmp_path / "mutable-external") + "\n")
    with pytest.raises(RodexInstallationError, match="external dependency"):
        publish(tmp_path, development_installation)
    assert not (tmp_path / "installations").exists()


def test_replaced_checkout_cannot_redirect_retained_tmux_hook(tmp_path, development_installation):
    """Actual tmux roster → fresh hook process → exact old daemon → resize callback."""
    first = publish(tmp_path, development_installation)
    old = info(first)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    runtime_root = Path(f"/proc/{os.getpid()}/fd/{directory_fd}")
    process = subprocess.Popen(
        [first, "-I", str(PROJECT / "tests/installation_daemon_fixture.py"), str(runtime_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = json.loads(process.stdout.readline())
        deadline = time.monotonic() + 5
        while not Path(ready["socket"]).exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with tmux_terminal(tmp_path / "terminal") as terminal:
            server_id = "a" * 32
            terminal.tmux("set-option", "-s", "@rodex_shared_tmux_protocol", old["tmux"])
            terminal.tmux("set-option", "-s", "@rodex_shared_tmux_server_id", server_id)
            # The hook derives its daemon root from the real tmux socket parent.
            # Expose that socket under the same short runtime root as the daemon.
            (tmp_path / "tmux.sock").symlink_to(tmp_path / "terminal/tmux.sock")
            options = {
                "@rodex_primary_pane_id": "%0",
                "@rodex_sharing_attached_count": "0",
                "@rodex_runtime_id": "0123456789abcdef",
                "@rodex_registration_state": "registered",
                "@rodex_session_id": "1111111111111111",
                "@rodex_registry_id": "2222222222222222",
                "@rodex_sessions_id": "1",
                "@rodex_codex_session_id": "01a00654-f2bc-7a30-834a-a5f886a65f82",
            }
            for name, value in options.items():
                terminal.tmux("set-option", "-t", "fixture", name, value)
            hook_code = """
import sys
from pathlib import Path
from rodex.tmux_sharing_coordinator import sharing_coordinator_hook_command
print(sharing_coordinator_hook_command(sys.executable, '/usr/bin/tmux', Path(sys.argv[1]), sys.argv[2]))
"""
            hook = subprocess.check_output(
                [first, "-I", "-c", hook_code, str(runtime_root / "tmux.sock"), server_id], text=True, timeout=15
            ).strip()
            source, library = development_installation
            for path, old_text, new_text in (
                (source / "rodex/version.py", "0.15.0a1", "0.16.0a1"),
                (
                    source / "rodex/tmux_session_capability.py",
                    "PROTOCOL_GENERATION: Final = 5",
                    "PROTOCOL_GENERATION: Final = 6",
                ),
                (library / "installation_probe_dependency.py", "'A'", "'B'"),
            ):
                path.write_text(path.read_text().replace(old_text, new_text))
            second = publish(tmp_path, development_installation)
            assert info(second)["identity"] != old["identity"]
            environment = {**os.environ, "PYTHONPATH": str(source)}
            result = subprocess.run(["/bin/sh", "-c", shlex.split(hook)[-1]], cwd=source, env=environment, timeout=15)
            assert result.returncode == 0
            events = tmp_path / f"{old['identity']}.events"
            assert json.loads(events.read_text()) == ["0123456789abcdef", "terminal_resize"]
            # Even explicitly aiming the new implementation at the old daemon is rejected.
            new = info(second)
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(5)
                connection.connect(ready["socket"])
                connection.sendall(
                    json.dumps(
                        {
                            "protocol": new["daemon"],
                            "implementation_id": new["identity"],
                            "operation": "wake_runtime",
                            "runtime_id": "0123456789abcdef",
                            "cause": "terminal_resize",
                        }
                    ).encode()
                    + b"\n"
                )
                response = json.loads(connection.recv(65536))
            assert response["ok"] is False
            assert "implementation" in response["error"]
            assert len(events.read_text().splitlines()) == 1
    finally:
        process.terminate()
        _output, errors = process.communicate(timeout=10)
        os.close(directory_fd)
    assert process.returncode == 0, errors


def test_installation_store_stays_outside_bootstrap_environment(tmp_path, monkeypatch):
    import rodex.installation as installation

    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.delenv("RODEX_INSTALLATIONS_ROOT", raising=False)
    stores = []
    interpreter = tmp_path / "retained/.venv/bin/python"
    monkeypatch.setattr(installation, "prepare_installation", lambda store: stores.append(store) or interpreter)

    class ExecObserved(Exception):
        pass

    def observe_exec(executable, arguments, environment):
        assert executable == interpreter
        assert arguments[:3] == [str(interpreter), "-I", "-c"]
        assert environment["XDG_STATE_HOME"] == str(state_home)
        raise ExecObserved

    monkeypatch.setattr(installation.os, "execve", observe_exec)
    with pytest.raises(ExecObserved):
        installation.enter_fixed_installation()
    assert stores == [state_home / "rodex/implementations"]
    assert (state_home / "rodex").stat().st_mode & 0o777 == 0o700


def test_changed_copy_is_not_published(tmp_path, development_installation, monkeypatch):
    import rodex.installation as installation

    actual_copy = installation.shutil.copytree

    def changed_copy(source, destination, *args, **kwargs):
        result = actual_copy(source, destination, *args, **kwargs)
        if source == development_installation[0] / "rodex":
            (destination / "version.py").write_text("RODEX_VERSION = 'incomplete-copy'\n")
        return result

    monkeypatch.setattr(installation.shutil, "copytree", changed_copy)
    with pytest.raises(RodexInstallationError, match="changed while preparing"):
        publish(tmp_path, development_installation)
    assert all(path.suffix == ".lock" for path in (tmp_path / "installations").iterdir())
