"""Exact SQLite schema and durable Rodex registry identity."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from cool_name.functions import create_and_verify_cool_names_schema
from rodex_sql import (
    RODEX_DATABASE_SCHEMA_GENERATION,
    normalise_rodex_database_path,
    open_rodex_read_transaction,
    open_rodex_transaction,
    require_existing_rodex_database_path,
)
from rodex_sql import (
    default_rodex_database_path as _default_rodex_database_path,
)

from .errors import RodexSessionError
from .identity import RodexRegistryId
from .statistics_fields import SESSION_STATISTICS_SCALARS, TURN_STATISTICS_SCALARS

RODEX_SCHEMA_GENERATIONS_TABLE: Final = "rodex_schema_generations"
RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_REGISTRIES_TABLE: Final = "rodex_registries"
RODEX_REGISTRIES_ID_UNIQUE_INDEX: Final = "rodex_registries_registry_id_unique"
RODEX_SESSION_ID_UNIQUE_INDEX: Final = "rodex_sessions_session_id_unique"
RODEX_SESSIONS_USERS_TABLE: Final = "rodex_sessions_users"
RODEX_SESSIONS_USERS_UNIQUE_INDEX: Final = "rodex_sessions_users_uid_gid_user_name_unique"
RODEX_SESSIONS_LOG_TABLE: Final = "rodex_sessions_log"
RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_log_rodex_sessions_id_unique"
)
CODEX_THREADS_TABLE: Final = "codex_threads"
CODEX_THREADS_PUBLIC_ID_UNIQUE_INDEX: Final = "codex_threads_public_id_unique"
RODEX_TMUX_SESSIONS_TABLE: Final = "rodex_tmux_sessions"
RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_tmux_sessions_rodex_sessions_id_unique"
)
RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX: Final = "rodex_tmux_sessions_endpoint_unique"
RODEX_RUNTIME_INSTANCES_TABLE: Final = "rodex_runtime_instances"
RODEX_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX: Final = (
    "rodex_runtime_instances_rodex_sessions_id_unique"
)
RODEX_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX: Final = (
    "rodex_runtime_instances_runtime_id_unique"
)
MODEL_NAMES_TABLE: Final = "model_names"
MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX: Final = "model_names_name_of_the_model_unique"
REASONING_EFFORT_NAMES_TABLE: Final = "reasoning_effort_names"
REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX: Final = (
    "reasoning_effort_names_name_of_the_reasoning_effort_unique"
)
TOOL_NAMES_TABLE: Final = "tool_names"
TOOL_NAMES_NAME_UNIQUE_INDEX: Final = "tool_names_name_unique"
RODEX_SESSIONS_STATISTICS_TABLE: Final = "rodex_sessions_statistics"
RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_rodex_sessions_id_unique"
)
RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE: Final = (
    "rodex_sessions_statistics_distributions"
)
RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_distributions_kind_unique"
)
RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE: Final = (
    "rodex_sessions_statistics_named_counts"
)
RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_named_counts_key_unique"
)
RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE: Final = (
    "rodex_sessions_statistics_audit_limits"
)
RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_audit_limits_ordinal_unique"
)
RODEX_SESSIONS_CODEX_THREADS_TABLE: Final = "rodex_sessions_codex_threads"
RODEX_SESSIONS_CODEX_THREADS_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_threads_codex_threads_id_unique"
)
RODEX_SESSIONS_CODEX_THREADS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_threads_session_id_unique"
)
RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE: Final = "rodex_sessions_current_codex_threads"
RODEX_SESSIONS_CURRENT_CODEX_THREADS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_current_codex_threads_session_unique"
)
RODEX_SESSIONS_CURRENT_CODEX_THREADS_MEMBERSHIP_UNIQUE_INDEX: Final = (
    "rodex_sessions_current_codex_threads_membership_unique"
)
RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE: Final = "rodex_sessions_codex_rollout_sources"
RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_THREAD_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_rollout_sources_thread_unique"
)
RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_rollout_sources_session_id_unique"
)
RODEX_SESSIONS_CODEX_TURNS_TABLE: Final = "rodex_sessions_codex_turns"
RODEX_SESSIONS_CODEX_TURNS_SOURCE_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_turns_source_turn_unique"
)
RODEX_SESSIONS_CODEX_TURNS_SESSION_TURN_INDEX: Final = (
    "rodex_sessions_codex_turns_session_turn"
)
RODEX_SESSIONS_CODEX_TURNS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_turns_session_id_unique"
)
RODEX_SESSIONS_CODEX_TURNS_SOURCE_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_turns_source_id_unique"
)
RODEX_SESSIONS_CODEX_TURNS_PUBLIC_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_turns_public_id_unique"
)
RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE: Final = "rodex_sessions_codex_activity_scopes"
RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_WITHOUT_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_activity_scopes_without_turn_unique"
)
RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_activity_scopes_turn_unique"
)
RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_SESSION_THREAD_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_activity_scopes_session_thread_id_unique"
)
RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_IMMUTABLE_TRIGGER: Final = (
    "rodex_sessions_codex_activity_scopes_immutable"
)
RODEX_SESSIONS_CODEX_TURN_STATES_TABLE: Final = "rodex_sessions_codex_turn_states"
RODEX_SESSIONS_CODEX_TURN_STATES_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_turn_states_turn_unique"
)
RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE: Final = (
    "rodex_sessions_statistics_turn_metrics"
)
RODEX_SESSIONS_STATISTICS_TURN_METRICS_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turn_metrics_turn_unique"
)
RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE: Final = "rodex_sessions_subagent_spawns"
RODEX_SESSIONS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_subagent_spawns_source_unique"
)
RODEX_SESSIONS_SUBAGENT_SPAWNS_TURN_INDEX: Final = "rodex_sessions_subagent_spawns_turn"
RODEX_SESSIONS_SUBAGENT_SPAWNS_PARENT_INDEX: Final = "rodex_sessions_subagent_spawns_parent"
RODEX_CURRENT_CODEX_THREAD_REJECT_SPAWN_INSERT_TRIGGER: Final = (
    "rodex_current_codex_thread_reject_spawn_insert"
)
RODEX_CURRENT_CODEX_THREAD_REJECT_SPAWN_UPDATE_TRIGGER: Final = (
    "rodex_current_codex_thread_reject_spawn_update"
)
RODEX_SUBAGENT_SPAWN_REJECT_CURRENT_INSERT_TRIGGER: Final = (
    "rodex_subagent_spawn_reject_current_insert"
)
RODEX_SUBAGENT_SPAWN_REJECT_CURRENT_UPDATE_TRIGGER: Final = (
    "rodex_subagent_spawn_reject_current_update"
)
RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE: Final = (
    "rodex_sessions_statistics_turn_named_counts"
)
RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turn_named_counts_key_unique"
)
RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX: Final = (
    "rodex_sessions_statistics_turn_named_counts_session_kind"
)
RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE: Final = "rodex_sessions_analytics_workers"
RODEX_SESSIONS_ANALYTICS_WORKERS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_analytics_workers_rodex_sessions_id_unique"
)
RODEX_SESSIONS_ANALYTICS_WORKERS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_analytics_workers_session_id_unique"
)
RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE: Final = (
    "rodex_sessions_analytics_worker_thread_checkpoints"
)
RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_UNIQUE_INDEX: Final = (
    "rodex_sessions_analytics_worker_thread_checkpoints_unique"
)
RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE: Final = (
    "rodex_sessions_agent_trace_publications"
)
RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_publications_session_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE: Final = "rodex_sessions_agent_trace_events"
RODEX_SESSIONS_AGENT_TRACE_EVENTS_SOURCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_events_source_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ORDER_INDEX: Final = (
    "rodex_sessions_agent_trace_events_session_order"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_ID_KIND_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_events_id_kind_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ID_KIND_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_events_session_id_kind_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_events_session_id_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_PUBLIC_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_events_public_id_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_SCOPE_ID_KIND_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_events_session_scope_id_kind_unique"
)
RODEX_SESSIONS_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER: Final = (
    "rodex_sessions_agent_trace_events_immutable"
)
RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE: Final = "rodex_sessions_agent_trace_messages"
RODEX_SESSIONS_CODEX_ITEMS_TABLE: Final = "rodex_sessions_codex_items"
RODEX_SESSIONS_CODEX_ITEMS_PUBLIC_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_items_public_id_unique"
)
RODEX_SESSIONS_CODEX_ITEMS_IDENTITY_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_items_identity_unique"
)
RODEX_SESSIONS_CODEX_ITEMS_SESSION_THREAD_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_items_session_thread_id_unique"
)
RODEX_SESSIONS_CODEX_ITEMS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_items_session_id_unique"
)
RODEX_SESSIONS_CODEX_ITEMS_SESSION_SCOPE_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_items_session_scope_id_unique"
)
RODEX_SESSIONS_CODEX_ITEMS_IMMUTABLE_TRIGGER: Final = "rodex_sessions_codex_items_immutable"
RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE: Final = "rodex_sessions_codex_item_aliases"
RODEX_SESSIONS_CODEX_ITEM_ALIASES_IDENTITY_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_item_aliases_identity_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE: Final = "rodex_sessions_codex_tool_calls"
RODEX_SESSIONS_CODEX_TOOL_CALLS_PUBLIC_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_tool_calls_public_id_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALLS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_tool_calls_session_id_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_tool_calls_session_scope_id_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER: Final = (
    "rodex_sessions_codex_tool_calls_identity_immutable"
)
RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE: Final = (
    "rodex_sessions_codex_tool_call_aliases"
)
RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_CALL_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_tool_call_aliases_call_id_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_ITEM_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_tool_call_aliases_item_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_codex_tool_call_aliases_event_unique"
)
RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_CALL_INDEX: Final = (
    "rodex_sessions_codex_tool_call_aliases_call"
)
RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE: Final = (
    "rodex_sessions_agent_trace_tool_call_activities"
)
RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE: Final = (
    "rodex_sessions_agent_trace_command_executions"
)
RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE: Final = "rodex_sessions_agent_trace_contexts"
RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE: Final = (
    "rodex_sessions_agent_trace_token_usage"
)
RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE: Final = (
    "rodex_sessions_agent_trace_rate_limit_windows"
)
RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE: Final = (
    "rodex_sessions_agent_trace_subagent_activities"
)
RODEX_SESSIONS_AGENT_TRACE_MESSAGES_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_messages_event_unique"
)
RODEX_SESSIONS_AGENT_TRACE_MESSAGES_SESSION_SCOPE_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_messages_session_scope_id_unique"
)
RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_tool_call_activities_event_unique"
)
RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_tool_call_activities_session_scope_id_unique"
)
RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_CALL_INDEX: Final = (
    "rodex_sessions_agent_trace_tool_call_activities_call"
)
RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_command_executions_event_unique"
)
RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_contexts_event_unique"
)
RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_token_usage_event_unique"
)
RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_EVENT_ORDINAL_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_rate_limit_windows_event_ordinal_unique"
)
RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_EVENT_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_subagent_activities_event_unique"
)
RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_SESSION_SCOPE_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_trace_subagent_activities_session_scope_id_unique"
)
RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TARGET_INDEX: Final = (
    "rodex_sessions_agent_trace_subagent_activities_target"
)
RODEX_SESSIONS_AGENT_REQUESTS_TABLE: Final = "rodex_sessions_agent_requests"
RODEX_SESSIONS_AGENT_REQUESTS_PUBLIC_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_requests_public_id_unique"
)
RODEX_SESSIONS_AGENT_REQUESTS_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_requests_session_id_unique"
)
RODEX_SESSIONS_AGENT_REQUESTS_TOOL_ACTIVITY_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_requests_tool_activity_unique"
)
RODEX_SESSIONS_AGENT_REQUESTS_SUBAGENT_ACTIVITY_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_requests_subagent_activity_unique"
)
RODEX_SESSIONS_AGENT_REQUESTS_PARENT_MESSAGE_INDEX: Final = (
    "rodex_sessions_agent_requests_parent_message"
)
RODEX_SESSIONS_AGENT_REQUESTS_TARGET_ORDER_INDEX: Final = (
    "rodex_sessions_agent_requests_target_order"
)
RODEX_SESSIONS_AGENT_REQUESTS_VALIDATE_INSERT_TRIGGER: Final = (
    "rodex_sessions_agent_requests_validate_insert"
)
RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE: Final = (
    "rodex_sessions_agent_request_target_turns"
)
RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_REQUEST_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_request_target_turns_request_unique"
)
RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_agent_request_target_turns_turn_unique"
)
RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_VALIDATE_INSERT_TRIGGER: Final = (
    "rodex_sessions_agent_request_target_turns_validate_insert"
)
RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX: Final = "rodex_sessions_cool_names_id_unique"
RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX: Final = (
    "rodex_sessions_user_defined_cool_names_id_unique"
)
STATISTICS_COVERAGE_STATES: Final = frozenset({"complete", "gapped"})
STATISTICS_TURN_OUTCOMES: Final = frozenset({"open", "completed", "aborted"})
STATISTICS_WORKER_STATES: Final = frozenset(
    {"starting", "catching_up", "up_to_date", "degraded", "stopped"}
)
_CREATE_SCHEMA_GENERATIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SCHEMA_GENERATIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_generation INTEGER NOT NULL CHECK (schema_generation >= 1),
    CHECK (id = 1)
)
"""
_CREATE_REGISTRIES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_REGISTRIES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_registry_id_signed_bigint BIGINT NOT NULL CHECK (
        typeof(rodex_registry_id_signed_bigint) = 'integer'
    ),
    CHECK (id = 1)
)
"""
_CREATE_REGISTRIES_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_REGISTRIES_ID_UNIQUE_INDEX}
ON {RODEX_REGISTRIES_TABLE} (rodex_registry_id_signed_bigint)
"""
_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_session_id_signed_bigint BIGINT NOT NULL CHECK (
        typeof(rodex_session_id_signed_bigint) = 'integer'
    ),
    cool_names_id INTEGER NOT NULL,
    user_defined_cool_names_id INTEGER DEFAULT NULL,
    FOREIGN KEY (cool_names_id) REFERENCES cool_names (id),
    FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id)
)
"""
_CREATE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (rodex_session_id_signed_bigint)
"""
_CREATE_SESSIONS_COOL_NAMES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (cool_names_id)
"""
_CREATE_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (user_defined_cool_names_id)
"""
_CREATE_USERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_USERS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    user_name TEXT NOT NULL
)
"""
_CREATE_USERS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_USERS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_USERS_TABLE} (uid, gid, user_name)
"""
_CREATE_LOG_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    rodex_sessions_users_id INTEGER NOT NULL,
    last_accessed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id),
    FOREIGN KEY (rodex_sessions_users_id) REFERENCES {RODEX_SESSIONS_USERS_TABLE} (id)
)
"""
_CREATE_LOG_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_LOG_TABLE} (rodex_sessions_id)
"""
_CREATE_TMUX_SESSIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_TMUX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    tmux_server_socket_path TEXT NOT NULL,
    tmux_session_name TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_TMUX_SESSIONS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX}
ON {RODEX_TMUX_SESSIONS_TABLE} (rodex_sessions_id)
"""
_CREATE_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX}
ON {RODEX_TMUX_SESSIONS_TABLE} (tmux_server_socket_path, tmux_session_name)
"""
_CREATE_RUNTIME_INSTANCES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_RUNTIME_INSTANCES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    runtime_id_signed_bigint BIGINT NOT NULL CHECK (
        typeof(runtime_id_signed_bigint) = 'integer'
    ),
    started_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX}
