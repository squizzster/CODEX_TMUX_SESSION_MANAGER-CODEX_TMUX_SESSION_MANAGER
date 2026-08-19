# Agent instructions

Retain these standards when architecture changes. This is a compact current blueprint,
not a change log. Amend a standard only after an agent suggestion and user agreement.
Consider one independent reviewing agent, critically assess its advice, retain ownership,
and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex matches a durable session and 64-bit runtime ID to its Codex thread/session tree
and tmux endpoint.

## Runtime shape

```text
user terminal
    │
    ▼
Rodex CLI ───────────────► SQLite registry
    │                         ▲
    │                         │ durable identity
    ▼
private tmux server
    │  ├── advertises sockets, Rodex/registry/runtime/Codex IDs and registration state
    │  └──► verified snapshots/following ──► _cat / _tail
    ▼
session host ─┬► Codex TUI ──► protocol proxy ──► Codex app-server
             │                  ├──► context status ──► tmux base status
             │                  └──► live event tap ──► events/wait/control clients
             └► analytics worker ──► in-memory analyzer ──► SQLite projections
```

The inline TUI remains the normal Codex interface while tmux owns 50,000-line history,
copy-mode, `_cat`, `_tail`, and attachment privacy. The proxy forwards protocol frames
and fans derived tool, context, and event signals; it never buffers the screen.

## Application control plane

```text
argv → classify once → select route and preparation → execute one domain
                       ├─ direct: help, Codex passthrough, statistics
                       ├─ selector: resolve owned session or canonical Codex candidate
                       └─ runtime: acquire tmux/Codex services for session, machine, or launch
```

