"""Process-lifetime Linux inotify guard for one immutable database location."""

from __future__ import annotations

import atexit
import ctypes
import os
import select
import struct
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Final

from .errors import RodexDatabaseMovedError, RodexSQLError
from .private_database_path import (
    ValidatedDatabaseFile,
    normalise_rodex_database_path,
)

_IN_ATTRIB: Final = 0x00000004
_IN_MOVED_FROM: Final = 0x00000040
_IN_MOVED_TO: Final = 0x00000080
_IN_CREATE: Final = 0x00000100
_IN_DELETE: Final = 0x00000200
_IN_DELETE_SELF: Final = 0x00000400
_IN_MOVE_SELF: Final = 0x00000800
_IN_UNMOUNT: Final = 0x00002000
_IN_Q_OVERFLOW: Final = 0x00004000
_IN_IGNORED: Final = 0x00008000
_PARENT_WATCH_MASK: Final = (
    _IN_ATTRIB
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
    | _IN_UNMOUNT
)
_DATABASE_WATCH_MASK: Final = _IN_ATTRIB | _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT
_EVENT_HEADER: Final = struct.Struct("iIII")
_READ_SIZE: Final = 64 * 1024

TerminalSubscriber = Callable[[Path, str], None]


class DatabaseLocationGuard:
    """Permanent terminal latch for one path and its first accepted identities."""

    def __init__(
        self,
        opened: ValidatedDatabaseFile,
        manager: _InotifyGuardManager,
    ) -> None:
        boundary = opened.boundary
        self.path = opened.path
        self.parent_identity = (
            boundary.parent_state.st_dev,
            boundary.parent_state.st_ino,
        )
        self.database_identity = (opened.state.st_dev, opened.state.st_ino)
        self.transition_lock_identity = (
            boundary.transition_lock_state.st_dev,
            boundary.transition_lock_state.st_ino,
        )
        self.terminal_event = Event()
        self._manager = manager
        self._lock = Lock()
        self._terminal_reason: str | None = None
        self._subscribers: dict[int, TerminalSubscriber] = {}
        self._next_subscriber_id = 0
        self._active_connections: dict[int, object] = {}

    @property
    def terminal_reason(self) -> str | None:
        with self._lock:
            return self._terminal_reason

    def require_available(self, stage: str) -> None:
        """Synchronously drain queued events and reject a latched location."""
        self._manager.drain_pending_events()
        if self.terminal_event.is_set():
            raise RodexDatabaseMovedError(
                self.path,
                self.terminal_reason or f"location guard failed at {stage}",
            )

    def subscribe_terminal(self, callback: TerminalSubscriber) -> Callable[[], None]:
        """Subscribe once; an already-latched guard invokes the callback immediately."""
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = callback
            reason = self._terminal_reason
        if reason is not None:
            callback(self.path, reason)

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)

        return unsubscribe

    def register_connection(self, connection: object) -> None:
        with self._lock:
            reason = self._terminal_reason
            if reason is None:
                self._active_connections[id(connection)] = connection
        if reason is not None:
            _interrupt_connection(connection)
            raise RodexDatabaseMovedError(self.path, reason)

    def unregister_connection(self, connection: object) -> None:
        with self._lock:
            self._active_connections.pop(id(connection), None)

    def latch(self, reason: str) -> None:
        """Permanently reject this location and interrupt its active connections."""
        with self._lock:
            if self._terminal_reason is not None:
                return
            self._terminal_reason = reason
            self.terminal_event.set()
            connections = tuple(self._active_connections.values())
            subscribers = tuple(self._subscribers.values())
        for connection in connections:
            _interrupt_connection(connection)
        for subscriber in subscribers:
            try:
                subscriber(self.path, reason)
            except Exception:
                continue


@dataclass(frozen=True, slots=True)
class _Watch:
    guard: DatabaseLocationGuard
    kind: str
    database_name: str