ON {RODEX_RUNTIME_INSTANCES_TABLE} (rodex_sessions_id)
"""
_CREATE_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX}
ON {RODEX_RUNTIME_INSTANCES_TABLE} (runtime_id_signed_bigint)
"""
_CREATE_MODEL_NAMES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {MODEL_NAMES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_of_the_model TEXT NOT NULL CHECK (length(trim(name_of_the_model)) > 0)
)
"""
_CREATE_MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX}
ON {MODEL_NAMES_TABLE} (name_of_the_model)
"""
_CREATE_REASONING_EFFORT_NAMES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {REASONING_EFFORT_NAMES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_of_the_reasoning_effort TEXT NOT NULL CHECK (
        length(trim(name_of_the_reasoning_effort)) > 0
    )
)
"""
_CREATE_REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX}
ON {REASONING_EFFORT_NAMES_TABLE} (name_of_the_reasoning_effort)
"""
_CREATE_TOOL_NAMES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TOOL_NAMES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL CHECK (length(trim(tool_name)) > 0)
)
"""
_CREATE_TOOL_NAMES_NAME_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {TOOL_NAMES_NAME_UNIQUE_INDEX}
ON {TOOL_NAMES_TABLE} (tool_name)
"""
_CREATE_STATISTICS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    statistics_publication_sequence INTEGER NOT NULL CHECK (
        statistics_publication_sequence >= 1
    ),
    statistics_projection_schema_version TEXT NOT NULL,
    calculated_at_utc TEXT NOT NULL,
    coverage_state TEXT NOT NULL CHECK (coverage_state IN ('complete', 'gapped')),
    analyzer_event_count INTEGER NOT NULL CHECK (analyzer_event_count >= 0),
    analyzer_source_count INTEGER NOT NULL CHECK (analyzer_source_count >= 0),
    history_sessions_count INTEGER NOT NULL CHECK (history_sessions_count >= 0),
    history_records_count INTEGER NOT NULL CHECK (history_records_count >= 0),
    history_malformed_records_count INTEGER NOT NULL CHECK (
        history_malformed_records_count >= 0
    ),
    turns_started_count INTEGER NOT NULL CHECK (turns_started_count >= 0),
    turns_completed_count INTEGER NOT NULL CHECK (turns_completed_count >= 0),
    turns_aborted_count INTEGER NOT NULL CHECK (turns_aborted_count >= 0),
    turns_open_count INTEGER NOT NULL CHECK (turns_open_count >= 0),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (cached_input_tokens >= 0),
    cache_write_input_tokens INTEGER NOT NULL CHECK (cache_write_input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_output_tokens INTEGER NOT NULL CHECK (reasoning_output_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    context_observation_count INTEGER NOT NULL CHECK (context_observation_count >= 0),
    context_latest_session_median_percent REAL DEFAULT NULL CHECK (
        context_latest_session_median_percent IS NULL
        OR context_latest_session_median_percent BETWEEN 0 AND 100
    ),
    context_high_water_percent REAL NOT NULL CHECK (
        context_high_water_percent BETWEEN 0 AND 100
    ),
    commands_executed_count INTEGER NOT NULL CHECK (commands_executed_count >= 0),
    model_tool_requests_count INTEGER NOT NULL CHECK (model_tool_requests_count >= 0),
    model_tool_outputs_paired_count INTEGER NOT NULL CHECK (
        model_tool_outputs_paired_count >= 0
        AND model_tool_outputs_paired_count <= model_tool_requests_count
    ),
    file_change_operations_count INTEGER NOT NULL CHECK (
        file_change_operations_count >= 0
    ),
    file_change_distinct_paths_count INTEGER NOT NULL CHECK (
        file_change_distinct_paths_count >= 0
    ),
    file_change_occurrences_count INTEGER NOT NULL CHECK (
        file_change_occurrences_count >= 0
    ),
    web_operations_count INTEGER NOT NULL CHECK (web_operations_count >= 0),
    web_queries_count INTEGER NOT NULL CHECK (web_queries_count >= 0),
    web_result_records_count INTEGER NOT NULL CHECK (web_result_records_count >= 0),
    web_distinct_result_or_action_urls_count INTEGER NOT NULL CHECK (
        web_distinct_result_or_action_urls_count >= 0
    ),
    compactions_count INTEGER NOT NULL CHECK (compactions_count >= 0),
    distinct_workspaces_count INTEGER NOT NULL CHECK (distinct_workspaces_count >= 0),
    typical_turns_count INTEGER NOT NULL CHECK (typical_turns_count >= 0),
    hands_on_turn_count INTEGER NOT NULL CHECK (
        hands_on_turn_count >= 0 AND hands_on_turn_count <= turns_started_count
    ),
    hands_on_turn_rate_percent REAL DEFAULT NULL CHECK (
        hands_on_turn_rate_percent IS NULL OR hands_on_turn_rate_percent BETWEEN 0 AND 100
    ),
    turns_with_nonzero_command_count INTEGER NOT NULL CHECK (
        turns_with_nonzero_command_count >= 0
    ),
    turns_subsequently_completed_count INTEGER NOT NULL CHECK (
        turns_subsequently_completed_count >= 0
        AND turns_subsequently_completed_count <= turns_with_nonzero_command_count
    ),
    completed_after_nonzero_command_percent REAL DEFAULT NULL CHECK (
        completed_after_nonzero_command_percent IS NULL
        OR completed_after_nonzero_command_percent BETWEEN 0 AND 100
    ),
    command_zero_exit_rate_percent REAL DEFAULT NULL CHECK (
        command_zero_exit_rate_percent IS NULL
        OR command_zero_exit_rate_percent BETWEEN 0 AND 100
    ),
    repeated_command_execution_count INTEGER NOT NULL CHECK (
        repeated_command_execution_count >= 0
        AND repeated_command_execution_count <= commands_executed_count
    ),
    exact_command_repeat_rate_percent REAL DEFAULT NULL CHECK (
        exact_command_repeat_rate_percent IS NULL
        OR exact_command_repeat_rate_percent BETWEEN 0 AND 100
    ),
    cached_input_share_percent REAL DEFAULT NULL CHECK (
        cached_input_share_percent IS NULL OR cached_input_share_percent BETWEEN 0 AND 100
    ),
    reasoning_output_share_percent REAL DEFAULT NULL CHECK (
        reasoning_output_share_percent IS NULL
        OR reasoning_output_share_percent BETWEEN 0 AND 100
    ),
    edited_turns_count INTEGER NOT NULL CHECK (edited_turns_count >= 0),
    verified_after_edit_count INTEGER NOT NULL CHECK (
        verified_after_edit_count >= 0 AND verified_after_edit_count <= edited_turns_count
    ),
    edit_then_verify_percent REAL DEFAULT NULL CHECK (
        edit_then_verify_percent IS NULL OR edit_then_verify_percent BETWEEN 0 AND 100
    ),
    web_turns_count INTEGER NOT NULL CHECK (web_turns_count >= 0),
    web_later_command_or_file_work_count INTEGER NOT NULL CHECK (
        web_later_command_or_file_work_count >= 0
        AND web_later_command_or_file_work_count <= web_turns_count
    ),
    web_follow_through_percent REAL DEFAULT NULL CHECK (
        web_follow_through_percent IS NULL OR web_follow_through_percent BETWEEN 0 AND 100
    ),
    revisited_distinct_path_count INTEGER NOT NULL CHECK (
        revisited_distinct_path_count >= 0
        AND revisited_distinct_path_count <= file_change_distinct_paths_count
    ),
    file_revisit_rate_percent REAL DEFAULT NULL CHECK (
        file_revisit_rate_percent IS NULL OR file_revisit_rate_percent BETWEEN 0 AND 100
    ),
    workspace_tagged_turn_count INTEGER NOT NULL CHECK (workspace_tagged_turn_count >= 0),
    turns_in_busiest_workspace_count INTEGER NOT NULL CHECK (
        turns_in_busiest_workspace_count >= 0
        AND turns_in_busiest_workspace_count <= workspace_tagged_turn_count
    ),
    busiest_workspace_turn_share_percent REAL DEFAULT NULL CHECK (
        busiest_workspace_turn_share_percent IS NULL
        OR busiest_workspace_turn_share_percent BETWEEN 0 AND 100
    ),
    turns_with_local_hour_count INTEGER NOT NULL CHECK (turns_with_local_hour_count >= 0),
    busiest_local_hour INTEGER DEFAULT NULL CHECK (
        busiest_local_hour IS NULL OR busiest_local_hour BETWEEN 0 AND 23
    ),
    turns_in_busiest_local_hour_count INTEGER NOT NULL CHECK (
        turns_in_busiest_local_hour_count >= 0
        AND turns_in_busiest_local_hour_count <= turns_with_local_hour_count
    ),
    goal_updates_count INTEGER NOT NULL CHECK (goal_updates_count >= 0),
    audit_privacy TEXT NOT NULL CHECK (length(audit_privacy) > 0),
    audit_percentile_method TEXT NOT NULL CHECK (length(audit_percentile_method) > 0),
    audit_token_method TEXT NOT NULL CHECK (length(audit_token_method) > 0),
    audit_token_snapshots_count INTEGER NOT NULL CHECK (audit_token_snapshots_count >= 0),
    audit_repeated_token_snapshots_count INTEGER NOT NULL CHECK (
        audit_repeated_token_snapshots_count >= 0
    ),
    audit_token_epochs_count INTEGER NOT NULL CHECK (audit_token_epochs_count >= 0),
    audit_duplicate_operations_ignored_count INTEGER NOT NULL CHECK (
        audit_duplicate_operations_ignored_count >= 0
    ),
    audit_duplicate_terminals_ignored_count INTEGER NOT NULL CHECK (
        audit_duplicate_terminals_ignored_count >= 0
    ),
    audit_terminal_events_without_start_ignored_count INTEGER NOT NULL CHECK (
        audit_terminal_events_without_start_ignored_count >= 0
    ),
    audit_new_event_type_warnings_count INTEGER NOT NULL CHECK (
        audit_new_event_type_warnings_count >= 0
    ),
    CHECK (
        turns_started_count = turns_completed_count + turns_aborted_count + turns_open_count
    ),
    CHECK (cached_input_tokens <= input_tokens),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_STATISTICS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TABLE} (rodex_sessions_id)
"""
_CREATE_STATISTICS_DISTRIBUTIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    distribution_kind TEXT NOT NULL CHECK (distribution_kind IN (
        'completed_turn_duration_ms', 'time_to_first_token_ms',
        'per_turn_total_tokens', 'command_duration_ms', 'commands_per_turn',
        'tool_requests_per_turn', 'files_per_turn'
    )),
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0),
    total INTEGER NOT NULL CHECK (total >= 0),
    median REAL DEFAULT NULL CHECK (median IS NULL OR median >= 0),
    p75 INTEGER DEFAULT NULL CHECK (p75 IS NULL OR p75 >= 0),
    p90 INTEGER DEFAULT NULL CHECK (p90 IS NULL OR p90 >= 0),
    p95 INTEGER DEFAULT NULL CHECK (p95 IS NULL OR p95 >= 0),
    maximum INTEGER DEFAULT NULL CHECK (maximum IS NULL OR maximum >= 0),
    CHECK (
        (observation_count = 0 AND total = 0 AND median IS NULL AND p75 IS NULL
            AND p90 IS NULL AND p95 IS NULL AND maximum IS NULL)
        OR
        (observation_count > 0 AND median IS NOT NULL AND p75 IS NOT NULL
            AND p90 IS NOT NULL AND p95 IS NOT NULL AND maximum IS NOT NULL
            AND p75 <= p90 AND p90 <= p95 AND p95 <= maximum)
    ),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE}
    (rodex_sessions_id, distribution_kind)
"""
_CREATE_STATISTICS_NAMED_COUNTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    count_kind TEXT NOT NULL CHECK (count_kind IN (
        'command_exit_status', 'command_family', 'model_tool', 'file_change_type',
        'web_action', 'goal_status'
    )),
    count_name TEXT NOT NULL CHECK (length(count_name) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE}
    (rodex_sessions_id, count_kind, count_name)
"""
_CREATE_STATISTICS_AUDIT_LIMITS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    limit_ordinal INTEGER NOT NULL CHECK (limit_ordinal >= 0),
    limitation TEXT NOT NULL CHECK (length(limitation) > 0),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE}
    (rodex_sessions_id, limit_ordinal)
"""
_CREATE_CANONICAL_CODEX_THREADS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {CODEX_THREADS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codex_thread_public_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(codex_thread_public_id_signed_bigint_1) = 'integer'
    ),
    codex_thread_public_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(codex_thread_public_id_signed_bigint_2) = 'integer'
    )
)
"""
_CREATE_CANONICAL_CODEX_THREADS_PUBLIC_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {CODEX_THREADS_PUBLIC_ID_UNIQUE_INDEX}
ON {CODEX_THREADS_TABLE}
    (codex_thread_public_id_signed_bigint_1,
        codex_thread_public_id_signed_bigint_2)
"""
_CREATE_CODEX_THREADS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_THREADS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    codex_threads_id INTEGER NOT NULL,
    first_linked_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id),
    FOREIGN KEY (codex_threads_id) REFERENCES {CODEX_THREADS_TABLE} (id)
)
"""
_CREATE_CURRENT_CODEX_THREADS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_CODEX_ROLLOUT_SOURCES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rollout_file_path TEXT NOT NULL CHECK (length(rollout_file_path) > 0),
    first_observed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_CODEX_ROLLOUT_SOURCES_THREAD_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_THREAD_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE}
    (rodex_sessions_codex_threads_id)
"""
_CREATE_CODEX_ROLLOUT_SOURCES_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} (rodex_sessions_id, id)
"""
_CREATE_CODEX_THREADS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_CODEX_THREADS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_THREADS_TABLE}
    (codex_threads_id)
"""
_CREATE_CODEX_THREADS_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_THREADS_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
"""
_CREATE_CURRENT_CODEX_THREADS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CURRENT_CODEX_THREADS_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} (rodex_sessions_id)
"""
_CREATE_CURRENT_CODEX_THREADS_MEMBERSHIP_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CURRENT_CODEX_THREADS_MEMBERSHIP_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE}
    (rodex_sessions_codex_threads_id)
