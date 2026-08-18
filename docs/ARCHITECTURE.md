# Agent instructions

Retain these standards when architecture changes. This is a compact current blueprint,
not a change log. Amend a standard only after an agent suggestion and user agreement.
Consider one independent reviewing agent, critically assess its advice, retain ownership,
and keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex is a local match-maker between a durable Rodex session, one current runtime UUID,
the Codex thread/session tree it represents, and the tmux endpoint hosting it.

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
    │  └── advertises sockets, Rodex/registry/runtime/Codex IDs and registration state
    ▼
session host ─┬► Codex TUI ──► protocol proxy ──► Codex app-server
             │                  ├──► context status ──► tmux base status
             │                  └──► live event tap ──► tail/wait/send clients
             └► analytics worker ──► in-memory analyzer ──► SQLite projections
```

The TUI remains the normal Codex interface and runs inline so rendered output enters tmux's 50,000-line pane history. tmux owns keyboard copy-mode scrollback and derives status privacy from clients attached to that exact session. The proxy forwards WebSocket frames, derives tool and context signals, and fans structured events; it never buffers the screen.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt process arguments/output and orchestrate launch, attach, or Codex passthrough. |
| `rodex.command_contract` | Own command vocabulary, routes, generated help, and machine-command grammar. |
| `rodex.session_commands` / `statistics_commands` / `machine_commands` | Execute the three command domains behind the shared CLI contract. |
| `rodex.runtime` | Own tmux scrollback, app-server discovery, attachment, and supervision. |
| `rodex.process_contracts` | Own typed subprocess configurations and their lossless argv wire forms. |
| `rodex.status_bar` | Own named segments, the authoritative palette, order, and base rendering. |
| `rodex.tmux_status` | Arbitrate base status, animations, and priority-ordered transient claims. |
| `rodex.status_animation` | Render cancellable, one-shot sharing transitions. |
| `rodex.session_host` | Keep one app-server, proxy, foreground TUI, and its runtime paths together. |
| `rodex.analytics` | Authenticate rollout prefixes and supervise fail-open analysis. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.app_server_contract` | Own App Server command, clients, RPC vocabulary, lifecycle messages, and version gate. |
| `rodex.protocol_proxy` | Forward traffic and derive bounded live tool/context signals and event streams. |
| `rodex_registry.identity` | Own distinct Rodex ID domains and lossless signed-BIGINT codecs. |
| `rodex_registry.schema` | Create and exactly verify the complete SQLite schema and registry ID. |
| `rodex_registry.lifecycle` | Own session creation, ownership, naming, and runtime transitions. |
| `rodex_registry.statistics` | Publish and read relational statistics behind identity fences. |
| `rodex_registry.statistics_fields` | Derive scalar SQL layout and codecs from typed statistics records. |
| `rodex_sql` | Separate fail-closed read-only transactions from explicit bootstrap/write transactions. |
| `cool_name` | Allocate and resolve collision-resistant session names. |

## Identity and data model

`rodex_sessions.id` is the private relational key. The public Rodex session identity is a
random unsigned 64-bit value with one canonical 16-character lowercase hexadecimal wire
form. SQLite stores all bits in one signed `BIGINT` using lossless two's-complement mapping.
The Codex session ID uses two signed 64-bit integers; registry identity remains separate.

Separate tables hold registry ID, tmux endpoint, runtime UUID, POSIX owner/log, names,
statistics/worker health, and retained Codex rollout lineage. A display alias never
replaces the permanent generated name. Detailed rules live in [SQL_SCHEMA.md](SQL_SCHEMA.md).

## Authoritative lifecycle

### New session (bare `rodex`, `_create`, or `_detach`)

1. Allocate an unregistered 64-bit Rodex session ID and random runtime UUID.
2. Set the shared tmux server's 50,000-line history before creating the detached session and its first pane; mouse configuration remains user-owned.
3. Start one private Codex app-server and connect an inline (`--no-alt-screen`) TUI through the proxy so rendered conversation rows enter tmux history.
4. Refresh live paths and supervise analytics without blocking the TUI.
5. Observe the single real Codex session ID from that app-server.
6. Advertise Rodex session, registry, runtime, and Codex thread IDs with `pending` state.
7. Transactionally register those identities, analytics source, name, user/log, and tmux link, then mark the runtime `registered`.
8. Rename tmux to the display name, configure status, and attach the terminal.

### Existing name

1. Resolve either generated or user-defined name to `rodex_sessions.id`.
2. Enforce ownership using the effective POSIX identity.
3. Attach only when the exact stored tmux endpoint advertises matching registered Rodex, registry, and Codex identities. An exact pending tuple completes interrupted registration.
4. Otherwise ask Codex to resume the stored Codex session ID and verify the observed ID.
5. If Codex says that ID was never saved, start empty and atomically relink the new Codex
   session ID; every other resume failure remains fatal.
6. Replace the tmux endpoint before attaching.

Named opens hold one private, per-session advisory transition lock across live resolution,
resume, and durable endpoint replacement; it is released before blocking tmux attach.
Concurrent shells converge on the first verified runtime. An exact relocated `pending`
runtime repairs interrupted launch. During shutdown, the host retries only the exact Codex
`active writer` conflict for a bounded interval; persistent or mismatched writers fail.

`_running` follows the same ownership and live-endpoint rules. `_alias` changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

Named immutable segments own ordinary left-status colour and content. Sharing animations and
transient claims use `TmuxStatusPipeline`; priority arbitration prevents a lower-priority
refresh from clearing an active warning. Per-client `client_prefix` preserves fast keys.
Shared `Ctrl-C` requires same-client confirmation; private input and root bindings stay
unchanged; `/rodex` remains disabled.

The base status reads a pane-stable context option from the primary protocol observer. Usage divided by model window supplies context fill; compaction lifecycle animates it. Durable analytics remain downstream; transient restoration returns to the one base format.

## Persistent analytics

The worker authenticates complete rollout prefixes and analyzes private copies in a
fresh in-memory library. Typed session/turn columns, relational children, exact source
provenance, and worker health replace raw events and JSON blobs. A complete projection
publishes atomically behind Codex-session-ID and prior-revision fences; failures preserve the
last good view and cannot affect the TUI. Statistics reads need neither Codex nor tmux.

## Live control

All WebSocket transport uses private Unix-domain sockets. The proven legacy `_send` and
idle-based `_wait` retain their short-lived secondary App Server connection. Additive
machine commands inspect/start/steer/wait/result/interrupt by exact turn ID and emit a
schema-v1 envelope. Dispatch start/steer also carry an opaque caller-owned ID;
`_dispatch-status` maps it to zero, one, or multiple exact thread-history observations
and returns a structured next-command recommendation. They require a matching persisted/live runtime UUID and the
characterized App Server version; results are read live and never copied into SQLite.
Timeout never interrupts. A sent mutation without a reply is explicitly indeterminate
and never silently retried. Live 0.147 experiments show approval and user-input requests
route to the subscribed primary after the short-lived mutation client disconnects;
requests observed in the attached TUI remain user-handled there.

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

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path
checks protect external grammar and lifecycle boundaries. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md)
and [CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for design and runtime contracts.
