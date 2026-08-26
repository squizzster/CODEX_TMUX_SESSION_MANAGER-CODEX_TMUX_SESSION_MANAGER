# SQL schema methodology

Apply these standards to future schema decisions. These authorative standards may be modified only by an agent-suggestion followed by a user-agreement.

- Table names are always plural.
- Every table starts with `id INTEGER PRIMARY KEY AUTOINCREMENT`.
- Every durable, reusable, or identity-bearing concept is represented once as a
  canonical row. Its uniform internal integer identity is separate from its semantic
  unique key; dependent rows use `<table_name>_id`; externally visible entities use a
  separate opaque public identity; provenance and activity use separate rows.
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
- Do not copy an identity onto an owning or relationship row. Resolve the canonical
  identity once and store its integer foreign key on memberships and relationships.
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

## Current v12 execution, statistics, and agent-trace projection

- `rodex_registries` contains the database instance's one durable 64-bit ID row. Live tmux
  identity includes it so another registry cannot adopt the same session/Codex pair.
- `rodex_sessions` contains one signed-BIGINT Rodex session ID and
  permanent/optional display-name links. The public Rodex
  session ID is always serialized as a 16-character lowercase hex string, never a JSON
  number. The incompatible ALPHA v12 generation is stored in `rodex-v12.sqlite3`.
  `rodex_schema_generations` marks the exact generation inside the database; Rodex
  rejects nonempty unmarked databases and wrong generations before creating any domain
  table. Earlier generation files remain untouched and are not read or migrated.
- `rodex_runtime_instances` contains one signed-`BIGINT` random 64-bit `runtime_id` and
  its start time for a Rodex session. Unique indexes fence both session cardinality and
  runtime ID reuse. Allocation uses the same ten-candidate indexed-selection pipeline
  as the public Rodex session ID.
  Resume replaces this control identity; the table contains no conversation content.
- `rodex_sessions_statistics` is the latest successful aggregate snapshot, one row per
  Rodex session. Every fixed aggregate and its available base count is a typed scalar
  column. Rodex allocates its session-local monotonic
  `statistics_publication_sequence` only when a changed authenticated projection is
  successfully published; no-op lifecycle events do not advance it. The sequence is a
  compare-and-set and relational consistency
  token, not a turn count or retained history; temporary analyzer dataset identities,
  dataset revisions, prior Rodex snapshots, and redundant JSON documents are excluded.
- `rodex_sessions_statistics_distributions` stores the seven bounded distribution kinds
  as `n`, total, median, p75, p90, p95, and maximum fields. Empty and nonempty shapes are
  constrained in SQL.
- `rodex_sessions_statistics_named_counts` stores only genuine dynamic maps as bounded
  category/name/count facts. Its `(session, category, name)` key supports deterministic
  reconstruction and direct aggregation. `rodex_sessions_statistics_audit_limits`
  retains limitation order with `(session, ordinal)`.
- `model_names`, `reasoning_effort_names`, and `tool_names` are independent append-only
  dimensions.
  Each has an `AUTOINCREMENT` integer primary key and a unique exact source name. A
  publication resolves each distinct name once in its transaction by selecting before
  inserting; database-local caches never cross a rollback or database boundary.
- `codex_threads` is the sole storage location for a Codex-owned 128-bit thread UUID,
  held losslessly as two signed `BIGINT` halves with independent semantic uniqueness.
  `rodex_sessions_codex_threads` is verified session membership and stores only
  `codex_threads_id`; it stores neither UUID halves nor lineage. Membership rows reject
  update and delete after verification. The one-to-one
  `rodex_sessions_current_codex_threads` relationship selects the active root.
  Historical root memberships may remain after recovery without entering the active
  recursive tree. Database triggers reject both insertion and update paths that would
  make the current root a sub-agent spawn, and the lifecycle boundary rejects the same
  transition before writing.
- `rodex_sessions_codex_rollout_sources` owns the immutable canonical rollout path
  observed for each thread. `rodex_sessions_analytics_worker_thread_checkpoints` owns
  the analytics worker's accepted append-stream prefix byte count, observation mtime,
  SHA-256, and verification time for that rollout. Codex rollouts are a trusted append-only event
  stream: the resident hot path extends this digest from suffix bytes only, while cold
  startup, clean replay, and explicit body reads re-hash the durable accepted prefix.
  This avoids a full historical reread for every emitted record without presenting the
  digest as continuous hostile-file authentication. Composite session/row foreign keys
  prevent a worker checkpoint from joining another session's worker or rollout. This
  separates source identity from worker-specific progress.