"""
_CREATE_CODEX_TURNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_TURNS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    turn_public_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(turn_public_id_signed_bigint_1) = 'integer'
    ),
    turn_public_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(turn_public_id_signed_bigint_2) = 'integer'
    ),
    codex_turn_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(codex_turn_id_signed_bigint_1) = 'integer'
    ),
    codex_turn_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(codex_turn_id_signed_bigint_2) = 'integer'
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE}
            (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_CODEX_TURN_STATES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_turns_id INTEGER NOT NULL,
    started_at_utc TEXT DEFAULT NULL,
    terminal_at_utc TEXT DEFAULT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('open', 'completed', 'aborted')),
    model_names_id INTEGER DEFAULT NULL,
    reasoning_effort_names_id INTEGER DEFAULT NULL,
    CHECK (outcome != 'open' OR terminal_at_utc IS NULL),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_turns_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TURNS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (model_names_id) REFERENCES {MODEL_NAMES_TABLE} (id),
    FOREIGN KEY (reasoning_effort_names_id)
        REFERENCES {REASONING_EFFORT_NAMES_TABLE} (id)
)
"""
_CREATE_CODEX_TURN_STATES_TURN_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TURN_STATES_TURN_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} (rodex_sessions_codex_turns_id)
"""
_CREATE_STATISTICS_TURN_METRICS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_turns_id INTEGER NOT NULL,
    duration_ms INTEGER DEFAULT NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    time_to_first_token_ms INTEGER DEFAULT NULL CHECK (
        time_to_first_token_ms IS NULL OR time_to_first_token_ms >= 0
    ),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (
        cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
    ),
    cache_write_input_tokens INTEGER NOT NULL CHECK (cache_write_input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_output_tokens INTEGER NOT NULL CHECK (reasoning_output_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    context_observation_count INTEGER NOT NULL CHECK (context_observation_count >= 0),
    context_high_water_percent REAL NOT NULL CHECK (
        context_high_water_percent BETWEEN 0 AND 100
    ),
    commands_executed_count INTEGER NOT NULL CHECK (commands_executed_count >= 0),
    command_duration_observation_count INTEGER NOT NULL CHECK (
        command_duration_observation_count >= 0
    ),
    command_duration_total_ms INTEGER NOT NULL CHECK (command_duration_total_ms >= 0),
    command_duration_median_ms REAL DEFAULT NULL CHECK (
        command_duration_median_ms IS NULL OR command_duration_median_ms >= 0
    ),
    command_duration_p75_ms INTEGER DEFAULT NULL CHECK (
        command_duration_p75_ms IS NULL OR command_duration_p75_ms >= 0
    ),
    command_duration_p90_ms INTEGER DEFAULT NULL CHECK (
        command_duration_p90_ms IS NULL OR command_duration_p90_ms >= 0
    ),
    command_duration_p95_ms INTEGER DEFAULT NULL CHECK (
        command_duration_p95_ms IS NULL OR command_duration_p95_ms >= 0
    ),
    command_duration_maximum_ms INTEGER DEFAULT NULL CHECK (
        command_duration_maximum_ms IS NULL OR command_duration_maximum_ms >= 0
    ),
    model_tool_requests_count INTEGER NOT NULL CHECK (model_tool_requests_count >= 0),
    model_tool_outputs_paired_count INTEGER NOT NULL CHECK (
        model_tool_outputs_paired_count >= 0
        AND model_tool_outputs_paired_count <= model_tool_requests_count
    ),
    file_change_operations_count INTEGER NOT NULL CHECK (
        file_change_operations_count >= 0
    ),
    file_change_distinct_paths_count INTEGER NOT NULL CHECK (
        file_change_distinct_paths_count >= 0
    ),
    file_change_occurrences_count INTEGER NOT NULL CHECK (
        file_change_occurrences_count >= 0
    ),
    web_operations_count INTEGER NOT NULL CHECK (web_operations_count >= 0),
    web_queries_count INTEGER NOT NULL CHECK (web_queries_count >= 0),
    web_result_records_count INTEGER NOT NULL CHECK (web_result_records_count >= 0),
    web_distinct_result_or_action_urls_count INTEGER NOT NULL CHECK (
        web_distinct_result_or_action_urls_count >= 0
    ),
    compactions_count INTEGER NOT NULL CHECK (compactions_count >= 0),
    workspace_digest TEXT DEFAULT NULL,
    local_start_hour INTEGER DEFAULT NULL CHECK (
        local_start_hour IS NULL OR local_start_hour BETWEEN 0 AND 23
    ),
    hands_on INTEGER NOT NULL CHECK (hands_on IN (0, 1)),
    completed_after_nonzero_command INTEGER NOT NULL CHECK (
        completed_after_nonzero_command IN (0, 1)
    ),
    cached_input_share_percent REAL DEFAULT NULL CHECK (
        cached_input_share_percent IS NULL OR cached_input_share_percent BETWEEN 0 AND 100
    ),
    reasoning_output_share_percent REAL DEFAULT NULL CHECK (
        reasoning_output_share_percent IS NULL
        OR reasoning_output_share_percent BETWEEN 0 AND 100
    ),
    edited_then_verified INTEGER NOT NULL CHECK (edited_then_verified IN (0, 1)),
    web_research_followed_by_command_or_file_work INTEGER NOT NULL CHECK (
        web_research_followed_by_command_or_file_work IN (0, 1)
    ),
    goal_updates_count INTEGER NOT NULL CHECK (goal_updates_count >= 0),
    CHECK (
        (command_duration_observation_count = 0 AND command_duration_total_ms = 0
            AND command_duration_median_ms IS NULL AND command_duration_p75_ms IS NULL
            AND command_duration_p90_ms IS NULL AND command_duration_p95_ms IS NULL
            AND command_duration_maximum_ms IS NULL)
        OR
        (command_duration_observation_count > 0
            AND command_duration_median_ms IS NOT NULL
            AND command_duration_p75_ms IS NOT NULL
            AND command_duration_p90_ms IS NOT NULL
            AND command_duration_p95_ms IS NOT NULL
            AND command_duration_maximum_ms IS NOT NULL
            AND command_duration_p75_ms <= command_duration_p90_ms
            AND command_duration_p90_ms <= command_duration_p95_ms
            AND command_duration_p95_ms <= command_duration_maximum_ms)
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_turns_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TURNS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_TURN_METRICS_TURN_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TURN_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE}
    (rodex_sessions_codex_turns_id)
"""
_CREATE_CODEX_TURNS_SOURCE_TURN_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TURNS_SOURCE_TURN_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TURNS_TABLE} (
    rodex_sessions_codex_threads_id,
    codex_turn_id_signed_bigint_1,
    codex_turn_id_signed_bigint_2
)
"""
_CREATE_CODEX_TURNS_SESSION_TURN_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_CODEX_TURNS_SESSION_TURN_INDEX}
ON {RODEX_SESSIONS_CODEX_TURNS_TABLE} (
    rodex_sessions_id,
    codex_turn_id_signed_bigint_1,
    codex_turn_id_signed_bigint_2
)
"""
_CREATE_CODEX_TURNS_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TURNS_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TURNS_TABLE}
    (rodex_sessions_id, id)
"""
_CREATE_CODEX_TURNS_SOURCE_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TURNS_SOURCE_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TURNS_TABLE}
    (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
"""
_CREATE_CODEX_TURNS_PUBLIC_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TURNS_PUBLIC_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TURNS_TABLE}
    (turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2)
"""
_CREATE_CODEX_ACTIVITY_SCOPES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rodex_sessions_codex_turns_id INTEGER DEFAULT NULL,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id,
        rodex_sessions_codex_turns_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TURNS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_CODEX_ACTIVITY_SCOPES_WITHOUT_TURN_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_WITHOUT_TURN_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
    (rodex_sessions_codex_threads_id)
WHERE rodex_sessions_codex_turns_id IS NULL
"""
_CREATE_CODEX_ACTIVITY_SCOPES_TURN_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TURN_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
    (rodex_sessions_codex_turns_id)
WHERE rodex_sessions_codex_turns_id IS NOT NULL
"""
_CREATE_CODEX_ACTIVITY_SCOPES_SESSION_THREAD_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_SESSION_THREAD_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
    (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
"""
_CREATE_CODEX_ACTIVITY_SCOPES_IMMUTABLE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_IMMUTABLE_TRIGGER}
BEFORE UPDATE ON {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'Codex activity scope identity is immutable');
END
"""
_CREATE_SUBAGENT_SPAWNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    subagent_rodex_sessions_codex_threads_id INTEGER NOT NULL,
    parent_rodex_sessions_codex_threads_id INTEGER NOT NULL,
    spawning_rodex_sessions_codex_turns_id INTEGER NOT NULL,
    agent_path TEXT NOT NULL CHECK (length(agent_path) > 0),
    agent_nickname TEXT DEFAULT NULL CHECK (
        agent_nickname IS NULL OR length(agent_nickname) > 0
    ),
    history_inheritance_kind TEXT NOT NULL CHECK (
        history_inheritance_kind IN ('clean', 'inherited')
    ),
    inherited_history_start_ordinal INTEGER DEFAULT NULL,
    CHECK (
        (history_inheritance_kind = 'clean'
            AND inherited_history_start_ordinal IS NULL)
        OR
        (history_inheritance_kind = 'inherited'
            AND inherited_history_start_ordinal >= 0)
    ),
    FOREIGN KEY (rodex_sessions_id, subagent_rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE}
            (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, parent_rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE}
            (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id,
        parent_rodex_sessions_codex_threads_id,
        spawning_rodex_sessions_codex_turns_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TURNS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        subagent_rodex_sessions_codex_threads_id
        != parent_rodex_sessions_codex_threads_id
    )
)
"""
_CREATE_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
    (subagent_rodex_sessions_codex_threads_id)
"""
_CREATE_SUBAGENT_SPAWNS_TURN_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_SUBAGENT_SPAWNS_TURN_INDEX}
ON {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
    (spawning_rodex_sessions_codex_turns_id)
"""
_CREATE_SUBAGENT_SPAWNS_PARENT_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_SUBAGENT_SPAWNS_PARENT_INDEX}
ON {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
    (parent_rodex_sessions_codex_threads_id)
"""
_CREATE_CURRENT_CODEX_THREAD_REJECT_SPAWN_INSERT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_CURRENT_CODEX_THREAD_REJECT_SPAWN_INSERT_TRIGGER}
BEFORE INSERT ON {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE}
WHEN EXISTS (
    SELECT 1 FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
    WHERE subagent_rodex_sessions_codex_threads_id =
        NEW.rodex_sessions_codex_threads_id
)
BEGIN
    SELECT RAISE(ABORT, 'current Codex thread cannot be a subagent spawn');
END
"""
_CREATE_CURRENT_CODEX_THREAD_REJECT_SPAWN_UPDATE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_CURRENT_CODEX_THREAD_REJECT_SPAWN_UPDATE_TRIGGER}
BEFORE UPDATE OF rodex_sessions_codex_threads_id
ON {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE}
WHEN EXISTS (
    SELECT 1 FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
    WHERE subagent_rodex_sessions_codex_threads_id =
        NEW.rodex_sessions_codex_threads_id
)
BEGIN
    SELECT RAISE(ABORT, 'current Codex thread cannot be a subagent spawn');
END
"""
_CREATE_SUBAGENT_SPAWN_REJECT_CURRENT_INSERT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_SUBAGENT_SPAWN_REJECT_CURRENT_INSERT_TRIGGER}
BEFORE INSERT ON {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
WHEN EXISTS (
    SELECT 1 FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE}
    WHERE rodex_sessions_codex_threads_id =
        NEW.subagent_rodex_sessions_codex_threads_id
)
BEGIN
    SELECT RAISE(ABORT, 'current Codex thread cannot be a subagent spawn');
END
"""
_CREATE_SUBAGENT_SPAWN_REJECT_CURRENT_UPDATE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_SUBAGENT_SPAWN_REJECT_CURRENT_UPDATE_TRIGGER}
BEFORE UPDATE OF subagent_rodex_sessions_codex_threads_id
ON {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE}
WHEN EXISTS (
    SELECT 1 FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE}
    WHERE rodex_sessions_codex_threads_id =
        NEW.subagent_rodex_sessions_codex_threads_id
)
BEGIN
    SELECT RAISE(ABORT, 'current Codex thread cannot be a subagent spawn');
END
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_turns_id INTEGER NOT NULL,
    count_kind TEXT NOT NULL CHECK (count_kind IN (
        'command_exit_status', 'command_family', 'model_tool', 'file_change_type',
        'web_action', 'goal_status'
    )),
    count_name TEXT NOT NULL CHECK (length(count_name) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_turns_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TURNS_TABLE}
            (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE}
    (rodex_sessions_codex_turns_id, count_kind, count_name)
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX = f"""
CREATE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE}
    (rodex_sessions_id, count_kind, count_name)
"""
_CREATE_ANALYTICS_WORKERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    worker_state TEXT NOT NULL CHECK (
        worker_state IN ('starting', 'catching_up', 'up_to_date', 'degraded', 'stopped')
    ),
    diagnostic_code TEXT DEFAULT NULL,
    last_attempted_at_utc TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    next_retry_at_utc TEXT DEFAULT NULL,
    CHECK (
        worker_state != 'up_to_date'
        OR (diagnostic_code IS NULL AND consecutive_failures = 0
            AND next_retry_at_utc IS NULL)
    ),
    CHECK (
        diagnostic_code IS NULL
        OR (length(diagnostic_code) BETWEEN 1 AND 64
            AND diagnostic_code NOT GLOB '*[^a-z0-9_]*')
    ),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_ANALYTICS_WORKERS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_ANALYTICS_WORKERS_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} (rodex_sessions_id)
"""
_CREATE_ANALYTICS_WORKERS_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_ANALYTICS_WORKERS_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} (rodex_sessions_id, id)
"""
_CREATE_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS
    {RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_analytics_workers_id INTEGER NOT NULL,
    rodex_sessions_codex_rollout_sources_id INTEGER NOT NULL,
    analyzed_size_bytes INTEGER NOT NULL CHECK (analyzed_size_bytes >= 0),
    analyzed_mtime_ns INTEGER NOT NULL CHECK (analyzed_mtime_ns >= 0),
    analyzed_prefix_sha256 TEXT NOT NULL CHECK (
        length(analyzed_prefix_sha256) = 64
        AND analyzed_prefix_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    verified_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_analytics_workers_id)
        REFERENCES {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_rollout_sources_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_ANALYTICS_WORKER_THREAD_CHECKPOINTS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE}
    (rodex_sessions_id, rodex_sessions_analytics_workers_id,
        rodex_sessions_codex_rollout_sources_id)
"""
_CREATE_AGENT_TRACE_PUBLICATIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    trace_publication_sequence INTEGER NOT NULL CHECK (trace_publication_sequence >= 1),
    trace_schema_version TEXT NOT NULL CHECK (length(trace_schema_version) > 0),
    calculated_at_utc TEXT NOT NULL,
    coverage_state TEXT NOT NULL CHECK (coverage_state IN ('complete', 'gapped')),
    durable_event_count INTEGER NOT NULL CHECK (durable_event_count >= 0),
    unrecognized_record_count INTEGER NOT NULL CHECK (unrecognized_record_count >= 0),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_AGENT_TRACE_PUBLICATIONS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} (rodex_sessions_id)
"""
_CREATE_AGENT_TRACE_EVENTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_event_public_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(trace_event_public_id_signed_bigint_1) = 'integer'
    ),
    trace_event_public_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(trace_event_public_id_signed_bigint_2) = 'integer'
    ),
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    source_record_ordinal INTEGER NOT NULL CHECK (source_record_ordinal >= 0),
    derived_event_ordinal INTEGER NOT NULL CHECK (derived_event_ordinal >= 0),
    first_trace_publication_sequence INTEGER NOT NULL CHECK (
        first_trace_publication_sequence >= 1
    ),
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'session_metadata', 'turn_context', 'turn_started', 'turn_completed',
        'turn_aborted',
        'message', 'tool_call', 'command_execution', 'subagent_activity',
        'token_usage', 'rate_limit', 'compaction', 'unrecognized_record'
    )),
    event_time_utc TEXT DEFAULT NULL,
    detail_sha256 TEXT NOT NULL CHECK (
        length(detail_sha256) = 64 AND detail_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id,
        rodex_sessions_codex_activity_scopes_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_AGENT_TRACE_EVENTS_SOURCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_EVENTS_SOURCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
    (rodex_sessions_codex_threads_id, source_record_ordinal, derived_event_ordinal)
"""
_CREATE_AGENT_TRACE_EVENTS_SESSION_ORDER_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ORDER_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
    (rodex_sessions_id, event_time_utc, id)
"""
_CREATE_AGENT_TRACE_EVENTS_ID_KIND_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_EVENTS_ID_KIND_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (id, event_kind)
"""
_CREATE_AGENT_TRACE_EVENTS_SESSION_ID_KIND_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ID_KIND_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (rodex_sessions_id, id, event_kind)
"""
_CREATE_AGENT_TRACE_EVENTS_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (rodex_sessions_id, id)
"""
_CREATE_AGENT_TRACE_EVENTS_PUBLIC_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_AGENT_TRACE_EVENTS_PUBLIC_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
    (trace_event_public_id_signed_bigint_1, trace_event_public_id_signed_bigint_2)
"""
_CREATE_AGENT_TRACE_EVENTS_SESSION_SCOPE_ID_KIND_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_SCOPE_ID_KIND_UNIQUE_INDEX}
ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
    (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id, event_kind)
"""
_CREATE_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER}
BEFORE UPDATE ON {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'agent trace event provenance is immutable');
END
"""
_CREATE_AGENT_TRACE_MESSAGES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'message' CHECK (event_kind = 'message'),
    rodex_sessions_codex_items_id INTEGER DEFAULT NULL,
    message_phase TEXT NOT NULL CHECK (message_phase IN (
        'commentary', 'final_answer', 'analysis', 'unknown'
    )),
    message_role TEXT NOT NULL CHECK (message_role IN (
        'assistant', 'user', 'system', 'unknown'
    )),
    content_block_count INTEGER NOT NULL CHECK (content_block_count >= 0),
    body_utf8_bytes INTEGER NOT NULL CHECK (body_utf8_bytes >= 0),
    body_capture_state TEXT NOT NULL CHECK (body_capture_state IN (
        'rollout_reference', 'encrypted', 'redacted', 'unavailable'
    )),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
                id, event_kind),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_items_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id)
)
"""
_CREATE_CODEX_ITEMS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_ITEMS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    item_public_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(item_public_id_signed_bigint_1) = 'integer'
    ),
    item_public_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(item_public_id_signed_bigint_2) = 'integer'
    ),
    codex_item_id_signed_bigint_1 BIGINT DEFAULT NULL CHECK (
        codex_item_id_signed_bigint_1 IS NULL
        OR typeof(codex_item_id_signed_bigint_1) = 'integer'
    ),
    codex_item_id_signed_bigint_2 BIGINT DEFAULT NULL CHECK (
        codex_item_id_signed_bigint_2 IS NULL
        OR typeof(codex_item_id_signed_bigint_2) = 'integer'
    ),
    CHECK (
        (codex_item_id_signed_bigint_1 IS NULL
            AND codex_item_id_signed_bigint_2 IS NULL)
        OR (codex_item_id_signed_bigint_1 IS NOT NULL
            AND codex_item_id_signed_bigint_2 IS NOT NULL)
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id,
        rodex_sessions_codex_activity_scopes_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_CODEX_ITEMS_PUBLIC_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ITEMS_PUBLIC_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
    (item_public_id_signed_bigint_1, item_public_id_signed_bigint_2)
"""
_CREATE_CODEX_ITEMS_IDENTITY_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ITEMS_IDENTITY_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
    (rodex_sessions_codex_threads_id, codex_item_id_signed_bigint_1,
        codex_item_id_signed_bigint_2)
