# Agent instructions

Keep this a current blueprint, not a change log. Amend a standard only after user
agreement, retain clear ownership, and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex binds session/runtime IDs to a Codex thread tree and verified tmux endpoint.

## Runtime shape

```text
user → Rodex CLI → rodexd-v2.sock → one shared Python daemon
         │                              ├── runtime A → tmux-A → TUI ↔ proxy ↔ app-server
         ├──► SQLite registry           ├── runtime B → tmux-B → TUI ↔ proxy ↔ app-server
         ├──► _cat / _tail              └── one analytics coordinator → trace/stats → SQLite
         └──► bounded update check ───────────────────────────────────────► TUI warning
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
| `rodex.managed_session_lifecycle` / `human_messages` / `cool_name` | Session lifecycle, names, collisions and human messages. |
| `rodex.exact_turn_mutation` | Lock, re-resolve and validate exact start/steer/interrupt/mouse/alias operations. |
| `rodex.interaction_pipeline` / `interaction_transport` | Typed targets, intent-preserving hooks, outcomes and one private endpoint. |
| `rodex.session_read_pipeline` / `session_tail` | Verified reads and incremental terminal history. |
| `rodex.process_environment` / `environment_exec` | Exact caller-owned environment at child exec. |
| `rodex.daemon` / `daemon_client` / `terminal_bridge` | Own one daemon socket, exact runtime reservations, pane-TTY handoff and runtime threads. |
| `rodex.runtime` / `process_contracts` / `process_guard` / `process_receipts` | Stage runtimes, supervise exact native children and reconcile daemon crashes. |
| `rodex.runtime_endpoint` / `runtime_peer` | Exclusive socket lifetime and connected runtime/process identity. |
| `rodex.terminal_gateway` / `terminal_exec` / `terminal_surface` / `presentation_policy` | PTY, native projection, typed display policies and restoration. |
| `rodex.tmux_session_capability` | Server/runtime/session authority and exact read/mutation fences. |
| `rodex.tmux_shared_ctrl_c` | Native originating-client admission, guarded private exit and shared detach. |
| `rodex.tmux_sharing_coordinator` / status modules | Convert hook wakeups into fenced roster and display transitions. |
| Observer projection/state/pane modules | Validate events, reduce state and perform exact pane mechanics. |
| `rodex.primary_connection_lifecycle` | Isolate primary-connection resets and terminal runtime-shutdown interrupts. |
| `rodex.analytics.SharedAnalyticsCoordinator` / source readers | Serialize every runtime's fail-open analytics through one daemon thread. |
| `rodex_registry.agent_trace_contract` / writer / reader | Normalize traces, transactional append and bounded reads. |
| `rodex_registry.execution` / `statistics` | Own canonical lineage, publication orchestration, and relational projections. |
| `rodex_registry.schema` | Generate, install when authorized, and attest the complete relational catalog. |
| `rodex_sql` | Private paths, storage identity, transactions, WAL lifetime and natural keys. |

## Runtime isolation boundary

One runtime owns one `tmux-v4-<runtime-id>.sock` server. Native pane movement cannot
cross server boundaries. Creation claims only an empty server with every ownership
marker absent. The attempt carries its original server nonce through startup and
cleanup; a refused claim cannot rediscover an incumbent's destruction authority.
One canonical database owns names and registered incarnations across these servers.

`TmuxRuntimeCapability` binds socket, server, immutable `$session_id`, primary `%pane_id`,
and runtime; `TmuxSessionCapability` adds registered Rodex, registry, SQL-row, and Codex
identities. The launcher mints it from a checked roster and async actors carry it. Every
action is exact-target fenced; primary actions also require the pane ID. Names and hook
context grant no authority. Predicates run only in direct `if-shell -F`; an owned read
proves capability there, then runs `display-message` for payload alone. Mixing predicate
and payload contexts corrupts literal tmux identifiers such as `%4`.

Indexed client hooks only wake the sharing coordinator, which verifies one roster and
submits changes under full capability. Root `C-c` kills a guarded private runtime or
detaches its originating shared client; `C-d` uses tmux's current-client detach. Creation
sets `exit-unattached off` and `destroy-unattached off`. Other keys enter the terminal
pipeline; Rodex uses no tmux Enter binding, synthetic keys or pane piping.

Discovery compares the session snapshot with a guarded primary-pane read. Every tmux process
crosses `tmux_executor`; calls have deadlines and cancellation reaps the child.
The staged pane starts a one-shot bridge. The daemon admits its same-uid peer PID, exact
primary pane, TTY descriptor, server nonce and runtime reservation before starting the
runtime thread. Caller-owned environment crosses the reservation; tmux-owned values come
only from the admitted bridge process. `TmuxStatusPipeline`
arbitrates status; animation admission owns capability/generation/lease/token/recovery fences.

The daemon acceptor blocks on its Unix listener and explicit shutdown event; it has no accept timer.
Runtime supervisors block through their gateways until terminal readiness, child exit, an exact deadline, or explicit
registration, diagnostic, resize, presentation, or stop wake; owning runtimes revalidate tmux resize hints.

Interactive routes expose the attached client as `rodex_<display_name>`, print
`Rodex attach [name].` before tmux, then classify the observed runtime after return:
`Rodex detach [name].` while it remains live or `Rodex exited [name].` after it ends.
Tmux's exit line is erased first. One daemon-runtime PTY adapts all TUI I/O;
attachers never create input owners. Its gateway blocks on terminal readiness, an exact
child `pidfd`, input-frame deadlines and wake-only state notifications; idle sessions do
not run a fixed relay poll. Interception config owns live/Enter expressions, menus and
typed actions; unmatched input stays native. Prefix/cursor confirmation occurs only at
handoff. DISPLAY_STATE draws menus; light selects root commentary structurally while
preserving the hidden native TUI, and dark redraws that projection in place.

## Identity and lifecycle

`rodex_sessions.id` is private. Session, registry, runtime, Codex thread, turn, item,
trace-event, and tool-call identities never substitute for one another. See
[SQL_SCHEMA.md](SQL_SCHEMA.md).

ALPHA hosts require complete current identity and protocol fields, without adapters.

New sessions allocate IDs, create detached tmux, reserve the runtime in the shared daemon,
hand off the exact pane TTY, observe one Codex root ID, and advertise a `pending` tuple.
The immutable session-ID transition lock spans
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

The producer owns identity, events, tombstones, epochs and revisions. A newest-only
dispatcher sends bounded snapshots; the consumer applies each revision once and resets
at epoch/overflow boundaries. The [interaction inventory](INTERACTION_PATHS.md) records
the chat, status, lifecycle and storage owners.

On primary connection loss, `PrimaryConnectionLifecycleCoordinator` calls every reset
participant despite failures; only the reducer advances observer epoch. SQL transactions
check database identity synchronously; the daemon has no database watcher.

## Persistence and integrity

One daemon-owned coordinator fairly schedules every active runtime on one thread. Each
runtime retains separate cursors, analyzers and SQL health rows, while all `poll_once`
work passes through that singular pipeline. Protocol bursts are coalesced; overflow asks
for a full reconcile. One fenced transaction publishes checkpoints, statistics, trace,
associations, and health.
Source failures park by fingerprint until change and preserve the last good view. Codex
metadata supplies turn identity; only sequence races reset cursors. Failures cannot affect the TUI.

- Related writes use explicit transactions; one fork-safe process-local idle connection
  keeps a bounded WAL generation live between sparse writes without owning a transaction.
- Ordinary reads and mutations are existing-only. Explicit first use alone may create storage.
- Private database/runtime paths validate owner, type, mode, descriptor, and symlink boundaries.
- External tmux mutation requires an explicit capability and an atomic full-tuple fence.
- Exact App Server and TUI process-group receipts survive daemon failure. A new daemon
  verifies PID, start time, uid and process group before retiring an orphan; native
  children also arm Linux parent-death signals.
- Runtime path refresh fails closed when continued runtime addressing is unsafe; database
  storage validation occurs synchronously at SQL transaction boundaries.
