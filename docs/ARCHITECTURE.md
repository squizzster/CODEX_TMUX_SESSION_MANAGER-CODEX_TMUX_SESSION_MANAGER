# Agent instructions

Keep this a current blueprint, not a change log. Amend a standard only after user
agreement, retain clear ownership, and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex binds a durable session and 64-bit runtime incarnation to one Codex thread tree
and one authenticated tmux endpoint.

## Runtime shape

```text
user → Rodex CLI → shared tmux → session host → Codex TUI ↔ proxy ↔ app-server
         ├──► SQLite registry          │                 ├──► status / live clients
         ├──► _cat / _tail             │                 └──► agent observer pane
         └──► bounded update check ───────────────────► TUI-only warning
                                       └──► analytics + trace → SQLite
```

## Application control plane

```text
argv → Rodex contract → characterized Codex contract → execute one domain
                        ├─ direct: local reads or Codex passthrough
                        ├─ selector: owned session or canonical Codex candidate
                        └─ runtime: managed interactive, session, or machine work
```

`rodex.cli` composes dependencies; `rodex.application_pipeline` owns typed routing.
One interactive token receives one selector lookup. Unmatched input remains a managed
prompt. An unregistered canonical Codex identity requires a transient App Server check.

## Canonical owners

| Component | Responsibility |
|---|---|
| `rodex.application_pipeline` / command contracts | Classify and dispatch one typed invocation. |
| `rodex.managed_session_lifecycle` / `cool_name` | Select, name, create, attach, resume, recover, and resolve collisions. |
| `rodex.exact_turn_mutation` | Re-resolve mutation selectors under the per-session transition lock; validate incarnation and choose start, steer, interrupt, mouse, or alias policy. |
| `rodex.session_read_pipeline` / `session_tail` | Verify live reads and follow terminal history without idle full-history scans. |
| `rodex.process_environment` | Remove only Rodex's bootstrap virtualenv and retain caller-owned process state. |
| `rodex.runtime` / `process_contracts` / `session_host` | Discover, launch, attach, supervise, and clean up app-server, TUI, proxy, analytics, and runtime paths. |
| `rodex.tmux_session_capability` | Define server/runtime and fully registered session authority plus atomic tmux fences. |
| `rodex.tmux_sharing_coordinator` | Turn hook wakeups into exact roster transitions. |
| `rodex.tmux_status` / `status_animation` | Arbitrate status claims and render one already-admitted transition. |
| `rodex.status_animation_admission` | Own tmux-native generation, pending event, lease, handoff, and watchdog recovery. |
| `rodex.observer_projection` | Statelessly validate and bound App Server fields for observation. |
| `rodex.observer_state` | Own observer identity keys, active state, tombstones, target pruning, revisions, and connection epochs. |
| `rodex.agent_observer` | Coordinate observer owners, transport snapshots, and render the presentation view. |
| `rodex.observer_pane` | Locate or create the input-disabled tmux pane; own no semantic observer state. |
| `rodex.primary_connection_lifecycle` | Isolate primary-connection resets and terminal runtime-shutdown interrupts. |
| `rodex.analytics` / source readers | Authenticate bounded rollout suffixes and supervise fail-open analysis. |
| `rodex_registry.agent_trace_contract` / writer / reader | Normalize immutable trace facts before SQL, append them in a caller-owned transaction, and read bounded projections. |
| `rodex_registry.execution` / `statistics` | Own canonical lineage, publication orchestration, and relational projections. |
| `rodex_registry.schema` | Generate, install when authorized, and attest the complete relational catalog. |
| `rodex_sql` | Own private no-follow paths, storage identity, connections, transactions, one process-local WAL lifetime, and natural-key lookups. |

## Shared tmux capability boundary

`tmux-shared-v1.sock` is multiplexed transport, never session authority. Server-scope
protocol and random incarnation markers identify one compatible server. Creation alone
may claim a completely unmarked server, and only while it has no session; an unmarked
nonempty or incompatible server is left untouched. Display names are namespaced as
`<display>--r<registry-id>` because tmux names are server-global.

`TmuxRuntimeCapability` binds the owning host to socket, server, immutable `$session_id`,
primary `%pane_id`, and runtime. `TmuxSessionCapability` adds registered Rodex, registry,
SQL-row, and Codex identities. The launcher mints it from a uniqueness-checked roster;
async actors carry it. Every terminal action is exact-target fenced; primary operations
also require the immutable pane ID. Name, socket, runtime, or hook context is no authority.

