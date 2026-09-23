"""Exclusive lifetime ownership of one runtime Unix endpoint."""

from __future__ import annotations

import fcntl
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BoundUnixAlias:
    """A child-owned rendezvous symlink and its physical Unix socket."""

    path: Path
    target: Path


class ExclusiveUnixEndpoint:
    """Retain the ownership lock and exact endpoint inode until cleanup completes.

    The lock file deliberately survives listener restarts. Unlinking it while
    another process waits would introduce a second, independently locked inode.
    Child-owned aliases are admitted only when explicitly enabled by their caller.
    """

    def __init__(self, path: Path, *, allow_child_alias: bool = False) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._allow_child_alias = allow_child_alias
        self._lock_descriptor: int | None = None
        self._bound_path_descriptor: int | None = None
        self._target_descriptor: int | None = None
        self._bound_alias: BoundUnixAlias | None = None
        self._listener: socket.socket | None = None

    @property
    def bound_alias(self) -> BoundUnixAlias | None:
        return self._bound_alias

    def acquire(self) -> None:
        """Reserve one endpoint lifetime before a listener or child can bind it."""
        if self._lock_descriptor is not None:
            raise RuntimeError("Unix endpoint already owns its lifecycle")
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        self._lock_descriptor = descriptor
        try:
            state = os.fstat(descriptor)
            if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o600:
                raise OSError("Unix endpoint lock is not a private current-user regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self._path_matches_descriptor(self.lock_path, descriptor):
                raise OSError("Unix endpoint lock pathname changed during admission")
            self._remove_stale_endpoint()
        except BaseException:
            self.close()
            raise

    def retain_bound_path(self) -> None:
        """Pin a current-user socket or a permitted child-owned alias."""
        lock = self._lock_descriptor
        if lock is None or not self._path_matches_descriptor(self.lock_path, lock):
            raise OSError("Unix endpoint lifetime lock is not current")
        if self._bound_path_descriptor is not None:
            raise RuntimeError("Unix endpoint already retains a bound pathname")
        descriptor = os.open(self.path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            state = os.fstat(descriptor)
            if stat.S_ISSOCK(state.st_mode) and state.st_uid == os.getuid():
                self._bound_path_descriptor = descriptor
                self.path.chmod(0o600)
                return
            if not self._allow_child_alias or not stat.S_ISLNK(state.st_mode) or state.st_uid != os.getuid():
                raise OSError("Unix endpoint pathname is not a current-user socket")
            target = self._child_alias_target(descriptor)
            target_descriptor = os.open(target, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                target_state = os.fstat(target_descriptor)
                if (
                    not stat.S_ISSOCK(target_state.st_mode)
                    or target_state.st_uid != os.getuid()
                    or stat.S_IMODE(target_state.st_mode) != 0o600
                    or not self._path_matches_descriptor(target, target_descriptor)
                    or not self._path_matches_descriptor(self.path, descriptor)
                    or os.readlink(self.path) != os.fspath(target)
                ):
                    raise OSError("Unix endpoint alias does not retain a private current-user socket")
            except BaseException:
                os.close(target_descriptor)
                raise
            self._target_descriptor = target_descriptor
            self._bound_alias = BoundUnixAlias(self.path, target)
            self._bound_path_descriptor = descriptor
        except BaseException:
            if self._bound_path_descriptor != descriptor:
                os.close(descriptor)
            raise

    def open(self) -> socket.socket:
        """Acquire ownership and return a bound, listening stream socket."""
        self.acquire()
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener = listener
            listener.bind(str(self.path))
            # Pin the filesystem inode, not the socket's separate sockfs inode.
            self.retain_bound_path()
            listener.listen()
            return listener
        except BaseException:
            self.close()
            raise

    def _remove_stale_endpoint(self) -> None:
        try:
            descriptor = os.open(self.path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        try:
            state = os.fstat(descriptor)
            if stat.S_ISLNK(state.st_mode) and self._allow_child_alias and state.st_uid == os.getuid():
                target = self._child_alias_target(descriptor)
                try:
                    target_state = target.lstat()
                except FileNotFoundError:
                    target_state = None
                if target_state is not None and (
                    not stat.S_ISSOCK(target_state.st_mode) or target_state.st_uid != os.getuid()
                ):
                    raise OSError("Unix endpoint alias target is not a current-user socket")
            elif not stat.S_ISSOCK(state.st_mode) or state.st_uid != os.getuid():
                raise OSError("Unix endpoint pathname is not a current-user socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                try:
                    probe.connect(str(self.path))
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
                else:
                    raise OSError("Unix endpoint already has a live listener")
            if not self._path_matches_descriptor(self.path, descriptor):
                raise OSError("Unix endpoint changed during stale-path reconciliation")
            self.path.unlink()
        finally:
            os.close(descriptor)

    def _child_alias_target(self, descriptor: int) -> Path:
        target = Path(os.readlink(self.path))
        if not target.is_absolute() or not self._path_matches_descriptor(self.path, descriptor):
            raise OSError("Unix endpoint alias pathname changed during admission")
        parent = target.parent
        state = parent.lstat()
        if (
            not stat.S_ISDIR(state.st_mode)
            or state.st_uid != os.getuid()
            or stat.S_IMODE(state.st_mode) & 0o077
            or parent.resolve(strict=True) != parent
        ):
            raise OSError("Unix endpoint alias target directory is not private and current-user owned")
        return target

    @staticmethod
    def _path_matches_descriptor(path: Path, descriptor: int) -> bool:
        try:
            current = path.lstat()
        except FileNotFoundError:
            return False
        retained = os.fstat(descriptor)
        return (current.st_dev, current.st_ino, current.st_mode, current.st_uid) == (
            retained.st_dev,
            retained.st_ino,
            retained.st_mode,
            retained.st_uid,
        )

    def close(self) -> None:
        listener, self._listener = self._listener, None
        descriptor, self._bound_path_descriptor = self._bound_path_descriptor, None
        target_descriptor, self._target_descriptor = self._target_descriptor, None
        self._bound_alias = None
        lock, self._lock_descriptor = self._lock_descriptor, None
        try:
            if listener is not None:
                listener.close()
            if (
                descriptor is not None
                and lock is not None
                and self._path_matches_descriptor(self.lock_path, lock)
                and self._path_matches_descriptor(self.path, descriptor)
            ):
                self.path.unlink(missing_ok=True)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if target_descriptor is not None:
                os.close(target_descriptor)
            if lock is not None:
                os.close(lock)
