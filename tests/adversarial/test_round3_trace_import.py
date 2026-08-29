from __future__ import annotations

import gc
import uuid
import weakref
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import rodex_registry.agent_trace_writer as writer_module
import rodex_sql.transactions as transactions_module
from rodex_registry import (
    RodexAgentTraceEvent,
    RodexAgentTracePublication,
    create_a_rodex_session,
)
from rodex_registry.agent_trace_contract import (
    PreparedAgentTraceEvent,
    PreparedAgentTracePublication,
    TraceContext,
    TraceToolCall,
    prepare_agent_trace_publication,
)
from rodex_registry.agent_trace_writer import publish_agent_trace_in_transaction
from rodex_registry.execution import resolve_codex_thread_identity_in_transaction
from rodex_sql import RodexSQLError, open_rodex_transaction

THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


def _cold_publication(
    event_count: int,
    *,
    thread_id: uuid.UUID = THREAD_ID,
) -> RodexAgentTracePublication:
    return RodexAgentTracePublication(
        based_on_trace_publication_sequence=None,
        trace_schema_version="round3-v1",
        calculated_at_utc="2026-08-29T00:00:00Z",
        coverage_state="complete",
        events=tuple(
            RodexAgentTraceEvent(
                codex_thread_id=thread_id,
                codex_turn_id=None,
                source_record_ordinal=ordinal,
                derived_event_ordinal=0,
                event_kind="unrecognized_record",
                event_time_utc=None,
            )
            for ordinal in range(event_count)
        ),
    )


def _is_membership_lookup(statement: str) -> bool:
    normalized = " ".join(statement.upper().split())
    return (
        "FROM RODEX_SESSIONS_CODEX_THREADS AS MEMBERSHIPS" in normalized
        and "JOIN CODEX_THREADS AS IDENTITIES" in normalized
        and "MEMBERSHIPS.RODEX_SESSIONS_ID" in normalized
    )


def test_round3_cold_trace_import_resolves_each_thread_membership_once(
    tmp_path: Path,
) -> None:
    """Same-thread cold events must not issue one membership SELECT per event."""
    database = tmp_path / "registry.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    statements: list[str] = []
    prepared = prepare_agent_trace_publication(_cold_publication(12))

    with open_rodex_transaction(database) as connection:
        connection.set_trace_callback(statements.append)
        publish_agent_trace_in_transaction(
            connection,
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )
        connection.set_trace_callback(None)

    membership_lookups = sum(map(_is_membership_lookup, statements))
    assert membership_lookups <= 1, (
        f"one cold batch for one thread performed {membership_lookups} identical "
        "membership lookups"
    )


