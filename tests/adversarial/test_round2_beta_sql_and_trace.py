from __future__ import annotations

import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import rodex.agent_trace_commands as trace_commands
import rodex_sql.private_database_path as private_path_module
import rodex_sql.transactions as transactions_module
from rodex.errors import RodexLaunchError
from rodex_registry import (
    RodexAgentTraceSnapshot,
    create_a_rodex_session,
    initialise_rodex_database,
    record_a_rodex_session_access,
)

STEADY_STATE_STATEMENT_BUDGET = 32


def _trace_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[object]]:
    statements: list[str] = []
    connections: list[object] = []
    real_connect = transactions_module.sqlite3.connect

    def traced_connect(*args: object, **kwargs: object):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(transactions_module.sqlite3, "connect", traced_connect)
    return statements, connections


def _empty_trace_snapshot() -> RodexAgentTraceSnapshot:
    return RodexAgentTraceSnapshot(
        trace_publication_sequence=1,
        trace_schema_version="test-v1",
        calculated_at_utc="2026-08-29T00:00:00Z",
        coverage_state="complete",
        durable_event_count=0,
        unrecognized_record_count=0,
        events=(),
    )


def test_round2_steady_state_schema_bootstrap_has_a_small_sql_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    initialise_rodex_database(database)
    statements, connections = _trace_sql(monkeypatch)

    initialise_rodex_database(database)

    assert len(statements) <= STEADY_STATE_STATEMENT_BUDGET, (
        "steady-state bootstrap must use a schema-generation fast path; "
        f"observed {len(statements)} SQL statements"
    )
    assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) <= 1
    assert sum(sql == "COMMIT" for sql in statements) <= 1
    assert len(connections) == 1


def test_round2_hot_access_mutation_uses_one_small_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    statements, connections = _trace_sql(monkeypatch)

    record_a_rodex_session_access(session.rodex_sessions_id, database)

    assert len(statements) <= STEADY_STATE_STATEMENT_BUDGET, (
        "a hot row mutation must not replay the whole schema verifier; "
        f"observed {len(statements)} SQL statements"
    )
    assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
    assert sum(sql == "COMMIT" for sql in statements) == 1
    assert len(connections) == 1


def test_round2_hot_session_creation_uses_one_bounded_writer_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    initialise_rodex_database(database)
    statements, connections = _trace_sql(monkeypatch)

    create_a_rodex_session(database, codex_session_id=uuid.uuid4())

    assert len(statements) <= STEADY_STATE_STATEMENT_BUDGET
    assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
    assert sum(sql == "COMMIT" for sql in statements) == 1
    assert len(connections) == 1
    assert not any("PRAGMA table_info(cool_names)" in sql for sql in statements)


def test_round2_steady_paths_do_not_repeat_database_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    real_fchmod = private_path_module.os.fchmod
    chmod_calls: list[int] = []

    def tracked_fchmod(descriptor: int, mode: int) -> None:
        chmod_calls.append(mode)
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(private_path_module.os, "fchmod", tracked_fchmod)

    initialise_rodex_database(database)
    record_a_rodex_session_access(session.rodex_sessions_id, database)

    assert chmod_calls == []


def test_round2_concurrent_bootstrap_runs_full_schema_creation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    statements, _connections = _trace_sql(monkeypatch)

    with ThreadPoolExecutor(max_workers=8) as workers:
        paths = list(
            workers.map(lambda _index: initialise_rodex_database(database), range(8))
        )

    assert paths == [database] * 8
    registry_creates = [
        sql
        for sql in statements
        if sql.lstrip().startswith("CREATE TABLE IF NOT EXISTS rodex_registries")
    ]
    assert len(registry_creates) == 1


def test_round2_trace_follow_rejects_include_bodies_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    def unexpected_read(*_args: object, **_kwargs: object) -> RodexAgentTraceSnapshot:
        nonlocal reads
        reads += 1
        raise AssertionError("rejected body-follow mode reached SQLite")

    monkeypatch.setattr(trace_commands, "read_rodex_agent_trace", unexpected_read)

    with pytest.raises(
        RodexLaunchError,
        match=r"--include-bodies is snapshot-only.*--follow",
    ):
        trace_commands._show_or_follow_trace(
            "session",
            1,
            tmp_path / "unused.sqlite3",
            follow=True,
            include_bodies=True,
            as_json=False,
            after_event_id=None,
            limit=1,
        )

    assert reads == 0


