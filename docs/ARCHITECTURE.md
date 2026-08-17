# Agent instructions

Retain these standards when architecture changes. This is a compact current blueprint,
not a change log. Amend a standard only after an agent suggestion and user agreement.
Consider one independent reviewing agent, critically assess its advice, and retain
ownership of the result. Keep this file within 150 lines and 10,240 bytes.

# Rodex architecture

Rodex is a local match-maker between three separate identities: a Rodex session, the
real Codex session it represents, and the tmux runtime currently hosting it.

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
    │  └── advertises live sockets, Rodex ID, registry/Codex session IDs, registration state
    ▼
session host ─┬► Codex TUI ──► protocol proxy ──► Codex app-server
             │                  └──► live event tap ──► tail/wait/send clients
             └► analytics worker ──► in-memory analyzer ──► SQLite projections
```

The TUI remains the normal Codex interface and runs inline so rendered output enters tmux's 50,000-line pane history. tmux owns keyboard copy-mode scrollback and derives the
status privacy state from clients attached to that exact session. The proxy forwards
WebSocket frames, counts tools, and fans structured events; it never buffers the screen.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Pass through to Codex or route exact underscore commands and stored names. |
| `rodex.runtime` | Own tmux scrollback, app-server discovery, attachment, and supervision. |
| `rodex.status_animation` | Render cancellable, one-shot sharing transitions. |
| `rodex.session_host` | Keep one app-server, proxy, foreground TUI, and its runtime paths together. |
| `rodex.analytics` | Authenticate rollout prefixes and supervise fail-open analysis. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.protocol_proxy` | Forward traffic and fan out bounded live event streams. |
| `rodex_registry.identity` | Own distinct 64-bit Rodex ID domains and the 128-bit Codex ID codec. |
| `rodex_registry.schema` | Create and exactly verify the complete SQLite schema and registry ID. |
| `rodex_registry.lifecycle` | Own session creation, ownership, naming, and runtime transitions. |
| `rodex_registry.statistics` | Publish and read relational statistics behind identity fences. |
| `rodex_sql` | Provide transactional SQLite initialization and access. |
| `cool_name` | Allocate and resolve collision-resistant session names. |

## Identity and data model

`rodex_sessions.id` is the private relational key. The public Rodex session identity is
a random unsigned 64-bit value with one canonical 16-character lowercase hexadecimal
wire form. SQLite stores all of its bits in one signed `BIGINT` using a lossless
two's-complement mapping. The Codex session ID remains on the owning row as two signed 64-bit
integers. The full registry ID remains a separate database-instance identity.

Separate tables hold the registry ID, tmux endpoint, POSIX owner/log, permanent and
optional display names, statistics/worker health, and retained Codex rollout lineage.
A display alias never replaces the permanent generated name. Detailed rules live in
[SQL_SCHEMA.md](SQL_SCHEMA.md).

## Authoritative lifecycle

### New session (bare `rodex`, `_create`, or `_detach`)

1. Allocate an unregistered 64-bit Rodex session ID candidate for the pending
   runtime.
2. Set the shared tmux server's 50,000-line history before creating the detached
   session and its first pane; mouse configuration remains user-owned.
3. Start one private Codex app-server and connect an inline (`--no-alt-screen`) TUI
   through the proxy so rendered conversation rows enter tmux history.
4. Refresh live paths and supervise analytics without blocking the TUI.
5. Observe the single real Codex session ID from that app-server.
6. Advertise the exact Rodex session ID, registry ID, and Codex session ID with `pending`
   registration state.
7. Transactionally register the identities, initial analytics source, name, user/log,
   and tmux link, then mark the runtime `registered`.
8. Rename tmux to the display name, configure status, and attach the terminal.

### Existing name

1. Resolve either generated or user-defined name to `rodex_sessions.id`.
2. Enforce ownership using the effective POSIX identity.
3. Attach only when the exact stored tmux endpoint advertises the matching registered
   Rodex session, registry, and Codex identities. An exact pending tuple completes
   interrupted registration.
4. Otherwise ask Codex to resume the stored Codex session ID and verify the observed ID.
5. If Codex explicitly says that ID was never saved, start empty and atomically
   relink the new Codex session ID; every other resume failure remains fatal.
6. Replace the tmux endpoint before attaching.

Named opens hold one private, per-session advisory transition lock only across live
resolution, resume, and durable endpoint replacement; the lock is released before the
blocking tmux attach. Concurrent shells therefore converge on the first verified
runtime instead of launching duplicate writers. An exact relocated `pending` runtime
repairs an interrupted launch. During ordinary shutdown, the new session host retries
only the exact Codex `active writer` handoff conflict for a bounded interval while
keeping its single tmux runtime alive. Persistent or mismatched writers still fail.

`_running` follows the same ownership and live-endpoint rules. `_alias` changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

Sharing animations and transient left-status claims run out of process but publish and
restore atomically through `TmuxStatusLeftPipeline`. The base format reads per-client
`client_prefix`, so `CTRL-B MODE` never intercepts a fast key sequence. The guarded
shared `Ctrl-C` binding requires same-client confirmation; private input is unchanged.
User-owned root bindings are preserved. `/rodex` input interception remains implemented,
tested, and disabled at `RODEX_TMUX_SLASH_ENABLED`.

## Persistent analytics

The worker authenticates complete rollout prefixes and analyzes private copies in a
fresh in-memory library. Typed session/turn columns, relational children, exact source
provenance, and worker health replace raw events and JSON blobs. A complete projection
publishes atomically behind Codex-session-ID and prior-revision fences; failures preserve the
last good view and cannot affect the TUI. Statistics reads need neither Codex nor tmux.

## Live control

tmux advertises live sockets, Rodex ID, registry/Codex session IDs, and registration state;
SQL remains authoritative. Pending runtimes expire, while registered endpoints are
never adopted or changed without an exact durable match. Control commands enforce
ownership, marker equality, and the exact thread loaded by the private app-server.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit; WAL mode and
  a busy timeout allow consistent readers to coexist with ordinary analytics writes.
- Multi-table statistics reads use one deferred transaction for a consistent view.
- Analytics paths are low-priority, short-transaction, derived-only, and fail-open.
- External tmux changes use exact targets and compensating transitions where needed.
- A live host keeps runtime paths fresh and fails closed if that cannot be maintained.
- Session commands use tmux's `=name`; target-pane commands use `=name:`.
- Failure cleanup targets only the runtime created by the failed operation.
- Private runtime/database paths validate owner, type, mode, and symlink boundaries.

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path
checks protect the external boundaries where command grammar and lifecycle are part of
correctness. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md) for the forward design ethos and
[CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for the focused identity/runtime contract.
