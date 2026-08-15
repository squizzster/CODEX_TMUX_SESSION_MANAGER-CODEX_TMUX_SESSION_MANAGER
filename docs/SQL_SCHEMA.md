# SQL schema conventions

These are the default Rodex schema rules unless a later requirement explicitly
changes them.

- Table names are always plural.
- Every table starts with `id INTEGER PRIMARY KEY AUTOINCREMENT`.
- A connecting field is named `<referenced_table>_<lookup_field>`, for example
  `rodex_sessions_id` when looking up `rodex_sessions.id`.
- Foreign-key fields use `INTEGER` and reference the target table's `id`.
- Every natural key and one-to-one relationship has a clearly named unique index.
- Lookup tables use their complete natural key: `SELECT id` first and `INSERT` only
  when no row exists. This avoids unnecessary AUTOINCREMENT gaps.
- Related selects and writes run inside one `BEGIN IMMEDIATE` transaction. Any failure
  rolls back the complete unit of work.
- SQLite foreign-key enforcement is enabled for every transaction.
- UTC timestamps use fixed microsecond ISO-8601 `TEXT`, such as
  `2026-08-15T12:34:56.123456Z`.
- A 128-bit UUID is stored losslessly as two signed `BIGINT` values. Names must say
  which domain owns the UUID; Rodex and Codex UUIDs are never interchangeable.
- In PROTO, a schema change resets the disposable database to empty. Without a schema
  change, existing contents are preserved.

Current identity boundaries:

- `rodex_sessions.uuid_int_1/2` — Rodex's secure UUID.
- `rodex_codex_sessions.codex_session_uuid_int_1/2` — Codex's real session UUID.
- `rodex_codex_sessions.rodex_sessions_id` — the one-to-one matchmaking key.
- `rodex_tmux_sessions.rodex_sessions_id` — the one-to-one tmux matchmaking key.

