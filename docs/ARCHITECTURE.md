# Rodex architecture

Rodex is a local match-maker between three separate identities: a Rodex session, the
real Codex session it represents, and the tmux runtime currently hosting it.

## Runtime shape

```text
user terminal
    │
    ▼
Rodex CLI ───────────────► SQLite registry
    │
    ▼
private tmux server
    │
    ▼
session host ──► Codex TUI ──► Rodex protocol proxy ──► Codex app-server
```

The TUI remains the normal Codex interface. The proxy forwards WebSocket frames in
both directions and currently derives only a runtime tool-call count. It does not
screen-scrape or persist conversation content.

## Component boundaries

| Component | Responsibility |
|---|---|
| `rodex.cli` | Adapt commands, choose create/attach/resume, and present results. |
| `rodex.runtime` | Own tmux, app-server discovery, attachment, and supervision. |
| `rodex.session_host` | Keep one app-server, proxy, and foreground TUI together. |
| `rodex.protocol_proxy` | Forward protocol traffic and derive live counters. |
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
3. Observe the single real Codex UUID from that app-server.
4. Transactionally create the Rodex UUID, name, user/log, and tmux link.
5. Rename tmux to the display name, configure status, and attach the terminal.

### Existing name

1. Resolve either generated or user-defined name to `rodex_sessions.id`.
2. Enforce ownership using the effective POSIX identity.
3. Attach when the exact stored tmux endpoint is live.
4. Otherwise ask Codex to resume the stored Codex UUID, verify the observed UUID, and
   replace the tmux endpoint before attaching.

`running` follows the same ownership and live-endpoint rules. Alias changes use the
same naming pipeline and compensate a tmux rename if the database transition fails.

## Integrity boundaries

- Rodex, Codex, tmux, users, and names never substitute identities for one another.
- SQLite relationships, natural keys, and cardinality are enforced by constraints.
- Related database changes use `BEGIN IMMEDIATE` and commit as one unit.
- External tmux changes use exact targets and compensating transitions where needed.
- Session commands use tmux's `=name`; target-pane commands use `=name:`.
- Failure cleanup targets only the runtime created by the failed operation.

Mocked tests carry domain invariants quickly. Real SQLite, tmux, proxy, and launch-path
checks protect the external boundaries where command grammar and lifecycle are part of
correctness. See [CODE_CONCEPTS.md](CODE_CONCEPTS.md) for the forward design ethos and
[CODEX_RODEX_TMUX.md](CODEX_RODEX_TMUX.md) for the focused identity/runtime contract.