WHERE codex_item_id_signed_bigint_1 IS NOT NULL
"""
_CREATE_CODEX_ITEMS_SESSION_THREAD_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ITEMS_SESSION_THREAD_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
    (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
"""
_CREATE_CODEX_ITEMS_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ITEMS_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ITEMS_TABLE} (rodex_sessions_id, id)
"""
_CREATE_CODEX_ITEMS_SESSION_SCOPE_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ITEMS_SESSION_SCOPE_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
    (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id)
"""
_CREATE_CODEX_ITEMS_IMMUTABLE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_SESSIONS_CODEX_ITEMS_IMMUTABLE_TRIGGER}
BEFORE UPDATE ON {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
BEGIN
    SELECT RAISE(ABORT, 'Codex item identity is immutable');
END
"""
_CREATE_CODEX_ITEM_ALIASES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    rodex_sessions_codex_items_id INTEGER NOT NULL,
    codex_item_alias_sha256_int_1 BIGINT NOT NULL CHECK (
        typeof(codex_item_alias_sha256_int_1) = 'integer'
    ),
    codex_item_alias_sha256_int_2 BIGINT NOT NULL CHECK (
        typeof(codex_item_alias_sha256_int_2) = 'integer'
    ),
    codex_item_alias_sha256_int_3 BIGINT NOT NULL CHECK (
        typeof(codex_item_alias_sha256_int_3) = 'integer'
    ),
    codex_item_alias_sha256_int_4 BIGINT NOT NULL CHECK (
        typeof(codex_item_alias_sha256_int_4) = 'integer'
    ),
    codex_item_alias TEXT NOT NULL CHECK (length(codex_item_alias) > 0),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id,
        rodex_sessions_codex_items_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_items_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id)
)
"""
_CREATE_CODEX_ITEM_ALIASES_IDENTITY_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_ITEM_ALIASES_IDENTITY_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE}
    (rodex_sessions_codex_threads_id, codex_item_alias_sha256_int_1,
        codex_item_alias_sha256_int_2, codex_item_alias_sha256_int_3,
        codex_item_alias_sha256_int_4)
"""
_CREATE_CODEX_TOOL_CALLS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    tool_names_id INTEGER DEFAULT NULL,
    tool_call_public_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(tool_call_public_id_signed_bigint_1) = 'integer'
    ),
    tool_call_public_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(tool_call_public_id_signed_bigint_2) = 'integer'
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id)
        REFERENCES {RODEX_SESSIONS_CODEX_THREADS_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id,
        rodex_sessions_codex_activity_scopes_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tool_names_id) REFERENCES {TOOL_NAMES_TABLE} (id)
)
"""
_CREATE_CODEX_TOOL_CALLS_PUBLIC_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALLS_PUBLIC_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE}
    (tool_call_public_id_signed_bigint_1, tool_call_public_id_signed_bigint_2)
"""
_CREATE_CODEX_TOOL_CALLS_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALLS_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} (rodex_sessions_id, id)
"""
_CREATE_CODEX_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE}
    (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id)
"""
_CREATE_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER}
BEFORE UPDATE ON {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE}
WHEN NOT (
    OLD.tool_names_id IS NULL
    AND NEW.tool_names_id IS NOT NULL
    AND NEW.id = OLD.id
    AND NEW.rodex_sessions_id = OLD.rodex_sessions_id
    AND NEW.rodex_sessions_codex_threads_id =
        OLD.rodex_sessions_codex_threads_id
    AND NEW.rodex_sessions_codex_activity_scopes_id =
        OLD.rodex_sessions_codex_activity_scopes_id
    AND NEW.tool_call_public_id_signed_bigint_1 =
        OLD.tool_call_public_id_signed_bigint_1
    AND NEW.tool_call_public_id_signed_bigint_2 =
        OLD.tool_call_public_id_signed_bigint_2
)
BEGIN
    SELECT RAISE(ABORT, 'Codex tool-call identity is immutable');
END
"""
_CREATE_CODEX_TOOL_CALL_ALIASES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_threads_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    rodex_sessions_codex_tool_calls_id INTEGER NOT NULL,
    alias_kind TEXT NOT NULL CHECK (
        alias_kind IN ('call_id', 'item_id', 'source_event')
    ),
    codex_call_id_sha256_int_1 BIGINT DEFAULT NULL CHECK (
        codex_call_id_sha256_int_1 IS NULL
        OR typeof(codex_call_id_sha256_int_1) = 'integer'
    ),
    codex_call_id_sha256_int_2 BIGINT DEFAULT NULL CHECK (
        codex_call_id_sha256_int_2 IS NULL
        OR typeof(codex_call_id_sha256_int_2) = 'integer'
    ),
    codex_call_id_sha256_int_3 BIGINT DEFAULT NULL CHECK (
        codex_call_id_sha256_int_3 IS NULL
        OR typeof(codex_call_id_sha256_int_3) = 'integer'
    ),
    codex_call_id_sha256_int_4 BIGINT DEFAULT NULL CHECK (
        codex_call_id_sha256_int_4 IS NULL
        OR typeof(codex_call_id_sha256_int_4) = 'integer'
    ),
    codex_call_id TEXT DEFAULT NULL,
    rodex_sessions_codex_items_id INTEGER DEFAULT NULL,
    rodex_sessions_agent_trace_events_id INTEGER DEFAULT NULL,
    source_event_kind TEXT DEFAULT NULL CHECK (
        source_event_kind IS NULL OR source_event_kind = 'tool_call'
    ),
    CHECK (
        (alias_kind = 'call_id' AND codex_call_id IS NOT NULL
            AND length(codex_call_id) > 0
            AND codex_call_id_sha256_int_1 IS NOT NULL
            AND codex_call_id_sha256_int_2 IS NOT NULL
            AND codex_call_id_sha256_int_3 IS NOT NULL
            AND codex_call_id_sha256_int_4 IS NOT NULL
            AND rodex_sessions_codex_items_id IS NULL
            AND rodex_sessions_agent_trace_events_id IS NULL
            AND source_event_kind IS NULL)
        OR
        (alias_kind = 'item_id' AND codex_call_id IS NULL
            AND codex_call_id_sha256_int_1 IS NULL
            AND codex_call_id_sha256_int_2 IS NULL
            AND codex_call_id_sha256_int_3 IS NULL
            AND codex_call_id_sha256_int_4 IS NULL
            AND rodex_sessions_codex_items_id IS NOT NULL
            AND rodex_sessions_agent_trace_events_id IS NULL
            AND source_event_kind IS NULL)
        OR
        (alias_kind = 'source_event' AND codex_call_id IS NULL
            AND codex_call_id_sha256_int_1 IS NULL
            AND codex_call_id_sha256_int_2 IS NULL
            AND codex_call_id_sha256_int_3 IS NULL
            AND codex_call_id_sha256_int_4 IS NULL
            AND rodex_sessions_codex_items_id IS NULL
            AND rodex_sessions_agent_trace_events_id IS NOT NULL
            AND source_event_kind = 'tool_call')
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_threads_id,
        rodex_sessions_codex_activity_scopes_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_threads_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_tool_calls_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_items_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_events_id, source_event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
                id, event_kind)
)
"""
_CREATE_CODEX_TOOL_CALL_ALIASES_CALL_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_CALL_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE}
    (rodex_sessions_codex_threads_id, codex_call_id_sha256_int_1,
        codex_call_id_sha256_int_2, codex_call_id_sha256_int_3,
        codex_call_id_sha256_int_4)
WHERE alias_kind = 'call_id'
"""
_CREATE_CODEX_TOOL_CALL_ALIASES_ITEM_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_ITEM_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE}
    (rodex_sessions_codex_items_id)
WHERE alias_kind = 'item_id'
"""
_CREATE_CODEX_TOOL_CALL_ALIASES_EVENT_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_EVENT_UNIQUE_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE}
    (rodex_sessions_agent_trace_events_id)
WHERE alias_kind = 'source_event'
"""
_CREATE_CODEX_TOOL_CALL_ALIASES_CALL_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_CALL_INDEX}
ON {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE}
    (rodex_sessions_codex_tool_calls_id)
"""
_CREATE_AGENT_TRACE_TOOL_CALLS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    rodex_sessions_codex_tool_calls_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'tool_call' CHECK (event_kind = 'tool_call'),
    activity_kind TEXT NOT NULL CHECK (
        activity_kind IN ('request', 'output', 'status')
    ),
    rodex_sessions_codex_items_id INTEGER DEFAULT NULL,
    tool_status TEXT DEFAULT NULL,
    request_utf8_bytes INTEGER NOT NULL CHECK (request_utf8_bytes >= 0),
    response_utf8_bytes INTEGER NOT NULL CHECK (response_utf8_bytes >= 0),
    payload_capture_state TEXT NOT NULL CHECK (payload_capture_state IN (
        'rollout_reference', 'encrypted', 'redacted', 'unavailable'
    )),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
                id, event_kind),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_tool_calls_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_items_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id)
)
"""
_CREATE_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'command_execution' CHECK (
        event_kind = 'command_execution'
    ),
    rodex_sessions_codex_items_id INTEGER DEFAULT NULL,
    command_argument_count INTEGER NOT NULL CHECK (command_argument_count >= 0),
    working_directory TEXT DEFAULT NULL,
    command_status TEXT DEFAULT NULL,
    duration_ms INTEGER DEFAULT NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    exit_code INTEGER DEFAULT NULL,
    stdout_utf8_bytes INTEGER NOT NULL CHECK (stdout_utf8_bytes >= 0),
    stderr_utf8_bytes INTEGER NOT NULL CHECK (stderr_utf8_bytes >= 0),
    aggregated_output_utf8_bytes INTEGER NOT NULL CHECK (
        aggregated_output_utf8_bytes >= 0
    ),
    payload_capture_state TEXT NOT NULL CHECK (payload_capture_state IN (
        'rollout_reference', 'encrypted', 'redacted', 'unavailable'
    )),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
                id, event_kind),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_items_id)
        REFERENCES {RODEX_SESSIONS_CODEX_ITEMS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id)
)
"""
_CREATE_AGENT_TRACE_CONTEXTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'turn_context' CHECK (event_kind = 'turn_context'),
    model_names_id INTEGER DEFAULT NULL,
    reasoning_effort_names_id INTEGER DEFAULT NULL,
    working_directory TEXT DEFAULT NULL,
    sandbox_mode TEXT DEFAULT NULL,
    approval_policy TEXT DEFAULT NULL,
    permission_profile_type TEXT DEFAULT NULL,
    workspace_root_count INTEGER NOT NULL CHECK (workspace_root_count >= 0),
    FOREIGN KEY (rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (id, event_kind),
    FOREIGN KEY (model_names_id) REFERENCES {MODEL_NAMES_TABLE} (id),
    FOREIGN KEY (reasoning_effort_names_id)
        REFERENCES {REASONING_EFFORT_NAMES_TABLE} (id)
)
"""
_CREATE_AGENT_TRACE_TOKEN_USAGE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'token_usage' CHECK (event_kind = 'token_usage'),
    input_tokens INTEGER DEFAULT NULL CHECK (input_tokens IS NULL OR input_tokens >= 0),
    cached_input_tokens INTEGER DEFAULT NULL CHECK (
        cached_input_tokens IS NULL OR cached_input_tokens >= 0
    ),
    output_tokens INTEGER DEFAULT NULL CHECK (output_tokens IS NULL OR output_tokens >= 0),
    reasoning_output_tokens INTEGER DEFAULT NULL CHECK (
        reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0
    ),
    total_tokens INTEGER DEFAULT NULL CHECK (total_tokens IS NULL OR total_tokens >= 0),
    context_used_percent REAL DEFAULT NULL CHECK (
        context_used_percent IS NULL OR context_used_percent BETWEEN 0 AND 100
    ),
    FOREIGN KEY (rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (id, event_kind)
)
"""
_CREATE_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'rate_limit' CHECK (event_kind = 'rate_limit'),
    window_ordinal INTEGER NOT NULL CHECK (window_ordinal >= 0),
    limit_id TEXT NOT NULL CHECK (length(limit_id) > 0),
    used_percent REAL DEFAULT NULL CHECK (
        used_percent IS NULL OR used_percent BETWEEN 0 AND 100
    ),
    window_minutes INTEGER DEFAULT NULL CHECK (
        window_minutes IS NULL OR window_minutes > 0
    ),
    resets_at_unix_seconds INTEGER DEFAULT NULL CHECK (
        resets_at_unix_seconds IS NULL OR resets_at_unix_seconds >= 0
    ),
    plan_type TEXT DEFAULT NULL,
    FOREIGN KEY (rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} (id, event_kind)
)
"""
_CREATE_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_agent_trace_events_id INTEGER NOT NULL,
    event_kind TEXT NOT NULL DEFAULT 'subagent_activity' CHECK (
        event_kind = 'subagent_activity'
    ),
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    target_codex_threads_id INTEGER DEFAULT NULL,
    rodex_sessions_codex_tool_calls_id INTEGER DEFAULT NULL,
    activity_kind TEXT NOT NULL CHECK (length(activity_kind) > 0),
    agent_path TEXT DEFAULT NULL,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_events_id, event_kind)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
                id, event_kind),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_codex_tool_calls_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (target_codex_threads_id) REFERENCES {CODEX_THREADS_TABLE} (id)
)
"""
_CREATE_AGENT_REQUESTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_request_public_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(agent_request_public_id_signed_bigint_1) = 'integer'
    ),
    agent_request_public_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(agent_request_public_id_signed_bigint_2) = 'integer'
    ),
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_codex_activity_scopes_id INTEGER NOT NULL,
    parent_rodex_sessions_agent_trace_messages_id INTEGER NOT NULL,
    rodex_sessions_agent_trace_tool_call_activities_id INTEGER NOT NULL,
    rodex_sessions_agent_trace_subagent_activities_id INTEGER NOT NULL,
    target_codex_threads_id INTEGER NOT NULL,
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        parent_rodex_sessions_agent_trace_messages_id)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_tool_call_activities_id)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id,
        rodex_sessions_agent_trace_subagent_activities_id)
        REFERENCES {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE}
            (rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, id),
    FOREIGN KEY (target_codex_threads_id) REFERENCES {CODEX_THREADS_TABLE} (id)
)
"""
_CREATE_AGENT_REQUESTS_VALIDATE_INSERT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {RODEX_SESSIONS_AGENT_REQUESTS_VALIDATE_INSERT_TRIGGER}
BEFORE INSERT ON {RODEX_SESSIONS_AGENT_REQUESTS_TABLE}
WHEN NOT EXISTS (
    SELECT 1
    FROM {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE} AS scope
    JOIN {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} AS parent_message
        ON parent_message.id =
            NEW.parent_rodex_sessions_agent_trace_messages_id
    JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS parent_event
        ON parent_event.id = parent_message.rodex_sessions_agent_trace_events_id
    JOIN {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} AS tool_activity
        ON tool_activity.id =
            NEW.rodex_sessions_agent_trace_tool_call_activities_id
    JOIN {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} AS tool_call
        ON tool_call.id = tool_activity.rodex_sessions_codex_tool_calls_id
    JOIN {TOOL_NAMES_TABLE} AS tool_name ON tool_name.id = tool_call.tool_names_id
    JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS tool_event
        ON tool_event.id = tool_activity.rodex_sessions_agent_trace_events_id
    JOIN {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} AS subagent_activity
        ON subagent_activity.id =
            NEW.rodex_sessions_agent_trace_subagent_activities_id
    JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS subagent_event
        ON subagent_event.id =
            subagent_activity.rodex_sessions_agent_trace_events_id
    WHERE scope.id = NEW.rodex_sessions_codex_activity_scopes_id
        AND scope.rodex_sessions_id = NEW.rodex_sessions_id
        AND scope.rodex_sessions_codex_turns_id IS NOT NULL
        AND parent_message.rodex_sessions_id = NEW.rodex_sessions_id
        AND parent_message.rodex_sessions_codex_activity_scopes_id = scope.id
        AND parent_message.message_role = 'user'
        AND parent_message.body_capture_state = 'rollout_reference'
        AND parent_message.rodex_sessions_codex_items_id IS NOT NULL
        AND tool_activity.rodex_sessions_id = NEW.rodex_sessions_id
        AND tool_activity.rodex_sessions_codex_activity_scopes_id = scope.id
        AND tool_activity.activity_kind = 'request'
        AND subagent_activity.rodex_sessions_id = NEW.rodex_sessions_id
        AND subagent_activity.rodex_sessions_codex_activity_scopes_id = scope.id
        AND subagent_activity.target_codex_threads_id =
            NEW.target_codex_threads_id
        AND subagent_activity.rodex_sessions_codex_tool_calls_id = tool_call.id
        AND (
            (tool_name.tool_name = 'collaboration.spawn_agent'
                AND subagent_activity.activity_kind = 'started')
            OR
            (tool_name.tool_name = 'collaboration.followup_task'
                AND subagent_activity.activity_kind = 'interacted')
        )
        AND (
            parent_event.source_record_ordinal <
                subagent_event.source_record_ordinal
            OR (
                parent_event.source_record_ordinal =
                    subagent_event.source_record_ordinal
                AND parent_event.derived_event_ordinal <
                    subagent_event.derived_event_ordinal
            )
        )
        AND (
            tool_event.source_record_ordinal <
                subagent_event.source_record_ordinal
            OR (
                tool_event.source_record_ordinal =
                    subagent_event.source_record_ordinal
                AND tool_event.derived_event_ordinal <
                    subagent_event.derived_event_ordinal
            )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} AS later_message
            JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS later_event
                ON later_event.id =
                    later_message.rodex_sessions_agent_trace_events_id
            WHERE later_message.rodex_sessions_id = NEW.rodex_sessions_id
                AND later_message.rodex_sessions_codex_activity_scopes_id =
                    scope.id
                AND later_message.message_role = 'user'
                AND (
                    later_event.source_record_ordinal >
                        parent_event.source_record_ordinal
                    OR (
                        later_event.source_record_ordinal =
                            parent_event.source_record_ordinal
                        AND later_event.derived_event_ordinal >
                            parent_event.derived_event_ordinal
                    )
                )
                AND (
                    later_event.source_record_ordinal <
                        subagent_event.source_record_ordinal
                    OR (
                        later_event.source_record_ordinal =
                            subagent_event.source_record_ordinal
                        AND later_event.derived_event_ordinal <
                            subagent_event.derived_event_ordinal
                    )
                )
        )
)
BEGIN
    SELECT RAISE(ABORT, 'agent request provenance is inconsistent');
