"""Optional, exactly attested audit catalog for analytics normalization repair.

The base database generation and canonical identities remain unchanged. Existing
catalogs acquire this extension atomically with their first verified rebuild.
"""

from __future__ import annotations

NORMALIZATION_SCHEMA_TABLE = "rodex_analytics_normalization_schema"
NORMALIZATION_TRANSITIONS_TABLE = "rodex_analytics_normalization_transitions"
NORMALIZATION_SOURCES_TABLE = "rodex_analytics_normalization_sources"
NORMALIZATION_CHANGES_TABLE = "rodex_analytics_normalization_changes"
PREVIOUS_TRACE_VERSION = "rodex-agent-trace-v5"
CURRENT_TRACE_VERSION = "rodex-agent-trace-v6"

_TABLES = {
    NORMALIZATION_SCHEMA_TABLE: f"""
CREATE TABLE IF NOT EXISTS {NORMALIZATION_SCHEMA_TABLE} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    extension_generation INTEGER NOT NULL CHECK (extension_generation = 1)
)""",
    NORMALIZATION_TRANSITIONS_TABLE: f"""
CREATE TABLE IF NOT EXISTS {NORMALIZATION_TRANSITIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL REFERENCES rodex_sessions(id),
    previous_trace_schema_version TEXT NOT NULL CHECK (previous_trace_schema_version = '{PREVIOUS_TRACE_VERSION}'),
    rebuilt_trace_schema_version TEXT NOT NULL CHECK (rebuilt_trace_schema_version = '{CURRENT_TRACE_VERSION}'),
    previous_trace_publication_sequence INTEGER NOT NULL CHECK (previous_trace_publication_sequence >= 1),
    rebuilt_trace_publication_sequence INTEGER NOT NULL CHECK (
        rebuilt_trace_publication_sequence = previous_trace_publication_sequence + 1),
    verified_at_utc TEXT NOT NULL,
    verified_source_count INTEGER NOT NULL CHECK (verified_source_count >= 1),
    verified_event_count INTEGER NOT NULL CHECK (verified_event_count >= 0)
)""",
    NORMALIZATION_SOURCES_TABLE: f"""
CREATE TABLE IF NOT EXISTS {NORMALIZATION_SOURCES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_transitions_id INTEGER NOT NULL REFERENCES {NORMALIZATION_TRANSITIONS_TABLE}(id),
    rodex_sessions_codex_rollout_sources_id INTEGER NOT NULL REFERENCES rodex_sessions_codex_rollout_sources(id),
    accepted_size_bytes INTEGER NOT NULL CHECK (accepted_size_bytes >= 0),
    accepted_prefix_sha256 TEXT NOT NULL CHECK (length(accepted_prefix_sha256) = 64),
    replayed_size_bytes INTEGER NOT NULL CHECK (replayed_size_bytes >= accepted_size_bytes),
    replayed_prefix_sha256 TEXT NOT NULL CHECK (length(replayed_prefix_sha256) = 64)
)""",
    NORMALIZATION_CHANGES_TABLE: f"""
CREATE TABLE IF NOT EXISTS {NORMALIZATION_CHANGES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalization_transitions_id INTEGER NOT NULL REFERENCES {NORMALIZATION_TRANSITIONS_TABLE}(id),
    relation_name TEXT NOT NULL,
    relation_row_id INTEGER NOT NULL CHECK (relation_row_id >= 1),
    column_name TEXT NOT NULL,
    previous_integer INTEGER,
    rebuilt_integer INTEGER,
    previous_text TEXT,
    rebuilt_text TEXT,
    CHECK (previous_integer IS NULL OR previous_text IS NULL),
    CHECK (rebuilt_integer IS NULL OR rebuilt_text IS NULL)
)""",
}
_INDEXES = {
    "rodex_analytics_normalization_transition_session_unique": (
        NORMALIZATION_TRANSITIONS_TABLE,
        "rodex_sessions_id",
        True,
    ),
    "rodex_analytics_normalization_source_unique": (
        NORMALIZATION_SOURCES_TABLE,
        "normalization_transitions_id, rodex_sessions_codex_rollout_sources_id",
        True,
    ),
    "rodex_analytics_normalization_change_transition": (
        NORMALIZATION_CHANGES_TABLE,
        "normalization_transitions_id, relation_row_id",
        False,
    ),
}
_GUARDS = {
    "rodex_agent_trace_normalization_no_downgrade": f"""
CREATE TRIGGER IF NOT EXISTS rodex_agent_trace_normalization_no_downgrade
BEFORE UPDATE ON rodex_sessions_agent_trace_publications
WHEN OLD.trace_schema_version = '{CURRENT_TRACE_VERSION}' AND NEW.trace_schema_version != OLD.trace_schema_version
BEGIN SELECT RAISE(ABORT, 'agent trace normalization generation cannot be downgraded'); END
""",
    "rodex_agent_trace_normalization_no_delete": f"""
CREATE TRIGGER IF NOT EXISTS rodex_agent_trace_normalization_no_delete
BEFORE DELETE ON rodex_sessions_agent_trace_publications
WHEN OLD.trace_schema_version = '{CURRENT_TRACE_VERSION}'
BEGIN SELECT RAISE(ABORT, 'agent trace normalization head cannot be deleted'); END
""",
}
