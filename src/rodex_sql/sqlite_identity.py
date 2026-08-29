"""Fail-closed SQLite pathname and retained-descriptor identity checks."""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
from pathlib import Path
from typing import NoReturn

from .database_location_guard import DatabaseLocationGuard
from .private_database_path import ValidatedDatabaseFile


def validated_database_uri(opened: ValidatedDatabaseFile, *, read_only: bool) -> str:
    """Direct stdlib SQLite through the retained database descriptor."""
    descriptor_path = Path("/proc/self/fd") / str(opened.descriptor)
    if not descriptor_path.exists():
        _reject(opened, None, "Linux /proc/self/fd is unavailable")
    mode = "ro" if read_only else "rw"
    return f"{descriptor_path.as_uri()}?mode={mode}"


def require_database_identity(
    opened: ValidatedDatabaseFile,
    guard: DatabaseLocationGuard,
    *,
    stage: str,
) -> None:
    """Revalidate parent, lock, database path, descriptors, owner, type, and mode."""
    guard.require_available(stage)
    boundary = opened.boundary
    try:
        parent_descriptor_state = os.fstat(boundary.parent_descriptor)
        lock_descriptor_state = os.fstat(boundary.transition_lock_descriptor)
        database_descriptor_state = os.fstat(opened.descriptor)
        parent_path_state = opened.path.parent.lstat()
        lock_relative_state = os.stat(
            boundary.transition_lock_path.name,
            dir_fd=boundary.parent_descriptor,
            follow_symlinks=False,
        )
        database_relative_state = os.stat(
            opened.path.name,
            dir_fd=boundary.parent_descriptor,
            follow_symlinks=False,
        )
        lock_absolute_state = boundary.transition_lock_path.lstat()
        database_absolute_state = opened.path.lstat()
    except OSError as error:
        _reject(opened, guard, f"{stage} identity check failed: {error}")

    expected_parent = guard.parent_identity
    expected_lock = guard.transition_lock_identity
    expected_database = guard.database_identity
    identities = {
        "parent descriptor": _identity(parent_descriptor_state),
        "parent path": _identity(parent_path_state),
        "transition lock descriptor": _identity(lock_descriptor_state),
        "transition lock relative path": _identity(lock_relative_state),
        "transition lock absolute path": _identity(lock_absolute_state),
        "database descriptor": _identity(database_descriptor_state),
        "database relative path": _identity(database_relative_state),
        "database absolute path": _identity(database_absolute_state),
    }
    mismatches = [
        label
        for label, identity in identities.items()
        if identity
        != (
            expected_parent
            if label.startswith("parent")
            else expected_lock
            if label.startswith("transition")
            else expected_database
        )
    ]
    if mismatches:
        _reject(opened, guard, f"{stage} identity mismatch: {', '.join(mismatches)}")
    if (
        not _private_directory(parent_descriptor_state)
        or not _private_regular_file(lock_descriptor_state)
        or not _private_regular_file(database_descriptor_state)
        or stat_module.S_ISLNK(lock_absolute_state.st_mode)
        or stat_module.S_ISLNK(database_absolute_state.st_mode)
    ):
        _reject(opened, guard, f"{stage} owner/type/mode validation failed")
    guard.require_available(stage)


def require_sqlite_main_path(
    connection: sqlite3.Connection,
    opened: ValidatedDatabaseFile,
    guard: DatabaseLocationGuard,
) -> None:
    """Verify this connection reports the exact canonical requested main pathname."""
    require_database_identity(opened, guard, stage="post_connect")
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error as error:
        _reject(opened, guard, f"post_connect main-path query failed: {error}")
    main_paths = [Path(str(row[2])) for row in rows if row[1] == "main"]
    if main_paths != [opened.path]:
        _reject(
            opened,
            guard,
            f"post_connect SQLite main path mismatch: {main_paths!r}",
        )
    require_database_identity(opened, guard, stage="post_connect")


def _identity(state: os.stat_result) -> tuple[int, int]:
    return state.st_dev, state.st_ino


def _private_directory(state: os.stat_result) -> bool:
    return (
        stat_module.S_ISDIR(state.st_mode)
        and state.st_uid == os.getuid()
        and state.st_mode & 0o077 == 0
    )


def _private_regular_file(state: os.stat_result) -> bool:
    return (
        stat_module.S_ISREG(state.st_mode)
        and state.st_uid == os.getuid()
        and stat_module.S_IMODE(state.st_mode) == 0o600
    )


def _reject(
    opened: ValidatedDatabaseFile,
    guard: DatabaseLocationGuard | None,
    reason: str,
) -> NoReturn:
    if guard is not None:
        guard.latch(reason)
        guard.require_available("terminal")
    from .errors import RodexDatabaseMovedError

    raise RodexDatabaseMovedError(opened.path, reason)