END
"""
_CREATE_AGENT_REQUEST_TARGET_TURNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_agent_requests_id INTEGER NOT NULL,
    target_rodex_sessions_codex_turns_id INTEGER NOT NULL,
    association_kind TEXT NOT NULL DEFAULT 'next_observed_turn' CHECK (
        association_kind = 'next_observed_turn'
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_agent_requests_id)
        REFERENCES {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} (rodex_sessions_id, id),
    FOREIGN KEY (rodex_sessions_id, target_rodex_sessions_codex_turns_id)
        REFERENCES {RODEX_SESSIONS_CODEX_TURNS_TABLE} (rodex_sessions_id, id)
)
"""
_CREATE_AGENT_REQUEST_TARGET_TURNS_VALIDATE_INSERT_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS
    {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_VALIDATE_INSERT_TRIGGER}
BEFORE INSERT ON {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE}
WHEN NOT EXISTS (
    SELECT 1
    FROM {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} AS request
    JOIN {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} AS activity
        ON activity.id =
            request.rodex_sessions_agent_trace_subagent_activities_id
    JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS request_event
        ON request_event.id = activity.rodex_sessions_agent_trace_events_id
    JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS target_membership
        ON target_membership.rodex_sessions_id = request.rodex_sessions_id
        AND target_membership.codex_threads_id = request.target_codex_threads_id
    JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS target_turn
        ON target_turn.id = NEW.target_rodex_sessions_codex_turns_id
        AND target_turn.rodex_sessions_id = request.rodex_sessions_id
        AND target_turn.rodex_sessions_codex_threads_id = target_membership.id
    JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS target_state
        ON target_state.rodex_sessions_codex_turns_id = target_turn.id
    WHERE request.id = NEW.rodex_sessions_agent_requests_id
        AND request.rodex_sessions_id = NEW.rodex_sessions_id
        AND request_event.event_time_utc IS NOT NULL
        AND target_state.started_at_utc IS NOT NULL
        AND julianday(target_state.started_at_utc) >=
            julianday(request_event.event_time_utc)
)
BEGIN
    SELECT RAISE(ABORT, 'agent request target turn is inconsistent');
END
"""


def default_rodex_database_path() -> Path:
    """Resolve the current user's durable Rodex database path."""
    return _default_rodex_database_path()


