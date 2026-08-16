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
    │  └── advertises live control sockets and Codex UUID
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
| `rodex_functions` | Own session identity, ownership, naming, and state transitions. |
| `rodex_sql` | Provide transactional SQLite initialization and access. |
| `cool_name` | Allocate and resolve collision-resistant session names. |

The earlier `codex_tmux_session_manager` prototype remains while it converges into Rodex.

## Identity and data model

`rodex_sessions.id` links explicit Rodex/Codex UUID domains and the permanent generated
name. UUIDs are stored losslessly as two signed 64-bit integers.

Separate one-to-one or lookup tables hold:

- the exact tmux socket and session name;
- POSIX user identity and access timestamps;
- generated and optional user-defined cool names.
- latest session and exact-turn statistics plus independently updated worker health;
- every Codex rollout source retained in the Rodex identity's history.

A user-defined name becomes the display name; the generated name remains a permanent
lookup. Detailed rules live in [SQL_SCHEMA.md](SQL_SCHEMA.md), not duplicated here.

## Authoritative lifecycle

### New session (bare `rodex`, `_create`, or `_detach`)

1. Allocate an unregistered Rodex UUID candidate for the pending runtime.
2. Set the shared tmux server's 50,000-line history and mouse-off defaults before creating the detached session and its first pane.
3. Start one private Codex app-server and connect an inline (`--no-alt-screen`) TUI
   through the proxy so rendered conversation rows enter tmux history.
4. Refresh live paths and supervise analytics without blocking the TUI.
5. Observe the single real Codex UUID from that app-server.
6. Transactionally register the identities, initial analytics source, name, user/log, and tmux link.
7. Rename tmux to the display name, configure status, and attach the terminal.

### Existing name

1. Resolve either generated or user-defined name to `rodex_sessions.id`.
2. Enforce ownership using the effective POSIX identity.
3. Attach when the exact stored tmux endpoint is live.
4. Otherwise ask Codex to resume the stored Codex UUID and verify the observed UUID.
5. If Codex explicitly says that UUID was never saved, start empty and atomically
   relink the new Codex UUID; every other resume failure remains fatal.
6. Replace the tmux endpoint before attaching.

`_running` follows the same ownership and live-endpoint rules. `_alias` changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

tmux hooks launch sharing animations in a short-lived background process. Ownership
tokens stop an older animator restoring over a newer one; the owner removes temporary
status overrides and redraws attached clients without delaying tmux or the TUI.

The `/rodex` input proxy and completion observer are retained but temporarily disabled
by `RODEX_TMUX_SLASH_ENABLED` at the runtime configuration boundary. Status setup
removes their pane pipe and tmux Enter/Tab bindings so input passes directly to the
Codex TUI.

When enabled, that proxy consumes only exact `/rodex` input and otherwise forwards keys
unchanged. Its observer uses output only as a redraw wakeup; both remain tested while inactive.

## Persistent analytics

The worker waits for SQL registration, authenticates each lineage rollout against its
Codex UUID, and copies a private final-newline-complete prefix. A fresh in-memory
`CodexProtocolLibrary` analyzes those immutable copies in one chronological pass. Rodex
persists fixed metrics as typed session and turn columns; distributions, named counts,
and ordered limits as relational child rows; exact prefix provenance; and separate worker
health. It stores neither raw events, analyzer storage, nor statistics JSON blobs. Source
SHA-256 and stat/ctime caching detect rewrites. CLI JSON is deterministically rebuilt
from an indexed, transactionally consistent SQL view.
Session projection, complete turn set, source inclusion, and healthy state publish
atomically behind current-Codex UUID and prior-revision fences. Failures preserve the
last good revision and cannot affect TUI behavior or exit status. `_stats`, its exact
`--turn` lookup, and `_stats-status` read SQLite without requiring Codex or tmux.

## Live control

The running tmux session advertises proxy/event sockets and its Codex UUID as ephemeral
user options; they add no durable identity.

`send`, `wait`, and `tail` resolve a name, enforce POSIX ownership, locate the exact
live endpoint, compare stored and advertised Codex UUIDs, then verify that the private
app-server has that exact thread loaded.

`send` starts or steers work. `wait` verifies live state so an older signal cannot
finish a newer wait. `tail` omits token deltas, and bounded subscriber queues prevent a
slow observer blocking the TUI.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit.
- Multi-table statistics reads use one deferred transaction for a consistent view.
- Analytics paths are low-priority, short-transaction, derived-only, and fail-open.
- External tmux changes use exact targets and compensating transitions where needed.
- A live session host keeps its runtime paths fresh and fails closed if that guarantee
  cannot be maintained.
- Session commands use tmux's `=name`; target-pane commands use `=name:`.
- Failure cleanup targets only the runtime created by the failed operation.

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path
checks protect the external boundaries where command grammar and lifecycle are part of
correctness. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md) for the forward design ethos and
[CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for the focused identity/runtime contract.
