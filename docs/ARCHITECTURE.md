# Agent instructions

Retain these standards when architecture changes. This is a compact current blueprint,
not a change log. Amend a standard only after an agent suggestion and user agreement.
Consider one independent reviewing agent, critically assess its advice, retain ownership,
and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex is a harness and bridge. It matches a durable Rodex session and runtime UUID
to its Codex thread/session tree and tmux endpoint while preserving Codex's interface.

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

The TUI remains the normal Codex interface and runs inline so rendered output enters tmux's 50,000-line pane history. tmux owns keyboard copy-mode, `_cat` snapshots, and `_tail`'s plain-text source; it also derives status privacy from clients attached to that exact session. The proxy forwards WebSocket frames, derives tool and context signals, and fans structured events; it never buffers the screen.

## Application control plane

```text
argv → classify once → select route and preparation → execute one domain
                       ├─ direct: help, Codex passthrough, statistics
                       ├─ selector: resolve one owned session, then acquire runtime on match
                       └─ runtime: acquire tmux/Codex services for session, machine, or launch
```

`rodex.cli` is only the process boundary and dependency-composition root. The application
pipeline normalizes argv, carries the authoritative command classification and exact
machine spec, and owns exhaustive routing. Selector preparation carries one typed integer
session identity into `ManagedSessionLifecycle`; it is never resolved again from display
text. An unmatched selector passes to Codex only after the private-tmux collision policy.
Preparation names describe these control-flow branches, not every dependency a handler may
read; for example, direct statistics still reads SQLite without acquiring a live runtime.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt process arguments/errors and compose application dependencies. |
| `rodex.application_pipeline` | Classify, prepare, and dispatch every invocation through one typed route. |
| `rodex.command_contract` | Own command vocabulary, routes, generated help, and machine-command grammar. |
| `rodex.managed_session_lifecycle` | Own selector resolution, create, attach, resume, recovery, and collision policy. |
| `rodex.session_commands` / `statistics_commands` / `machine_commands` | Execute the three command domains behind the shared CLI contract. |
| `rodex.session_read_pipeline` | Own resolve/read-or-stream/revalidate/access order for live session reads. |
| `rodex.session_tail` | Parse tail-compatible selection and follow committed or settled terminal rows. |
| `rodex.runtime` | Own tmux scrollback, app-server discovery, attachment, and supervision. |
| `rodex.process_contracts` | Own typed subprocess configurations and their lossless argv wire forms. |
| `rodex.status_bar` / `tmux_status` / `status_animation` | Own base status, palette, arbitration, and cancellable transitions. |
| `rodex.session_host` | Keep one app-server, proxy, foreground TUI, and its runtime paths together. |
| `rodex.analytics` | Authenticate rollout prefixes and supervise fail-open analysis. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.app_server_contract` / `protocol_proxy` | Own App Server RPC/version contracts, forwarding, and bounded live signals. |
| `rodex_registry.identity` / `schema` / `lifecycle` | Own typed IDs, exact schema, session ownership, names, and transitions. |
| `rodex_registry.statistics` / `statistics_fields` | Publish and read relational projections behind identity fences. |
| `rodex_sql` | Separate fail-closed read-only transactions from explicit bootstrap/write transactions. |
| `cool_name` | Allocate and resolve collision-resistant session names. |

## Identity and data model

`rodex_sessions.id` is the private relational key. The public Rodex session identity is a random unsigned 64-bit value with one canonical 16-character lowercase hexadecimal wire form. SQLite stores all bits in one signed `BIGINT` using lossless two's-complement mapping. The Codex session ID uses two signed 64-bit integers; registry identity remains separate.

Separate tables hold registry ID, tmux endpoint, runtime UUID, POSIX owner/log, names, statistics/worker health, and retained Codex rollout lineage. A display alias never replaces the permanent generated name. Detailed rules live in [SQL_SCHEMA.md](SQL_SCHEMA.md).

## Authoritative lifecycle

### New session (bare `rodex`, `_create`, or `_detach`)

1. Allocate an unregistered Rodex session ID and runtime UUID.
2. Configure tmux history, then create the detached session; mouse remains user-owned.
3. Start one private app-server, proxy, and inline (`--no-alt-screen`) Codex TUI.
4. Supervise runtime-path freshness and fail-open analytics beside the TUI.
5. Observe the app-server's one real Codex session ID.
6. Advertise Rodex, registry, runtime, and Codex IDs as `pending`.
7. Transactionally register identities, provenance, name, owner, and tmux endpoint; mark the runtime `registered`.
8. Rename tmux, configure status, and attach when requested.

### Existing selector

1. Resolve a generated name, display name, or Codex UUID to `rodex_sessions.id` once.
2. Enforce ownership using the effective POSIX identity.
3. Attach only when the exact endpoint advertises matching registered identities; an exact pending tuple completes interrupted registration.
4. Otherwise ask Codex to resume the stored Codex session ID and verify the observed ID.
5. If that ID was never saved, start empty and atomically relink it; other failures remain fatal.
6. Replace the tmux endpoint before attaching.

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

The worker authenticates complete rollout prefixes and analyzes private copies in memory. Typed relational projections retain source provenance and worker health without raw events or JSON blobs. Publication is atomic behind source/revision fences; failures preserve the last good view and cannot affect the TUI. Statistics reads need neither Codex nor tmux.

## Live control

WebSocket transport uses private Unix sockets. Legacy `_send` and idle `_wait` retain short-lived secondary connections. Machine commands operate by exact turn ID and emit schema-v1 envelopes; start/steer carry caller-owned dispatch IDs that `_dispatch-status` observes in thread history. Control requires matching runtime identity and App Server compatibility; results stay live. Timeout never interrupts, and reply loss is indeterminate rather than silently retried. Approval and user-input requests remain routed to the subscribed TUI.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit; WAL mode and
  a busy timeout allow consistent readers to coexist with ordinary analytics writes.
- Read paths require an existing private database and use SQLite read-only/query-only mode;
  only explicit bootstrap or mutation paths may create or repair storage.
- Multi-table statistics reads use one deferred transaction for a consistent view.
- Analytics paths are low-priority, short-transaction, derived-only, and fail-open.
- External tmux changes use exact targets and compensating transitions where needed.
- A live host keeps runtime paths fresh and fails closed if that cannot be maintained.
- Session commands use tmux's `=name`; target-pane commands use `=name:`.
- Failure cleanup targets only the runtime created by the failed operation.
- Private runtime/database paths validate owner, type, mode, and symlink boundaries.

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path checks protect external grammar and lifecycle boundaries. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md) and [CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for design and runtime contracts.
