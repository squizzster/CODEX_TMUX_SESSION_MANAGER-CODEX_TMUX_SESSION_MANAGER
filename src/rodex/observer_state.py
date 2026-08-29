"""Canonical observer active-state, tombstone, pruning, and epoch reducer."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .observer_contract import (
    OBSERVER_MAX_FRAME_BYTES,
    OBSERVER_SCHEMA,
    OBSERVER_SNAPSHOT_EVENT_LIMIT,
)

ObserverStateKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class ObserverStateDelta:
    epoch: int
    revision: int
    epoch_changed: bool
    state_replaced: bool
    upserted_events: tuple[dict[str, object], ...]
    tombstone_events: tuple[dict[str, object], ...]
    removed_target_thread_ids: frozenset[str]
    active_events: tuple[dict[str, object], ...]


class ObserverStateReducer:
    """Reduce producer events and consumer snapshots with identical identity rules."""

    def __init__(self, mode: Literal["producer", "consumer"]) -> None:
        self._mode = mode
        self._epoch = 0 if mode == "producer" else -1
        self._revision = 0 if mode == "producer" else -1
        self._active: dict[ObserverStateKey, dict[str, object]] = {}
        self._tombstones: dict[ObserverStateKey, dict[str, object]] = {}
        self._removed_targets: dict[str, None] = {}
        self._dropped_event_count = 0
        self._known_activity_item_ids: set[str] = set()
        self._tracked_target_thread_ids: set[str] = set()
        self._latest_parent_user_message: dict[str, object] | None = None
        self._collaboration_invocations: dict[str, dict[str, object]] = {}
        self._subagent_activities: dict[str, dict[str, object]] = {}
        self._sent_root_request_context_ids: set[str] = set()

    @classmethod
    def producer(cls) -> ObserverStateReducer:
        return cls("producer")

    @classmethod
    def consumer(cls) -> ObserverStateReducer:
        return cls("consumer")

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def active_events(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(event) for event in self._active.values())

    @property
    def tracked_target_thread_ids(self) -> frozenset[str]:
        self._require_mode("producer")
        return frozenset(self._tracked_target_thread_ids)

    @property
    def latest_parent_user_message(self) -> dict[str, object] | None:
        self._require_mode("producer")
        message = self._latest_parent_user_message
        return None if message is None else dict(message)

    def remember_parent_user_message(self, event: Mapping[str, object] | None) -> None:
        self._require_mode("producer")
        self._latest_parent_user_message = None if event is None else dict(event)

    def is_known_activity(self, item_id: str) -> bool:
        self._require_mode("producer")
        return item_id in self._known_activity_item_ids

    def tracks_target(self, target_thread_id: str) -> bool:
        self._require_mode("producer")
        return target_thread_id in self._tracked_target_thread_ids

    def remember_activity(
        self,
        item_id: str,
        target_thread_id: str,
        event: dict[str, object],
        *,
        new_spawn: bool,
    ) -> None:
        self._require_mode("producer")
        self._subagent_activities[item_id] = event
        self._tracked_target_thread_ids.add(target_thread_id)
        if new_spawn:
            self._known_activity_item_ids.add(item_id)

    def activity(self, item_id: str) -> dict[str, object] | None:
        self._require_mode("producer")
        return self._subagent_activities.get(item_id)

    def collaboration_invocation(self, item_id: str) -> dict[str, object] | None:
        self._require_mode("producer")
        return self._collaboration_invocations.get(item_id)

    def remember_collaboration_invocation(
        self,
        item_id: str,
        invocation: dict[str, object],
    ) -> None:
        self._require_mode("producer")
        self._collaboration_invocations[item_id] = invocation

    def forget_collaboration_invocation(self, item_id: str) -> None:
        self._require_mode("producer")
        self._collaboration_invocations.pop(item_id, None)

    def root_request_context_was_sent(self, item_id: str) -> bool:
        self._require_mode("producer")
        return item_id in self._sent_root_request_context_ids

    def mark_root_request_context_sent(self, item_id: str) -> None:
        self._require_mode("producer")
        self._sent_root_request_context_ids.add(item_id)

    def complete_activity(self, item_id: str, target_thread_id: str) -> None:
        self._require_mode("producer")
        self._known_activity_item_ids.discard(item_id)
        self._subagent_activities.pop(item_id, None)
        self._collaboration_invocations.pop(item_id, None)
        self._sent_root_request_context_ids.discard(item_id)
        if not any(
            observer_event_target_thread_id(activity) == target_thread_id
            for activity in self._subagent_activities.values()
        ):
            self._tracked_target_thread_ids.discard(target_thread_id)

    def prune_protocol_target(self, target_thread_id: str) -> dict[str, object]:
        self._require_mode("producer")
        item_ids = {
            item_id
            for item_id, activity in self._subagent_activities.items()
            if observer_event_target_thread_id(activity) == target_thread_id
        }
        self._tracked_target_thread_ids.discard(target_thread_id)
        self._known_activity_item_ids.difference_update(item_ids)
        self._sent_root_request_context_ids.difference_update(item_ids)
        for item_id in item_ids:
            self._subagent_activities.pop(item_id, None)
            self._collaboration_invocations.pop(item_id, None)
        return self.prune_target(target_thread_id)

    def observe(self, event: Mapping[str, object]) -> dict[str, object]:
        """Apply one projected producer event and return a complete bounded snapshot."""
        self._require_mode("producer")
        projected = dict(event)
        key = observer_state_event_key(projected)
        target = observer_event_target_thread_id(projected)
        if target is not None:
            self._removed_targets.pop(target, None)
        if _is_terminal_event(projected):
            self._active.pop(key, None)
            self._tombstones.pop(key, None)
            self._tombstones[key] = projected
        else:
            self._tombstones.pop(key, None)
            self._active.pop(key, None)
            self._active[key] = projected
        self._advance_revision()
        self._enforce_bounds()
        return self.snapshot()

    def prune_target(self, target_thread_id: str) -> dict[str, object]:
        """Remove every retained event for one terminal target and record a tombstone."""
        self._require_mode("producer")
        if not target_thread_id:
            raise ValueError("observer target thread ID must be non-empty")
        for collection in (self._active, self._tombstones):
            for key, event in tuple(collection.items()):
                if _event_mentions_target(event, target_thread_id):
                    collection.pop(key, None)
        self._removed_targets.pop(target_thread_id, None)
        self._removed_targets[target_thread_id] = None
        self._advance_revision()
        self._enforce_bounds()
        return self.snapshot()

    def reset_epoch(self) -> dict[str, object]:
        """Atomically clear connection-scoped state and publish the new empty epoch."""
        self._require_mode("producer")
        self._epoch += 1
        self._revision += 1
        self._active.clear()
        self._tombstones.clear()
        self._removed_targets.clear()
        self._dropped_event_count = 0
        self._clear_protocol_state()
        return self.snapshot()

    def discard_producer_state(self) -> None:
        """Release all retained producer state during final shutdown."""
        self._require_mode("producer")
        self._active.clear()
        self._tombstones.clear()
        self._removed_targets.clear()
        self._clear_protocol_state()

    def snapshot(self) -> dict[str, object]:
        self._require_mode("producer")
        snapshot = self._snapshot_value()
        while _snapshot_size(snapshot) > OBSERVER_MAX_FRAME_BYTES:
            if self._active:
                self._active.pop(next(iter(self._active)))
            elif self._tombstones:
                self._tombstones.pop(next(iter(self._tombstones)))
            elif self._removed_targets:
                self._removed_targets.pop(next(iter(self._removed_targets)))
            else:
                break
            self._dropped_event_count += 1
            snapshot = self._snapshot_value()
        return snapshot

    def consume_snapshot(self, snapshot: Mapping[str, object]) -> ObserverStateDelta:
        """Apply a complete producer snapshot once and return presentation deltas."""
        self._require_mode("consumer")
        parsed = _parse_snapshot(snapshot)
        if parsed is None:
            return self._empty_delta()
        (
            epoch,
            revision,
            incoming_active,
            incoming_tombstones,
            removed_targets,
            dropped_event_count,
        ) = parsed
        if epoch < self._epoch or (epoch == self._epoch and revision <= self._revision):
            return self._empty_delta()
        epoch_changed = epoch != self._epoch
        if not epoch_changed and dropped_event_count < self._dropped_event_count:
            return self._empty_delta()
        state_replaced = epoch_changed or (dropped_event_count > self._dropped_event_count)
        if state_replaced:
            self._active.clear()
            self._tombstones.clear()
            self._removed_targets.clear()
        prior_active = dict(self._active)
        upserted = tuple(
            event
            for key, event in incoming_active.items()
            if prior_active.get(key) != event
        )
        tombstone_events = tuple(
            event
            for key, event in incoming_tombstones.items()
            if self._tombstones.get(key) != event
        )
        newly_removed_targets = frozenset(
            target for target in removed_targets if target not in self._removed_targets
        )
        self._epoch = epoch
        self._revision = revision
        self._dropped_event_count = dropped_event_count
        self._active = incoming_active
        self._tombstones = incoming_tombstones
        self._removed_targets = {target: None for target in removed_targets}
        return ObserverStateDelta(
            epoch,
            revision,
            epoch_changed,
            state_replaced,
            upserted,
            tombstone_events,
            newly_removed_targets,
            self.active_events,
        )

    def _snapshot_value(self) -> dict[str, object]:
        return {
            "schema": OBSERVER_SCHEMA,
            "kind": "observer_state_snapshot",
            "epoch": self._epoch,
            "revision": self._revision,
            "state": {
                "events": list(self._active.values()),
                "tombstones": list(self._tombstones.values()),
                "removedTargetThreadIds": list(self._removed_targets),
            },
            "overflow": {
                "dropped_event_count": self._dropped_event_count,
                "state_complete": self._dropped_event_count == 0,
            },
        }

    def _advance_revision(self) -> None:
        self._revision += 1

    def _enforce_bounds(self) -> None:
        while (
            len(self._active) + len(self._tombstones) + len(self._removed_targets)
            > OBSERVER_SNAPSHOT_EVENT_LIMIT
        ):
            if self._tombstones:
                self._tombstones.pop(next(iter(self._tombstones)))
            elif self._removed_targets:
                self._removed_targets.pop(next(iter(self._removed_targets)))
            else:
                self._active.pop(next(iter(self._active)))
            self._dropped_event_count += 1

    def _empty_delta(self) -> ObserverStateDelta:
        return ObserverStateDelta(
            self._epoch,
            self._revision,
            False,
            False,
            (),
            (),
            frozenset(),
            self.active_events,
        )

    def _require_mode(self, expected: Literal["producer", "consumer"]) -> None:
        if self._mode != expected:
            raise RuntimeError(f"observer state reducer is not a {expected}")

    def _clear_protocol_state(self) -> None:
        self._known_activity_item_ids.clear()
        self._tracked_target_thread_ids.clear()
        self._latest_parent_user_message = None
        self._collaboration_invocations.clear()
        self._subagent_activities.clear()
        self._sent_root_request_context_ids.clear()


def observer_state_event_key(event: Mapping[str, object]) -> ObserverStateKey:
    kind = str(event.get("kind", "unknown"))
    item = event.get("item")
    thread_id = event.get("target_thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = event.get("thread_id")
    if isinstance(item, Mapping):
        if kind == "app_server_subagent_activity":
            target_thread_id = item.get("agent_thread_id")
            if isinstance(target_thread_id, str) and target_thread_id:
                thread_id = target_thread_id
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            return str(thread_id or "global"), item_id, kind
    activity_item_id = event.get("activity_item_id")
    if isinstance(activity_item_id, str) and activity_item_id:
        return str(thread_id or "global"), activity_item_id, kind
    if kind == "trace_published":
        return "global", "latest", kind
    return str(thread_id or "global"), "latest", kind


def observer_event_target_thread_id(event: Mapping[str, object]) -> str | None:
    target = event.get("target_thread_id")
    if isinstance(target, str) and target:
        return target
    item = event.get("item")
    if isinstance(item, Mapping):
        candidate = item.get("agent_thread_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    thread_id = event.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _is_terminal_event(event: Mapping[str, object]) -> bool:
    if event.get("method") == "item/completed":
        return True
    item = event.get("item")
    if not isinstance(item, Mapping):
        return False
    activity_kind = item.get("activity_kind")
    return isinstance(activity_kind, str) and activity_kind.lower() in {
        "completed",
        "failed",
        "aborted",
        "shutdown",
    }


def _event_mentions_target(event: Mapping[str, object], target: str) -> bool:
    if observer_event_target_thread_id(event) == target:
        return True
    item = event.get("item")
    receiver_ids = item.get("receiver_thread_ids") if isinstance(item, Mapping) else None
    return isinstance(receiver_ids, list) and target in receiver_ids


def _parse_snapshot(
    snapshot: Mapping[str, object],
) -> (
    tuple[
        int,
        int,
        dict[ObserverStateKey, dict[str, object]],
        dict[ObserverStateKey, dict[str, object]],
        tuple[str, ...],
        int,
    ]
    | None
):
    if snapshot.get("schema") != OBSERVER_SCHEMA or snapshot.get("kind") != (
        "observer_state_snapshot"
    ):
        return None
    epoch = snapshot.get("epoch")
    revision = snapshot.get("revision")
    state = snapshot.get("state")
    overflow = snapshot.get("overflow")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(state, Mapping)
        or not isinstance(overflow, Mapping)
    ):
        return None
    dropped_event_count = overflow.get("dropped_event_count")
    state_complete = overflow.get("state_complete")
    if (
        isinstance(dropped_event_count, bool)
        or not isinstance(dropped_event_count, int)
        or dropped_event_count < 0
        or not isinstance(state_complete, bool)
        or state_complete != (dropped_event_count == 0)
    ):
        return None
    active = _event_mapping(state.get("events"))
    tombstones = _event_mapping(state.get("tombstones"))
    removed = state.get("removedTargetThreadIds")
    if active is None or tombstones is None or not isinstance(removed, list):
        return None
    if len(active) + len(tombstones) + len(removed) > OBSERVER_SNAPSHOT_EVENT_LIMIT:
        return None
    removed_targets = tuple(
        target for target in removed if isinstance(target, str) and target
    )
    return (
        epoch,
        revision,
        active,
        tombstones,
        removed_targets,
        dropped_event_count,
    )


def _event_mapping(value: object) -> dict[ObserverStateKey, dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    events: dict[ObserverStateKey, dict[str, object]] = {}
    for event in value:
        if not isinstance(event, Mapping) or event.get("schema") != OBSERVER_SCHEMA:
            return None
        copied = dict(event)
        events[observer_state_event_key(copied)] = copied
    return events


def _snapshot_size(snapshot: Mapping[str, object]) -> int:
    return len(
        json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
