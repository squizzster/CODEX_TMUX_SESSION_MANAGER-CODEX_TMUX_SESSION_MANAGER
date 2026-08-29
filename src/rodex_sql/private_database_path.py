"""Linux no-follow path and descriptor ownership for Rodex databases."""

from __future__ import annotations

import os
import stat as stat_module
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import (
    RodexDatabaseNotFoundError,
    RodexDatabaseNotInitializedError,
    RodexSQLError,
)

RODEX_DATABASE_SCHEMA_GENERATION: Final = 14
RODEX_DATABASE_FILENAME: Final = f"rodex-v{RODEX_DATABASE_SCHEMA_GENERATION}.sqlite3"
DATABASE_TRANSITION_LOCK_SUFFIX: Final = ".rodex-transition.lock"


@dataclass(frozen=True, slots=True)
class PrivateDatabaseBoundary:
    """Retained private parent and process-external transition-lock descriptors."""

    path: Path
    parent_descriptor: int
    parent_state: os.stat_result
    transition_lock_path: Path
    transition_lock_descriptor: int
    transition_lock_state: os.stat_result

    def open_database(
        self,
        *,
        writable: bool,
        create: bool,
    ) -> ValidatedDatabaseFile:
        flags = os.O_RDWR if writable else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(
                self.path.name,
                flags,
                0o600,
                dir_fd=self.parent_descriptor,
            )
        except FileNotFoundError as error:
            raise RodexDatabaseNotFoundError(
                f"database does not exist: {self.path}"
            ) from error
        except OSError as error:
            raise RodexSQLError(
                f"could not securely open database {self.path}: {error}"
            ) from error
        try:
            state = os.fstat(descriptor)
            _validate_private_regular_file(self.path, state)
            if stat_module.S_IMODE(state.st_mode) != 0o600:
                raise RodexSQLError(f"database is not private: {self.path}")
        except BaseException:
            os.close(descriptor)
            raise
        return ValidatedDatabaseFile(boundary=self, descriptor=descriptor, state=state)

    def close(self) -> None:
        try:
            os.close(self.transition_lock_descriptor)
        finally:
            os.close(self.parent_descriptor)

    def __enter__(self) -> PrivateDatabaseBoundary:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ValidatedDatabaseFile:
    """A retained private database descriptor below one stable parent boundary."""

    boundary: PrivateDatabaseBoundary
    descriptor: int
    state: os.stat_result

    @property
    def path(self) -> Path:
        return self.boundary.path

    def close(self) -> None:
        os.close(self.descriptor)

    def __enter__(self) -> ValidatedDatabaseFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def default_rodex_database_path() -> Path:
    """Resolve the durable database path for the current Linux user."""
    configured = os.environ.get("RODEX_DATABASE_PATH")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    configured_state_home = os.environ.get("XDG_STATE_HOME")
    state_home = (
        Path(configured_state_home).expanduser()
        if configured_state_home
        else Path.home() / ".local" / "state"
    )
    return Path(os.path.abspath(state_home / "rodex" / RODEX_DATABASE_FILENAME))


def normalise_rodex_database_path(
    database_path: str | os.PathLike[str] | None,
) -> Path:
    """Resolve an explicit path or the current user's durable default."""
    if database_path is None:
        return default_rodex_database_path()
    return Path(os.path.abspath(Path(database_path).expanduser()))


def database_transition_lock_path(database_path: str | os.PathLike[str]) -> Path:
    path = normalise_rodex_database_path(database_path)
    return path.with_name(f".{path.name}{DATABASE_TRANSITION_LOCK_SUFFIX}")


def open_private_database_boundary(
    database_path: str | os.PathLike[str],
    *,
    create: bool,
) -> PrivateDatabaseBoundary:
    """Retain a private parent and its no-follow database transition lock."""
    _require_linux_secure_open_support()
    path = normalise_rodex_database_path(database_path)
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
    except FileNotFoundError as error:
        raise RodexDatabaseNotFoundError(f"database does not exist: {path}") from error
    except OSError as error:
        raise RodexSQLError(
            f"could not securely open database parent {path.parent}: {error}"
        ) from error
    try:
        parent_state = os.fstat(parent_descriptor)
        _validate_private_database_parent(path, parent_state)
        if not create:
            try:
                existing_database_state = os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError as error:
                raise RodexDatabaseNotFoundError(
                    f"database does not exist: {path}"
                ) from error
            _validate_private_regular_file(path, existing_database_state)
            if stat_module.S_IMODE(existing_database_state.st_mode) != 0o600:
                raise RodexSQLError(f"database is not private: {path}")
        lock_path = database_transition_lock_path(path)
        lock_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if create:
            lock_flags |= os.O_CREAT
        try:
            lock_descriptor = os.open(
                lock_path.name,
                lock_flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError as error:
            raise RodexDatabaseNotInitializedError(
                f"database transition lock is missing for {path}; "
                "initialize it through Rodex"
            ) from error
        except OSError as error:
            raise RodexSQLError(
                f"could not securely open database transition lock {lock_path}: {error}"
            ) from error
        try:
            lock_state = os.fstat(lock_descriptor)
            _validate_private_regular_file(lock_path, lock_state)
            if stat_module.S_IMODE(lock_state.st_mode) != 0o600:
                raise RodexSQLError(f"database transition lock is not private: {lock_path}")
        except BaseException:
            os.close(lock_descriptor)
            raise
    except BaseException:
        os.close(parent_descriptor)
        raise
    return PrivateDatabaseBoundary(
        path=path,
        parent_descriptor=parent_descriptor,
        parent_state=parent_state,
        transition_lock_path=lock_path,
        transition_lock_descriptor=lock_descriptor,
        transition_lock_state=lock_state,
    )


def _require_linux_secure_open_support() -> None:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if sys.platform != "linux" or any(not hasattr(os, name) for name in required):
        raise RodexSQLError(
            "Rodex SQLite storage requires Linux O_NOFOLLOW, O_DIRECTORY, and O_CLOEXEC"
        )


def _validate_private_database_parent(path: Path, parent: os.stat_result) -> None:
    if (
        not stat_module.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
    ):
        raise RodexSQLError(
            f"database parent must be a private current-user directory: {path.parent}"
        )


def _validate_private_regular_file(path: Path, state: os.stat_result) -> None:
    if not stat_module.S_ISREG(state.st_mode):
        raise RodexSQLError(f"database storage path is not a regular file: {path}")
    if state.st_uid != os.getuid():
        raise RodexSQLError(f"database storage is not owned by uid {os.getuid()}: {path}")
