"""Exclusive lifetime ownership of one runtime Unix endpoint."""

from __future__ import annotations

import fcntl
import os
import socket
import stat
from pathlib import Path


class ExclusiveUnixEndpoint:
    """Retain the ownership lock and exact socket inode until cleanup completes.

    The lock file deliberately survives listener restarts. Unlinking it while
    another process waits would introduce a second, independently locked inode.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._lock_descriptor: int | None = None
        self._socket_descriptor: int | None = None
        self._listener: socket.socket | None = None

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
        """Pin the current-user socket created under this retained lifetime lock."""
        lock = self._lock_descriptor
        if lock is None or not self._path_matches_descriptor(self.lock_path, lock):
            raise OSError("Unix endpoint lifetime lock is not current")
        if self._socket_descriptor is not None:
            raise RuntimeError("Unix endpoint already retains a bound pathname")
        descriptor = os.open(self.path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        state = os.fstat(descriptor)
        if not stat.S_ISSOCK(state.st_mode) or state.st_uid != os.getuid():
            os.close(descriptor)
            raise OSError("Unix endpoint pathname is not a current-user socket")
        self._socket_descriptor = descriptor
        self.path.chmod(0o600)

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
            if not stat.S_ISSOCK(state.st_mode) or state.st_uid != os.getuid():
                raise OSError("Unix endpoint pathname is not a current-user socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    pass
                else:
                    raise OSError("Unix endpoint already has a live listener")
            if not self._path_matches_descriptor(self.path, descriptor):
                raise OSError("Unix endpoint changed during stale-path reconciliation")
            self.path.unlink()
        finally:
            os.close(descriptor)

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
        descriptor, self._socket_descriptor = self._socket_descriptor, None
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
            if lock is not None:
                os.close(lock)
