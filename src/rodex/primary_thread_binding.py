"""Connection-local main identity, proven by native lifecycle RPC acknowledgements.

The proxy owns synchronization and connection fencing. Notifications cannot select
main: child starts share the stream, and resume need not emit thread/started.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

PRIMARY_THREAD_METHODS: Final = frozenset({"thread/start", "thread/resume", "thread/fork"})
MAX_PENDING_THREAD_SELECTIONS: Final = 32


@dataclass(frozen=True, slots=True)
class _PendingThread:
    expected_thread_id: str | None
    ephemeral: bool


class PrimaryThreadBinding:
    def __init__(self) -> None:
        self._selected_thread_id: str | None = None
        self._pending: dict[tuple[type, int | str], _PendingThread] = {}
        self._invalid = False

    @property
    def thread_id(self) -> str | None:
        # An in-flight selection can change the native conversation. Do not keep
        # authorizing the previous root until its acknowledgement arrives.
        selecting_main = any(not pending.ephemeral for pending in self._pending.values())
        return None if self._invalid or selecting_main else self._selected_thread_id

    def observe_request(self, message: Mapping[str, object]) -> None:
        method = message.get("method")
        if not isinstance(method, str) or self._invalid:
            return
        key = _request_key(message.get("id"))
        if key is not None and key in self._pending:
            self._invalidate()  # Reused in-flight IDs cannot attest a selection.
            return
        if method not in PRIMARY_THREAD_METHODS:
            return
        params = message.get("params")
        if key is None or not isinstance(params, Mapping) or len(self._pending) >= MAX_PENDING_THREAD_SELECTIONS:
            self._invalidate()
            return
        expected_thread = params.get("threadId") if method == "thread/resume" else None
        if expected_thread is not None and (not isinstance(expected_thread, str) or not expected_thread):
            self._invalidate()
            return
        # Native temporary structured requests share the TUI connection. Only
        # start/fork support ephemeral; absent/null means the native default.
        ephemeral = params.get("ephemeral") if method in {"thread/start", "thread/fork"} else False
        if ephemeral is None:
            ephemeral = False
        if type(ephemeral) is not bool:
            self._invalidate()
            return
        self._pending[key] = _PendingThread(expected_thread, ephemeral)

    def observe_response(self, message: Mapping[str, object]) -> None:
        if "method" in message or self._invalid:
            return
        key = _request_key(message.get("id"))
        if key is None or key not in self._pending:
            return
        pending = self._pending.pop(key)
        if "error" in message and "result" not in message:
            error = message["error"]
            if isinstance(error, Mapping) and type(error.get("code")) is int and isinstance(error.get("message"), str):
                return  # Definitive rejection leaves the previous selection intact.
            self._invalidate()
            return
        result = message.get("result")
        thread = result.get("thread") if isinstance(result, Mapping) else None
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        ephemeral = thread.get("ephemeral") if isinstance(thread, Mapping) else None
        if (
            "error" in message
            or not isinstance(thread_id, str)
            or not thread_id
            or ephemeral is not pending.ephemeral
            or (pending.expected_thread_id is not None and pending.expected_thread_id != thread_id)
            or (not ephemeral and self._selected_thread_id is not None and self._selected_thread_id != thread_id)
        ):
            # One managed runtime owns one root. Malformed or conflicting success
            # must not preserve old authority or guess a replacement root.
            self._invalidate()
            return
        if not ephemeral:
            self._selected_thread_id = thread_id

    def _invalidate(self) -> None:
        self._invalid = True
        self._selected_thread_id = None
        self._pending.clear()

    def invalidate_after_disconnect(self) -> None:
        """Late callbacks on this connection cannot restore lost authority."""
        self._invalidate()


def _request_key(value: object) -> tuple[type, int | str] | None:
    return (type(value), value) if type(value) in {int, str} else None
