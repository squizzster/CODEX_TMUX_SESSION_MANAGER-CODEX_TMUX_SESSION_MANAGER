from __future__ import annotations

import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

import rodex_sql.transactions as transactions_module
from rodex_registry import (
    RodexRuntimeId,
    RodexSessionError,
    audit_rodex_database_integrity,
    create_a_rodex_session,
    initialise_rodex_database,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_or_create_rodex_sessions_user,
    lookup_rodex_runtime_instance,
    lookup_rodex_session_log,
    lookup_rodex_tmux_session,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
)


def test_round2_hot_mutation_checks_generation_after_begin_immediate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    statements: list[str] = []
    real_connect = transactions_module.sqlite3.connect

    def traced_connect(*args: object, **kwargs: object):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(transactions_module.sqlite3, "connect", traced_connect)

    record_a_rodex_session_access(session.rodex_sessions_id, database)

    begin = statements.index("BEGIN IMMEDIATE")
    generation = next(
        index
        for index, statement in enumerate(statements)
        if "FROM rodex_schema_generations" in statement
    )
    mutation = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("UPDATE rodex_sessions_log")
    )
    assert begin < generation < mutation
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    assert len(statements) <= 32


def test_round2_hot_mutation_atomically_bootstraps_an_empty_private_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch(mode=0o600)

    user = lookup_or_create_rodex_sessions_user(1234, 1234, "round2", database)

    assert user.id == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_generation FROM rodex_schema_generations WHERE id = 1"
        ).fetchone() == (17,)
        assert connection.execute(
            "SELECT uid, gid, user_name FROM rodex_sessions_users"
        ).fetchall() == [(1234, 1234, "round2")]


def test_round2_concurrent_empty_mutations_run_the_cold_bootstrap_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch(mode=0o600)
    statements: list[str] = []
    real_connect = transactions_module.sqlite3.connect

    def traced_connect(*args: object, **kwargs: object):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(transactions_module.sqlite3, "connect", traced_connect)
    barrier = Barrier(4)

    def create_user(index: int) -> int:
        barrier.wait()
        return lookup_or_create_rodex_sessions_user(
            2000 + index,
            2000 + index,
            f"round2-{index}",
            database,
        ).id

    with ThreadPoolExecutor(max_workers=4) as workers:
        user_ids = list(workers.map(create_user, range(4)))

    assert len(set(user_ids)) == 4
    assert (
        sum(
            statement.lstrip().startswith(
                "CREATE TABLE IF NOT EXISTS rodex_schema_generations"
            )
            for statement in statements
        )
        == 1
    )
    assert statements.count("BEGIN IMMEDIATE") == 4
    assert statements.count("COMMIT") == 4


def test_round2_hot_mutation_rejects_a_mismatched_generation_before_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    before = lookup_rodex_session_log(session.rodex_sessions_id, database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rodex_schema_generations SET schema_generation = 13 WHERE id = 1"
        )

    with pytest.raises(RodexSessionError, match="schema generation does not match"):
        record_a_rodex_session_access(session.rodex_sessions_id, database)

    assert lookup_rodex_session_log(session.rodex_sessions_id, database) == before


def test_round2_stale_resume_rejects_the_whole_incarnation_tuple(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    original_codex_id = uuid.uuid4()
    current_codex_id = uuid.uuid4()
    stale_codex_id = uuid.uuid4()
    session = create_a_rodex_session(
        database,
        codex_session_id=original_codex_id,
        tmux_server_socket_path="/tmp/rodex/original.sock",
        tmux_session_name="original",
    )
    current_runtime_id = RodexRuntimeId.generate()
    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/current.sock",
        "current",
        database,
        codex_session_id=current_codex_id,
        runtime_id=current_runtime_id,
        accessed_at_utc=datetime(2032, 1, 1, 12, tzinfo=UTC),
    )

    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/stale.sock",
        "stale",
        database,
        codex_session_id=stale_codex_id,
        runtime_id=RodexRuntimeId.generate(),
        accessed_at_utc=datetime(2032, 1, 1, 11, tzinfo=UTC),
    )

    runtime = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    tmux = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert runtime is not None
    assert tmux is not None
    assert runtime.runtime_id == current_runtime_id
    assert runtime.started_at_utc == "2032-01-01T12:00:00.000000Z"
    assert (tmux.tmux_server_socket_path, tmux.tmux_session_name) == (
        "/tmp/rodex/current.sock",
        "current",
    )
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == current_codex_id
    )


