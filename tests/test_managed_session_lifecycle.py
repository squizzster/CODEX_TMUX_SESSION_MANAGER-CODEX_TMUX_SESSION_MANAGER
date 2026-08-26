from __future__ import annotations

from pathlib import Path

import pytest

import rodex.managed_session_lifecycle as lifecycle_module
from rodex.managed_session_lifecycle import (
    ManagedSessionLifecycle,
    OwnedSessionSelection,
    UnregisteredCodexSessionSelection,
)
from rodex.runtime import LiveTmuxSession
from rodex_registry import parse_codex_session_id


def test_selector_resolution_returns_the_owned_identity_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups: list[tuple[str, Path]] = []

    def lookup(selector: str, database_path: Path) -> int:
        lookups.append((selector, database_path))
        return 41

    monkeypatch.setattr(
        lifecycle_module,
        "_lookup_owned_rodex_session_selector",
        lookup,
    )
    database = tmp_path / "rodex.sqlite3"
    database.touch()

    selection = ManagedSessionLifecycle().resolve_selector("worker", database)

    assert selection == OwnedSessionSelection("worker", 41)
    assert lookups == [("worker", database)]


def test_canonical_unregistered_codex_identity_is_selected_without_opening_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector = "01a015f4-f27c-7592-8060-d12313e8d0ce"
    database = tmp_path / "absent.sqlite3"
    monkeypatch.setattr(
        lifecycle_module,
        "_lookup_owned_rodex_session_selector",
        lambda *_arguments: pytest.fail("an absent registry must not be opened"),
    )

    selection = ManagedSessionLifecycle().resolve_selector(selector, database)

    assert selection == UnregisteredCodexSessionSelection(
        selector, parse_codex_session_id(selector)
    )
    assert not database.exists()


def test_noncanonical_selector_without_registry_remains_codex_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "absent.sqlite3"
    monkeypatch.setattr(
        lifecycle_module,
        "_lookup_owned_rodex_session_selector",
        lambda *_arguments: pytest.fail("an absent registry must not be opened"),
    )

    assert ManagedSessionLifecycle().resolve_selector("worker", database) is None
    assert not database.exists()


def test_collision_policy_uses_the_application_runtime_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    trace: list[object] = []
    monkeypatch.setattr(
        lifecycle_module,
        "default_tmux_server_socket_path",
        lambda: socket_path,
    )

    def resolve_executable(command: str) -> str:
        trace.append(("resolve", command))
        return "/configured/tmux"

    class CollisionLauncher:
        def session_exists(self, runtime: LiveTmuxSession) -> bool:
            trace.append(("session_exists", runtime))
            return False

    def launcher_factory(codex_binary: str, tmux_binary: str) -> CollisionLauncher:
        trace.append(("launcher", codex_binary, tmux_binary))
        return CollisionLauncher()

    ManagedSessionLifecycle().guard_unregistered_selector_collision(
        "worker",
        configured_codex="configured-codex",
        configured_tmux="configured-tmux",
        resolve_executable=resolve_executable,
        runtime_launcher_factory=launcher_factory,  # type: ignore[arg-type]
    )

    assert trace == [
        ("resolve", "configured-tmux"),
        ("launcher", "configured-codex", "/configured/tmux"),
        ("session_exists", LiveTmuxSession(socket_path, "worker")),
    ]
