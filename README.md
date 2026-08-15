# Rodex session manager

Development mode: **PROTO**

Rodex is becoming a session-aware launcher for Codex. The project-root `./rodex`
command currently allocates a Rodex identity in SQLite and then hands control to the
ordinary `codex` process, forwarding its command-line arguments.

## Try it

Requires Linux, Python 3.12+, `uv`, and the `codex` CLI.

```bash
uv sync
./rodex
```

The default database is `.rodex/rodex.sqlite3` beneath the launch directory. Set
`RODEX_DATABASE_PATH` to use a different path. Runtime databases are ignored by Git.

## Session tables

`rodex_sessions` remains sealed at three columns:

```sql
CREATE TABLE rodex_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid_int_1 BIGINT NOT NULL,
    uuid_int_2 BIGINT NOT NULL
);
CREATE UNIQUE INDEX rodex_sessions_uuid_ints_unique
    ON rodex_sessions (uuid_int_1, uuid_int_2);
```

Each newly created session receives one provenance and access row:

```sql
CREATE TABLE rodex_sessions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    created_by_user TEXT NOT NULL,
    last_accessed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES rodex_sessions (id)
);
CREATE UNIQUE INDEX rodex_sessions_log_rodex_sessions_id_unique
    ON rodex_sessions_log (rodex_sessions_id);
```

Connecting fields follow `<table_name>_<lookup_field>`, hence
`rodex_sessions_id`. Both tables use their own `id INTEGER PRIMARY KEY
AUTOINCREMENT`; SQLite provides the primary-key uniqueness, while the named unique
index enforces one log row per session.

UTC timestamps use fixed, microsecond-precision ISO-8601 text such as
`2026-08-15T12:34:56.123456Z`. This representation is unambiguous, readable, and
sorts chronologically as text. `created_by_user` defaults to the operating-system
user.

Rodex generates a secure 128-bit UUID. SQLite integers are signed 64-bit values, so
each UUID half is stored in signed two's-complement form. The `rodex_functions` API
reverses that storage mapping without losing any UUID bits.

Public helpers include:

- `create_a_rodex_session`
- `lookup_id_from_a_rodex_uuid`
- `lookup_rodex_uuid_from_an_id`
- `lookup_rodex_session_log`
- `record_a_rodex_session_access`
- `initialise_rodex_database`

The earlier `ctsm` tmux experiment remains available while the two paths converge.

## Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov --cov-report=term-missing
uv build
```

The prototype coverage floor is deliberately modest at 70% so rapid iteration stays
cheap; current coverage may be higher where tests already provide useful evidence.