def test_round2_concurrent_resume_keeps_one_newest_incarnation_tuple(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=uuid.uuid4(),
        tmux_server_socket_path="/tmp/rodex/original.sock",
        tmux_session_name="original",
    )
    newer_runtime_id = RodexRuntimeId.generate()
    newer_codex_id = uuid.uuid4()
    barrier = Barrier(2)

    def resume(
        endpoint: str,
        name: str,
        codex_id: uuid.UUID,
        runtime_id: RodexRuntimeId,
        hour: int,
    ) -> None:
        barrier.wait()
        record_a_rodex_session_runtime_resume(
            session.rodex_sessions_id,
            endpoint,
            name,
            database,
            codex_session_id=codex_id,
            runtime_id=runtime_id,
            accessed_at_utc=datetime(2032, 1, 1, hour, tzinfo=UTC),
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = (
            workers.submit(
                resume,
                "/tmp/rodex/newer.sock",
                "newer",
                newer_codex_id,
                newer_runtime_id,
                12,
            ),
            workers.submit(
                resume,
                "/tmp/rodex/older.sock",
                "older",
                uuid.uuid4(),
                RodexRuntimeId.generate(),
                11,
            ),
        )
        for future in futures:
            future.result()

    runtime = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    tmux = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert runtime is not None
    assert tmux is not None
    assert runtime.runtime_id == newer_runtime_id
    assert (tmux.tmux_server_socket_path, tmux.tmux_session_name) == (
        "/tmp/rodex/newer.sock",
        "newer",
    )
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == newer_codex_id
    )


def test_round2_resume_heals_a_poisoned_future_incarnation_as_one_tuple(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=uuid.uuid4(),
        tmux_server_socket_path="/tmp/rodex/original.sock",
        tmux_session_name="original",
        runtime_id=RodexRuntimeId.generate(),
    )
    replacement_codex_id = uuid.uuid4()
    replacement_runtime_id = RodexRuntimeId.generate()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rodex_runtime_instances SET started_at_utc = ?",
            ("2099-01-01T00:00:00.000000Z",),
        )
        connection.execute(
            "UPDATE rodex_sessions_log SET last_accessed_at_utc = ?",
            ("2099-01-01T00:00:00.000000Z",),
        )
    connection.close()

    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/healed.sock",
        "healed",
        database,
        codex_session_id=replacement_codex_id,
        runtime_id=replacement_runtime_id,
        accessed_at_utc=datetime(2030, 1, 2, tzinfo=UTC),
    )

    runtime = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    tmux = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    log = lookup_rodex_session_log(session.rodex_sessions_id, database)
    assert runtime is not None
    assert tmux is not None
    assert log is not None
    assert runtime.runtime_id == replacement_runtime_id
    assert runtime.started_at_utc == "2030-01-02T00:00:00.000000Z"
    assert (tmux.tmux_server_socket_path, tmux.tmux_session_name) == (
        "/tmp/rodex/healed.sock",
        "healed",
    )
    assert log.last_accessed_at_utc == "2030-01-02T00:00:00.000000Z"
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == replacement_codex_id
    )


