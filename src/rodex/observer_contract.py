"""Immutable wire limits shared by observer components."""

from __future__ import annotations

import struct
from typing import Final

OBSERVER_SCHEMA: Final = "rodex-agent-observer-v2"
OBSERVER_CONTROL_SOCKET_PREFIX: Final = "agent-observer-"
OBSERVER_FRAME_LENGTH: Final = struct.Struct("!Q")
OBSERVER_MAX_FRAME_BYTES: Final = 256 * 1024
OBSERVER_SNAPSHOT_EVENT_LIMIT: Final = 64
OBSERVER_RECEIVE_DEADLINE_SECONDS: Final = 1.0
OBSERVER_PROJECTED_TEXT_MAX_CHARS: Final = 16 * 1024
OBSERVER_PROJECTED_FIELD_MAX_CHARS: Final = 1024
OBSERVER_PROJECTED_ID_MAX_CHARS: Final = 256
OBSERVER_PROJECTED_LIST_ITEM_LIMIT: Final = 64
OBSERVER_RECOVERED_TARGET_LIMIT: Final = 256