Global indexed `client-attached` and `client-detached` hooks contain no session target;
they only wake `tmux_sharing_coordinator`. The coordinator verifies server scope, reads
one roster, and conditionally submits each changed count to its full capability. This
avoids tmux 3.2's lost source-session context after destruction. Rodex changes only owned
hook indices and options behind a server-incarnation fence; it never clears session
hooks. Root `C-c` is installed exactly or setup fails closed on conflict.
Rodex performs no general key/input interception or pane piping.

Discovery reads server, session, control, and registration fields in one tmux snapshot,
then parses the tuple as a unit. All tmux processes cross `tmux_executor`; captured calls
have absolute deadlines, interactive attach owns the terminal, and asynchronous timeout
cancellation kills and reaps its child. Runtime creation replaces Rodex bootstrap
environment fields on every new session. `TmuxStatusPipeline` arbitrates status claims;
animation admission uses exact capability, generation, lease, token, and recovery fences.

## Identity and lifecycle

`rodex_sessions.id` is private. Session, registry, and runtime IDs are separate 64-bit
domains. Codex thread UUIDs, turns, items, trace events, and tool calls retain their own
identities; provenance never substitutes for identity. See [SQL_SCHEMA.md](SQL_SCHEMA.md).

New sessions allocate unregistered IDs, create detached tmux with the configured history,
start the private host processes, observe one Codex root ID, and advertise a `pending`
tuple. The immutable Rodex session-ID transition lock spans SQL publication, registration
confirmation, registry-namespaced tmux rename, and UI setup. Competing selectors cannot
use a partially finalized row. The optional update notice and attach follow.

Existing selectors resolve once to the owned relational identity. A live endpoint must
match every advertised identity. Otherwise Rodex resumes and verifies the stored Codex
ID before replacing the endpoint; a never-saved ID starts empty and relinks atomically.
Managed opens hold the per-session lock through resolution and endpoint replacement but
release it before attach. Exact pending runtimes repair interrupted confirmation. Alias
changes compensate tmux rename if their database transition fails.

## Observer flow and connection lifecycle

```text
App Server event → stateless projection → producer reducer → newest snapshot dispatcher
                                              ↓
tmux pane ← presentation view ← consumer reducer ← length-framed private socket
```

The producer reducer is the sole semantic owner of active events, tombstones, tracked
targets, pruning, epoch, and revision. It publishes complete bounded snapshots. The
single-slot dispatcher transports only the newest snapshot; the consumer applies each
revision once and replaces presentation-derived state on epoch or overflow boundaries.
Target tombstones therefore survive unrelated updates and prevent resurrection. The
view owns rendering state only, while `ObserverPaneController` owns tmux pane mechanics.
Committed analytics publications wake bounded indexed reads; the observer does not poll
SQLite. Projection bounds text before JSON encoding and control frames are capped at
256 KiB.

On primary connection loss, `PrimaryConnectionLifecycleCoordinator` independently calls
every reset participant, records failures, and completes the lifecycle transition even
when one reset fails. The reducer alone advances the observer transport epoch. This is
independent of SQLite. Database identity is checked synchronously only by canonical SQL
transactions; the runtime host has no database watcher or subscription.

## Persistence and integrity

One blocking scheduler coalesces protocol activity and feeds authenticated, newline-
complete rollout suffixes to a resident analyzer and trace normalizer. One fenced
transaction publishes source checkpoints, statistics, append-only trace, request/turn
associations, and health. Permanent source failures park by authenticated fingerprint
until relevant source state changes; failures preserve the last good view and cannot
affect the TUI. Nested Codex response metadata supplies canonical turn identity, and only
sequence-fence races reset cursors; deterministic semantic conflicts park.

- Rodex, Codex, tmux, user, session, and runtime identities never substitute for one another.
- Related writes use explicit transactions; one fork-safe process-local idle connection
  keeps a bounded WAL generation live between sparse writes without owning a transaction.
- Ordinary reads and mutations are existing-only. Explicit first use alone may create storage.
- Private database/runtime paths validate owner, type, mode, descriptor, and symlink boundaries.
- External tmux mutation requires an explicit capability and an atomic full-tuple fence.
- Runtime path refresh fails closed when continued runtime addressing is unsafe; database
  storage validation occurs synchronously at SQL transaction boundaries.
