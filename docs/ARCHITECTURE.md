# Agent instructions

Keep this a current blueprint, not a change log. Amend a standard only after user
agreement, retain clear ownership, and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex binds session/runtime IDs to a Codex thread tree and verified tmux endpoint.

## Runtime shape

```text
user → Rodex CLI → shared tmux → host/PTY gateway → Codex TUI ↔ proxy ↔ app-server
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
| `rodex.managed_session_lifecycle` / `human_messages` / `cool_name` | Session lifecycle, names, collisions and human messages. |
| `rodex.exact_turn_mutation` | Lock, re-resolve and validate exact start/steer/interrupt/mouse/alias operations. |
| `rodex.interaction_pipeline` / `interaction_transport` | Typed targets, intent-preserving hooks, outcomes and one private endpoint. |
| `rodex.session_read_pipeline` / `session_tail` | Verified reads and incremental terminal history. |
| `rodex.process_environment` / `environment_exec` | Exact caller-owned environment at child exec. |
| `rodex.runtime` / `process_contracts` / `session_host` | Discovery, launch, attach, supervision and cleanup. |
| `rodex.terminal_gateway` / `terminal_exec` / `terminal_completion` | PTY, byte pipelines, native projection, inline display and restoration. |
| `rodex.terminal_input` / `input_interceptor_config` / `input_interceptor_presentation` | Configured live/Enter rules, editing, submission, release and helper text. |
| `rodex.tmux_session_capability` | Server/runtime/session authority and exact read/mutation fences. |
| `rodex.tmux_shared_ctrl_c` | Exact private exit or same-client confirmed shared exit. |
| `rodex.tmux_sharing_coordinator` | Turn hook wakeups into exact roster transitions. |
| `rodex.tmux_status` / `status_animation` | Arbitrate status claims and render one already-admitted transition. |
| `rodex.status_animation_admission` | Own tmux-native generation, pending event, lease, handoff, and watchdog recovery. |
| `rodex.observer_projection` | Statelessly validate and bound App Server fields for observation. |
| `rodex.observer_state` | Identity, active state, tombstones, pruning, revisions and epochs. |
| `rodex.agent_observer` | Observer coordination, snapshot transport and view. |
| `rodex.observer_pane` / `pane_control` | Interaction adapters and exact pane mechanics. |
| `rodex.terminal_presentation` | Observer bootstrap/live/SQL text through the pipeline before stdout. |
| `rodex.primary_connection_lifecycle` | Isolate primary-connection resets and terminal runtime-shutdown interrupts. |
| `rodex.analytics` / source readers | Authenticate bounded rollout suffixes and supervise fail-open analysis. |
| `rodex_registry.agent_trace_contract` / writer / reader | Normalize traces, transactional append and bounded reads. |
| `rodex_registry.execution` / `statistics` | Own canonical lineage, publication orchestration, and relational projections. |
| `rodex_registry.schema` | Generate, install when authorized, and attest the complete relational catalog. |
| `rodex_sql` | Private paths, storage identity, transactions, WAL lifetime and natural keys. |

## Shared tmux capability boundary

`tmux-shared-v2.sock` is multiplexed transport, never session authority. Protocol and
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

Global indexed attach/detach hooks only wake `tmux_sharing_coordinator`. It verifies the
server, reads one roster and submits changed counts under full capability, avoiding
tmux 3.2's lost source-session context after destruction. Rodex fences its own indexed
hooks/options and never clears session hooks. Root key conflicts fail initialization. The
`C-c` command owns the exit action: it capability-fences and kills the exact private
session immediately, or the exact shared session after same-client confirmation. Root
`C-d` directly runs tmux `detach-client`, whose current-client command context is the
complete target; it never reaches the TUI. Creation owns `exit-unattached off` before
any session exists plus global and exact-session `destroy-unattached off`, and
reconciliation reapplies the server/session settings. Other keys enter the session-owned
terminal pipeline; no tmux Enter binding, synthetic tmux keys or pane piping is used.

Discovery parses server/session/control/registration as one snapshot. Every tmux process
crosses `tmux_executor`: captured calls have deadlines; async cancellation reaps its child.
The staged-pane pipeline prepares the exact caller environment before host startup;
shared-server globals and Rodex bootstrap fields are not authority. `TmuxStatusPipeline`
arbitrates status; animation admission owns capability/generation/lease/token/recovery fences.

Interactive routes print `Rodex attach [name].` before tmux and `Rodex exited [name].`
after return. Tmux's exit line is erased first. One host-owned PTY adapts all TUI I/O;
attachers never create separate input owners. Each interception config owns live/Enter
expressions, completion/helper text and command list; unmatched Enter returns to native.
Native prefix/cursor confirmation is bounded at handoff, never polled in the background.
Unknown editor state stays native. Inline display uses terminal DISPLAY_STATE; replies use MESSAGE(false).

## Identity and lifecycle

`rodex_sessions.id` is private. Session, registry, runtime, Codex thread, turn, item,
trace-event, and tool-call identities never substitute for one another. See
[SQL_SCHEMA.md](SQL_SCHEMA.md).

ALPHA hosts require complete current identity and protocol fields, without adapters.

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
cannot resurrect. The interaction pipeline admits pane work; `TmuxPaneController` owns mechanics.
Analytics publication wakes bounded indexed reads; projection bounds text before JSON,
and control frames are capped at 256 KiB.
The complete [interaction path inventory](INTERACTION_PATHS.md) distinguishes chat, status, lifecycle and storage owners.

On primary connection loss, `PrimaryConnectionLifecycleCoordinator` calls every reset
participant despite failures; only the reducer advances observer epoch. SQL transactions
check database identity synchronously; the runtime host has no database watcher.

## Persistence and integrity

One scheduler feeds authenticated rollout suffixes to the analyzer and trace normalizer.
One fenced transaction publishes checkpoints, statistics, trace, associations, and health.
Source failures park by fingerprint until change and preserve the last good view. Codex
metadata supplies turn identity; only sequence races reset cursors. Failures cannot affect the TUI.

- Related writes use explicit transactions; one fork-safe process-local idle connection
  keeps a bounded WAL generation live between sparse writes without owning a transaction.
- Ordinary reads and mutations are existing-only. Explicit first use alone may create storage.
- Private database/runtime paths validate owner, type, mode, descriptor, and symlink boundaries.
- External tmux mutation requires an explicit capability and an atomic full-tuple fence.
- Runtime path refresh fails closed when continued runtime addressing is unsafe; database
  storage validation occurs synchronously at SQL transaction boundaries.
