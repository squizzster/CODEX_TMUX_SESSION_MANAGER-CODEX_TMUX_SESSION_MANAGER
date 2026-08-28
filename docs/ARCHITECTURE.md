# Agent instructions

Keep this a current blueprint, not a change log. Amend a standard only after an agent
suggestion and user agreement. Consider one independent reviewer, retain ownership, and
keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex matches a durable session and 64-bit runtime ID to its Codex thread/session tree and tmux endpoint.

## Runtime shape

```text
user → Rodex CLI → private tmux → session host → Codex TUI ↔ proxy ↔ app-server
         ├──► SQLite registry          │                 ├──► status / live clients
         ├──► _cat / _tail             │                 └──► agent observer pane
         └──► bounded update check ───────────────────► TUI-only warning
                                       └──► analytics + trace → SQLite
```

## Application control plane

```text
argv → Rodex contract → Codex 0.150.1 contract → execute one domain
                        ├─ direct: local reads or Codex passthrough
                        ├─ selector: owned session or canonical Codex candidate
                        └─ runtime: managed interactive, session, or machine work
```

`rodex.cli` composes dependencies and the characterized Codex grammar. The pipeline owns
typed routing. A sole interactive token gets one selector lookup; an unmatched token
remains the managed prompt. Canonical unregistered identities require a transient App
Server check. Direct reads and Codex passthrough need no runtime.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt process arguments/errors and compose application dependencies. |
| `rodex.application_pipeline` | Classify, prepare, and dispatch every invocation through one typed route. |
| `codex_cli_contract` / `rodex.command_contract` | Own current Codex grammar/routing and local command/help grammar. |
| `rodex.managed_session_lifecycle` / `cool_name` | Own selector resolution, naming, create, attach, resume, recovery, and collision policy. |
| `rodex.session_commands` / `statistics_commands` / `agent_trace_commands` / `machine_commands` | Execute the four command domains behind the shared CLI contract. |
| `rodex.session_read_pipeline` / `session_tail` | Own verified live reads and tail-compatible terminal following. |
| `rodex.runtime` / `process_contracts` / `session_host` | Own typed app-server/tmux discovery, processes, attachment, and supervision. |
| `rodex.status_bar` / `tmux_status` / `status_animation` | Own base status, palette, arbitration, and cancellable transitions. |
| `rodex.agent_observer` | Own the input-disabled tmux pane and the human/developer presentation of invocation, exact requests, agent-authored prose, and durable outcomes. |
| `rodex.analytics` / `analytics_source_*` | Authenticate bounded rollout sources and supervise fail-open suffix analysis. |
| `rodex.agent_trace` | Normalize authenticated rollout records into typed trace facts. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.codex_update_notice` | Compare stable Codex versions through bounded commands and a fail-open 24-hour npm cache. |
| `rodex.app_server_contract` / `protocol_proxy` | Own App Server contracts, forwarding, bounded live signals, and downstream-only native TUI notices. |
| `rodex_registry.execution` / `agent_trace` / `statistics` | Own canonical lineage, request/turn provenance, typed trace persistence, and relational projections. |
| `rodex_registry.agent_observer` | Read one bounded projection for only the observer's exact current agent turns. |
| `rodex_registry.schema` | Own and exactly attest the complete v14 relational schema. |
| `rodex_sql` | Separate fail-closed read-only transactions from explicit bootstrap/write transactions. |

## Identity and data model

`rodex_sessions.id` is the private relational key. Rodex session, registry, and runtime
IDs are distinct random 64-bit domains with one 16-character lowercase hex wire form.
`codex_threads` stores each 128-bit UUID once as signed `BIGINT` halves; dependents use
integer foreign keys. Turns, items, trace events, and tool calls have separate opaque
128-bit public IDs. Provenance and activity never replace canonical identity. Detailed
rules live in [SQL_SCHEMA.md](SQL_SCHEMA.md).

## Authoritative lifecycle

### New session (native interactive syntax, `_create`, or `_detach`)

1. Allocate unregistered Rodex session and runtime IDs through the same bounded,
   indexed ten-candidate pipeline.
2. Configure tmux history, then create the detached session; mouse remains user-owned.
3. Start and supervise one private app-server, proxy, updater-disabled inline Codex TUI,
   and analytics.
4. Observe its one Codex ID and advertise all identities as `pending`.
5. Transactionally register identity, provenance, name, owner, and endpoint; confirm it.
6. Rename tmux and configure status.
7. Before an attach, check for a newer stable Codex release and, when present, ask the
   proxy to emit a native warning only to the primary TUI; then attach regardless.

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
continue as a managed initial prompt. Verify the loaded UUID before commit and attach.

Managed opens hold one per-session lock through live resolution, resume, and endpoint replacement, releasing it before tmux attach. Concurrent shells converge on one verified runtime. An exact relocated `pending` runtime repairs interrupted launch. Only the exact Codex `active writer` shutdown conflict receives a bounded retry.

`_running` follows the same ownership and live-endpoint rules. `_alias` changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

Named segments own base status. `TmuxStatusPipeline` arbitrates animations and transient claims so refreshes cannot clear higher-priority warnings. Per-client prefix state preserves fast keys; shared `Ctrl-C` requires same-client confirmation; `/rodex` remains disabled. One primary context coordinator combines live token snapshots from the exact rollout path named by `thread/started` with App Server usage and compaction events; durable analytics remains downstream.

## Live observation

Live reads share one verified session-read pipeline. `_cat` snapshots tmux; `_tail`
emits committed history and settled visible changes outside the status/composer region;
`_events` streams App Server events. Observation is not turn completion; controllers
use `_inspect`, `_wait`, and `_result`. A Rodex update notice terminates at the proxy and
becomes TUI-owned scrollback; it is absent from App Server traffic, `_events`, durable
thread content, and model turns.

## Persistent analytics

One blocking scheduler coalesces protocol activity and sends authenticated source
suffixes through cursors into a resident analyzer and trace normalizer. Cold lineage
recovery prefers event-named UUIDs; its bounded startup fallback reads only candidate
metadata. Resident wakes never repeat discovery or reload unchanged prefixes.

Identity, provenance, checkpoints, replaceable statistics, and the append-only trace have
separate relational owners. One fenced transaction publishes progress, trace, metrics,
request/turn associations, and worker health. Failures preserve the last good view and
cannot affect the TUI. See [SQL_SCHEMA.md](SQL_SCHEMA.md) for persistence rules.

After each publication, the observer reads one bounded projection for its current agent
turns. Codex 0.150.1's `collabAgentToolCall` supplies the exact collaboration tool and,
when available, its plaintext prompt; `subAgentActivity` supplies child identity and
lifecycle under the same call ID. The durable trace retains that exact relationship while
keeping rollout arguments encrypted. Only spawn and follow-up operations enter the FIFO
target-turn association; `send_message` continues the current turn. The latest completed
same-root, same-turn `userMessage` is separately labelled as root-request provenance.
Runtime-specific length-framed control messages are root- and sender-checked and keyed by
exact activity identity. Other-root content, protected instructions, encrypted payloads,
reasoning, arbitrary tool arguments, and output payloads stay outside the boundary.

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
