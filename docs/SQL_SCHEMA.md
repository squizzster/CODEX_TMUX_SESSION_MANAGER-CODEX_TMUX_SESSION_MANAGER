# SQL schema methodology

This document describes the current v14 SQLite boundary and the standards applied to
future schema decisions. These authoritative standards may be modified only by an agent
suggestion followed by user agreement.

## Current SQLite boundary

- Rodex uses Python's stdlib `sqlite3` exclusively. `rodex_sql.transactions` is the sole
  owner of SQLite connection creation, connection PRAGMAs, the cooperative database lock,
  `BEGIN`, `COMMIT`, rollback, WAL activation, and connection close. Domain modules receive
  a connection only inside one of these context-managed transactions.
- `rodex_sql.private_database_path` normalizes the path and owns secure Linux filesystem
  admission. The immediate parent must be a real current-user-owned directory with no
  group/world permissions. The sibling transition lock and database must be regular
  current-user-owned mode-`0600` files. Rodex opens the parent, lock, and database with
  `O_NOFOLLOW` and `O_CLOEXEC`, opens children relative to the retained parent descriptor,
  and retains all three descriptors through the transaction.
- `open_rodex_bootstrap_transaction` is the only entry allowed to create the private
  parent, transition lock, or database file. It is used only by explicit first-use flows.
  `open_rodex_transaction` and `open_rodex_read_transaction` are existing-only: a missing
  database or transition lock fails without creating filesystem state. An existing private
  database and lock are admitted from their exact opened descriptors by the first
  transaction that uses them.
- SQLite opens `/proc/self/fd/<validated-database-fd>` in URI `mode=rw` or `mode=ro`, then
  must report the canonical requested main path through `PRAGMA database_list`. Rodex
  revalidates parent, transition-lock, and database descriptor/path identities plus owner,
  type, mode, and symlink state before connect, after connect, before `BEGIN`, and before
  `COMMIT`. A failure becomes the terminal `database_moved` error.
- Every ordinary transaction holds a shared `flock` on the retained transition-lock
  descriptor for its complete storage lifetime. The maintenance entry holds the exclusive
  form and is for offline diagnostics. Lock acquisition has a ten-second monotonic deadline
  with sleeping exponential backoff capped at 50 ms; it does not spin. All Rodex processes
  that access the database must honor this boundary.
- Writer transactions use `BEGIN IMMEDIATE`, WAL, `synchronous=NORMAL`, foreign keys, and a
  ten-second busy timeout. Enabling WAL is a bounded cold-path operation with the same
  sleeping deadline/backoff. Read transactions use a read-only URI, `query_only=ON`, and a
  deferred `BEGIN`, so a writer does not hold the cooperative lock exclusively or
  head-of-line block WAL readers. Success commits once; any exception rolls back; every
  connection and retained descriptor is closed on exit.
- `rodex_sql.database_location_guard` admits only the exact descriptor accepted by the
  transaction boundary. One process-wide manager owns one blocking inotify worker, one
  inotify descriptor, and one wake pipe; each admitted location adds parent-name and
  database-inode watches, not another thread or descriptor set. The worker blocks in
  `select`, performs no polling and no disk writes, and queued events are also drained
  synchronously at transaction identity fences.
- A database/name move, delete, replacement, parent move, watched parent/database
  ownership or mode change, unmount, ignored watch, inotify overflow, or watcher failure
  permanently latches that location for the life of the process. The latch interrupts
  registered SQLite connections and notifies subscribers. It cannot be reset or replaced
  while execution continues; its worker and three manager descriptors are reclaimed only
  at process exit.
- `database_terminal_signal` is subscription-only. A long-lived runtime can obtain it only
  after a transaction has admitted that database; the function never opens or admits a
  path. A latch shuts down the live runtime, and both long-lived and one-shot commands
  report `database_moved: ...; please restart Rodex` rather than following replacement
  storage.
- The explicit integrity audit uses the same existing-only, shared-lock, read-only
  transaction. Its normal WAL-aware snapshot includes committed WAL content, executes no
  DDL, and compares every non-internal table, index, trigger, and view with the canonical
  catalog. Location changes and live storage relocation are unsupported; the exclusive
  maintenance lock does not move or repair the database.

## Schema standards

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
- SQLite foreign-key enforcement is enabled for every transaction.
- Cold schema initialisation creates and verifies the complete current generation in one
  transaction. Once the generation marker is current, steady-state initialisation checks
  that marker in one cheap WAL-aware read transaction. Every ordinary mutation rechecks
  the generation inside its one writer transaction; only empty marker-less private
  storage receives the cold schema bootstrap in that same transaction. A nonempty
  marker-less database or a different generation fails closed before v14 domain DDL.
- `synchronous=NORMAL` preserves SQLite consistency, but an operating-system crash or
  power loss can lose recently committed transactions that have not reached durable
  storage; this is not a `FULL` synchronous durability promise.
- UTC timestamps use fixed microsecond ISO-8601 `TEXT`, such as
  `2026-08-15T12:34:56.123456Z`.
- Access and runtime-start updates compare those canonical timestamps in the writer
  transaction, so a delayed older writer cannot regress the durable high-water mark.
  Durable text must already use canonical UTC microsecond form. A value up to 24 hours
  ahead of an incoming observation is retained as plausible reordering; a larger lead is
  treated as a poisoned high-water mark and healed to the incoming canonical timestamp.
