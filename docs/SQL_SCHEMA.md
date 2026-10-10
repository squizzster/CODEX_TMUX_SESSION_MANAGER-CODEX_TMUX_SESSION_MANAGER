# SQLite contracts and schema methodology

[`rodex_registry.schema`](../src/rodex_registry/schema.py) is the canonical generated
catalog and attestation authority; [current-contract tests](../tests/test_current_contracts.py)
pin its generation. This document preserves schema design standards and non-obvious
constraints, not a copied table/column reference. Semantic changes to these standards
require user agreement.

## Storage and transactions

[`rodex_sql.transactions`](../src/rodex_sql/transactions.py) alone owns stdlib
`sqlite3` connections, PRAGMAs, locks, transaction boundaries, WAL lifetime, and close.
Domain code receives a connection only inside its context managers:

- `open_rodex_bootstrap_transaction`: explicit first-use creation only.
- `open_rodex_transaction`: existing-only writer, `BEGIN IMMEDIATE`.
- `open_rodex_read_transaction`: existing-only read-only URI, `query_only=ON`,
  deferred `BEGIN`, WAL-aware snapshot.
- `open_rodex_maintenance_lock`: exclusive offline coordination, not relocation or repair.

[`private_database_path`](../src/rodex_sql/private_database_path.py) admits a real,
current-user private parent and regular mode-`0600` database/transition-lock files.
No-follow, close-on-exec, descriptor-relative opens retain parent/lock descriptors
through a transaction and borrow the validated main descriptor through its storage
lifetime. SQLite connects through `/proc/self/fd/<validated-database-fd>` and must
report the canonical main path.

The first admission records parent, lock, and database `(device, inode)` identities.
Revalidate descriptors/pathnames, owner, mode, type, symlinks, and reported SQLite path
before/after connect, before `BEGIN`/`COMMIT`, and on errors. Missing/replaced storage
fails with `database_moved`; even bootstrap cannot recreate something this process
already admitted. Restart is required after an authorized offline move. There is no
watcher: move-away-and-back entirely between fences is unobservable.

Ordinary transactions and integrity audits hold a shared `flock`; writers do not
exclusively block WAL readers. Lock/WAL activation waits have ten-second monotonic
deadlines and sleeping backoff capped at 50 ms. Writers enable foreign keys, WAL,
`synchronous=NORMAL`, and a ten-second busy timeout. Commit once on success, roll back
on exception; do not put source I/O or external process work under the writer lock.
Direct same-uid SQLite access ignoring the cooperative lock is outside this contract.

Process-local owners retain every active catalog borrower, with at most one idle
descriptor/connection for sparse-write WAL reuse. Opening catalog B cannot close A's
borrowed descriptor. Idle owners hold no transaction or `flock`; close SQLite before
its descriptor. Policy remains 1,000-page auto-checkpoint and 8 MiB journal-size limit.
Before fork, close idle owners, not active parent borrowers. A child forked during a
transaction must immediately exec or `_exit`; acquiring SQL or using/unwinding inherited
contexts is unsupported. No recurring SQL or watcher maintains this storage boundary.

`require_current_rodex_schema` uses a cheap generation-marker read on the steady path;
mutations recheck inside their writer transaction. Only empty marker-less storage
may bootstrap. Nonempty unmarked or incompatible catalogs fail before domain DDL;
there is no implicit reset/migration/repair. Verified additive extensions preserve
current-generation contents. `audit_rodex_database_integrity` explicitly attests all
non-internal tables, indexes, views, and triggers in a read-only snapshot including
committed WAL. It performs no DDL. SQL token comparison preserves quoted bytes and
literal whitespace, normalizing only unquoted formatting and CREATE-prefix `IF NOT EXISTS`.

## Schema standards

- Plural tables begin with `id INTEGER PRIMARY KEY AUTOINCREMENT`.
- Represent each durable/reusable identity once. Keep internal integer PK, semantic
  unique key, and externally visible opaque identity separate; provenance/activity
  use separate rows. Do not duplicate canonical identity onto relationship rows.
- Connecting fields use `<referenced_table>_<lookup_field>`; FK-to-PK fields therefore
  end in `<plural_table>_id`, use `INTEGER`, and reference `id`.
