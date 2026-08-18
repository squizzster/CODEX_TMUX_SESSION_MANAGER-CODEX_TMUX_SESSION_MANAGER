# SQL schema methodology

Apply these standards to future schema decisions. These authorative standards may be modified only by an agent-suggestion followed by a user-agreement.

- Table names are always plural.
- Every table starts with `id INTEGER PRIMARY KEY AUTOINCREMENT`.
- A connecting field is named `<referenced_table>_<lookup_field>`; a link to another
  table's primary key therefore ends in `<plural_table>_id`.
- Foreign-key fields use `INTEGER` and reference the target table's `id`.
- Required values use `NOT NULL`. Nullability represents a genuine domain state, never
  implementation convenience.
- Enforce identity, cardinality, and referential integrity with database constraints,
  not application assumptions alone.
- Lookups use `INTEGER` relational keys or `BIGINT` domain IDs. Stored `TEXT` may be
  payload, but is not indexed or searched when integer identity fields exist.
- Index fields used for lookup, joins, or enforced uniqueness. Do not index payload
  without an observed query that needs it.
- Every natural key and one-to-one relationship has a named unique index whose ordered
  fields match the authoritative lookup key.
- Put intrinsic one-to-one identity on the owning root row. Add another table only
  when cardinality, lifecycle, reuse, or ownership is genuinely separate.
- Preserve required canonical identity when adding an optional user-defined identity;
  represent the latter with a separate nullable relationship rather than replacement.
- Do not introduce redundant association tables without a current cardinality or
  ownership requirement.
- Lookup tables use their complete natural key: `SELECT id` first and `INSERT` only
  when no row exists. This avoids unnecessary AUTOINCREMENT gaps.
- Related writes run inside one `BEGIN IMMEDIATE` transaction. Multi-table read views
  use one deferred `BEGIN` transaction for a consistent snapshot without a write lock.
  Any failure rolls back the complete unit of work.
- SQLite foreign-key enforcement is enabled for every transaction.
- Schema initialisation creates and verifies columns, types, nullability, primary-key
  form, and index shape in the same transaction; incompatible schemas are rejected.
- UTC timestamps use fixed microsecond ISO-8601 `TEXT`, such as
  `2026-08-15T12:34:56.123456Z`.
- Unsigned identity up to 64 bits is stored losslessly in one signed `BIGINT` through
  an explicit two's-complement codec. Wider identity is stored losslessly in ordered
  signed `BIGINT` fields with one composite unique index.
- Every identity `BIGINT` enforces `typeof(column) = 'integer'`; readers pass stored
  values through strict codecs without coercion so malformed storage fails closed.
- Derived-identity field names state the source value, derivation, integer storage,
  and part order. Never truncate identity merely to fit one column.
- Names must identify the domain that owns an identity. Similar representations from
  different domains are never assumed to be interchangeable, even when stored on the
  same owning row.
- When text needs deterministic identity, derive integer lookup fields from it and
  retain the original text only when useful as payload. The derivation must be stable
  and preserve enough bits for the domain's collision requirements.
- Normalisation, derivation, insertion, and lookup share one authoritative pipeline.
  A matching derived integer identity is occupied; do not fall back to a text lookup.
- Random Rodex IDs use exactly 64 bits, one unique index, and ten bounded
  candidates before failing explicitly. They never change representation or width.
  This includes runtime IDs: their compact form is an intentional agent-legibility
  boundary, while the unique index and incarnation check carry the integrity contract.
  Generated cool names try ten candidates at each approved word count before escalating
  to the next word count, then fail explicitly.
- In ALPHA, schema changes may reset a disposable database only after an explicit
  decision. Additive, verified schema extensions preserve existing contents.

## Current statistics projection

- `rodex_registries` contains the database instance's one durable 64-bit ID row. Live tmux
  identity includes it so another registry cannot adopt the same session/Codex pair.
- `rodex_sessions` contains one signed-BIGINT Rodex session ID, the two signed
  halves of the Codex session ID, and permanent/optional display-name links. The public Rodex
  session ID is always serialized as a 16-character lowercase hex string, never a JSON
  number. This incompatible ALPHA schema generation is stored in `rodex-v5.sqlite3`;
  Rodex leaves v4 and earlier generation files untouched and does not read or migrate
  their schemas.
- `rodex_runtime_instances` contains one signed-`BIGINT` random 64-bit `runtime_id` and
  its start time for a Rodex session. Unique indexes fence both session cardinality and
  runtime ID reuse. Allocation uses the same ten-candidate indexed-selection pipeline
  as the public Rodex session ID.
  Resume replaces this control identity; the table contains no conversation content.
- `rodex_sessions_statistics` is the latest successful aggregate snapshot, one row per
  Rodex session. Every fixed aggregate and its available base count is a typed scalar
  column. Rodex allocates its monotonic `statistics_revision`; temporary analyzer
  identities, revisions, and redundant JSON documents are excluded.
- `rodex_sessions_statistics_distributions` stores the seven bounded distribution kinds
  as `n`, total, median, p75, p90, p95, and maximum fields. Empty and nonempty shapes are
  constrained in SQL.
- `rodex_sessions_statistics_named_counts` stores only genuine dynamic maps as bounded
  category/name/count facts. Its `(session, category, name)` key supports deterministic
  reconstruction and direct aggregation. `rodex_sessions_statistics_audit_limits`
  retains limitation order with `(session, ordinal)`.
- `rodex_sessions_statistics_sources` retains every Codex session ID linked to the Rodex
  lineage. A Codex session ID is globally unique to one lineage. Nullable rollout provenance
  represents a registered source not yet authenticated; included provenance carries an
  absolute canonical path, complete-prefix byte count, mtime, SHA-256, and revision.
- `rodex_sessions_statistics_turns` stores one typed, privacy-filtered projection per
  exact `(Codex source, turn_id)` in the current revision. Four signed 64-bit SHA-256
  pieces provide indexed lookup while retained text verifies exact identity. Deferred
  foreign keys bind every turn to both the included source revision and session snapshot.
  `rodex_sessions_statistics_turn_named_counts` normalizes its dynamic category maps.
- `rodex_sessions_statistics_workers` is independent one-to-one health. Its bounded
  diagnostic code cannot contain free-form errors or paths. Failure never fabricates or
  overwrites a statistics snapshot.
- Publishing a session projection, complete turn set, exact included sources, and
  healthy state is one Codex-session-ID- and prior-revision-fenced transaction. Revision
  mark-and-sweep updates existing turn identities and removes obsolete turns without a
  variable-length SQL key list. Health-only failure publication uses the ID fence but
  does not mutate last-good statistics, turns, or source analysis.
- Strict typed projection parsing rejects missing, unknown, or wrongly typed analyzer
  fields before SQL begins. Reads select root and child rows in one transaction; the CLI
  presentation layer can reconstruct every analyzer statistic without stored JSON.