def test_round2_trace_parser_explains_that_follow_is_metadata_only() -> None:
    with pytest.raises(
        RodexLaunchError,
        match=r"snapshot-only.*metadata-only",
    ):
        trace_commands._parse_trace_arguments(
            ["_trace", "session", "--follow", "--include-bodies"]
        )


def test_round2_public_trace_rejects_body_follow_before_session_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups = 0

    def unexpected_lookup(*_args: object, **_kwargs: object) -> int:
        nonlocal lookups
        lookups += 1
        raise AssertionError("rejected body-follow mode reached SQLite")

    monkeypatch.setattr(
        trace_commands,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        unexpected_lookup,
    )

    with pytest.raises(
        RodexLaunchError,
        match=r"snapshot-only.*metadata-only",
    ):
        trace_commands.execute_agent_trace_command(
            ["_trace", "session", "--follow", "--include-bodies"],
            tmp_path / "unused.sqlite3",
        )

    assert lookups == 0


def test_round2_one_shot_trace_body_lookup_reauthenticates_same_size_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = uuid.uuid4()
    rollout = tmp_path / "rollout.jsonl"

    def content(answer: str) -> bytes:
        return (
            json.dumps(
                {
                    "ordinal": 0,
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {"type": "AgentMessage", "content": answer},
                    },
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

    authenticated = content("safe")
    replacement = content("evil")
    assert len(authenticated) == len(replacement)
    rollout.write_bytes(authenticated)
    source = SimpleNamespace(
        codex_thread_id=thread_id,
        rollout_file_path=str(rollout),
        analyzed_size_bytes=len(authenticated),
        analyzed_prefix_sha256=hashlib.sha256(authenticated).hexdigest(),
    )
    monkeypatch.setattr(
        trace_commands,
        "list_rodex_session_codex_rollout_sources",
        lambda *_args, **_kwargs: [source],
    )
    real_pread = trace_commands.os.pread
    bytes_read = 0

    def counted_pread(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal bytes_read
        content = real_pread(descriptor, size, offset)
        bytes_read += len(content)
        return content

    monkeypatch.setattr(trace_commands.os, "pread", counted_pread)
    event = {
        "codex_thread_id": str(thread_id),
        "source_record_ordinal": 0,
        "event_kind": "message",
    }
    trace_commands._attach_authenticated_rollout_bodies(
        [event],
        session_id=1,
        database_path=tmp_path / "unused.sqlite3",
    )
    assert event["body"]["value"]["content"] == "safe"  # type: ignore[index]
    before = rollout.stat()
    rollout.write_bytes(replacement)
    os.utime(
        rollout,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )

    with pytest.raises(RodexLaunchError, match="authenticated rollout prefix changed"):
        trace_commands._attach_authenticated_rollout_bodies(
            [dict(event)],
            session_id=1,
            database_path=tmp_path / "unused.sqlite3",
        )
    assert bytes_read == 2 * len(authenticated)


def test_round2_idle_trace_follow_increases_its_database_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    queries = 0

    def read_empty(*args: object, **kwargs: object) -> RodexAgentTraceSnapshot:
        nonlocal queries
        queries += 1
        return _empty_trace_snapshot()

    requested_delays: list[float] = []

    def virtual_sleep(delay: float) -> None:
        requested_delays.append(delay)
        if len(requested_delays) == 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(trace_commands, "read_rodex_agent_trace", read_empty)
    monkeypatch.setattr(trace_commands.time, "sleep", virtual_sleep)

    with pytest.raises(KeyboardInterrupt):
        trace_commands._show_or_follow_trace(
            "session",
            1,
            tmp_path / "unused.sqlite3",
            follow=True,
            include_bodies=False,
            as_json=False,
            after_event_id=None,
            limit=10,
        )

    capsys.readouterr()
    assert queries == len(requested_delays)
    assert requested_delays == [1.0, 2.0, 4.0, 8.0], (
        "an idle follower must back off its DB-open/query cadence; "
        f"requested delays were {requested_delays}"
    )