- Runtime resume persists one incarnation tuple: runtime ID/start, tmux endpoint, and
  current Codex root are either all accepted or all rejected against the current runtime
  start. A stale incarnation cannot partially replace any member of that tuple.
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
- Rodex does not implicitly reset, rewrite, or repair incompatible schema generations.
  Additive, verified schema extensions preserve current-generation contents.

## Current v14 execution, request, statistics, and agent-trace projection

- `rodex_registries` contains the database instance's one durable 64-bit ID row. Live tmux
  identity includes it so another registry cannot adopt the same session/Codex pair.
- `rodex_sessions` contains one signed-BIGINT Rodex session ID and
  permanent/optional display-name links. The public Rodex
  session ID is always serialized as a 16-character lowercase hex string, never a JSON
  number. The incompatible ALPHA v14 generation is stored in `rodex-v14.sqlite3`.
  `rodex_schema_generations` marks the exact generation inside the database; Rodex
  rejects nonempty unmarked databases and wrong generations before creating any domain
  table. Files for other generations remain outside the current v14 database path and
  are not read.
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
  SHA-256, and verification time for that rollout. Codex rollouts are a trusted
  append-only event stream: the resident hot path extends this digest from suffix bytes
  only, while cold startup, clean replay, and explicit body reads re-hash the durable
  accepted prefix.
  Clean replay invalidates cached verified lineage metadata together with reader state;
  no source can reuse pre-recovery parent, depth, path, nickname, or inheritance facts.
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
- `rodex_sessions_agent_requests` is the canonical identity for one observed
  **turn-producing** agent request. It receives its own opaque 128-bit public ID and
  joins one exact parent user-message reference, collaboration tool request activity,
  spawn/follow-up activity, activity scope, and target `codex_threads` row. Semantic
  uniqueness is enforced independently for the tool activity and sub-agent activity. An
  insert trigger proves
  same session/scope/turn ownership, requires the latest user message preceding the
  collaboration tool request (which must itself precede the activity), and
  accepts only `collaboration.spawn_agent → started` or
  `collaboration.followup_task → interacted`. The row records identity and provenance;
  plaintext bodies remain authenticated rollout references. A
  `collaboration.send_message → interacted` activity deliberately has no request row:
  it continues an existing agent turn rather than producing another one.
- `rodex_sessions_agent_request_target_turns` separately associates one canonical
  request with one target agent turn. The normalizer pairs unmatched requests and later
  unclaimed turns FIFO per exact target thread. Unique indexes enforce one target turn
  per request and one request per target turn; a trigger proves target membership and
  time ordering, rejects a later request while an earlier request remains unmatched, and
  rejects a later eligible turn while an earlier one remains unclaimed. A follow-up on
  an existing agent therefore becomes another request row and another turn association
  without changing the agent's canonical thread or earlier history.
- `rodex_sessions_analytics_workers` is independent one-to-one health. Its bounded
  diagnostic code cannot contain free-form errors or paths. Failure never fabricates or
  overwrites a statistics snapshot.
- `rodex_sessions_agent_trace_publications` is an independent session-local CAS head.
  It records trace schema, calculation time, coverage, durable event count, and
  unrecognized-record count. Statistics and trace publication sequences are separate
  domains even though the worker commits both projections atomically. Each append
  advances those counts from the persisted head plus newly inserted rows; it never
  recounts the historical event ledger. Coverage is cumulative at this boundary: a
  prior `gapped` head remains gapped, and a nonzero cumulative unrecognized-record count
  cannot be published as `complete`.
- Agent-trace publication has one canonical pre-transaction contract. It normalizes
  UTC/text/typed details, validates every identity and source coordinate, rejects
  duplicate source keys, and computes canonical detail hashes before `BEGIN IMMEDIATE`.
  The transactional writer accepts only the contract-issued immutable prepared form
  and rejects callers outside an active Rodex transaction before SQL. After current
  same-transaction membership updates, it resolves only the distinct thread IDs present
  in the batch through bounded row-value `VALUES` chunks; unrelated memberships are
  neither selected nor materialized.
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
  body reads select authenticated rollout checkpoints for all current and historical
  session memberships, re-hash each requested prefix, and redact hidden reasoning and
  encrypted text through the same shared classifier used during normalization.
  Message, command, and tool satellites carry the same activity-scope ID plus a literal
  event kind and exact composite foreign keys to their event and optional item/tool-call
  rows. Every typed detail rejects update and delete after publication. SQL therefore
  rejects cross-session, cross-turn, wrong-event-domain, and post-publication mutation.
  A sub-agent activity additionally binds
  `(Rodex session, event_id, event_kind)` to the same event tuple, preventing a detail
  from claiming another session. Its `target_codex_threads_id` can point to a canonical
  identity before verified session membership exists; later verification reuses that
  row without updating the activity. The activity also holds the exact canonical
  collaboration tool-call foreign key resolved from its source call ID. The bounded
  public trace read projects that linked tool identity, source call ID, argument byte
  count, and capture state as `collaboration_invocation`. It projects `turn_request`
  only when the narrower request row actually exists; it never derives a follow-up from
  `activity_kind = interacted` alone. Existing v14 rows therefore expose historical
  `send_message` operations correctly without rewriting historical rows.
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