- `rodex_sessions_codex_turns` stores only stable execution-turn identity per exact
  `(Codex thread, turn_id)`. Codex turn IDs are strict canonical lowercase UUIDs stored
  losslessly as two signed `BIGINT` halves; a separate opaque 128-bit public ID is safe
  to expose. Exact-turn JSON names these separately as `codex_turn_id` and `turn_id`.
  SQL guards reject update/delete of canonical turn identity, so trace and spawn foreign
  keys remain durable. `rodex_sessions_codex_turn_states` owns the mutable
  lifecycle projection—start, terminal time, outcome, model, and reasoning effort—so
  activity is not folded into the canonical identity. Nullable `model_names_id` and
  `reasoning_effort_names_id` fields there preserve the two separate turn-scoped facts.
  `rodex_sessions_statistics_turn_metrics` owns the replaceable scalar statistics
  projection for one core turn.
  `rodex_sessions_statistics_turn_named_counts` normalizes the remaining dynamic
  category maps. Canonical collaboration operations are model-tool facts, so neither
  statistics table stores collaboration scalar columns or `collaboration_tool` named
  counts; read views derive that vocabulary directly from the stored `model_tool` rows.
- `rodex_sessions_codex_activity_scopes` canonicalizes one exact activity owner: a
  session/thread pair with either one turn foreign key or an explicit no-turn state.
  Partial unique indexes enforce one no-turn scope per thread and one scope per turn;
  composite foreign keys prove that any non-null turn belongs to that exact thread and
  session. Events, items, tool calls, aliases, and typed details carry its integer ID,
  while a trigger makes the scope identity immutable after insertion.
- `rodex_sessions_subagent_spawns` is the sole stored lineage edge and records exactly
  one verified relationship from each sub-agent source to its direct parent's spawning
  turn. Composite deferred
  foreign keys bind the child membership, parent membership, spawning turn's owning
  source, and the Rodex session. Agent path, optional nickname, and clean/inherited
  history provenance are immutable properties of this spawn relationship. The schema
  cannot attach a child to its own turn, an unrelated source's turn, or another
  session's turn, and rejects update or delete of a published lineage edge.
- `rodex_sessions_analytics_workers` is independent one-to-one health. Its bounded
  diagnostic code cannot contain free-form errors or paths. Failure never fabricates or
  overwrites a statistics snapshot.
- `rodex_sessions_agent_trace_publications` is an independent session-local CAS head.
  It records trace schema, calculation time, coverage, durable event count, and
  unrecognized-record count. Statistics and trace publication sequences are separate
  domains even though the worker commits both projections atomically. Each append
  advances those counts from the persisted head plus newly inserted rows; it never
  recounts the historical event ledger.
- `rodex_sessions_agent_trace_events` is the append-only event ledger. Its natural key
  is `(Codex thread row, rollout record ordinal, derived event ordinal)`. Each event has
  a bounded kind, non-null canonical activity-scope foreign key, timestamp, and first
  publication sequence. A physical authenticated line ordinal supplies the coordinate
  when Codex does not embed one. A canonical typed-detail SHA-256 makes replay equality
  cover the complete fact, not only its envelope. Every event also receives a random
  opaque 128-bit public identity; internal monotonic SQL IDs never cross the command
  boundary. Exact SQL-attested triggers reject update or delete of event provenance.
  Replaying authenticated history is idempotent; changed facts at a published source
  coordinate are rejected as a conflict.
