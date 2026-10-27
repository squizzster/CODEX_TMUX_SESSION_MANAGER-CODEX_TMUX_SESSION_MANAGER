"""Canonical observer active-state, tombstone, pruning, and epoch reducer."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

from .observer_contract import (
    OBSERVER_CORRELATION_METADATA_LIMIT,
    OBSERVER_CORRELATION_PAYLOAD_LIMIT,
    OBSERVER_CORRELATION_PAYLOAD_MAX_BYTES,
    OBSERVER_CORRELATION_RETENTION_SECONDS,
    OBSERVER_PROJECTED_LIST_ITEM_LIMIT,
    OBSERVER_RECOVERED_TARGET_LIMIT,
    OBSERVER_SCHEMA,
    OBSERVER_SNAPSHOT_EVENT_LIMIT,
    OBSERVER_SNAPSHOT_MAX_BYTES,
)

ObserverStateKey = tuple[str, str, str]
ObserverCorrelationKey = tuple[int, str, str]
ActiveTurnReader = Callable[[str], tuple[bool, str | None]]


@dataclass(slots=True)
class _Correlation:
    """Bounded content-free identity/completion facts survive payload retirement."""

    expires_at: float
    uncertain: bool = False
    targets: set[str] = field(default_factory=set)
    finished_targets: set[str] = field(default_factory=set)
    started_targets: set[str] = field(default_factory=set)
    context_sent_targets: set[str] = field(default_factory=set)
    activity_completed_targets: set[str] = field(default_factory=set)
    activities: dict[str, dict[str, object]] = field(default_factory=dict)
    invocation_phases: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ActiveAgentWork:
    turn_id: str | None = None
    request_id: str | None = None


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

    def __init__(
        self,
        mode: Literal["producer", "consumer"],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        active_turn_reader: ActiveTurnReader = lambda _target: (False, None),
    ) -> None:
        self._mode = mode
        self._epoch = 0 if mode == "producer" else -1
        self._revision = 0 if mode == "producer" else -1
        self._active: dict[ObserverStateKey, dict[str, object]] = {}
        self._tombstones: dict[ObserverStateKey, dict[str, object]] = {}
        self._removed_targets: dict[str, None] = {}
        self._dropped_event_count = 0
        self._known_agent_thread_ids: set[str] = set()
        self._known_agent_paths: dict[str, str] = {}
        self._active_agent_work: dict[str, ActiveAgentWork] = {}
        self._latest_parent_user_message: dict[str, object] | None = None
        self._monotonic = monotonic
        self._active_turn_reader = active_turn_reader
        self._root_thread_id: str | None = None
        self._correlations: dict[ObserverCorrelationKey, _Correlation] = {}
        self._collaboration_invocations: dict[ObserverCorrelationKey, dict[str, object]] = {}
        self._invocation_targets: dict[str, set[ObserverCorrelationKey]] = {}
        self._correlation_history_incomplete = False

    @classmethod
    def producer(
        cls,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        active_turn_reader: ActiveTurnReader = lambda _target: (False, None),
    ) -> ObserverStateReducer:
        return cls("producer", monotonic=monotonic, active_turn_reader=active_turn_reader)

    def bind_root_thread(self, root_thread_id: str) -> None:
        self._require_mode("producer")
        if self._root_thread_id not in {None, root_thread_id}:
            raise ValueError("observer root cannot change within its lifetime")
        self._root_thread_id = root_thread_id

    def bind_active_turn_reader(self, reader: ActiveTurnReader) -> None:
        self._require_mode("producer")
        self._active_turn_reader = reader

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
        return frozenset(self._active_agent_work)

    @property
    def running_agent_count(self) -> int:
        self._require_mode("producer")
        return len(self._active_agent_work)

    def knows_agent(self, target_thread_id: str) -> bool:
        self._require_mode("producer")
        return target_thread_id in self._known_agent_thread_ids

    def start_agent_work(
        self, target_thread_id: str, turn_id: str | None = None, *, request_id: str | None = None
    ) -> bool:
        """Start once per request; missing retained identity is never proof of new work."""
        self._require_mode("producer")
        if request_id is not None:
            correlation = self._correlation(request_id)
            if (
                target_thread_id not in correlation.targets
                and len(correlation.targets) >= OBSERVER_PROJECTED_LIST_ITEM_LIMIT
            ):
                self._mark_correlation_incomplete()
                return False
            self._index_target(request_id, target_thread_id, correlation)
            if target_thread_id in correlation.started_targets or target_thread_id in correlation.finished_targets:
                return False
            if correlation.uncertain:
                # Reconcile the currently active turn from the verified primary
                # source. It does not attribute this delayed request to that turn.
                try:
                    available, active_turn_id = self._active_turn_reader(target_thread_id)
                except Exception:
                    available, active_turn_id = False, None
                if not available or active_turn_id is None:
                    return False
                turn_id, request_id = active_turn_id, None
            else:
                correlation.started_targets.add(target_thread_id)
        if not self._remember_agent(target_thread_id):
            return False
        previous = self._active_agent_work.get(target_thread_id, ActiveAgentWork())
        continues_turn = turn_id is None or previous.turn_id in {None, turn_id}
        current = ActiveAgentWork(
            turn_id if turn_id is not None else previous.turn_id,
            request_id if request_id is not None else previous.request_id if continues_turn else None,
        )
        changed = target_thread_id not in self._active_agent_work or current != previous
        self._active_agent_work[target_thread_id] = current
        return changed

    def _remember_agent(self, target_thread_id: str, path: str | None = None) -> bool:
        if target_thread_id not in self._known_agent_thread_ids:
            if len(self._known_agent_thread_ids) >= OBSERVER_RECOVERED_TARGET_LIMIT:
                retired = next(
                    (target for target in self._known_agent_paths if target not in self._active_agent_work), None
                )
                if retired is None:
                    self._mark_correlation_incomplete()
                    return False
                self._known_agent_thread_ids.discard(retired)
                self._known_agent_paths.pop(retired, None)
                self._mark_correlation_incomplete()
            self._known_agent_thread_ids.add(target_thread_id)
        if path is not None:
            self._known_agent_paths[target_thread_id] = path
        return True

    def finish_agent_work(
        self, target_thread_id: str, turn_id: str | None = None, *, request_id: str | None = None
    ) -> bool:
        """Remove one running agent, never a newer turn or an already finished entry."""
        self._require_mode("producer")
        active = self._active_agent_work.get(target_thread_id)
        if active is None:
            return False
        if turn_id is not None and active.turn_id is not None and turn_id != active.turn_id:
            return False
        if request_id is not None and request_id != active.request_id:
            return False
        del self._active_agent_work[target_thread_id]
        return True

    def agent_work_started_event(self, target_thread_id: str, root_thread_id: str) -> dict[str, object]:
        """Carry current agent identity into a reopened view without replaying an old spawn or prompt."""
        self._require_mode("producer")
        return {
            "schema": OBSERVER_SCHEMA,
            "kind": "agent_work_started",
            "thread_id": root_thread_id,
            "target_thread_id": target_thread_id,
            "agent_path": self._known_agent_paths[target_thread_id],
            "turn_id": self._active_agent_work[target_thread_id].turn_id,
        }

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
        correlation = self._correlations.get(self._correlation_key(item_id))
        return correlation is not None and bool(correlation.activities)

    def tracks_target(self, target_thread_id: str) -> bool:
        self._require_mode("producer")
        return target_thread_id in self._active_agent_work

    def remember_activity(self, item_id: str, event: dict[str, object]) -> bool:
        self._require_mode("producer")
        if event.get("thread_id") != self._root_thread_id:
            return False
        correlation = self._correlation(item_id)
        item = event["item"]
        assert isinstance(item, dict)
        target = str(item["agent_thread_id"])
        if target in correlation.finished_targets:
            return False
        if target not in correlation.targets and len(correlation.targets) >= OBSERVER_PROJECTED_LIST_ITEM_LIMIT:
            self._mark_correlation_incomplete()
            return False
        if not self._remember_agent(target, str(item["agent_path"])):
            return False
        previous = correlation.activities.get(target)
        if previous == event:
            return False
        # Retain only the activity's own bounded metadata, never attached prompts.
        correlation.activities[target] = {key: value for key, value in event.items() if key != "collaboration_invocation"}
        self._index_target(item_id, target, correlation)
        return True

    def activities(self, item_id: str) -> tuple[dict[str, object], ...]:
        self._require_mode("producer")
        correlation = self._correlations.get(self._correlation_key(item_id))
        if correlation is None or correlation.uncertain:
            return ()
        return tuple(
            dict(activity)
            for target, activity in correlation.activities.items()
            if target not in correlation.finished_targets
        )

    def collaboration_invocation(self, item_id: str) -> dict[str, object] | None:
        self._require_mode("producer")
        return self._collaboration_invocations.get(self._correlation_key(item_id))

    def remember_collaboration_invocation(self, item_id: str, invocation: dict[str, object]) -> bool:
        self._require_mode("producer")
        if invocation.get("thread_id") != self._root_thread_id:
            return False
        correlation = self._correlation(item_id)
        phase = str(invocation.get("method"))
        if phase in correlation.invocation_phases or "item/completed" in correlation.invocation_phases:
            return False
        correlation.invocation_phases.add(phase)
        item = invocation["item"]
        assert isinstance(item, dict)
        for target in item["receiver_thread_ids"]:
            self._index_target(item_id, str(target), correlation)
        if not correlation.uncertain and not (
            correlation.targets and correlation.targets <= correlation.finished_targets
        ):
            self._collaboration_invocations[self._correlation_key(item_id)] = deepcopy(invocation)
            self._enforce_payload_bounds()
        return True

    def correlation_is_uncertain(self, item_id: str) -> bool:
        return self._correlation(item_id).uncertain

    def root_request_context_was_sent(self, item_id: str, target: str) -> bool:
        correlation = self._correlation(item_id)
        return (
            correlation.uncertain or target in correlation.context_sent_targets or target in correlation.finished_targets
        )

    def mark_root_request_context_sent(self, item_id: str, target: str) -> None:
        self._correlation(item_id).context_sent_targets.add(target)

    def complete_activity(self, item_id: str, target: str) -> None:
        """Keep completion identity for late correlation; tool completion is not agent completion."""
        self._require_mode("producer")
        self._correlation(item_id).activity_completed_targets.add(target)
        self.settle_correlation(item_id)

    def settle_correlation(self, item_id: str) -> None:
        correlation = self._correlation(item_id)
        settled = correlation.finished_targets | correlation.activity_completed_targets
        if correlation.targets and (
            correlation.targets <= correlation.finished_targets
            or ("item/completed" in correlation.invocation_phases and correlation.targets <= settled)
        ):
            self._collaboration_invocations.pop(self._correlation_key(item_id), None)

    def prune_protocol_target(self, target_thread_id: str) -> dict[str, object]:
        self._require_mode("producer")
        self.finish_agent_work(target_thread_id)
        # This index is populated by invocation recipients independently of any
        # activity. Invocation-first and activity-first therefore retire alike.
        for key in tuple(self._invocation_targets.get(target_thread_id, ())):
            correlation = self._correlations.get(key)
            if correlation is not None:
                correlation.finished_targets.add(target_thread_id)
                correlation.activities.pop(target_thread_id, None)
                self.settle_correlation(key[2])
        return self.prune_target(target_thread_id)

    def expire_protocol_state(self) -> bool:
        self._require_mode("producer")
        now = self._monotonic()
        expired = False
        for key, correlation in tuple(self._correlations.items()):
            if correlation.expires_at <= now:
                self._evict_correlation(key)
                expired = True
        return expired

    def _correlation_key(self, item_id: str) -> ObserverCorrelationKey:
        return self._epoch, self._root_thread_id or "", item_id

    def _correlation(self, item_id: str) -> _Correlation:
        key = self._correlation_key(item_id)
        correlation = self._correlations.get(key)
        if correlation is None:
            while len(self._correlations) >= OBSERVER_CORRELATION_METADATA_LIMIT:
                self._evict_correlation(next(iter(self._correlations)))
            correlation = _Correlation(
                self._monotonic() + OBSERVER_CORRELATION_RETENTION_SECONDS,
                uncertain=self._correlation_history_incomplete,
            )
            self._correlations[key] = correlation
        return correlation

    def _index_target(self, item_id: str, target: str, correlation: _Correlation) -> None:
        if target not in correlation.targets and len(correlation.targets) >= OBSERVER_PROJECTED_LIST_ITEM_LIMIT:
            self._mark_correlation_incomplete()
            return
        correlation.targets.add(target)
        self._invocation_targets.setdefault(target, set()).add(self._correlation_key(item_id))

    def _enforce_payload_bounds(self) -> None:
        payload_bytes = sum(_snapshot_size(value) for value in self._collaboration_invocations.values())
        while (
            len(self._collaboration_invocations) > OBSERVER_CORRELATION_PAYLOAD_LIMIT
            or payload_bytes > OBSERVER_CORRELATION_PAYLOAD_MAX_BYTES
        ):
            key = next(iter(self._collaboration_invocations))
            payload_bytes -= _snapshot_size(self._collaboration_invocations.pop(key))
            self._correlations[key].uncertain = True
            self._mark_correlation_incomplete()

    def _evict_correlation(self, key: ObserverCorrelationKey) -> None:
        correlation = self._correlations.pop(key)
        self._collaboration_invocations.pop(key, None)
        for target in correlation.targets:
            keys = self._invocation_targets[target]
            keys.discard(key)
            if not keys:
                del self._invocation_targets[target]
        self._mark_correlation_incomplete()

    def _mark_correlation_incomplete(self) -> None:
        self._correlation_history_incomplete = True
        self._dropped_event_count += 1
        self._advance_revision()

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
                    item = event.get("item")
                    receivers = item.get("receiver_thread_ids") if isinstance(item, dict) else None
                    remaining = (
                        [target for target in receivers if target != target_thread_id]
                        if isinstance(receivers, list)
                        else []
                    )
                    if remaining:
                        collection[key] = {**event, "item": {**item, "receiver_thread_ids": remaining}}
                        continue
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
        while _snapshot_size(snapshot) > OBSERVER_SNAPSHOT_MAX_BYTES:
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
        upserted = tuple(event for key, event in incoming_active.items() if prior_active.get(key) != event)
        tombstone_events = tuple(
            event for key, event in incoming_tombstones.items() if self._tombstones.get(key) != event
        )
        newly_removed_targets = frozenset(target for target in removed_targets if target not in self._removed_targets)
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
        while len(self._active) + len(self._tombstones) + len(self._removed_targets) > OBSERVER_SNAPSHOT_EVENT_LIMIT:
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
        self._known_agent_thread_ids.clear()
        self._known_agent_paths.clear()
        self._active_agent_work.clear()
        self._latest_parent_user_message = None
        self._collaboration_invocations.clear()
        self._correlations.clear()
        self._invocation_targets.clear()
        self._correlation_history_incomplete = False


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
    if snapshot.get("schema") != OBSERVER_SCHEMA or snapshot.get("kind") != ("observer_state_snapshot"):
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
    removed_targets = tuple(target for target in removed if isinstance(target, str) and target)
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
    return len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
