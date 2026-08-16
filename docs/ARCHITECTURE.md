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
    │  └── advertises live control sockets and Codex UUID
    ▼
session host ──► Codex TUI ──► protocol proxy ──► Codex app-server
                                │
                                └──► live event tap ──► tail/wait/send clients
```

The TUI remains the normal Codex interface. The tmux status derives its private/shared
state directly from the number of clients attached to that exact session. The proxy
forwards WebSocket frames in both directions, derives a runtime tool-call count, and
fans out selected structured events. It does not screen-scrape or persist conversation
content.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt commands, choose create/attach/resume, and present results. |
| `rodex.runtime` | Own tmux, app-server discovery, attachment, and supervision. |
| `rodex.status_animation` | Render cancellable, one-shot sharing transitions. |
| `rodex.session_host` | Keep one app-server, proxy, foreground TUI, and its runtime paths together. |
| `rodex.control` | Verify and control one exact loaded Codex thread. |
| `rodex.protocol_proxy` | Forward traffic and fan out bounded live event streams. |
| `rodex_functions` | Own session identity, ownership, naming, and state transitions. |
| `rodex_sql` | Provide transactional SQLite initialization and access. |
| `cool_name` | Allocate and resolve collision-resistant session names. |

The earlier `codex_tmux_session_manager` package is a prototype path retained while
its remaining useful behaviour converges into Rodex.

## Identity and data model

`rodex_sessions.id` is the internal lookup identity. Its root row links two explicit
128-bit UUID domains—Rodex and Codex—and the permanent generated cool name. UUIDs are
stored losslessly as two signed 64-bit integers.

Separate one-to-one or lookup tables hold:

- the exact tmux socket and session name;
- POSIX user identity and access timestamps;
- generated and optional user-defined cool names.

A user-defined name becomes the outward display name, while the generated name remains
a permanent alternative lookup. Detailed future schema rules live in
[SQL_SCHEMA.md](SQL_SCHEMA.md); this overview deliberately does not duplicate its DDL.

## Authoritative lifecycle

### New session

1. Start a detached tmux session containing the Rodex session host.
2. Start one private Codex app-server and connect the TUI through the proxy.
3. Refresh every required live pathname immediately and hourly until the host exits.
4. Observe the single real Codex UUID from that app-server.
5. Transactionally create the Rodex UUID, name, user/log, and tmux link.
6. Rename tmux to the display name, configure status, and attach the terminal.

### Existing name

1. Resolve either generated or user-defined name to `rodex_sessions.id`.
2. Enforce ownership using the effective POSIX identity.
3. Attach when the exact stored tmux endpoint is live.
4. Otherwise ask Codex to resume the stored Codex UUID, verify the observed UUID, and
   replace the tmux endpoint before attaching.

`running` and its `sessions` aliases follow the same ownership and live-endpoint rules.
Alias changes use the same naming pipeline and compensate a tmux rename if the database
transition fails.

tmux session hooks launch sharing animations with background `run-shell`. The animator
uses scheduled callbacks in its own short-lived process, so attaching, detaching, tmux,
and the Codex TUI do not wait for its five-second sequence. Each transition replaces a
tmux ownership token; an older animator stops without restoring over a newer one. The
owner finally removes the complete temporary `status-format` and style overrides, then
redraws every attached client against the ordinary Rodex status.

## Live control

The running tmux session advertises its proxy socket, event socket, and observed Codex
UUID as user options. This operational metadata disappears with the runtime and does
not add another durable identity or SQLite relationship.

`send`, `wait`, and `tail` resolve the requested cool name to the existing integer
session ID, enforce POSIX ownership, locate the exact live tmux endpoint, and compare
the advertised Codex UUID with the UUID stored on `rodex_sessions`. The control client
then verifies that the private app-server has that exact thread loaded.

`send` starts an idle turn or steers the observed active turn. `wait` confirms live
thread state before accepting a completion signal, so a queued event from an older turn
cannot finish a newer wait. `tail` emits useful lifecycle events as compact JSON lines
while omitting high-volume token deltas. Subscriber queues are bounded so a slow
observer cannot block the interactive TUI.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit.
- External tmux changes use exact targets and compensating transitions where needed.
- A live session host keeps its runtime paths fresh and fails closed if that guarantee
  cannot be maintained.
- Session commands use tmux's `=name`; target-pane commands use `=name:`.
- Failure cleanup targets only the runtime created by the failed operation.

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path
checks protect the external boundaries where command grammar and lifecycle are part of
correctness. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md) for the forward design ethos and
[CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for the focused identity/runtime contract.