- Trace detail tables are typed by domain: messages, tool calls, command executions,
  contexts, token usage, rate-limit windows, and sub-agent activities. There are no JSON
  columns. SQL stores bounded metadata and body byte counts; message, command, tool, and
  output bodies remain references to the authenticated rollout by default. Explicit
  body reads re-hash the recorded prefix and redact hidden reasoning and encrypted text.
  Message, command, and tool satellites carry the same activity-scope ID plus a literal
  event kind and exact composite foreign keys to their event and optional item/tool-call
  rows. Every typed detail rejects update and delete after publication. SQL therefore
  rejects cross-session, cross-turn, wrong-event-domain, and post-publication mutation.
  A sub-agent activity additionally binds
  `(Rodex session, event_id, event_kind)` to the same event tuple, preventing a detail
  from claiming another session. Its `target_codex_threads_id` can point to a canonical
  identity before verified session membership exists; later verification reuses that
  row without updating the activity.
  `rodex_sessions_codex_items` is the sole storage location for every observed Codex
  item identity. A strict canonical UUID is stored losslessly as two signed `BIGINT`
  halves; a non-UUID source identity is retained in
  `rodex_sessions_codex_item_aliases` with four indexed SHA-256 `BIGINT` parts and exact
  text for collision verification. The alias repeats the canonical activity-scope key
  and has exact composite foreign keys to its item for both thread and scope ownership.
  Both forms resolve to one canonical item row with an opaque 128-bit public identity;
  alias update/delete is prohibited. Message, command, and tool activities retain only
  its integer foreign key, while public reads expose semantic UUID, source alias, and
  opaque public ID as separate fields. `rodex_sessions_codex_tool_calls` owns one
  canonical invocation with a tool-name foreign key and opaque 128-bit public identity.
  The name may transition once from unknown to verified while every identity and
  ownership field remains unchanged; any later update and every delete are rejected.
  `rodex_sessions_codex_tool_call_aliases` independently canonicalizes its observed
  call-ID, item, or source-event aliases, using integer foreign keys for item and event
  aliases. These alias rows are also immutable. Thus any observed alias can resolve the
  same invocation without duplicating the canonical call. Request, output, and status
  remain explicit immutable rows in
  `rodex_sessions_agent_trace_tool_call_activities`; activity kind comes from the
  source record type, including valid empty request or output payloads.
- Per-thread lifecycle, token, command, tool, file, web, collaboration, and compaction
  summaries are grouped from the existing turn and named-count rows by internal
  `rodex_sessions_codex_threads_id`; no redundant agent-summary table is stored, and
  public JSON exposes Codex UUIDs rather than membership IDs.
- Publishing a team projection, turn metrics, exact thread closure, rollout checkpoints,
  trace batch, and healthy state is one registry/session/runtime/Codex-identity-fenced
  transaction. Statistics mark-and-sweep removes obsolete metrics and dynamic counts,
  not stable core turns. Trace events append idempotently under their own CAS head.
  Health-only failure publication does not mutate last-good statistics, checkpoints, or
  trace events.
- The analytics worker enters SQL through its registry boundary. It caches stable lookup
  identities for its database lifetime, prepares a publication once, and reuses that
  immutable publication if SQLite asks it to retry; a retry never reruns analysis or
  source I/O inside the transaction. Cold startup warms resident analyzer and trace
  state from the accepted checkpoint prefix, then publishes only later suffix bytes. A
  stale compare-and-set conflict discards the in-memory source cursor and reloads SQL
  before accepting a subsequent append, preventing a stale worker from skipping bytes.
- Child rollout staging retains its own `session_meta` and records after
  `subagent_history_start_ordinal`; inherited parent history is excluded before analysis.
  A clean child with no inherited history legitimately omits both `forked_from_id` and
  the cutoff; Rodex canonicalizes that observed shape to cutoff zero. Cold recovery
  first follows exact 128-bit targets already named by lifecycle or parent activity.
  If historical activity carries no child UUID, one startup-only fallback enumerates
  regular JSONL files in the root UUIDv7 three-day window, reads only the first metadata
  line, authenticates the complete root/parent closure, and caches exact paths. Resident
  append wakes never repeat that directory scan.
  Original physical coordinates travel beside filtered bytes so trace/body references
  do not shift when inherited lines are removed.
  Collaboration operations derive from canonical model-tool counts, while verified
  spawn relationships determine agents started at session, source, and exact-turn scope.
- Strict typed projection parsing rejects missing, unknown, or wrongly typed analyzer
  fields before SQL begins. Reads select root and child rows in one transaction; the CLI
  presentation layer can reconstruct every analyzer statistic without stored JSON.
