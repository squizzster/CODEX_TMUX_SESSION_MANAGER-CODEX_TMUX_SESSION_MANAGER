"""Explicit incarnation and retained-process doubles for isolated transport tests."""

import os
from types import SimpleNamespace

from rodex.runtime_peer import RuntimePeerIdentity
from rodex_registry import RodexRuntimeId

TEST_PEER = RuntimePeerIdentity(RodexRuntimeId.parse("a" * 16), "b" * 32)


class LiveTestProcess:
    """The upstream test WebSocket server runs inside the current test process."""

    pid = os.getpid()

    @staticmethod
    def poll() -> None:
        return None


def peer_response() -> SimpleNamespace:
    return SimpleNamespace(headers=TEST_PEER.headers())
