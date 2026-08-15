# Rodex session manager

Development mode: **PROTO**

The project-root `./rodex` command opens the ordinary Codex TUI inside tmux. A private
Codex app-server supplies the real Codex session UUID; Rodex allocates its own distinct
UUID and records the exact Rodex ↔ Codex ↔ tmux match before attaching the terminal.
Command-line arguments are forwarded to Codex.

## Try it

Requires Linux or a compatible POSIX system, Python 3.12+, `uv`, and the `codex`
CLI. Windows is explicitly unsupported.

```bash
uv sync
./rodex
```

At the `›` prompt, Codex commands such as `/status` work normally. Detach without
stopping Codex with `Ctrl-b d`.

The default database is `.rodex/rodex.sqlite3` beneath the launch directory. Set
`RODEX_DATABASE_PATH` to use a different path. Runtime databases are ignored by Git.

Prototype database rule: when the schema changes, delete and recreate the database
empty. When the schema does not change, preserve its contents.

## Session tables

`rodex_sessions` owns the complete one-to-one Rodex, Codex, and cool-name identity:

```sql
CREATE TABLE rodex_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid_int_1 BIGINT NOT NULL,
    uuid_int_2 BIGINT NOT NULL,
    codex_session_uuid_int_1 BIGINT NOT NULL,
    codex_session_uuid_int_2 BIGINT NOT NULL,
    cool_names_id INTEGER NOT NULL,
    user_defined_cool_names_id INTEGER DEFAULT NULL,
    FOREIGN KEY (cool_names_id) REFERENCES cool_names (id),
    FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id)
);
CREATE UNIQUE INDEX rodex_sessions_uuid_ints_unique
    ON rodex_sessions (uuid_int_1, uuid_int_2);
CREATE UNIQUE INDEX rodex_sessions_codex_session_uuid_ints_unique
    ON rodex_sessions (codex_session_uuid_int_1, codex_session_uuid_int_2);
CREATE UNIQUE INDEX rodex_sessions_cool_names_id_unique
    ON rodex_sessions (cool_names_id);
CREATE UNIQUE INDEX rodex_sessions_user_defined_cool_names_id_unique
    ON rodex_sessions (user_defined_cool_names_id);
```

POSIX users are normalized through a lookup table:

```sql
CREATE TABLE rodex_sessions_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    user_name TEXT NOT NULL
);
CREATE UNIQUE INDEX rodex_sessions_users_uid_gid_user_name_unique
    ON rodex_sessions_users (uid, gid, user_name);
```

Each newly created session receives one provenance and access row:

```sql
CREATE TABLE rodex_sessions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    rodex_sessions_users_id INTEGER NOT NULL,
    last_accessed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES rodex_sessions (id),
    FOREIGN KEY (rodex_sessions_users_id) REFERENCES rodex_sessions_users (id)
);
CREATE UNIQUE INDEX rodex_sessions_log_rodex_sessions_id_unique
    ON rodex_sessions_log (rodex_sessions_id);
```

The tmux endpoint is a separate one-to-one match:

```sql
CREATE TABLE rodex_tmux_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    tmux_server_socket_path TEXT NOT NULL,
    tmux_session_name TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES rodex_sessions (id)
);
```

Named unique indexes enforce unique Rodex UUIDs, Codex UUIDs, cool names, and tmux
`(tmux_server_socket_path, tmux_session_name)` endpoints.

Connecting fields follow `<table_name>_<lookup_field>`, hence `rodex_sessions_id`
and `rodex_sessions_users_id`. Every table uses its own `id INTEGER PRIMARY KEY
AUTOINCREMENT`; SQLite provides the primary-key uniqueness, while named unique
indexes enforce natural lookup keys and one log row per session.

Lookup-table writes use the shared `rodex_sql` transaction library. They always run
`SELECT id` against the complete natural key before considering an insert. Existing
rows are reused without consuming an AUTOINCREMENT value. `BEGIN IMMEDIATE` serializes
the select/insert decision, and errors roll back the complete unit of work. Session,
user lookup, session log, Codex match, and tmux match are one transaction for a live
launch.

UTC timestamps use fixed, microsecond-precision ISO-8601 text such as
`2026-08-15T12:34:56.123456Z`. This representation is unambiguous, readable, and
sorts chronologically as text. The user lookup defaults to the effective POSIX UID,
GID, and password-database user name.

Rodex generates a secure 128-bit UUID. SQLite integers are signed 64-bit values, so
each UUID half is stored in signed two's-complement form. The `rodex_functions` API
reverses that storage mapping without losing any UUID bits.

Public helpers include:

- `create_a_rodex_session`
- `lookup_id_from_a_rodex_uuid`
- `lookup_rodex_uuid_from_an_id`
- `lookup_or_create_rodex_sessions_user`
- `lookup_rodex_sessions_user`
- `lookup_rodex_session_log`
- `lookup_codex_uuid_from_a_rodex_session_id`
- `lookup_rodex_tmux_session`
- `lookup_rodex_session_id_from_a_codex_uuid`
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

## Cool names

The `cool_name` library allocates a two-word `coolname` slug, tries five integer-MD5
lookups, then tries five three-word slugs before failing. `cool_names` stores the full
MD5 as the uniquely indexed `cool_name_md5_int_1/2`; its `cool_name` text is deliberately
unindexed. Use `get_unique_new_cool_name` to allocate and
`get_unique_id_from_cool_name` to resolve the internal integer id.

Every Rodex creation allocates a cool name in the same transaction and stores its
integer `cool_names_id` directly on `rodex_sessions`. The foreign key and unique index
enforce one valid permanent name per session and prevent reuse across sessions. The
nullable `user_defined_cool_names_id` reserves a separate, uniquely indexed cool-name
identity for a future user-defined descriptive name without replacing the original.