- Required values are `NOT NULL`; nullable values represent real domain states.
  Constraints enforce identity, cardinality, and referential integrity. Enable FKs
  in every transaction; do not rely solely on application assumptions.
- Each natural key/one-to-one relation has a named unique index in lookup-key order.
  Resolve complete natural keys with `SELECT id`, then insert only when absent;
  avoid needless AUTOINCREMENT gaps. Caches cannot cross rollback/database boundaries.
- Look up/join through integer keys or `BIGINT` domain IDs. Text may be payload, not
  a parallel index/search path when integer identity exists. Index actual lookup/join
  needs, not hypothetical payload queries. Do not add redundant association tables
  without a present ownership/cardinality requirement.
- Optional user-defined identity supplements canonical identity via a nullable
  relationship; it does not replace the permanent identity.
- Names identify the owning domain and any derivation/storage/part order. Similar
  representations do not make different domains interchangeable.
- Unsigned 64-bit identity uses lossless signed two's-complement `BIGINT`; wider IDs
  use ordered parts plus a composite unique index. Never truncate to fit. Enforce
  `typeof(column) = 'integer'` and strict non-coercing readers.
- Text-derived identities share one normalization/derivation/insert/lookup pipeline
  with enough bits for the collision domain. A matching derived key is occupied;
  never fall back to text lookup. Keep original text only when useful as payload.
- Random Rodex IDs remain exactly 64 bits with one unique index and ten bounded
  candidates. Generated names try ten candidates per approved word count, then
  escalate up to the configured limit or fail explicitly.
- Timestamps use canonical UTC microsecond ISO-8601 text. Writer comparisons preserve
  access high-water marks against delayed observations. A lead up to 24 hours is
  plausible reordering; a larger lead is healed as poisoned state.

## Identity and lineage

[`identity.py`](../src/rodex_registry/identity.py),
[`execution.py`](../src/rodex_registry/execution.py), and
[`lifecycle.py`](../src/rodex_registry/lifecycle.py) own normalization and transitions.

Rodex registry, session, and runtime IDs are distinct 64-bit values, exposed as
16-character lowercase hex strings, not JSON numbers. Internal SQL row IDs never
substitute for them. Codex thread/turn IDs are separate 128-bit UUIDs stored in two
signed halves; public turn/event/item/tool IDs are separate opaque identities.
App Server `thread.id` and `thread.sessionId` coincide for the managed root, not
necessarily forks. Exact-turn statistics JSON separates `codex_turn_id` from public
`turn_id`; control commands use native `codex.turn_id`, not the public statistics ID.

Permanent generated names are immutable anchors; optional aliases are preferred
display names. Name uniqueness spans the canonical database, not other state roots.
Resume replaces runtime ID/start, endpoint, and current root as one tuple against the
expected previous runtime. Complete-tuple retries are idempotent; timestamps do not
choose the winner. Stale writers cannot replace only part of the tuple.

`codex_threads` owns UUID identity. Immutable `rodex_sessions_codex_threads`
memberships, current-root selection, rollout sources, and direct-parent spawn edges
are separate relations. Historical roots remain members without entering the current
recursive tree or inflating its counts. Triggers and lifecycle validation forbid the
current root from becoming a sub-agent. Spawn edges bind child, parent, exact parent
turn, session, path/nickname, and inheritance provenance; they are immutable.

Canonical `rodex_sessions_codex_turns` survive replaceable statistics. Mutable turn
state/model/reasoning and statistics metrics are separate projections. Activity scopes
bind one exact session/thread to either a turn or explicit no-turn state, with partial
uniqueness and composite FKs. Trace satellites carry scope plus literal event kind to
reject cross-session, cross-turn, or wrong-domain references.

Canonical items and calls can have multiple observed aliases, not duplicate identities.
UUID item identities are lossless; non-UUID aliases use four SHA-256 integer parts and
text for collision verification. Calls canonicalize call-ID, item, or source-event
aliases. Aliases are immutable; a call name may become verified once from unknown,
without changing ownership. Request/output/status remain separate activities,
including valid empty payloads.

## Agent requests and trace