`rodex.cli` only maps process inputs/errors and composes dependencies. The pipeline owns
classification and exhaustive routing. Selector preparation carries either one owned
integer identity or one canonical unregistered Codex identity into
`ManagedSessionLifecycle`; display text is not resolved again. The latter requires a
transient App Server check before registration. Missing/unmatched selectors return to
Codex after private-tmux collision policy. Direct statistics may still read SQLite
without acquiring a live runtime.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt process arguments/errors and compose application dependencies. |
| `rodex.application_pipeline` | Classify, prepare, and dispatch every invocation through one typed route. |
| `rodex.command_contract` | Own command vocabulary, routes, generated help, and machine-command grammar. |
| `rodex.managed_session_lifecycle` / `cool_name` | Own selector resolution, naming, create, attach, resume, recovery, and collision policy. |
| `rodex.session_commands` / `statistics_commands` / `machine_commands` | Execute the three command domains behind the shared CLI contract. |
| `rodex.session_read_pipeline` / `session_tail` | Own verified live reads and tail-compatible terminal following. |
| `rodex.runtime` / `process_contracts` / `session_host` | Own typed app-server/tmux discovery, processes, attachment, and supervision. |
| `rodex.status_bar` / `tmux_status` / `status_animation` | Own base status, palette, arbitration, and cancellable transitions. |
| `rodex.analytics` | Authenticate rollout prefixes and supervise fail-open analysis. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.app_server_contract` / `protocol_proxy` | Own App Server RPC/version contracts, forwarding, and bounded live signals. |
| `rodex_registry` | Own typed IDs, schema, ownership, lifecycle, and relational statistics. |
| `rodex_sql` | Separate fail-closed read-only transactions from explicit bootstrap/write transactions. |

## Identity and data model

`rodex_sessions.id` is the private relational key. Rodex session, registry, and runtime
IDs are distinct random 64-bit domains with one 16-character lowercase hex wire form.
SQLite stores every Rodex-owned ID losslessly in one
signed `BIGINT`. The Codex session ID remains a Codex-owned 128-bit value stored in two
signed 64-bit integers.

Separate tables hold registry ID, tmux endpoint, runtime ID, owner/log, names,
statistics/worker health, and retained Codex lineage. A display alias never replaces
the permanent name. Detailed rules live in [SQL_SCHEMA.md](SQL_SCHEMA.md).

## Authoritative lifecycle

### New session (bare `rodex`, `_create`, or `_detach`)

1. Allocate unregistered Rodex session and runtime IDs through the same bounded,
   indexed ten-candidate pipeline.
2. Configure tmux history, then create the detached session; mouse remains user-owned.
3. Start and supervise one private app-server, proxy, inline Codex TUI, and analytics.
4. Observe its one Codex ID and advertise all identities as `pending`.
5. Transactionally register identity, provenance, name, owner, and endpoint; confirm it.
6. Rename tmux, configure status, and attach when requested.

### Existing selector

1. Resolve a generated name, display name, or Codex UUID to `rodex_sessions.id` once.
2. Enforce ownership using the effective POSIX identity.
3. Attach only to an endpoint with matching identities; an exact pending tuple completes interrupted registration.
4. Otherwise ask Codex to resume the stored Codex session ID and verify the observed ID.
5. If never saved, start empty and atomically relink; otherwise replace the endpoint.

### Persisted standalone Codex UUID

1. Parse only canonical hyphenated UUIDs; resolve an existing owned link first.
2. Ask a short-lived, version-checked App Server to `thread/read` the exact candidate.
3. Missing threads clean up and return to Codex without a Rodex row or tmux session.
4. Persisted non-ephemeral threads enter normal creation with `resume <UUID>`.
5. Allocate Rodex IDs/name and verify the loaded Codex ID before commit and attach.

Managed opens hold one per-session lock through live resolution, resume, and endpoint replacement, releasing it before tmux attach. Concurrent shells converge on one verified runtime. An exact relocated `pending` runtime repairs interrupted launch. Only the exact Codex `active writer` shutdown conflict receives a bounded retry.

`_running` follows the same ownership and live-endpoint rules. `_alias` changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

Named segments own base status. `TmuxStatusPipeline` arbitrates animations and transient claims so refreshes cannot clear higher-priority warnings. Per-client prefix state preserves fast keys; shared `Ctrl-C` requires same-client confirmation; `/rodex` remains disabled. Context usage and compaction signals come from the primary protocol observer, while durable analytics remains downstream.

## Live observation

Live reads share one verified session-read pipeline. `_cat` returns a tmux snapshot;
`_tail` emits committed history immediately and settled visible changes after 1.2
seconds, excluding the active status/composer region. `_events` streams structured App
Server events. Observation is not turn completion; controllers use `_inspect`, `_wait`,
and `_result`.

## Persistent analytics

The worker authenticates complete rollout prefixes and analyzes private copies in
memory. Typed relational projections retain source provenance and worker health without
raw events or JSON blobs. Publication is atomic behind
source/publication-sequence fences; failures preserve the last good view and cannot
affect the TUI. Statistics reads need neither Codex nor tmux. The session-local sequence
advances for every successfully published changed prefix and binds the current
relational snapshot; it is neither a turn count nor retained history.
Model and reasoning effort enter through the same exact-turn projection as independent,
nullable `turn_context` facts. Publication resolves each distinct name once per
transaction into its dedicated lookup table, stores only those integer relationships on
turns, and reconstructs session counts through joins over the finalized turn set.

## Live control

WebSocket transport uses private Unix sockets. Legacy `_send` and idle `_wait` retain short-lived secondary connections. Machine commands operate by exact turn ID and emit schema-v2 envelopes with `runtime.runtime_id`; start/steer carry caller-owned dispatch IDs that `_dispatch-status` observes in thread history. Control requires matching runtime identity and App Server compatibility; results stay live. Timeout never interrupts, and reply loss is indeterminate rather than silently retried. Approval and user-input requests remain routed to the subscribed TUI.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit; WAL mode and
  a busy timeout allow consistent readers to coexist with ordinary analytics writes.
- Read paths require an existing private database and use SQLite read-only/query-only mode;
  only explicit bootstrap or mutation paths may create or repair storage.
- External tmux changes use exact targets and compensating transitions where needed.
- A live host keeps runtime paths fresh and fails closed if that cannot be maintained.
- Session commands use tmux's `=name`; target-pane commands use `=name:`.
- Failure cleanup targets only the runtime created by the failed operation.
- Private runtime/database paths validate owner, type, mode, and symlink boundaries.

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path checks protect external grammar and lifecycle boundaries. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md) and [CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for design and runtime contracts.