def test_round2_resume_rejects_a_noncanonical_runtime_start(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=uuid.uuid4(),
        tmux_server_socket_path="/tmp/rodex/original.sock",
        tmux_session_name="original",
        runtime_id=RodexRuntimeId.generate(),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rodex_runtime_instances SET started_at_utc = ?",
            ("2030-01-01T00:00:00+00:00",),
        )

    with pytest.raises(RodexSessionError, match="canonical UTC timestamp"):
        record_a_rodex_session_runtime_resume(
            session.rodex_sessions_id,
            "/tmp/rodex/rejected.sock",
            "rejected",
            database,
            runtime_id=RodexRuntimeId.generate(),
            accessed_at_utc=datetime(2030, 1, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "durable_timestamp",
    ["not-a-timestamp", "2030-01-01T00:00:00+00:00"],
)
def test_round2_access_rejects_noncanonical_durable_high_water_marks(
    tmp_path: Path,
    durable_timestamp: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rodex_sessions_log SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            (durable_timestamp, session.rodex_sessions_id),
        )

    with pytest.raises(RodexSessionError, match="canonical UTC timestamp"):
        record_a_rodex_session_access(
            session.rodex_sessions_id,
            database,
            accessed_at_utc=datetime(2030, 1, 2, tzinfo=UTC),
        )


def test_round2_access_heals_a_durable_mark_beyond_the_skew_window(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rodex_sessions_log SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            ("2099-01-01T00:00:00.000000Z", session.rodex_sessions_id),
        )

    healed = record_a_rodex_session_access(
        session.rodex_sessions_id,
        database,
        accessed_at_utc=datetime(2030, 1, 2, tzinfo=UTC),
    )

    assert healed.last_accessed_at_utc == "2030-01-02T00:00:00.000000Z"
    assert lookup_rodex_session_log(session.rodex_sessions_id, database) == healed


@pytest.mark.parametrize("object_kind", ["table", "index", "trigger"])
def test_round2_integrity_audit_rejects_every_unexpected_schema_object(
    tmp_path: Path,
    object_kind: str,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    statements = {
        "table": "CREATE TABLE unexpected_round2_table (id INTEGER)",
        "index": (
            "CREATE INDEX unexpected_round2_index "
            "ON rodex_sessions_log (last_accessed_at_utc)"
        ),
        "trigger": (
            "CREATE TRIGGER unexpected_round2_trigger AFTER UPDATE "
            "ON rodex_sessions_log BEGIN SELECT 1; END"
        ),
    }
    with sqlite3.connect(database) as connection:
        connection.execute(statements[object_kind])
    connection.close()

    with pytest.raises(RodexSessionError, match="unexpected schema objects"):
        audit_rodex_database_integrity(database)


def test_round2_integrity_audit_uses_a_read_only_wal_aware_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = initialise_rodex_database(tmp_path / "valid.sqlite3")
    invalid = initialise_rodex_database(tmp_path / "invalid.sqlite3")
    with sqlite3.connect(invalid) as connection:
        connection.execute("CREATE TABLE unexpected_round2_table (id INTEGER)")
    connection.close()
    # Complete fixture WAL cleanup before measuring the read-only audit itself.
    transactions_module._close_process_wal_lifetime_owner()
    before_valid = (valid.stat().st_size, valid.stat().st_mtime_ns)
    before_invalid = (invalid.stat().st_size, invalid.stat().st_mtime_ns)
    statements: list[str] = []
    real_connect = transactions_module.sqlite3.connect

    def traced_connect(*args: object, **kwargs: object):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(transactions_module.sqlite3, "connect", traced_connect)

    assert audit_rodex_database_integrity(valid) == valid
    with pytest.raises(RodexSessionError):
        audit_rodex_database_integrity(invalid)

    assert (valid.stat().st_size, valid.stat().st_mtime_ns) == before_valid
    assert (invalid.stat().st_size, invalid.stat().st_mtime_ns) == before_invalid
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER ")
    assert not any(
        statement.lstrip().upper().startswith(forbidden) for statement in statements
    )
    assert statements.count("BEGIN") == 2


def test_round2_integrity_audit_sees_an_unexpected_view_in_an_active_wal(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "active.sqlite3")
    writer = sqlite3.connect(database, isolation_level=None)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "CREATE VIEW unexpected_round2_view AS "
            "SELECT rodex_sessions_id FROM rodex_sessions_log"
        )
        wal = Path(f"{database}-wal")
        before = (
            database.stat().st_mtime_ns,
            wal.read_bytes(),
            wal.stat().st_mtime_ns,
        )

        with pytest.raises(RodexSessionError, match="unexpected schema objects"):
            audit_rodex_database_integrity(database)

        after = (
            database.stat().st_mtime_ns,
            wal.read_bytes(),
            wal.stat().st_mtime_ns,
        )
        assert after == before
    finally:
        writer.close()