[`agent_trace_contract`](../src/rodex_registry/agent_trace_contract.py) validates and
normalizes typed facts, UTC/text/IDs, source coordinates, duplicate keys, and detail
hashes before `BEGIN`. Only its sealed `PreparedAgentTracePublication` is accepted;
manually constructed, copied, or replaced prepared values are not an alternative API.
[`agent_trace_writer`](../src/rodex_registry/agent_trace_writer.py) requires an active
Rodex transaction. It resolves only batch-referenced threads in bounded `VALUES`
chunks after same-transaction membership writes. Request reconciliation is internal
to this writer, not an independently sequenced public step.

The append-only event key is `(thread row, physical rollout record ordinal, derived
event ordinal)`. Detail hashes cover the complete fact: equal authenticated replay is
idempotent; changed facts at an existing coordinate conflict. Published provenance,
typed details, aliases, and ownership are immutable. Target thread identity can exist
before verified membership; later verification reuses it without mutating the activity.
Rate-limit normalization preserves supplied primary/secondary windows in order and
does not invent absent windows. Typed satellites contain no JSON columns.
Message detail stores `assistant`, `developer`, `user`, and `system` as distinct
roles. `unknown` is reserved for a missing or unsupported source role, not for a
recognized Codex developer instruction.

Trace normalization and statistics analysis have independent recognition contracts.
An `unrecognized_record` remains a durable trace-coverage fact even when another
projection understands the same source record; the
[token-accounting contract](ANALYTICS.md#token-accounting) is the current example.

`rodex_sessions_agent_requests` represents only turn-producing spawn/follow-up
requests. Each joins the exact collaboration tool request, activity, scope, target,
and latest parent user-message reference preceding the tool request in that same
turn. A separate association pairs unmatched requests with unclaimed target turns
FIFO per thread, enforcing one-to-one ownership and time order. Reusing an agent adds
a request/turn association, not a new thread or overwritten history.

`send_message` deliberately creates no request row: `interacted` alone cannot identify
follow-up intent. Public reads expose `collaboration_invocation` only through the
linked exact tool call and `turn_request` only when its narrower row exists.
Body/prose limits live in [security](SECURITY.md#content-and-privacy); source admission
and inherited-history filtering live in [analytics](ANALYTICS.md#source-admission-and-append-work).

## Publication and durability

[`statistics.py`](../src/rodex_registry/statistics.py) and
[`analytics_registry.py`](../src/rodex_registry/analytics_registry.py) publish through
one registry/session/runtime/Codex-fenced transaction: checkpoints, lineage, trace,
changed metrics, and health either commit together or do not. Strict projection
parsing rejects missing/extra/wrongly typed fields before SQL.

Statistics and trace have independent session-local compare-and-set heads. No-op
statistics do not advance their head. Trace totals add newly inserted facts to the
persisted head instead of recounting history. Coverage is cumulative: a prior gap or
nonzero unrecognized-record count cannot become complete through an ordinary append.
Statistics mark-and-sweep replaces metrics/counts, never canonical turns or trace.
Failure-health writes preserve last-good statistics, checkpoints, and trace.

Fixed metrics are typed scalar columns; distributions, genuine dynamic counts, and
ordered audit limits have relational rows. Model/reasoning/tool dimensions are
append-only. Collaboration counts derive from canonical model-tool facts, not another
stored vocabulary. Per-thread summaries derive from current-tree memberships/turns
in the same read snapshot; there is no redundant agent-summary table or retained
analyzer dataset/JSON snapshot history. Rollout identity and worker-specific accepted
prefix checkpoints are separate, with composite session FKs.

WAL with `synchronous=NORMAL` preserves consistency, but an operating-system crash or
power loss can lose recently committed transactions that have not reached durable
storage. This is not `FULL` synchronous durability. Analytics retirement has a
separate [best-effort completion limit](ANALYTICS.md#retirement-limits).

Evidence: [schema implementation](../src/rodex_registry/schema.py),
[SQLite adversarial boundaries](../tests/adversarial/test_round3_sqlite_boundaries.py),
and [effect ownership](../tests/adversarial/test_interaction_path_ownership.py).
