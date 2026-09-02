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

`rodex.cli` composes dependencies; `rodex.application_pipeline` characterizes argv once
and routes direct reads/passthrough, selectors, and managed runtime work. One interactive
token receives one lookup; unmatched input is a prompt. An unregistered canonical Codex
identity requires a transient App Server check.

## Canonical owners

| Component | Responsibility |
|---|---|
| `rodex.application_pipeline` / command contracts | Classify and dispatch one typed invocation. |
| `rodex.managed_session_lifecycle` / `human_messages` / `cool_name` | Own session lifecycle, naming, collisions, and action-first human messages. |
| `rodex.exact_turn_mutation` | Re-resolve mutation selectors under the per-session transition lock; validate incarnation and choose start, steer, interrupt, mouse, or alias policy. |
| `rodex.session_read_pipeline` / `session_tail` | Verify live reads and follow terminal history without idle full-history scans. |
| `rodex.process_environment` / `environment_exec` | Prepare caller-owned state and enforce the exact managed-process environment at tmux child exec boundaries. |
| `rodex.runtime` / `process_contracts` / `session_host` | Discover, launch, attach, supervise, and clean up app-server, TUI, proxy, analytics, and runtime paths. |
| `rodex.tmux_session_capability` | Define server/runtime and fully registered session authority; own direct tmux mutation fences and capability-fenced reads. |
| `rodex.tmux_shared_ctrl_c` | Own private session exit and same-client confirmed shared exit under the exact session capability. |
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

`tmux-shared-v1.sock` is multiplexed transport, never session authority. Protocol and
random-incarnation markers identify the server. Creation may claim only a completely
unmarked, empty server; mismatches remain untouched. Each stable XDG/runtime context has
one canonical database and server. SQL makes complete display names unique within its
context; different `XDG_STATE_HOME` or `RODEX_RUNTIME_DIR` contexts do not coordinate.

`TmuxRuntimeCapability` binds socket, server, immutable `$session_id`, primary `%pane_id`,
and runtime; `TmuxSessionCapability` adds registered Rodex, registry, SQL-row, and Codex
identities. The launcher mints it from a checked roster and async actors carry it. Every
action is exact-target fenced; primary actions also require the pane ID. Names and hook
context grant no authority. Predicates run only in direct `if-shell -F`; an owned read
proves capability there, then runs `display-message` for payload alone. Mixing predicate
and payload contexts corrupts literal tmux identifiers such as `%4`.

Global indexed `client-attached` and `client-detached` hooks contain no session target;
they only wake `tmux_sharing_coordinator`. The coordinator verifies server scope, reads
one roster, and conditionally submits each changed count to its full capability. This
avoids tmux 3.2's lost source-session context after destruction. Rodex changes only owned
hook indices and options behind a server-incarnation fence; it never clears session
hooks. Root `C-c` and `C-d` are installed exactly or setup fails closed on conflict. The
`C-c` command owns the exit action: it capability-fences and kills the exact private
session immediately, or the exact shared session after same-client confirmation. Root
`C-d` directly runs tmux `detach-client`, whose current-client command context is the
complete target; it never reaches the TUI. Creation owns `exit-unattached off` before
any session exists plus global and exact-session `destroy-unattached off`, and
reconciliation reapplies the server/session settings. Rodex otherwise performs no
general key/input interception or pane piping.

Discovery reads server, session, control, and registration fields in one tmux snapshot,
then parses the tuple as a unit. All tmux processes cross `tmux_executor`; captured calls
have absolute deadlines, interactive attach owns the terminal, and asynchronous timeout
cancellation kills and reaps its child. Runtime creation replaces Rodex bootstrap
environment fields on every new session. Its capability-gated staged-pane pipeline owns
the whole session environment before the real host may start; shared-server globals are
inputs to remove, not authority. `TmuxStatusPipeline` arbitrates status claims;
animation admission uses exact capability, generation, lease, token, and recovery fences.

Interactive routes print `Rodex attach [name].` before tmux and `Rodex exited [name].`
after return. Tmux's exit line is erased first; TUI I/O stays direct without a PTY proxy.

## Identity and lifecycle

`rodex_sessions.id` is private. Session, registry, runtime, Codex thread, turn, item,
trace-event, and tool-call identities never substitute for one another. See
[SQL_SCHEMA.md](SQL_SCHEMA.md).

New sessions allocate IDs, create detached tmux, start the private host, observe one Codex
root ID, and advertise a `pending` tuple. The immutable session-ID transition lock spans
SQL publication, registration, namespaced tmux rename, and UI setup; competing selectors
cannot use a partial row. Update notice and attach follow.

Existing selectors resolve once; live endpoints must match every advertised identity.
Otherwise Rodex verifies a resumed Codex ID before replacement; a never-saved ID starts
empty and relinks atomically. Opens lock through resolution/replacement, then unlock before
attach. Pending runtimes repair interrupted confirmation; alias failures compensate rename.

## Observer flow and connection lifecycle

```text
App Server event → stateless projection → producer reducer → newest snapshot dispatcher
                                              ↓
tmux pane ← presentation view ← consumer reducer ← length-framed private socket
```

The producer reducer owns events, tombstones, targets, pruning, epoch, and revision. It
publishes bounded snapshots through a newest-only dispatcher. The consumer applies each
revision once and replaces presentation state at epoch/overflow boundaries, so tombstones
cannot resurrect. The view only renders; `ObserverPaneController` owns pane mechanics.
Analytics publication wakes bounded indexed reads; projection bounds text before JSON,
and control frames are capped at 256 KiB.

On primary connection loss, `PrimaryConnectionLifecycleCoordinator` calls every reset
participant despite failures; only the reducer advances observer epoch. SQL transactions
check database identity synchronously; the runtime host has no database watcher.

## Persistence and integrity

One blocking scheduler coalesces protocol activity and feeds authenticated, complete
rollout suffixes to a resident analyzer and trace normalizer. One fenced transaction
publishes checkpoints, statistics, append-only trace, associations, and health. Permanent
source failures park by fingerprint until source change, preserve the last good view, and
cannot affect the TUI. Codex response metadata supplies turn identity; only sequence races
reset cursors, while deterministic conflicts park.

- Rodex, Codex, tmux, user, session, and runtime identities never substitute for one another.
- Related writes use explicit transactions; one fork-safe process-local idle connection
  keeps a bounded WAL generation live between sparse writes without owning a transaction.
- Ordinary reads and mutations are existing-only. Explicit first use alone may create storage.
- Private database/runtime paths validate owner, type, mode, descriptor, and symlink boundaries.
- External tmux mutation requires an explicit capability and an atomic full-tuple fence.
- Runtime path refresh fails closed when continued runtime addressing is unsafe; database
  storage validation occurs synchronously at SQL transaction boundaries.