def test_round3_cold_trace_writer_window_has_one_transaction_and_linear_sql_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold trace batch must use one bounded writer transaction, not N lookups."""
    database = tmp_path / "registry.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    statements: list[str] = []
    real_connect = transactions_module._connect_validated_database

    def traced_connect(*args: object, **kwargs: object):
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(
        transactions_module,
        "_connect_validated_database",
        traced_connect,
    )
    event_count = 40
    prepared = prepare_agent_trace_publication(_cold_publication(event_count))

    with open_rodex_transaction(database) as connection:
        publish_agent_trace_in_transaction(
            connection,
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )

    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    begin = statements.index("BEGIN IMMEDIATE")
    commit = statements.index("COMMIT", begin)
    writer_window = statements[begin : commit + 1]
    assert sum(map(_is_membership_lookup, writer_window)) == 1
    assert len(writer_window) <= event_count + 8, (
        "cold trace writer work must be one event INSERT per event plus a fixed "
        f"budget; observed {len(writer_window)} statements for {event_count} events"
    )


def test_round3_trace_contract_rejects_all_semantic_errors_before_sql(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    invalid = RodexAgentTracePublication(
        based_on_trace_publication_sequence=None,
        trace_schema_version="round3-v1",
        calculated_at_utc="not-a-timestamp",
        coverage_state="complete",
        events=(
            RodexAgentTraceEvent(
                codex_thread_id=THREAD_ID,
                codex_turn_id=None,
                source_record_ordinal=0,
                derived_event_ordinal=0,
                event_kind="tool_call",
                event_time_utc="also-not-a-timestamp",
                detail=TraceToolCall(
                    item_id=None,
                    call_id=None,
                    tool_name="exec",
                    tool_status=None,
                    request_utf8_bytes=-1,
                    response_utf8_bytes=0,
                    payload_capture_state="rollout_reference",
                    activity_kind="unsupported",
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match=r"UTC|timestamp"):
        prepare_agent_trace_publication(invalid)

    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_round3_trace_writer_rejects_raw_publications_without_preparing_them(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)

    with (
        open_rodex_transaction(database) as connection,
        pytest.raises(TypeError, match="contract-prepared"),
    ):
        publish_agent_trace_in_transaction(
            connection,
            1,
            cast(PreparedAgentTracePublication, _cold_publication(1)),
            model_name_ids={},
            reasoning_effort_name_ids={},
        )


class _NoSQLConnection:
    def __init__(self, *, in_transaction: bool) -> None:
        self.in_transaction = in_transaction
        self.statements = 0

    def execute(self, *_args: object, **_kwargs: object) -> object:
        self.statements += 1
        raise AssertionError("rejected trace input must not execute SQL")


def test_round3_trace_writer_rejects_a_forged_prepared_value_before_sql() -> None:
    legitimate = prepare_agent_trace_publication(_cold_publication(1))
    event = legitimate.events[0]
    with pytest.raises(TypeError, match="contract-prepared"):
        PreparedAgentTraceEvent(
            event=event.event,
            codex_thread_id=event.codex_thread_id,
            source_key=event.source_key,
            event_kind=event.event_kind,
            detail_sha256="0" * 64,
        )

    forged = object.__new__(PreparedAgentTracePublication)
    for field_name in (
        "based_on_trace_publication_sequence",
        "trace_schema_version",
        "calculated_at_utc",
        "coverage_state",
        "events",
        "source_thread_ids",
    ):
        object.__setattr__(forged, field_name, getattr(legitimate, field_name))
    object.__setattr__(forged, "_contract_token", legitimate._contract_token)
    connection = _NoSQLConnection(in_transaction=True)

    with pytest.raises(TypeError, match="contract-prepared"):
        publish_agent_trace_in_transaction(
            cast("object", connection),  # type: ignore[arg-type]
            1,
            forged,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )

    assert connection.statements == 0


def test_round3_trace_prepared_values_cannot_be_replaced() -> None:
    legitimate = prepare_agent_trace_publication(_cold_publication(1))

    with pytest.raises(TypeError, match="contract-prepared"):
        replace(legitimate, coverage_state="gapped")


def test_round3_trace_contract_seal_does_not_retain_finished_publications() -> None:
    prepared = prepare_agent_trace_publication(_cold_publication(1))
    reference = weakref.ref(prepared)

    del prepared
    gc.collect()

    assert reference() is None


def test_round3_trace_writer_requires_an_active_transaction_before_sql() -> None:
    prepared = prepare_agent_trace_publication(_cold_publication(1))
    connection = _NoSQLConnection(in_transaction=False)

    with pytest.raises(RodexSQLError, match="active transaction"):
        publish_agent_trace_in_transaction(
            cast("object", connection),  # type: ignore[arg-type]
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )

    assert connection.statements == 0


def test_round3_trace_contract_canonically_normalizes_typed_text_before_sql() -> None:
    raw = RodexAgentTracePublication(
        based_on_trace_publication_sequence=None,
        trace_schema_version="  round3-v1  ",
        calculated_at_utc="2026-08-29T01:00:00+01:00",
        coverage_state="  complete  ",
        events=(
            RodexAgentTraceEvent(
                codex_thread_id=THREAD_ID,
                codex_turn_id=None,
                source_record_ordinal=0,
                derived_event_ordinal=0,
                event_kind="  turn_context  ",
                event_time_utc="2026-08-29T01:00:00+01:00",
                detail=TraceContext(
                    model="  gpt-test  ",
                    reasoning_effort="  high  ",
                    working_directory="  /workspace  ",
                    sandbox_mode="  workspace-write  ",
                    approval_policy="  never  ",
                    permission_profile_type="  disabled  ",
                    workspace_root_count=1,
                ),
            ),
        ),
    )

    prepared = prepare_agent_trace_publication(raw)

    assert prepared.trace_schema_version == "round3-v1"
    assert prepared.calculated_at_utc == "2026-08-29T00:00:00.000000Z"
    assert prepared.coverage_state == "complete"
    event = prepared.events[0].event
    assert event.event_kind == "turn_context"
    assert event.event_time_utc == "2026-08-29T00:00:00.000000Z"
    assert event.detail == TraceContext(
        model="gpt-test",
        reasoning_effort="high",
        working_directory="/workspace",
        sandbox_mode="workspace-write",
        approval_policy="never",
        permission_profile_type="disabled",
        workspace_root_count=1,
    )


def test_round3_one_event_resolves_only_its_one_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    irrelevant_memberships = 4_000
    with open_rodex_transaction(database) as connection:
        connection.executemany(
            "INSERT INTO codex_threads "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (0, ?)",
            ((index,) for index in range(1, irrelevant_memberships + 1)),
        )
        connection.executemany(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, '2026-08-29T00:00:00Z')",
            ((index,) for index in range(2, irrelevant_memberships + 2)),
        )

    resolved_counts: list[int] = []
    real_resolver = writer_module._trace_thread_memberships

    def recording_resolver(*args: object, **kwargs: object):
        result = real_resolver(*args, **kwargs)  # type: ignore[arg-type]
        resolved_counts.append(len(result))
        return result

    monkeypatch.setattr(writer_module, "_trace_thread_memberships", recording_resolver)
    prepared = prepare_agent_trace_publication(_cold_publication(1))
    with open_rodex_transaction(database) as connection:
        publish_agent_trace_in_transaction(
            connection,
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )

    assert resolved_counts == [1]


def test_round3_trace_membership_resolution_observes_same_transaction_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    new_thread_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f84")
    prepared = prepare_agent_trace_publication(
        _cold_publication(1, thread_id=new_thread_id)
    )

    with open_rodex_transaction(database) as connection:
        identity_id = resolve_codex_thread_identity_in_transaction(
            connection,
            new_thread_id,
        )
        connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, '2026-08-29T00:00:00.000000Z')",
            (identity_id,),
        )
        receipt = publish_agent_trace_in_transaction(
            connection,
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )

    assert receipt.durable_event_count == 1


def test_round3_trace_membership_values_queries_are_bounded_chunks() -> None:
    parameter_counts: list[int] = []

    class EmptyCursor:
        def fetchall(self) -> list[object]:
            return []

    class RecordingConnection:
        def execute(self, statement: str, parameters: list[object]) -> EmptyCursor:
            assert "IN (VALUES" in statement
            parameter_counts.append(len(parameters))
            return EmptyCursor()

    thread_ids = frozenset(uuid.UUID(int=index) for index in range(1, 802))

    resolved = writer_module._trace_thread_memberships(
        RecordingConnection(),  # type: ignore[arg-type]
        1,
        thread_ids,
    )

    assert resolved == {}
    assert parameter_counts == [801, 801, 3]