def existing_rodex_database_path(
    database_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve an existing private registry without bootstrapping or repairing it."""
    return require_existing_rodex_database_path(
        normalise_rodex_database_path(database_path)
    )


def initialise_rodex_database(database_path: str | os.PathLike[str] | None = None) -> Path:
    """Create and verify the current Rodex schema in one transaction."""
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        _require_or_create_current_schema_generation(connection)
        connection.execute(_CREATE_REGISTRIES_TABLE)
        _verify_registries_table(connection)
        connection.execute(_CREATE_REGISTRIES_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_REGISTRIES_TABLE,
            RODEX_REGISTRIES_ID_UNIQUE_INDEX,
            ["rodex_registry_id_signed_bigint"],
        )
        registry_rows = connection.execute(
            f"SELECT id, rodex_registry_id_signed_bigint FROM {RODEX_REGISTRIES_TABLE}"
        ).fetchall()
        if not registry_rows:
            registry_id = RodexRegistryId.generate()
            connection.execute(
                f"INSERT INTO {RODEX_REGISTRIES_TABLE} "
                "(rodex_registry_id_signed_bigint) VALUES (?)",
                (registry_id.as_signed_bigint(),),
            )
        elif len(registry_rows) != 1 or registry_rows[0][0] != 1:
            raise RodexSessionError("Rodex registry must contain exactly its id=1 row")
        create_and_verify_cool_names_schema(connection)
        connection.execute(_CREATE_TABLE)
        _verify_sessions_table(connection)
        connection.execute(_CREATE_UNIQUE_INDEX)
        connection.execute(_CREATE_SESSIONS_COOL_NAMES_UNIQUE_INDEX)
        connection.execute(_CREATE_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX)
        _verify_sessions_unique_indexes(connection)
        connection.execute(_CREATE_USERS_TABLE)
        _verify_sessions_users_table(connection)
        connection.execute(_CREATE_USERS_UNIQUE_INDEX)
        _verify_sessions_users_unique_index(connection)
        connection.execute(_CREATE_LOG_TABLE)
        _verify_sessions_log_table(connection)
        connection.execute(_CREATE_LOG_SESSION_UNIQUE_INDEX)
        _verify_sessions_log_unique_index(connection)
        connection.execute(_CREATE_TMUX_SESSIONS_TABLE)
        _verify_tmux_sessions_table(connection)
        connection.execute(_CREATE_TMUX_SESSIONS_SESSION_UNIQUE_INDEX)
        connection.execute(_CREATE_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX)
        _verify_tmux_sessions_unique_indexes(connection)
        connection.execute(_CREATE_RUNTIME_INSTANCES_TABLE)
        _verify_runtime_instances_table(connection)
        connection.execute(_CREATE_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX)
        connection.execute(_CREATE_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_RUNTIME_INSTANCES_TABLE,
            RODEX_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        _verify_unique_index(
            connection,
            RODEX_RUNTIME_INSTANCES_TABLE,
            RODEX_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX,
            ["runtime_id_signed_bigint"],
        )
        connection.execute(_CREATE_MODEL_NAMES_TABLE)
        _verify_model_names_table(connection)
        connection.execute(_CREATE_MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            MODEL_NAMES_TABLE,
            MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX,
            ["name_of_the_model"],
        )
        _create_and_verify_append_only_triggers(
            connection,
            MODEL_NAMES_TABLE,
            "canonical model name is immutable",
        )
        connection.execute(_CREATE_REASONING_EFFORT_NAMES_TABLE)
        _verify_reasoning_effort_names_table(connection)
        connection.execute(
            _CREATE_REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX
        )
        _verify_unique_index(
            connection,
            REASONING_EFFORT_NAMES_TABLE,
            REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX,
            ["name_of_the_reasoning_effort"],
        )
        _create_and_verify_append_only_triggers(
            connection,
            REASONING_EFFORT_NAMES_TABLE,
            "canonical reasoning-effort name is immutable",
        )
        connection.execute(_CREATE_TOOL_NAMES_TABLE)
        _verify_lookup_name_table(connection, TOOL_NAMES_TABLE, "tool_name")
        connection.execute(_CREATE_TOOL_NAMES_NAME_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            TOOL_NAMES_TABLE,
            TOOL_NAMES_NAME_UNIQUE_INDEX,
            ["tool_name"],
        )
        _create_and_verify_append_only_triggers(
            connection,
            TOOL_NAMES_TABLE,
            "canonical tool name is immutable",
        )
        connection.execute(_CREATE_STATISTICS_TABLE)
        _verify_statistics_table(connection)
        connection.execute(_CREATE_STATISTICS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TABLE,
            RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        connection.execute(_CREATE_STATISTICS_DISTRIBUTIONS_TABLE)
        _verify_statistics_distributions_table(connection)
        connection.execute(_CREATE_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
            RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX,
            ["rodex_sessions_id", "distribution_kind"],
        )
        connection.execute(_CREATE_STATISTICS_NAMED_COUNTS_TABLE)
        _verify_statistics_named_counts_table(connection)
        connection.execute(_CREATE_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX,
            ["rodex_sessions_id", "count_kind", "count_name"],
        )
        connection.execute(_CREATE_STATISTICS_AUDIT_LIMITS_TABLE)
        _verify_statistics_audit_limits_table(connection)
        connection.execute(_CREATE_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
            RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX,
            ["rodex_sessions_id", "limit_ordinal"],
        )
        connection.execute(_CREATE_CANONICAL_CODEX_THREADS_TABLE)
        _verify_canonical_codex_threads_table(connection)
        connection.execute(_CREATE_CANONICAL_CODEX_THREADS_PUBLIC_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            CODEX_THREADS_TABLE,
            CODEX_THREADS_PUBLIC_ID_UNIQUE_INDEX,
            [
                "codex_thread_public_id_signed_bigint_1",
                "codex_thread_public_id_signed_bigint_2",
            ],
        )
        _create_and_verify_append_only_triggers(
            connection,
            CODEX_THREADS_TABLE,
            "canonical Codex thread identity is immutable",
        )
        connection.execute(_CREATE_CODEX_THREADS_TABLE)
        _verify_codex_threads_table(connection)
        connection.execute(_CREATE_CODEX_THREADS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_THREADS_TABLE,
            RODEX_SESSIONS_CODEX_THREADS_UNIQUE_INDEX,
            ["codex_threads_id"],
        )
        connection.execute(_CREATE_CODEX_THREADS_SESSION_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_THREADS_TABLE,
            RODEX_SESSIONS_CODEX_THREADS_SESSION_ID_UNIQUE_INDEX,
            ["rodex_sessions_id", "id"],
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_CODEX_THREADS_TABLE,
            "Codex thread membership is immutable",
        )
        connection.execute(_CREATE_CURRENT_CODEX_THREADS_TABLE)
        _verify_current_codex_threads_table(connection)
        connection.execute(_CREATE_CURRENT_CODEX_THREADS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
            RODEX_SESSIONS_CURRENT_CODEX_THREADS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        connection.execute(_CREATE_CURRENT_CODEX_THREADS_MEMBERSHIP_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
            RODEX_SESSIONS_CURRENT_CODEX_THREADS_MEMBERSHIP_UNIQUE_INDEX,
            ["rodex_sessions_codex_threads_id"],
        )
        connection.execute(_CREATE_CODEX_ROLLOUT_SOURCES_TABLE)
        _verify_codex_rollout_sources_table(connection)
        connection.execute(_CREATE_CODEX_ROLLOUT_SOURCES_THREAD_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
            RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_THREAD_UNIQUE_INDEX,
            ["rodex_sessions_codex_threads_id"],
        )
        connection.execute(_CREATE_CODEX_ROLLOUT_SOURCES_SESSION_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
            RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_SESSION_ID_UNIQUE_INDEX,
            ["rodex_sessions_id", "id"],
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
            "Codex rollout-source provenance is immutable",
        )
        connection.execute(_CREATE_CODEX_TURNS_TABLE)
        _verify_codex_turns_table(connection)
        connection.execute(_CREATE_CODEX_TURNS_SOURCE_TURN_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_TURNS_TABLE,
            RODEX_SESSIONS_CODEX_TURNS_SOURCE_TURN_UNIQUE_INDEX,
            [
                "rodex_sessions_codex_threads_id",
                "codex_turn_id_signed_bigint_1",
                "codex_turn_id_signed_bigint_2",
            ],
        )
        connection.execute(_CREATE_CODEX_TURNS_SESSION_TURN_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_CODEX_TURNS_TABLE,
            RODEX_SESSIONS_CODEX_TURNS_SESSION_TURN_INDEX,
            [
                "rodex_sessions_id",
                "codex_turn_id_signed_bigint_1",
                "codex_turn_id_signed_bigint_2",
            ],
            unique=False,
        )
        connection.execute(_CREATE_CODEX_TURNS_SESSION_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_TURNS_TABLE,
            RODEX_SESSIONS_CODEX_TURNS_SESSION_ID_UNIQUE_INDEX,
            ["rodex_sessions_id", "id"],
        )
        connection.execute(_CREATE_CODEX_TURNS_SOURCE_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_TURNS_TABLE,
            RODEX_SESSIONS_CODEX_TURNS_SOURCE_ID_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "rodex_sessions_codex_threads_id",
                "id",
            ],
        )
        connection.execute(_CREATE_CODEX_TURNS_PUBLIC_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_TURNS_TABLE,
            RODEX_SESSIONS_CODEX_TURNS_PUBLIC_ID_UNIQUE_INDEX,
            ["turn_public_id_signed_bigint_1", "turn_public_id_signed_bigint_2"],
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_CODEX_TURNS_TABLE,
            "canonical Codex turn identity is immutable",
        )
        connection.execute(_CREATE_CODEX_ACTIVITY_SCOPES_TABLE)
        _verify_codex_activity_scopes_table(connection)
        for statement, index_name, columns, predicate in (
            (
                _CREATE_CODEX_ACTIVITY_SCOPES_WITHOUT_TURN_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_WITHOUT_TURN_UNIQUE_INDEX,
                ["rodex_sessions_codex_threads_id"],
                "WHERE RODEX_SESSIONS_CODEX_TURNS_ID IS NULL",
            ),
            (
                _CREATE_CODEX_ACTIVITY_SCOPES_TURN_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TURN_UNIQUE_INDEX,
                ["rodex_sessions_codex_turns_id"],
                "WHERE RODEX_SESSIONS_CODEX_TURNS_ID IS NOT NULL",
            ),
            (
                _CREATE_CODEX_ACTIVITY_SCOPES_SESSION_THREAD_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_SESSION_THREAD_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_threads_id", "id"],
                None,
            ),
        ):
            connection.execute(statement)
            _verify_unique_index(
                connection,
                RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                index_name,
                columns,
            )
            if predicate is not None:
                _verify_schema_object_definition_contains(
                    connection, "index", index_name, (predicate,)
                )
        _create_and_verify_immutable_trigger(
            connection,
            _CREATE_CODEX_ACTIVITY_SCOPES_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
            "CODEX ACTIVITY SCOPE IDENTITY IS IMMUTABLE",
        )
        connection.execute(_CREATE_CODEX_TURN_STATES_TABLE)
        _verify_codex_turn_states_table(connection)
        connection.execute(_CREATE_CODEX_TURN_STATES_TURN_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
            RODEX_SESSIONS_CODEX_TURN_STATES_TURN_UNIQUE_INDEX,
            ["rodex_sessions_codex_turns_id"],
        )
        connection.execute(_CREATE_STATISTICS_TURN_METRICS_TABLE)
        _verify_statistics_turn_metrics_table(connection)
        connection.execute(_CREATE_STATISTICS_TURN_METRICS_TURN_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURN_METRICS_TURN_UNIQUE_INDEX,
            ["rodex_sessions_codex_turns_id"],
        )
        connection.execute(_CREATE_SUBAGENT_SPAWNS_TABLE)
        _verify_subagent_spawns_table(connection)
        connection.execute(_CREATE_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX,
            ["subagent_rodex_sessions_codex_threads_id"],
        )
        connection.execute(_CREATE_SUBAGENT_SPAWNS_TURN_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_TURN_INDEX,
            ["spawning_rodex_sessions_codex_turns_id"],
            unique=False,
        )
        connection.execute(_CREATE_SUBAGENT_SPAWNS_PARENT_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_PARENT_INDEX,
            ["parent_rodex_sessions_codex_threads_id"],
            unique=False,
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
            "subagent spawn provenance is immutable",
        )
        _create_and_verify_codex_root_role_exclusion(connection)
        connection.execute(_CREATE_STATISTICS_TURN_NAMED_COUNTS_TABLE)
        _verify_statistics_turn_named_counts_table(connection)
        connection.execute(_CREATE_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX,
            ["rodex_sessions_codex_turns_id", "count_kind", "count_name"],
        )
        connection.execute(_CREATE_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX,
            ["rodex_sessions_id", "count_kind", "count_name"],
            unique=False,
        )
        connection.execute(_CREATE_ANALYTICS_WORKERS_TABLE)
        _verify_analytics_workers_table(connection)
        connection.execute(_CREATE_ANALYTICS_WORKERS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
            RODEX_SESSIONS_ANALYTICS_WORKERS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        connection.execute(_CREATE_ANALYTICS_WORKERS_SESSION_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
            RODEX_SESSIONS_ANALYTICS_WORKERS_SESSION_ID_UNIQUE_INDEX,
            ["rodex_sessions_id", "id"],
        )
        connection.execute(_CREATE_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE)
        _verify_analytics_worker_thread_checkpoints_table(connection)
        connection.execute(_CREATE_ANALYTICS_WORKER_THREAD_CHECKPOINTS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
            RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "rodex_sessions_analytics_workers_id",
                "rodex_sessions_codex_rollout_sources_id",
            ],
        )
        connection.execute(_CREATE_AGENT_TRACE_PUBLICATIONS_TABLE)
        _verify_agent_trace_publications_table(connection)
        connection.execute(_CREATE_AGENT_TRACE_PUBLICATIONS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_TABLE)
        _verify_agent_trace_events_table(connection)
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_SOURCE_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_SOURCE_UNIQUE_INDEX,
            [
                "rodex_sessions_codex_threads_id",
                "source_record_ordinal",
                "derived_event_ordinal",
            ],
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_SESSION_ORDER_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ORDER_INDEX,
            ["rodex_sessions_id", "event_time_utc", "id"],
            unique=False,
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_ID_KIND_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_ID_KIND_UNIQUE_INDEX,
            ["id", "event_kind"],
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_SESSION_ID_KIND_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ID_KIND_UNIQUE_INDEX,
            ["rodex_sessions_id", "id", "event_kind"],
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_SESSION_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_ID_UNIQUE_INDEX,
            ["rodex_sessions_id", "id"],
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_PUBLIC_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_PUBLIC_ID_UNIQUE_INDEX,
            [
                "trace_event_public_id_signed_bigint_1",
                "trace_event_public_id_signed_bigint_2",
            ],
        )
        connection.execute(_CREATE_AGENT_TRACE_EVENTS_SESSION_SCOPE_ID_KIND_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_SESSION_SCOPE_ID_KIND_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "rodex_sessions_codex_activity_scopes_id",
                "id",
                "event_kind",
            ],
        )
        _create_and_verify_immutable_trigger(
            connection,
            _CREATE_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
            "AGENT TRACE EVENT PROVENANCE IS IMMUTABLE",
        )
        connection.execute(_CREATE_CODEX_ITEMS_TABLE)
        _verify_codex_items_table(connection)
        for index_statement, index_name, index_columns in (
            (
                _CREATE_CODEX_ITEMS_PUBLIC_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ITEMS_PUBLIC_ID_UNIQUE_INDEX,
                ["item_public_id_signed_bigint_1", "item_public_id_signed_bigint_2"],
            ),
            (
                _CREATE_CODEX_ITEMS_IDENTITY_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ITEMS_IDENTITY_UNIQUE_INDEX,
                [
                    "rodex_sessions_codex_threads_id",
                    "codex_item_id_signed_bigint_1",
                    "codex_item_id_signed_bigint_2",
                ],
            ),
            (
                _CREATE_CODEX_ITEMS_SESSION_THREAD_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ITEMS_SESSION_THREAD_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_threads_id", "id"],
            ),
            (
                _CREATE_CODEX_ITEMS_SESSION_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ITEMS_SESSION_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "id"],
            ),
            (
                _CREATE_CODEX_ITEMS_SESSION_SCOPE_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_ITEMS_SESSION_SCOPE_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_activity_scopes_id", "id"],
            ),
        ):
            connection.execute(index_statement)
            _verify_unique_index(
                connection, RODEX_SESSIONS_CODEX_ITEMS_TABLE, index_name, index_columns
            )
        _verify_schema_object_definition_contains(
            connection,
            "index",
            RODEX_SESSIONS_CODEX_ITEMS_IDENTITY_UNIQUE_INDEX,
            ("WHERE CODEX_ITEM_ID_SIGNED_BIGINT_1 IS NOT NULL",),
        )
        _create_and_verify_immutable_trigger(
            connection,
            _CREATE_CODEX_ITEMS_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_CODEX_ITEMS_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_CODEX_ITEMS_TABLE,
            "CODEX ITEM IDENTITY IS IMMUTABLE",
        )
        connection.execute(_CREATE_CODEX_ITEM_ALIASES_TABLE)
        _verify_codex_item_aliases_table(connection)
        connection.execute(_CREATE_CODEX_ITEM_ALIASES_IDENTITY_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE,
            RODEX_SESSIONS_CODEX_ITEM_ALIASES_IDENTITY_UNIQUE_INDEX,
            [
                "rodex_sessions_codex_threads_id",
                "codex_item_alias_sha256_int_1",
                "codex_item_alias_sha256_int_2",
                "codex_item_alias_sha256_int_3",
                "codex_item_alias_sha256_int_4",
            ],
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE,
            "Codex item alias identity is immutable",
        )
        connection.execute(_CREATE_CODEX_TOOL_CALLS_TABLE)
        _verify_codex_tool_calls_table(connection)
        for index_statement, index_name, index_columns in (
            (
                _CREATE_CODEX_TOOL_CALLS_PUBLIC_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALLS_PUBLIC_ID_UNIQUE_INDEX,
                [
                    "tool_call_public_id_signed_bigint_1",
                    "tool_call_public_id_signed_bigint_2",
                ],
            ),
            (
                _CREATE_CODEX_TOOL_CALLS_SESSION_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALLS_SESSION_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "id"],
            ),
            (
                _CREATE_CODEX_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_activity_scopes_id", "id"],
            ),
        ):
            connection.execute(index_statement)
            _verify_unique_index(
                connection,
                RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                index_name,
                index_columns,
            )
        _create_and_verify_immutable_trigger(
            connection,
            _CREATE_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER,
            RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
            "CODEX TOOL-CALL IDENTITY IS IMMUTABLE",
        )
        connection.execute(_CREATE_CODEX_TOOL_CALL_ALIASES_TABLE)
        _verify_codex_tool_call_aliases_table(connection)
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
            "Codex tool-call alias identity is immutable",
        )
        for statement, index_name, index_columns, unique in (
            (
                _CREATE_CODEX_TOOL_CALL_ALIASES_CALL_ID_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_CALL_ID_UNIQUE_INDEX,
                [
                    "rodex_sessions_codex_threads_id",
                    "codex_call_id_sha256_int_1",
                    "codex_call_id_sha256_int_2",
                    "codex_call_id_sha256_int_3",
                    "codex_call_id_sha256_int_4",
                ],
                True,
            ),
            (
                _CREATE_CODEX_TOOL_CALL_ALIASES_ITEM_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_ITEM_UNIQUE_INDEX,
                ["rodex_sessions_codex_items_id"],
                True,
            ),
            (
                _CREATE_CODEX_TOOL_CALL_ALIASES_EVENT_UNIQUE_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
                True,
            ),
            (
                _CREATE_CODEX_TOOL_CALL_ALIASES_CALL_INDEX,
                RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_CALL_INDEX,
                ["rodex_sessions_codex_tool_calls_id"],
                False,
            ),
        ):
            connection.execute(statement)
            _verify_index(
                connection,
                RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
                index_name,
                index_columns,
                unique=unique,
            )
        for statement, table_name, expected_columns in (
            (
                _CREATE_AGENT_TRACE_MESSAGES_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_id", "INTEGER", 1, 0),
                    ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("rodex_sessions_codex_items_id", "INTEGER", 0, 0),
                    ("message_phase", "TEXT", 1, 0),
                    ("message_role", "TEXT", 1, 0),
                    ("content_block_count", "INTEGER", 1, 0),
                    ("body_utf8_bytes", "INTEGER", 1, 0),
                    ("body_capture_state", "TEXT", 1, 0),
                ],
            ),
            (
                _CREATE_AGENT_TRACE_TOOL_CALLS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_id", "INTEGER", 1, 0),
                    ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("rodex_sessions_codex_tool_calls_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("activity_kind", "TEXT", 1, 0),
                    ("rodex_sessions_codex_items_id", "INTEGER", 0, 0),
                    ("tool_status", "TEXT", 0, 0),
                    ("request_utf8_bytes", "INTEGER", 1, 0),
                    ("response_utf8_bytes", "INTEGER", 1, 0),
                    ("payload_capture_state", "TEXT", 1, 0),
                ],
            ),
            (
                _CREATE_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_id", "INTEGER", 1, 0),
                    ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("rodex_sessions_codex_items_id", "INTEGER", 0, 0),
                    ("command_argument_count", "INTEGER", 1, 0),
                    ("working_directory", "TEXT", 0, 0),
                    ("command_status", "TEXT", 0, 0),
                    ("duration_ms", "INTEGER", 0, 0),
                    ("exit_code", "INTEGER", 0, 0),
                    ("stdout_utf8_bytes", "INTEGER", 1, 0),
                    ("stderr_utf8_bytes", "INTEGER", 1, 0),
                    ("aggregated_output_utf8_bytes", "INTEGER", 1, 0),
                    ("payload_capture_state", "TEXT", 1, 0),
                ],
            ),
            (
                _CREATE_AGENT_TRACE_CONTEXTS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("model_names_id", "INTEGER", 0, 0),
                    ("reasoning_effort_names_id", "INTEGER", 0, 0),
                    ("working_directory", "TEXT", 0, 0),
                    ("sandbox_mode", "TEXT", 0, 0),
                    ("approval_policy", "TEXT", 0, 0),
                    ("permission_profile_type", "TEXT", 0, 0),
                    ("workspace_root_count", "INTEGER", 1, 0),
                ],
            ),
            (
                _CREATE_AGENT_TRACE_TOKEN_USAGE_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("input_tokens", "INTEGER", 0, 0),
                    ("cached_input_tokens", "INTEGER", 0, 0),
                    ("output_tokens", "INTEGER", 0, 0),
                    ("reasoning_output_tokens", "INTEGER", 0, 0),
                    ("total_tokens", "INTEGER", 0, 0),
                    ("context_used_percent", "REAL", 0, 0),
                ],
            ),
            (
                _CREATE_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("window_ordinal", "INTEGER", 1, 0),
                    ("limit_id", "TEXT", 1, 0),
                    ("used_percent", "REAL", 0, 0),
                    ("window_minutes", "INTEGER", 0, 0),
                    ("resets_at_unix_seconds", "INTEGER", 0, 0),
                    ("plan_type", "TEXT", 0, 0),
                ],
            ),
            (
                _CREATE_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                [
                    ("id", "INTEGER", 0, 1),
                    ("rodex_sessions_agent_trace_events_id", "INTEGER", 1, 0),
                    ("event_kind", "TEXT", 1, 0),
                    ("rodex_sessions_id", "INTEGER", 1, 0),
                    (
                        "rodex_sessions_codex_activity_scopes_id",
                        "INTEGER",
                        1,
                        0,
                    ),
                    ("target_codex_threads_id", "INTEGER", 0, 0),
                    ("rodex_sessions_codex_tool_calls_id", "INTEGER", 0, 0),
                    ("activity_kind", "TEXT", 1, 0),
                    ("agent_path", "TEXT", 0, 0),
                ],
            ),
        ):
            connection.execute(statement)
            _verify_agent_trace_detail_table(connection, table_name, expected_columns)
            _create_and_verify_append_only_triggers(
                connection,
                table_name,
                "published agent trace detail is immutable",
            )
        for table_name, index_name, index_columns in (
            (
                RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_MESSAGES_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_EVENT_ORDINAL_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id", "window_ordinal"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_EVENT_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_events_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_MESSAGES_SESSION_SCOPE_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_activity_scopes_id", "id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_SESSION_SCOPE_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_activity_scopes_id", "id"],
            ),
            (
                RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_SESSION_SCOPE_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "rodex_sessions_codex_activity_scopes_id", "id"],
            ),
        ):
            connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON {table_name} ({', '.join(index_columns)})"
            )
            _verify_unique_index(connection, table_name, index_name, index_columns)
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS "
            f"{RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_CALL_INDEX} "
            f"ON {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} "
            "(rodex_sessions_codex_tool_calls_id)"
        )
        _verify_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_CALL_INDEX,
            ["rodex_sessions_codex_tool_calls_id"],
            unique=False,
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS "
            f"{RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TARGET_INDEX} "
            f"ON {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} "
            "(target_codex_threads_id)"
        )
        _verify_index(
            connection,
            RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
            RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TARGET_INDEX,
            ["target_codex_threads_id"],
            unique=False,
        )
        connection.execute(_CREATE_AGENT_REQUESTS_TABLE)
        _verify_agent_requests_table(connection)
        for index_name, index_columns, unique in (
            (
                RODEX_SESSIONS_AGENT_REQUESTS_PUBLIC_ID_UNIQUE_INDEX,
                [
                    "agent_request_public_id_signed_bigint_1",
                    "agent_request_public_id_signed_bigint_2",
                ],
                True,
            ),
            (
                RODEX_SESSIONS_AGENT_REQUESTS_SESSION_ID_UNIQUE_INDEX,
                ["rodex_sessions_id", "id"],
                True,
            ),
            (
                RODEX_SESSIONS_AGENT_REQUESTS_TOOL_ACTIVITY_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_tool_call_activities_id"],
                True,
            ),
            (
                RODEX_SESSIONS_AGENT_REQUESTS_SUBAGENT_ACTIVITY_UNIQUE_INDEX,
                ["rodex_sessions_agent_trace_subagent_activities_id"],
                True,
            ),
            (
                RODEX_SESSIONS_AGENT_REQUESTS_PARENT_MESSAGE_INDEX,
                ["parent_rodex_sessions_agent_trace_messages_id"],
                False,
            ),
            (
                RODEX_SESSIONS_AGENT_REQUESTS_TARGET_ORDER_INDEX,
                ["rodex_sessions_id", "target_codex_threads_id", "id"],
                False,
            ),
        ):
            connection.execute(
                f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS "
                f"{index_name} ON {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} "
                f"({', '.join(index_columns)})"
            )
            _verify_index(
                connection,
                RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
                index_name,
                index_columns,
                unique=unique,
            )
        connection.execute(_CREATE_AGENT_REQUESTS_VALIDATE_INSERT_TRIGGER)
        _verify_schema_object_definition_exact(
            connection,
            "trigger",
            RODEX_SESSIONS_AGENT_REQUESTS_VALIDATE_INSERT_TRIGGER,
            _CREATE_AGENT_REQUESTS_VALIDATE_INSERT_TRIGGER,
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
            "agent request identity and provenance are immutable",
        )
        connection.execute(_CREATE_AGENT_REQUEST_TARGET_TURNS_TABLE)
        _verify_agent_request_target_turns_table(connection)
        for index_name, index_columns in (
            (
                RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_REQUEST_UNIQUE_INDEX,
                ["rodex_sessions_agent_requests_id"],
            ),
            (
                RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TURN_UNIQUE_INDEX,
                ["target_rodex_sessions_codex_turns_id"],
            ),
        ):
            connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON "
                f"{RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE} "
                f"({', '.join(index_columns)})"
            )
            _verify_unique_index(
                connection,
                RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
                index_name,
                index_columns,
            )
        connection.execute(_CREATE_AGENT_REQUEST_TARGET_TURNS_VALIDATE_INSERT_TRIGGER)
        _verify_schema_object_definition_exact(
            connection,
            "trigger",
            RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_VALIDATE_INSERT_TRIGGER,
            _CREATE_AGENT_REQUEST_TARGET_TURNS_VALIDATE_INSERT_TRIGGER,
        )
        _create_and_verify_append_only_triggers(
            connection,
            RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
            "agent request target-turn association is immutable",
        )
    return path


def _require_or_create_current_schema_generation(
    connection: sqlite3.Connection,
) -> None:
    """Reject hybrid databases before creating any generation-owned domain table."""
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name != 'sqlite_sequence'"
        ).fetchall()
    }
    if RODEX_SCHEMA_GENERATIONS_TABLE not in existing_tables:
        if existing_tables:
            raise RodexSessionError(
                "Rodex database has no schema-generation marker and is not empty"
            )
        connection.execute(_CREATE_SCHEMA_GENERATIONS_TABLE)
        connection.execute(
            f"INSERT INTO {RODEX_SCHEMA_GENERATIONS_TABLE} (schema_generation) VALUES (?)",
            (RODEX_DATABASE_SCHEMA_GENERATION,),
        )
    _verify_schema_generations_table(connection)
    rows = connection.execute(
        f"SELECT id, schema_generation FROM {RODEX_SCHEMA_GENERATIONS_TABLE}"
    ).fetchall()
    if rows != [(1, RODEX_DATABASE_SCHEMA_GENERATION)]:
        raise RodexSessionError(
            "Rodex database schema generation does not match this Rodex version"
        )


def lookup_rodex_registry_id(
    database_path: str | os.PathLike[str] | None = None,
) -> RodexRegistryId:
    """Return the durable identity of one exact Rodex registry database."""
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT rodex_registry_id_signed_bigint "
            f"FROM {RODEX_REGISTRIES_TABLE} WHERE id = 1"
        ).fetchone()
    if row is None:
        raise RodexSessionError("Rodex registry identity disappeared")
    return RodexRegistryId.from_signed_bigint(row[0])


def _verify_schema_generations_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SCHEMA_GENERATIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("schema_generation", "INTEGER", 1, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SCHEMA_GENERATIONS_TABLE,
        (
            "ID INTEGER PRIMARY KEY AUTOINCREMENT",
            "SCHEMA_GENERATION >= 1",
            "CHECK (ID = 1)",
        ),
    )


def _verify_registries_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_REGISTRIES_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    if observed != [
        ("id", "INTEGER", 0, 1),
        ("rodex_registry_id_signed_bigint", "BIGINT", 1, 0),
    ]:
        raise RodexSessionError(f"{RODEX_REGISTRIES_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_REGISTRIES_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if (
        "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition
        or "CHECK (ID = 1)" not in definition
        or "TYPEOF(RODEX_REGISTRY_ID_SIGNED_BIGINT) = 'INTEGER'" not in definition
    ):
        raise RodexSessionError(f"{RODEX_REGISTRIES_TABLE} constraints mismatch")


def _verify_sessions_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_SESSIONS_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("rodex_session_id_signed_bigint", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    if observed != expected:
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} id must use AUTOINCREMENT")
    required_identity_constraints = ("TYPEOF(RODEX_SESSION_ID_SIGNED_BIGINT) = 'INTEGER'",)
    if any(fragment not in definition for fragment in required_identity_constraints):
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} identity constraints mismatch")
    if columns[-1][4] != "NULL":
        raise RodexSessionError(
            f"{RODEX_SESSIONS_TABLE}.user_defined_cool_names_id must default to NULL"
        )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_TABLE,
        (
            (("cool_names", "cool_names_id", "id"),),
            (("cool_names", "user_defined_cool_names_id", "id"),),
        ),
    )


def _verify_sessions_unique_indexes(connection: sqlite3.Connection) -> None:
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSION_ID_UNIQUE_INDEX,
        ["rodex_session_id_signed_bigint"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX,
        ["cool_names_id"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX,
        ["user_defined_cool_names_id"],
    )


def _verify_sessions_users_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_USERS_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("uid", "INTEGER", 1, 0),
        ("gid", "INTEGER", 1, 0),
        ("user_name", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_USERS_TABLE} schema mismatch: {observed!r}"
        )
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_USERS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_USERS_TABLE} id must use AUTOINCREMENT")


def _verify_sessions_users_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        f"PRAGMA index_list({RODEX_SESSIONS_USERS_TABLE})"
    ).fetchall()
    matching_indexes = [
        row for row in indexes if row[1] == RODEX_SESSIONS_USERS_UNIQUE_INDEX
    ]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_SESSIONS_USERS_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["uid", "gid", "user_name"]
    ):
        raise RodexSessionError(
            "Rodex sessions users unique index is missing: "
            f"{RODEX_SESSIONS_USERS_UNIQUE_INDEX}"
        )


def _verify_sessions_log_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("created_at_utc", "TEXT", 1, 0),
        ("rodex_sessions_users_id", "INTEGER", 1, 0),
        ("last_accessed_at_utc", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(f"{RODEX_SESSIONS_LOG_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_LOG_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_LOG_TABLE} id must use AUTOINCREMENT")
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_LOG_TABLE,
        (
            ((RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),),
            (
                (
                    RODEX_SESSIONS_USERS_TABLE,
                    "rodex_sessions_users_id",
                    "id",
                ),
            ),
        ),
    )


def _verify_sessions_log_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        f"PRAGMA index_list({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    matching_indexes = [
        row for row in indexes if row[1] == RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX
    ]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["rodex_sessions_id"]
    ):
        raise RodexSessionError(
            "Rodex sessions log unique index is missing: "
            f"{RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX}"
        )


def _verify_tmux_sessions_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("tmux_server_socket_path", "TEXT", 1, 0),
            ("tmux_session_name", "TEXT", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_tmux_sessions_unique_indexes(connection: sqlite3.Connection) -> None:
    _verify_unique_index(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX,
        ["rodex_sessions_id"],
    )
    _verify_unique_index(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX,
        ["tmux_server_socket_path", "tmux_session_name"],
    )


def _verify_runtime_instances_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_RUNTIME_INSTANCES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("runtime_id_signed_bigint", "BIGINT", 1, 0),
            ("started_at_utc", "TEXT", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_RUNTIME_INSTANCES_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_model_names_table(connection: sqlite3.Connection) -> None:
    _verify_lookup_name_table(
        connection,
        MODEL_NAMES_TABLE,
        "name_of_the_model",
    )


def _verify_reasoning_effort_names_table(connection: sqlite3.Connection) -> None:
    _verify_lookup_name_table(
        connection,
        REASONING_EFFORT_NAMES_TABLE,
        "name_of_the_reasoning_effort",
    )


def _verify_lookup_name_table(
    connection: sqlite3.Connection,
    table_name: str,
    name_column: str,
) -> None:
    _verify_table_columns(
        connection,
        table_name,
        [
            ("id", "INTEGER", 0, 1),
            (name_column, "TEXT", 1, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        table_name,
        (
            "ID INTEGER PRIMARY KEY AUTOINCREMENT",
            f"LENGTH(TRIM({name_column.upper()})) > 0",
        ),
    )


def _verify_statistics_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("statistics_publication_sequence", "INTEGER", 1, 0),
            ("statistics_projection_schema_version", "TEXT", 1, 0),
            ("calculated_at_utc", "TEXT", 1, 0),
            ("coverage_state", "TEXT", 1, 0),
            *SESSION_STATISTICS_SCALARS.schema_columns,
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        (
            "CHECK ( STATISTICS_PUBLICATION_SEQUENCE >= 1 )",
            "CHECK (COVERAGE_STATE IN ('COMPLETE', 'GAPPED'))",
            "TURNS_STARTED_COUNT = TURNS_COMPLETED_COUNT + TURNS_ABORTED_COUNT "
            "+ TURNS_OPEN_COUNT",
            "CHECK (CACHED_INPUT_TOKENS <= INPUT_TOKENS)",
        ),
    )


def _verify_statistics_distributions_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("distribution_kind", "TEXT", 1, 0),
            ("observation_count", "INTEGER", 1, 0),
            ("total", "INTEGER", 1, 0),
            ("median", "REAL", 0, 0),
            ("p75", "INTEGER", 0, 0),
            ("p90", "INTEGER", 0, 0),
            ("p95", "INTEGER", 0, 0),
            ("maximum", "INTEGER", 0, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_statistics_named_counts_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("count_kind", "TEXT", 1, 0),
            ("count_name", "TEXT", 1, 0),
            ("occurrence_count", "INTEGER", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_statistics_audit_limits_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("limit_ordinal", "INTEGER", 1, 0),
            ("limitation", "TEXT", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_canonical_codex_threads_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        CODEX_THREADS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("codex_thread_public_id_signed_bigint_1", "BIGINT", 1, 0),
            ("codex_thread_public_id_signed_bigint_2", "BIGINT", 1, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        CODEX_THREADS_TABLE,
        (
            "ID INTEGER PRIMARY KEY AUTOINCREMENT",
            "TYPEOF(CODEX_THREAD_PUBLIC_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(CODEX_THREAD_PUBLIC_ID_SIGNED_BIGINT_2) = 'INTEGER'",
        ),
    )


def _verify_codex_threads_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_THREADS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("codex_threads_id", "INTEGER", 1, 0),
            ("first_linked_at_utc", "TEXT", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_THREADS_TABLE,
        (
            ((RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),),
            ((CODEX_THREADS_TABLE, "codex_threads_id", "id"),),
        ),
    )


def _verify_current_codex_threads_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
        ("DEFERRABLE INITIALLY DEFERRED",),
    )


def _verify_codex_rollout_sources_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rollout_file_path", "TEXT", 1, 0),
            ("first_observed_at_utc", "TEXT", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
        ),
    )


def _verify_codex_turns_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_TURNS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("turn_public_id_signed_bigint_1", "BIGINT", 1, 0),
            ("turn_public_id_signed_bigint_2", "BIGINT", 1, 0),
            ("codex_turn_id_signed_bigint_1", "BIGINT", 1, 0),
            ("codex_turn_id_signed_bigint_2", "BIGINT", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_TURNS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_TURNS_TABLE,
        (
            "TYPEOF(TURN_PUBLIC_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(TURN_PUBLIC_ID_SIGNED_BIGINT_2) = 'INTEGER'",
            "TYPEOF(CODEX_TURN_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(CODEX_TURN_ID_SIGNED_BIGINT_2) = 'INTEGER'",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_codex_activity_scopes_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_turns_id", "INTEGER", 0, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_codex_turns_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
        ("DEFERRABLE INITIALLY DEFERRED",),
    )


def _verify_codex_turn_states_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_turns_id", "INTEGER", 1, 0),
            ("started_at_utc", "TEXT", 0, 0),
            ("terminal_at_utc", "TEXT", 0, 0),
            ("outcome", "TEXT", 1, 0),
            ("model_names_id", "INTEGER", 0, 0),
            ("reasoning_effort_names_id", "INTEGER", 0, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_codex_turns_id",
                    "id",
                ),
            ),
            ((MODEL_NAMES_TABLE, "model_names_id", "id"),),
            ((REASONING_EFFORT_NAMES_TABLE, "reasoning_effort_names_id", "id"),),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
        (
            "OUTCOME IN ('OPEN', 'COMPLETED', 'ABORTED')",
            "OUTCOME != 'OPEN' OR TERMINAL_AT_UTC IS NULL",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_statistics_turn_metrics_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_turns_id", "INTEGER", 1, 0),
            *TURN_STATISTICS_SCALARS.schema_columns,
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_codex_turns_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE,
        (
            "HANDS_ON IN (0, 1)",
            "EDITED_THEN_VERIFIED IN (0, 1)",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_subagent_spawns_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            (
                "subagent_rodex_sessions_codex_threads_id",
                "INTEGER",
                1,
                0,
            ),
            ("parent_rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("spawning_rodex_sessions_codex_turns_id", "INTEGER", 1, 0),
            ("agent_path", "TEXT", 1, 0),
            ("agent_nickname", "TEXT", 0, 0),
            ("history_inheritance_kind", "TEXT", 1, 0),
            ("inherited_history_start_ordinal", "INTEGER", 0, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "subagent_rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "parent_rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "parent_rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "spawning_rodex_sessions_codex_turns_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
        (
            "SUBAGENT_RODEX_SESSIONS_CODEX_THREADS_ID "
            "!= PARENT_RODEX_SESSIONS_CODEX_THREADS_ID",
            "CHECK (LENGTH(AGENT_PATH) > 0)",
            "HISTORY_INHERITANCE_KIND IN ('CLEAN', 'INHERITED')",
            "INHERITED_HISTORY_START_ORDINAL >= 0",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _create_and_verify_codex_root_role_exclusion(
    connection: sqlite3.Connection,
) -> None:
    invalid = connection.execute(
        f"SELECT 1 FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
        f"JOIN {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
        "ON spawns.subagent_rodex_sessions_codex_threads_id = "
        "current.rodex_sessions_codex_threads_id LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise RodexSessionError("current Codex thread is also a subagent spawn")
    trigger_contracts = (
        (
            _CREATE_CURRENT_CODEX_THREAD_REJECT_SPAWN_INSERT_TRIGGER,
            RODEX_CURRENT_CODEX_THREAD_REJECT_SPAWN_INSERT_TRIGGER,
        ),
        (
            _CREATE_CURRENT_CODEX_THREAD_REJECT_SPAWN_UPDATE_TRIGGER,
            RODEX_CURRENT_CODEX_THREAD_REJECT_SPAWN_UPDATE_TRIGGER,
        ),
        (
            _CREATE_SUBAGENT_SPAWN_REJECT_CURRENT_INSERT_TRIGGER,
            RODEX_SUBAGENT_SPAWN_REJECT_CURRENT_INSERT_TRIGGER,
        ),
        (
            _CREATE_SUBAGENT_SPAWN_REJECT_CURRENT_UPDATE_TRIGGER,
            RODEX_SUBAGENT_SPAWN_REJECT_CURRENT_UPDATE_TRIGGER,
        ),
    )
    for statement, trigger_name in trigger_contracts:
        connection.execute(statement)
        _verify_schema_object_definition_exact(
            connection,
            "trigger",
            trigger_name,
            statement,
        )


def _create_and_verify_immutable_trigger(
    connection: sqlite3.Connection,
    statement: str,
    trigger_name: str,
    table_name: str,
    diagnostic: str,
) -> None:
    connection.execute(statement)
    _verify_schema_object_definition_exact(
        connection,
        "trigger",
        trigger_name,
        statement,
    )
    delete_trigger_name = f"{trigger_name}_delete"
    delete_statement = f"""
CREATE TRIGGER IF NOT EXISTS {delete_trigger_name}
BEFORE DELETE ON {table_name}
BEGIN
    SELECT RAISE(ABORT, '{diagnostic.lower()}');
END
"""
    connection.execute(delete_statement)
    _verify_schema_object_definition_exact(
        connection,
        "trigger",
        delete_trigger_name,
        delete_statement,
    )


def _create_and_verify_append_only_triggers(
    connection: sqlite3.Connection,
    table_name: str,
    diagnostic: str,
) -> None:
    """Install exact UPDATE/DELETE guards without affecting the INSERT hot path."""
    for operation in ("UPDATE", "DELETE"):
        trigger_name = f"{table_name}_reject_{operation.lower()}"
        statement = f"""
CREATE TRIGGER IF NOT EXISTS {trigger_name}
BEFORE {operation} ON {table_name}
BEGIN
    SELECT RAISE(ABORT, '{diagnostic}');
END
"""
        connection.execute(statement)
        _verify_schema_object_definition_exact(
            connection,
            "trigger",
            trigger_name,
            statement,
        )


def _verify_statistics_turn_named_counts_table(
    connection: sqlite3.Connection,
) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_turns_id", "INTEGER", 1, 0),
            ("count_kind", "TEXT", 1, 0),
            ("count_name", "TEXT", 1, 0),
            ("occurrence_count", "INTEGER", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_codex_turns_id",
                    "id",
                ),
            ),
        ),
    )


def _verify_analytics_workers_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("worker_state", "TEXT", 1, 0),
            ("diagnostic_code", "TEXT", 0, 0),
            ("last_attempted_at_utc", "TEXT", 1, 0),
            ("consecutive_failures", "INTEGER", 1, 0),
            ("next_retry_at_utc", "TEXT", 0, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
        (
            "WORKER_STATE IN ('STARTING', 'CATCHING_UP', 'UP_TO_DATE', "
            "'DEGRADED', 'STOPPED')",
            "CHECK (CONSECUTIVE_FAILURES >= 0)",
            "WORKER_STATE != 'UP_TO_DATE' OR (DIAGNOSTIC_CODE IS NULL AND "
            "CONSECUTIVE_FAILURES = 0 AND NEXT_RETRY_AT_UTC IS NULL)",
            "LENGTH(DIAGNOSTIC_CODE) BETWEEN 1 AND 64",
            "DIAGNOSTIC_CODE NOT GLOB '*[^A-Z0-9_]*'",
        ),
    )


def _verify_analytics_worker_thread_checkpoints_table(
    connection: sqlite3.Connection,
) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_analytics_workers_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_rollout_sources_id", "INTEGER", 1, 0),
            ("analyzed_size_bytes", "INTEGER", 1, 0),
            ("analyzed_mtime_ns", "INTEGER", 1, 0),
            ("analyzed_prefix_sha256", "TEXT", 1, 0),
            ("verified_at_utc", "TEXT", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
                    "rodex_sessions_analytics_workers_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
                    "rodex_sessions_codex_rollout_sources_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
        (
            "CHECK (ANALYZED_SIZE_BYTES >= 0)",
            "CHECK (ANALYZED_MTIME_NS >= 0)",
            "LENGTH(ANALYZED_PREFIX_SHA256) = 64",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_agent_trace_publications_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("trace_publication_sequence", "INTEGER", 1, 0),
            ("trace_schema_version", "TEXT", 1, 0),
            ("calculated_at_utc", "TEXT", 1, 0),
            ("coverage_state", "TEXT", 1, 0),
            ("durable_event_count", "INTEGER", 1, 0),
            ("unrecognized_record_count", "INTEGER", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE,
        (
            "CHECK (TRACE_PUBLICATION_SEQUENCE >= 1)",
            "COVERAGE_STATE IN ('COMPLETE', 'GAPPED')",
            "CHECK (DURABLE_EVENT_COUNT >= 0)",
            "CHECK (UNRECOGNIZED_RECORD_COUNT >= 0)",
        ),
    )


def _verify_agent_trace_events_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("trace_event_public_id_signed_bigint_1", "BIGINT", 1, 0),
            ("trace_event_public_id_signed_bigint_2", "BIGINT", 1, 0),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
            ("source_record_ordinal", "INTEGER", 1, 0),
            ("derived_event_ordinal", "INTEGER", 1, 0),
            ("first_trace_publication_sequence", "INTEGER", 1, 0),
            ("event_kind", "TEXT", 1, 0),
            ("event_time_utc", "TEXT", 0, 0),
            ("detail_sha256", "TEXT", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
        (
            "TYPEOF(TRACE_EVENT_PUBLIC_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(TRACE_EVENT_PUBLIC_ID_SIGNED_BIGINT_2) = 'INTEGER'",
            "CHECK (SOURCE_RECORD_ORDINAL >= 0)",
            "CHECK (DERIVED_EVENT_ORDINAL >= 0)",
            "FIRST_TRACE_PUBLICATION_SEQUENCE >= 1",
            "'UNRECOGNIZED_RECORD'",
            "LENGTH(DETAIL_SHA256) = 64",
            "DETAIL_SHA256 NOT GLOB '*[^0-9A-F]*'",
        ),
    )


def _verify_codex_items_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_ITEMS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
            ("item_public_id_signed_bigint_1", "BIGINT", 1, 0),
            ("item_public_id_signed_bigint_2", "BIGINT", 1, 0),
            ("codex_item_id_signed_bigint_1", "BIGINT", 0, 0),
            ("codex_item_id_signed_bigint_2", "BIGINT", 0, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_ITEMS_TABLE,
        (
            "TYPEOF(ITEM_PUBLIC_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(ITEM_PUBLIC_ID_SIGNED_BIGINT_2) = 'INTEGER'",
            "CODEX_ITEM_ID_SIGNED_BIGINT_1 IS NULL",
            "CODEX_ITEM_ID_SIGNED_BIGINT_2 IS NULL",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_ITEMS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "id",
                ),
            ),
        ),
    )


def _verify_codex_item_aliases_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_items_id", "INTEGER", 1, 0),
            ("codex_item_alias_sha256_int_1", "BIGINT", 1, 0),
            ("codex_item_alias_sha256_int_2", "BIGINT", 1, 0),
            ("codex_item_alias_sha256_int_3", "BIGINT", 1, 0),
            ("codex_item_alias_sha256_int_4", "BIGINT", 1, 0),
            ("codex_item_alias", "TEXT", 1, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE,
        tuple(
            f"TYPEOF(CODEX_ITEM_ALIAS_SHA256_INT_{index}) = 'INTEGER'"
            for index in range(1, 5)
        ),
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_items_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_items_id",
                    "id",
                ),
            ),
        ),
    )


def _verify_codex_tool_calls_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
            ("tool_names_id", "INTEGER", 0, 0),
            ("tool_call_public_id_signed_bigint_1", "BIGINT", 1, 0),
            ("tool_call_public_id_signed_bigint_2", "BIGINT", 1, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
        (
            "TYPEOF(TOOL_CALL_PUBLIC_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(TOOL_CALL_PUBLIC_ID_SIGNED_BIGINT_2) = 'INTEGER'",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_THREADS_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "id",
                ),
            ),
            ((TOOL_NAMES_TABLE, "tool_names_id", "id"),),
        ),
    )


def _verify_codex_tool_call_aliases_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_threads_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_tool_calls_id", "INTEGER", 1, 0),
            ("alias_kind", "TEXT", 1, 0),
            ("codex_call_id_sha256_int_1", "BIGINT", 0, 0),
            ("codex_call_id_sha256_int_2", "BIGINT", 0, 0),
            ("codex_call_id_sha256_int_3", "BIGINT", 0, 0),
            ("codex_call_id_sha256_int_4", "BIGINT", 0, 0),
            ("codex_call_id", "TEXT", 0, 0),
            ("rodex_sessions_codex_items_id", "INTEGER", 0, 0),
            ("rodex_sessions_agent_trace_events_id", "INTEGER", 0, 0),
            ("source_event_kind", "TEXT", 0, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
        (
            "ALIAS_KIND IN ('CALL_ID', 'ITEM_ID', 'SOURCE_EVENT')",
            "ALIAS_KIND = 'CALL_ID' AND CODEX_CALL_ID IS NOT NULL",
            "LENGTH(CODEX_CALL_ID) > 0",
            "ALIAS_KIND = 'ITEM_ID' AND CODEX_CALL_ID IS NULL",
            "ALIAS_KIND = 'SOURCE_EVENT' AND CODEX_CALL_ID IS NULL",
            "SOURCE_EVENT_KIND = 'TOOL_CALL'",
            "TYPEOF(CODEX_CALL_ID_SHA256_INT_1) = 'INTEGER'",
            "TYPEOF(CODEX_CALL_ID_SHA256_INT_2) = 'INTEGER'",
            "TYPEOF(CODEX_CALL_ID_SHA256_INT_3) = 'INTEGER'",
            "TYPEOF(CODEX_CALL_ID_SHA256_INT_4) = 'INTEGER'",
        ),
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_threads_id",
                    "rodex_sessions_codex_threads_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                    "rodex_sessions_codex_tool_calls_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_items_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_agent_trace_events_id",
                    "id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "source_event_kind",
                    "event_kind",
                ),
            ),
        ),
    )


def _verify_agent_trace_detail_table(
    connection: sqlite3.Connection,
    table_name: str,
    expected_columns: list[tuple[str, str, int, int]],
) -> None:
    _verify_table_columns(connection, table_name, expected_columns)
    exact_event_scope_tables = {
        RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
        RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
        RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
        RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
    }
    item_scope_tables = {
        RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
        RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
        RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
    }
    expected_foreign_keys: list[tuple[tuple[str, str, str], ...]] = []
    if table_name in exact_event_scope_tables:
        expected_foreign_keys.append(
            (
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_agent_trace_events_id",
                    "id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "event_kind",
                    "event_kind",
                ),
            )
        )
    else:
        expected_foreign_keys.append(
            (
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "rodex_sessions_agent_trace_events_id",
                    "id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
                    "event_kind",
                    "event_kind",
                ),
            )
        )
    if table_name in item_scope_tables:
        expected_foreign_keys.append(
            (
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
                    "rodex_sessions_codex_items_id",
                    "id",
                ),
            )
        )
    if table_name == RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE:
        expected_foreign_keys.extend(
            (
                ((MODEL_NAMES_TABLE, "model_names_id", "id"),),
                (
                    (
                        REASONING_EFFORT_NAMES_TABLE,
                        "reasoning_effort_names_id",
                        "id",
                    ),
                ),
            )
        )
    elif table_name == RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE:
        expected_foreign_keys.extend(
            (
                ((CODEX_THREADS_TABLE, "target_codex_threads_id", "id"),),
                (
                    (
                        RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                        "rodex_sessions_id",
                        "rodex_sessions_id",
                    ),
                    (
                        RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                        "rodex_sessions_codex_activity_scopes_id",
                        "rodex_sessions_codex_activity_scopes_id",
                    ),
                    (
                        RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                        "rodex_sessions_codex_tool_calls_id",
                        "id",
                    ),
                ),
            )
        )
    elif table_name == RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE:
        expected_foreign_keys.append(
            (
                (
                    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
                    "rodex_sessions_codex_tool_calls_id",
                    "id",
                ),
            )
        )
    _verify_exact_foreign_keys(connection, table_name, expected_foreign_keys)
    required_fragments = {
        RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE: (
            "CHECK (MESSAGE_PHASE IN (",
            "CHECK (MESSAGE_ROLE IN (",
            "CHECK (CONTENT_BLOCK_COUNT >= 0)",
            "CHECK (BODY_UTF8_BYTES >= 0)",
        ),
        RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE: (
            "ACTIVITY_KIND IN ('REQUEST', 'OUTPUT', 'STATUS')",
            "CHECK (REQUEST_UTF8_BYTES >= 0)",
            "CHECK (RESPONSE_UTF8_BYTES >= 0)",
        ),
        RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE: (
            "CHECK (COMMAND_ARGUMENT_COUNT >= 0)",
            "CHECK (STDOUT_UTF8_BYTES >= 0)",
            "CHECK (STDERR_UTF8_BYTES >= 0)",
        ),
        RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE: ("CHECK (WORKSPACE_ROOT_COUNT >= 0)",),
        RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE: (
            "INPUT_TOKENS IS NULL OR INPUT_TOKENS >= 0",
            "CONTEXT_USED_PERCENT BETWEEN 0 AND 100",
        ),
        RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE: (
            "CHECK (WINDOW_ORDINAL >= 0)",
            "CHECK (LENGTH(LIMIT_ID) > 0)",
        ),
        RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE: (
            "CHECK (LENGTH(ACTIVITY_KIND) > 0)",
        ),
    }
    expected_event_kind = {
        RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE: "MESSAGE",
        RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE: "TOOL_CALL",
        RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE: "COMMAND_EXECUTION",
        RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE: "TURN_CONTEXT",
        RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE: "TOKEN_USAGE",
        RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE: "RATE_LIMIT",
        RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE: "SUBAGENT_ACTIVITY",
    }[table_name]
    _verify_table_definition_contains(
        connection,
        table_name,
        (*required_fragments[table_name], f"EVENT_KIND = '{expected_event_kind}'"),
    )


def _verify_agent_requests_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("agent_request_public_id_signed_bigint_1", "BIGINT", 1, 0),
            ("agent_request_public_id_signed_bigint_2", "BIGINT", 1, 0),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_codex_activity_scopes_id", "INTEGER", 1, 0),
            (
                "parent_rodex_sessions_agent_trace_messages_id",
                "INTEGER",
                1,
                0,
            ),
            (
                "rodex_sessions_agent_trace_tool_call_activities_id",
                "INTEGER",
                1,
                0,
            ),
            (
                "rodex_sessions_agent_trace_subagent_activities_id",
                "INTEGER",
                1,
                0,
            ),
            ("target_codex_threads_id", "INTEGER", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
                    "parent_rodex_sessions_agent_trace_messages_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
                    "rodex_sessions_agent_trace_tool_call_activities_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                    "rodex_sessions_codex_activity_scopes_id",
                    "rodex_sessions_codex_activity_scopes_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
                    "rodex_sessions_agent_trace_subagent_activities_id",
                    "id",
                ),
            ),
            ((CODEX_THREADS_TABLE, "target_codex_threads_id", "id"),),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
        (
            "TYPEOF(AGENT_REQUEST_PUBLIC_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(AGENT_REQUEST_PUBLIC_ID_SIGNED_BIGINT_2) = 'INTEGER'",
        ),
    )


def _verify_agent_request_target_turns_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_agent_requests_id", "INTEGER", 1, 0),
            ("target_rodex_sessions_codex_turns_id", "INTEGER", 1, 0),
            ("association_kind", "TEXT", 1, 0),
        ],
    )
    _verify_exact_foreign_keys(
        connection,
        RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
        (
            (
                (
                    RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
                    "rodex_sessions_agent_requests_id",
                    "id",
                ),
            ),
            (
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "rodex_sessions_id",
                    "rodex_sessions_id",
                ),
                (
                    RODEX_SESSIONS_CODEX_TURNS_TABLE,
                    "target_rodex_sessions_codex_turns_id",
                    "id",
                ),
            ),
        ),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
        ("ASSOCIATION_KIND = 'NEXT_OBSERVED_TURN'",),
    )


def _verify_table_columns(
    connection: sqlite3.Connection,
    table_name: str,
    expected: list[tuple[str, str, int, int]],
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    if observed != expected:
        raise RodexSessionError(f"{table_name} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{table_name} id must use AUTOINCREMENT")


def _verify_single_foreign_key(
    connection: sqlite3.Connection,
    table_name: str,
    expected: tuple[str, str, str],
) -> None:
    _verify_exact_foreign_keys(connection, table_name, ((expected,),))


def _verify_exact_foreign_keys(
    connection: sqlite3.Connection,
    table_name: str,
    expected: Sequence[Sequence[tuple[str, str, str]]],
) -> None:
    """Compare complete FK groups, preserving PRAGMA id/sequence ownership."""
    rows = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    grouped: dict[int, list[tuple[int, str, str, str, str, str, str]]] = {}
    for row in rows:
        grouped.setdefault(int(row[0]), []).append(
            (
                int(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
            )
        )
    observed_groups: list[tuple[tuple[str, str, str], ...]] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item[0])
        if [item[0] for item in ordered] != list(range(len(ordered))):
            raise RodexSessionError(f"{table_name} foreign key sequence is not contiguous")
        if any(item[4:] != ("NO ACTION", "NO ACTION", "NONE") for item in ordered):
            raise RodexSessionError(f"{table_name} foreign key actions mismatch")
        observed_groups.append(tuple((item[1], item[2], item[3]) for item in ordered))
    observed = sorted(observed_groups, key=repr)
    canonical_expected = sorted(
        (tuple(group) for group in expected),
        key=repr,
    )
    if observed != canonical_expected:
        raise RodexSessionError(f"{table_name} foreign keys mismatch: {observed!r}")


def _verify_unique_index(
    connection: sqlite3.Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    matching = [row for row in indexes if row[1] == index_name]
    columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    if (
        len(matching) != 1
        or matching[0][2] != 1
        or [row[2] for row in columns] != expected_columns
    ):
        raise RodexSessionError(f"unique index is missing: {index_name}")


def _verify_index(
    connection: sqlite3.Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
    *,
    unique: bool,
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    matching = [row for row in indexes if row[1] == index_name]
    columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    if (
        len(matching) != 1
        or bool(matching[0][2]) is not unique
        or [row[2] for row in columns] != expected_columns
    ):
        raise RodexSessionError(f"index is missing: {index_name}")


def _verify_table_definition_contains(
    connection: sqlite3.Connection,
    table_name: str,
    expected_fragments: Sequence[str],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    definition = " ".join(str(row[0]).upper().split())
    missing = [fragment for fragment in expected_fragments if fragment not in definition]
    if missing:
        raise RodexSessionError(f"{table_name} constraints are missing: {missing!r}")


def _verify_schema_object_definition_contains(
    connection: sqlite3.Connection,
    object_type: str,
    object_name: str,
    expected_fragments: Sequence[str],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, object_name),
    ).fetchone()
    if row is None or row[0] is None:
        raise RodexSessionError(f"{object_type} is missing: {object_name}")
    definition = " ".join(str(row[0]).upper().split())
    missing = [fragment for fragment in expected_fragments if fragment not in definition]
    if missing:
        raise RodexSessionError(
            f"{object_name} definition is missing constraints: {missing!r}"
        )


def _verify_schema_object_definition_exact(
    connection: sqlite3.Connection,
    object_type: str,
    object_name: str,
    expected_sql: str,
) -> None:
    """Attest one generated schema object rather than trusting its name/fragments."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, object_name),
    ).fetchone()
    if row is None or row[0] is None:
        raise RodexSessionError(f"{object_type} is missing: {object_name}")
    observed = _normalise_schema_sql(str(row[0]))
    expected = _normalise_schema_sql(expected_sql)
    if observed != expected:
        raise RodexSessionError(f"{object_name} definition mismatch")


def _normalise_schema_sql(value: str) -> str:
    normalised = " ".join(value.upper().split()).rstrip(";")
    return normalised.replace(" IF NOT EXISTS ", " ")
