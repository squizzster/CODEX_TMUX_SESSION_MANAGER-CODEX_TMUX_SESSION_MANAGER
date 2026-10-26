"""Source-ordered turn context shared by trace and statistical adapters."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RolloutRecordAdmission:
    """Deduplicate source coordinates before any event-driven projection effect."""

    fingerprints: dict[int, str] = field(default_factory=dict)

    def copy(self) -> "RolloutRecordAdmission":
        return RolloutRecordAdmission(dict(self.fingerprints))

    def admit(self, record: Mapping[str, Any], *, physical_ordinal: int | None = None) -> bool:
        supplied = record.get("ordinal")
        ordinal = (
            supplied
            if isinstance(supplied, int) and not isinstance(supplied, bool) and supplied >= 0
            else (physical_ordinal)
        )
        if ordinal is None:
            # The analyzer receives inherited-history-filtered records; absent
            # native coordinates cannot be reconstructed from that sequence.
            # Its append cursor owns those records' admission instead.
            return True
        fingerprint = hashlib.sha256(
            json.dumps(dict(record), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        previous = self.fingerprints.get(ordinal)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError("rollout source coordinate changed after admission")
            return False
        self.fingerprints[ordinal] = fingerprint
        return True


@dataclass(frozen=True, slots=True)
class RolloutTurnObservation:
    codex_turn_id: str | None
    lifecycle_eligible: bool


@dataclass(slots=True)
class RolloutTurnLifecycle:
    """Explicit event identity is independent of the active lifecycle binding.

    Context may initialize an empty binding. Only explicit starts establish a
    lifecycle, and only its first explicit terminal closes it. Missing-ID and
    missing-start terminals remain observations, matching the analyzer's audit
    counters; their inferred event scope never grants lifecycle authority.
    """

    active_turn_id: str | None = None
    started_turn_ids: set[str] = field(default_factory=set)
    terminal_turn_ids: set[str] = field(default_factory=set)

    def copy(self) -> "RolloutTurnLifecycle":
        return RolloutTurnLifecycle(self.active_turn_id, set(self.started_turn_ids), set(self.terminal_turn_ids))

    def observe(
        self, record_type: str | None, payload_type: str | None, explicit_turn_id: str | None
    ) -> RolloutTurnObservation:
        event_turn_id = explicit_turn_id or self.active_turn_id
        lifecycle_eligible = False
        if record_type == "event_msg" and payload_type == "task_started" and explicit_turn_id is not None:
            self.started_turn_ids.add(explicit_turn_id)
            self.active_turn_id = explicit_turn_id
            lifecycle_eligible = True
        elif record_type == "turn_context" and self.active_turn_id is None:
            self.active_turn_id = explicit_turn_id
        elif (
            record_type == "event_msg"
            and payload_type in {"task_complete", "turn_aborted"}
            and explicit_turn_id in self.started_turn_ids
            and explicit_turn_id not in self.terminal_turn_ids
        ):
            self.terminal_turn_ids.add(explicit_turn_id)
            lifecycle_eligible = True
            if self.active_turn_id == explicit_turn_id:
                self.active_turn_id = None
        return RolloutTurnObservation(event_turn_id, lifecycle_eligible)
