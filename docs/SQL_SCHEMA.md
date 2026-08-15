# SQL schema methodology

Apply these standards to future schema decisions unless a later requirement
explicitly changes them.

- Table names are always plural.
- Every table starts with `id INTEGER PRIMARY KEY AUTOINCREMENT`.
- A connecting field is named `<referenced_table>_<lookup_field>`; a link to another
  table's primary key therefore ends in `<plural_table>_id`.
- Foreign-key fields use `INTEGER` and reference the target table's `id`.
- Required values use `NOT NULL`. Nullability represents a genuine domain state, never
  implementation convenience.
- Enforce identity, cardinality, and referential integrity with database constraints,
  not application assumptions alone.
- Lookups use `INTEGER` fields. Stored `TEXT` may be payload, but is not indexed or
  searched when integer identity fields exist.
- Index fields used for lookup, joins, or enforced uniqueness. Do not index payload
  without an observed query that needs it.
- Every natural key and one-to-one relationship has a named unique index whose ordered
  fields match the authoritative lookup key.
- Put intrinsic one-to-one identity on the owning root row. Add another table only
  when cardinality, lifecycle, reuse, or ownership is genuinely separate.
- Do not introduce redundant association tables or compatibility structure merely to
  avoid changing an earlier schema, especially in PROTO.
- Lookup tables use their complete natural key: `SELECT id` first and `INSERT` only
  when no row exists. This avoids unnecessary AUTOINCREMENT gaps.
- Related selects and writes run inside one `BEGIN IMMEDIATE` transaction. Any failure
  rolls back the complete unit of work.
- SQLite foreign-key enforcement is enabled for every transaction.
- Schema initialisation creates and verifies columns, types, nullability, primary-key
  form, and index shape in the same transaction; incompatible schemas are rejected.
- UTC timestamps use fixed microsecond ISO-8601 `TEXT`, such as
  `2026-08-15T12:34:56.123456Z`.
- Identity wider than SQLite's signed 64-bit integer is stored losslessly in ordered
  signed `BIGINT` fields with one composite unique index.
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
- Generated identities use bounded attempts. Exhaust the preferred representation,
  move to the next approved representation, and fail explicitly rather than loop
  forever.
- In PROTO, a schema change resets the disposable database to empty. Without a schema
  change, existing contents are preserved.