class _InotifyGuardManager:
    """One blocking worker and one inotify queue for all process database locations."""

    def __init__(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add_watch = libc.inotify_add_watch
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        descriptor = init(os.O_CLOEXEC | os.O_NONBLOCK)
        if descriptor < 0:
            error_number = ctypes.get_errno()
            raise RodexSQLError(
                f"could not create database location guard: {os.strerror(error_number)}"
            )
        try:
            wake_read, wake_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._wake_read = wake_read
        self._wake_write = wake_write
        self._add_watch = add_watch
        self._lock = Lock()
        self._closed = False
        self._guards: dict[Path, DatabaseLocationGuard] = {}
        self._watches: dict[int, list[_Watch]] = {}
        self._thread = Thread(
            target=self._run,
            name="rodex-database-location-guard",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            os.close(wake_write)
            os.close(wake_read)
            os.close(descriptor)
            raise

    def get(self, path: Path) -> DatabaseLocationGuard | None:
        with self._lock:
            return self._guards.get(path)

    def get_or_create(self, opened: ValidatedDatabaseFile) -> DatabaseLocationGuard:
        mismatch: DatabaseLocationGuard | None = None
        prior_actions: tuple[tuple[DatabaseLocationGuard, str], ...] = ()
        with self._lock:
            existing = self._guards.get(opened.path)
            if existing is not None:
                if existing.database_identity != (opened.state.st_dev, opened.state.st_ino):
                    mismatch = existing
                guard = existing
            else:
                # A parent watch can already exist for a sibling database. Consume
                # events queued before this pathname was admitted so a historical
                # CREATE/MOVE cannot be attributed to the new guard.
                prior_actions = self._drain_locked()
                guard = DatabaseLocationGuard(opened, self)
                parent_watch = self._watch(opened.path.parent, _PARENT_WATCH_MASK)
                parent_watch_was_known = parent_watch in self._watches
                try:
                    database_watch = self._watch(opened.path, _DATABASE_WATCH_MASK)
                except BaseException:
                    if not parent_watch_was_known:
                        self._remove_watch(parent_watch)
                    raise
                self._watches.setdefault(parent_watch, []).append(
                    _Watch(guard, "parent", opened.path.name)
                )
                self._watches.setdefault(database_watch, []).append(
                    _Watch(guard, "database", opened.path.name)
                )
                self._guards[opened.path] = guard
        _apply_terminal_actions(prior_actions)
        if mismatch is not None:
            mismatch.latch("database inode differs from the process-lifetime identity")
        return guard

    def drain_pending_events(self) -> None:
        with self._lock:
            actions = () if self._closed else self._drain_locked()
        _apply_terminal_actions(actions)

    def inject_overflow_for_testing(self) -> None:
        with self._lock:
            actions = self._terminal_actions_locked(-1, _IN_Q_OVERFLOW, "")
        _apply_terminal_actions(actions)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with suppress(OSError):
            os.write(self._wake_write, b"x")
        self._thread.join(timeout=2)
        for descriptor in (self._descriptor, self._wake_read, self._wake_write):
            with suppress(OSError):
                os.close(descriptor)

    def _watch(self, path: Path, mask: int) -> int:
        watch = self._add_watch(
            self._descriptor,
            os.fsencode(path),
            mask,
        )
        if watch < 0:
            error_number = ctypes.get_errno()
            raise RodexSQLError(
                f"could not watch database storage {path}: {os.strerror(error_number)}"
            )
        return watch

    def _remove_watch(self, watch: int) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        remove_watch = libc.inotify_rm_watch
        remove_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        remove_watch.restype = ctypes.c_int
        remove_watch(self._descriptor, watch)

    def _run(self) -> None:
        while True:
            try:
                readable, _, _ = select.select(
                    [self._descriptor, self._wake_read],
                    [],
                    [],
                )
            except OSError as error:
                with self._lock:
                    actions = (
                        ()
                        if self._closed
                        else tuple(
                            (
                                guard,
                                f"database location guard select failed: {error}",
                            )
                            for guard in self._guards.values()
                        )
                    )
                _apply_terminal_actions(actions)
                return
            if self._wake_read in readable:
                return
            if self._descriptor in readable:
                self.drain_pending_events()

    def _drain_locked(self) -> tuple[tuple[DatabaseLocationGuard, str], ...]:
        actions: list[tuple[DatabaseLocationGuard, str]] = []
        while True:
            try:
                payload = os.read(self._descriptor, _READ_SIZE)
            except BlockingIOError:
                return tuple(actions)
            except OSError as error:
                reason = f"database location guard read failed: {error}"
                actions.extend((guard, reason) for guard in self._guards.values())
                return tuple(actions)
            if not payload:
                return tuple(actions)
            offset = 0
            while offset + _EVENT_HEADER.size <= len(payload):
                watch, mask, _cookie, name_length = _EVENT_HEADER.unpack_from(
                    payload,
                    offset,
                )
                offset += _EVENT_HEADER.size
                raw_name = payload[offset : offset + name_length]
                offset += name_length
                name = os.fsdecode(raw_name.rstrip(b"\0"))
                actions.extend(self._terminal_actions_locked(watch, mask, name))

    def _terminal_actions_locked(
        self,
        watch: int,
        mask: int,
        name: str,
    ) -> tuple[tuple[DatabaseLocationGuard, str], ...]:
        if mask & _IN_Q_OVERFLOW:
            return tuple(
                (guard, "database location guard queue overflowed")
                for guard in self._guards.values()
            )
        actions: list[tuple[DatabaseLocationGuard, str]] = []
        for watched in tuple(self._watches.get(watch, ())):
            if watched.kind == "parent":
                self_event = (
                    mask
                    & (
                        _IN_ATTRIB
                        | _IN_DELETE_SELF
                        | _IN_MOVE_SELF
                        | _IN_UNMOUNT
                        | _IN_IGNORED
                    )
                    and not name
                )
                named_event = name == watched.database_name and mask & (
                    _IN_ATTRIB | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE
                )
                if self_event or named_event:
                    actions.append(
                        (
                            watched.guard,
                            f"database parent/name received inotify mask 0x{mask:x}",
                        )
                    )
            elif mask & (
                _IN_ATTRIB | _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT | _IN_IGNORED
            ):
                actions.append(
                    (watched.guard, f"database inode received inotify mask 0x{mask:x}")
                )
        return tuple(actions)


_MANAGER_LOCK: Final = Lock()
_MANAGER: _InotifyGuardManager | None = None


def database_terminal_signal(
    database_path: str | os.PathLike[str],
) -> DatabaseLocationGuard:
    """Return the terminal signal for a location admitted by a transaction."""
    path = normalise_rodex_database_path(database_path)
    guard = _known_database_location_guard(path)
    if guard is None:
        raise RodexSQLError(
            f"database location has not been admitted by a Rodex transaction: {path}"
        )
    guard.require_available("terminal_subscription")
    return guard


def _admit_opened_database(opened: ValidatedDatabaseFile) -> DatabaseLocationGuard:
    """Bind one retained descriptor to its canonical process-lifetime guard."""
    return _guard_manager().get_or_create(opened)


def _known_database_location_guard(
    database_path: str | os.PathLike[str],
) -> DatabaseLocationGuard | None:
    """Return an established guard without opening or admitting any pathname."""
    path = normalise_rodex_database_path(database_path)
    with _MANAGER_LOCK:
        manager = _MANAGER
    return None if manager is None else manager.get(path)


def _guard_manager() -> _InotifyGuardManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = _InotifyGuardManager()
        return _MANAGER


def _interrupt_connection(connection: object) -> None:
    with suppress(Exception):
        connection.interrupt()  # type: ignore[attr-defined]


def _apply_terminal_actions(
    actions: tuple[tuple[DatabaseLocationGuard, str], ...],
) -> None:
    for guard, reason in actions:
        guard.latch(reason)


def _close_database_location_guard_at_exit() -> None:
    with _MANAGER_LOCK:
        manager = _MANAGER
    if manager is not None:
        manager.close()


atexit.register(_close_database_location_guard_at_exit)
