from __future__ import annotations

import uuid
from pathlib import Path

from rodex.analytics_source_catalog import AnalyticsSourceCatalog

CODEX_UUID_V7 = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


def test_catalog_searches_only_uuid_date_window_and_caches_resolution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    expected = root / "2026" / "08" / "16" / f"rollout-example-{CODEX_UUID_V7}.jsonl"
    expected.parent.mkdir(parents=True)
    expected.write_text("{}\n", encoding="utf-8")
    unrelated = root / "unrelated" / "deep" / f"rollout-{CODEX_UUID_V7}.jsonl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("{}\n", encoding="utf-8")
    catalog = AnalyticsSourceCatalog(root)

    assert catalog.candidate_paths(CODEX_UUID_V7) == (expected,)

    catalog.remember_resolved_path(CODEX_UUID_V7, expected.resolve())
    expected.rename(tmp_path / "moved.jsonl")
    assert catalog.candidate_paths(CODEX_UUID_V7) == (expected.resolve(),)


def test_catalog_learns_non_v7_thread_date_from_ready_lifecycle_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    thread_id = uuid.UUID("87654321-4321-4321-8321-123456789abc")
    expected = root / "2026" / "08" / "25" / f"rollout-example-{thread_id}.jsonl"
    expected.parent.mkdir(parents=True)
    expected.write_text("{}\n", encoding="utf-8")
    catalog = AnalyticsSourceCatalog(root)

    catalog.observe_protocol_event(
        {
            "method": "rodex/event-stream/ready",
            "params": {
                "knownThreads": [{"id": str(thread_id), "createdAt": 1_787_616_000}]
            },
        }
    )

    assert catalog.candidate_thread_ids() == frozenset({thread_id})
    assert catalog.candidate_paths(thread_id) == (expected,)


def test_cold_session_tree_candidates_are_date_bounded_regular_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    expected = root / "2026" / "08" / "16" / "rollout-child.jsonl"
    expected.parent.mkdir(parents=True)
    expected.write_text("{}\n", encoding="utf-8")
    outside = root / "2026" / "08" / "20" / "rollout-outside.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}\n", encoding="utf-8")
    linked = expected.parent / "rollout-symlink.jsonl"
    linked.symlink_to(outside)

    candidates = AnalyticsSourceCatalog(root).session_tree_candidate_paths(
        CODEX_UUID_V7,
        first_linked_at_utc="2026-08-20T12:00:00Z",
    )

    assert candidates == (expected,)
