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

## Sealed session table

`rodex_sessions` is fixed to three columns:

```sql
CREATE TABLE rodex_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid_int_1 BIGINT NOT NULL,
    uuid_int_2 BIGINT NOT NULL
);
CREATE UNIQUE INDEX rodex_sessions_uuid_ints_unique
    ON rodex_sessions (uuid_int_1, uuid_int_2);
```

Rodex generates a secure 128-bit UUID. SQLite integers are signed 64-bit values, so
each UUID half is stored in signed two's-complement form. The `rodex_functions` API
reverses that storage mapping without losing any UUID bits.

Public helpers include:

- `create_a_rodex_session`
- `lookup_id_from_a_rodex_uuid`
- `lookup_rodex_uuid_from_an_id`
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
