from __future__ import annotations

from pathlib import Path

import pytest

import rodex.managed_session_lifecycle as lifecycle_module
from rodex.managed_session_lifecycle import (
    ManagedSessionLaunchRequest,
    ManagedSessionLifecycle,
    OwnedSessionSelection,
    UnregisteredCodexSessionSelection,
)
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


def test_noncanonical_selector_without_registry_remains_unresolved(
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


def test_native_interactive_launch_is_not_reclassified_as_a_session_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[ManagedSessionLaunchRequest] = []
    monkeypatch.setattr(
        lifecycle_module,
        "_resolve_session_arguments",
        lambda *_arguments: pytest.fail("native syntax was already classified"),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_create_managed_session",
        lambda request, *_args, **_kwargs: requests.append(request) or 31,
    )

    status = ManagedSessionLifecycle().execute_managed_interactive(
        ("Project: CODEX_TMUX_SESSION_MANAGER",),
        tmp_path / "rodex.sqlite3",
        object(),  # type: ignore[arg-type]
        codex_binary="/usr/bin/codex",
        configured_codex="codex",
    )

    assert status == 31
    assert requests == [
        ManagedSessionLaunchRequest(
            ("Project: CODEX_TMUX_SESSION_MANAGER",),
            requested_name=None,
            detach=False,
            resolve_codex_arguments_as_session=False,
        )
    ]
