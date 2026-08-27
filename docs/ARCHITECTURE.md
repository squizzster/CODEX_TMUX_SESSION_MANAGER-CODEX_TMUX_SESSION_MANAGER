# Agent instructions

Retain these standards when architecture changes. This is a compact current blueprint, not a change log. Amend a standard only after an agent suggestion and user agreement.
Consider one independent reviewing agent, critically assess its advice, retain ownership,
and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex matches a durable session and 64-bit runtime ID to its Codex thread/session tree and tmux endpoint.

## Runtime shape

```text
user → Rodex CLI → private tmux → session host → Codex TUI → proxy → app-server
         ├──► SQLite registry          │               ├──► status / live clients
         └──► _cat / _tail             │               └──► agent observer pane
                                       └──► analytics + trace → SQLite
                                                                  └──► post-commit observer wake
```

## Application control plane

```text
argv → classify once → select route and preparation → execute one domain
                       ├─ direct: help, Codex passthrough, statistics, agent trace
                       ├─ selector: resolve owned session or canonical Codex candidate
                       └─ runtime: acquire tmux/Codex services for session, machine, or launch
```

`rodex.cli` only maps process inputs/errors and composes dependencies. The pipeline owns
classification and exhaustive routing. Selector preparation carries one owned identity or
one canonical unregistered Codex identity into `ManagedSessionLifecycle`; display text
is not resolved again. Unregistered candidates require a transient App Server check.
Missing/unmatched selectors return to Codex after collision policy. Direct reads need no runtime.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt process arguments/errors and compose application dependencies. |
| `rodex.application_pipeline` | Classify, prepare, and dispatch every invocation through one typed route. |
| `rodex.command_contract` | Own command vocabulary, routes, generated help, and machine-command grammar. |
| `rodex.managed_session_lifecycle` / `cool_name` | Own selector resolution, naming, create, attach, resume, recovery, and collision policy. |
| `rodex.session_commands` / `statistics_commands` / `agent_trace_commands` / `machine_commands` | Execute the four command domains behind the shared CLI contract. |
| `rodex.session_read_pipeline` / `session_tail` | Own verified live reads and tail-compatible terminal following. |
| `rodex.runtime` / `process_contracts` / `session_host` | Own typed app-server/tmux discovery, processes, attachment, and supervision. |
| `rodex.status_bar` / `tmux_status` / `status_animation` | Own base status, palette, arbitration, and cancellable transitions. |
| `rodex.agent_observer` | Own the input-disabled tmux pane, exact spawn-scope/agent-message projection, and indexed durable-trace presentation. |
| `rodex.analytics` / `analytics_source_*` | Authenticate bounded rollout sources and supervise fail-open suffix analysis. |
| `rodex.agent_trace` | Normalize authenticated rollout records into typed trace facts. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.app_server_contract` / `protocol_proxy` | Own App Server RPC/version contracts, forwarding, and bounded live signals. |
| `rodex_registry.execution` / `agent_trace` / `statistics` | Own canonical lineage, typed trace persistence, and relational projections. |
| `rodex_registry.schema` | Own and exactly attest the complete v12 relational schema. |
| `rodex_sql` | Separate fail-closed read-only transactions from explicit bootstrap/write transactions. |

## Identity and data model

`rodex_sessions.id` is the private relational key. Rodex session, registry, and runtime
IDs are distinct random 64-bit domains with one 16-character lowercase hex wire form.
`codex_threads` stores each 128-bit UUID once as signed `BIGINT` halves; dependents use
integer foreign keys. Turns, items, trace events, and tool calls have separate opaque
128-bit public IDs. Provenance and activity never replace canonical identity. Detailed
rules live in [SQL_SCHEMA.md](SQL_SCHEMA.md).

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

Parse only canonical hyphenated UUIDs and resolve an existing owned link first. A
short-lived, version-checked App Server must read the exact persisted, non-ephemeral
candidate before normal creation with `resume <UUID>`; missing candidates clean up and
return to Codex. Verify the loaded UUID before commit and attach.

Managed opens hold one per-session lock through live resolution, resume, and endpoint replacement, releasing it before tmux attach. Concurrent shells converge on one verified runtime. An exact relocated `pending` runtime repairs interrupted launch. Only the exact Codex `active writer` shutdown conflict receives a bounded retry.

`_running` follows the same ownership and live-endpoint rules. `_alias` changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

Named segments own base status. `TmuxStatusPipeline` arbitrates animations and transient claims so refreshes cannot clear higher-priority warnings. Per-client prefix state preserves fast keys; shared `Ctrl-C` requires same-client confirmation; `/rodex` remains disabled. Context usage and compaction signals come from the primary protocol observer, while durable analytics remains downstream.

## Live observation

Live reads share one verified session-read pipeline. `_cat` snapshots tmux; `_tail`
emits committed history and settled visible changes outside the status/composer region;
`_events` streams App Server events. Observation is not turn completion; controllers
use `_inspect`, `_wait`, and `_result`.

## Persistent analytics

The worker has one blocking, protocol-event-driven scheduling spine. It coalesces a
0.5-second quiet window up to a five-second ceiling and feeds newline-complete suffixes
through per-source cursors into one resident analyzer and trace normalizer. Cold lineage
recovery follows exact event-named UUIDs first; a startup-only fallback scans regular
JSONL files in the root UUIDv7 three-day window, reads only first metadata lines, and
accepts the authenticated parent closure. Resident wakes never repeat that scan or
reload unchanged prefixes. Catch-up, stale-publication recovery, and clean replay remain
bounded. Clean replay invalidates cached verified lineage and byte-reader state as one
recovery boundary before source metadata is trusted again.

Canonical execution identity, rollout provenance, worker checkpoints, replaceable
statistics metrics, and the append-only typed trace have separate relational owners and
no JSON/body copies. One fenced transaction publishes changed metrics, accepted source
progress, trace suffix, and worker health. Trace totals advance from the persisted head
instead of recounting history; trace coverage remains cumulatively gapped after any
durable gap or retained unrecognized record. Explicit body reads resolve authenticated
prefixes across both current and historical memberships. Failures preserve the last good
view and cannot affect the TUI. Statistics and trace reads need neither Codex nor tmux.
After each committed trace publication, the worker sends the observer its exact
publication sequence and catch-up state. The observer coalesces wakes, advances through
the existing `(rodex_sessions_id, id)` cursor index, and considers an agent display
drained only after durable terminal events and an up-to-date publication; App lifecycle
updates never trigger SQL reads. At the live boundary, stable collaboration-call and
receiver-thread identities join `collabAgentToolCall.prompt` to `subAgentActivity`.
Only that exact delegated scope and completed messages authored by a followed child cross
the content-display boundary; system/developer messages, unrelated user content, hidden
reasoning, commands, and tool payloads do not.

## Live control

WebSocket transport uses private Unix sockets. Legacy `_send` and idle `_wait` retain short-lived secondary connections. Machine commands operate by exact turn ID and emit schema-v2 envelopes with `runtime.runtime_id`; start/steer carry caller-owned dispatch IDs that `_dispatch-status` observes in thread history. Control requires matching runtime identity and App Server compatibility; results stay live. Timeout never interrupts, and reply loss is indeterminate rather than silently retried. Approval and user-input requests remain routed to the subscribed TUI.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit; WAL mode and
  a busy timeout allow consistent readers to coexist with ordinary analytics writes.
- Read paths use an existing private database in read-only/query-only mode; only explicit bootstrap or mutation may create or repair storage.
- External tmux changes use exact targets and compensating transitions where needed.
- A live host keeps runtime paths fresh and fails closed if that cannot be maintained.
- Private runtime/database paths validate owner, type, mode, and symlink boundaries.
