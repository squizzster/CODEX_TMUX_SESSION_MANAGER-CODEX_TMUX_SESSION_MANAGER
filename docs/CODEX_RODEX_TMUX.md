# Codex, Rodex, and tmux

Rodex is a match-maker and launcher. Each identity keeps its own meaning:

- **Rodex session:** a random 64-bit ID rendered as exactly 16 lowercase hex
  characters, plus internal `rodex_sessions.id`.
- **Rodex registry:** a separate random 64-bit ID for one database.
- **Rodex runtime:** a random UUID for one current live incarnation.
- **Codex thread/session tree:** separate App Server `thread.id` and `thread.sessionId`;
  they are equal for the managed root thread but must not be conflated for forks.
- **tmux session:** the exact tmux server socket path plus tmux session name.

Rodex session, Rodex registry, and Codex session IDs remain explicitly named and typed.
The tmux endpoint is a
separate operational row joined by `rodex_sessions_id`. Identities are never presented
or stored as another domain's identity.

`rodex _context` is the single machine-readable self-identification pipeline. It uses
the calling process's inherited `TMUX` and `TMUX_PANE` values only to address the live
pane, then authenticates the advertised Rodex, registry, and Codex session IDs against the
current POSIX user's persisted runtime. It reports the current display and tmux names,
permanent and optional user-defined names, registry/database provenance, Codex session ID,
exact tmux socket/session/window/pane address, registration state, and attached-client
snapshot as JSON. Missing, foreign, stale, or mismatched identity fails closed rather
than being inferred or adopted. Private proxy/event sockets and runtime logs remain
implementation details rather than agent context.
New runtimes also report their UUID and whether it matches the persisted incarnation.

## Basic launch pipeline

1. Bare `./rodex` (or explicit `./rodex _create`) validates the `codex` and `tmux`
   executables.
2. tmux starts a small supervisor directly; Rodex never types with `send-keys`.
3. The supervisor starts one private Codex app-server and a transparent Rodex
   WebSocket proxy on short Unix sockets, then connects the inline Codex TUI through
   it with `--no-alt-screen`.
4. Rodex asks that private app-server for its one loaded Codex session ID.
5. One SQLite transaction creates the Rodex/runtime identities, name, user/log, and
   tmux rows.
6. tmux is renamed to the cool name and displays its Rodex identity, tool count, and
   live private/shared attachment state in its status bar.
7. Rodex attaches to the ordinary Codex prompt; forwarded arguments and slash commands
   work.

The empty invocation is the default managed-create command. Every nonempty invocation
outside the exact underscore Rodex command namespace is handed to Codex unchanged,
with one exception: a single existing Rodex name follows the reattachment pipeline
below. Direct passthrough uses process replacement so Codex retains native stdin,
stdout, stderr, signals, and exit status.

`./rodex _help` prints the Rodex command namespace locally. It does not resolve Codex,
tmux, or the session database.

## Scrollback ownership

Rodex configures tmux's history default before the first pane is created: each new pane
inherits a 50,000-line history limit. Mouse configuration remains user-owned and a
Rodex session inherits tmux's global value unless `_mouse` sets a session override. The managed Codex TUI runs
without the alternate screen so rendered conversation rows reach that history.
`Ctrl-b [` enters keyboard copy mode and `q` exits it. `_mouse NAME inherit` removes a
session override without changing any other session or the global configuration.

tmux—not the terminal emulator or WebSocket proxy—owns managed-session scrollback.
The proxy continues to forward protocol frames and selected live events without
screen-scraping, reconstructing terminal rows, or persisting conversation content.

The proxy forwards protocol frames unchanged in both directions, counts unique tool
starts, and fans structured TUI events to bounded live subscribers. The subscribed
primary connection, normally the managed TUI, receives lifecycle, approval, and
user-input requests; short-lived control clients do not become subscribers by reading or
mutating. Each uses a separate upstream App Server connection over private Unix sockets.
tmux user options advertise the sockets and live identities. Tool counts cover one
runtime.

The separate tmux input proxy and completion observer for `/rodex` are retained but
temporarily disabled by `RODEX_TMUX_SLASH_ENABLED`. Runtime status setup removes their
Enter/Tab bindings and pane pipe, so all input currently passes directly to Codex.

## Persistent statistics

Each session host supervises one low-priority analytics subprocess keyed by the Rodex
session ID allocated for that launch. The worker waits for SQL registration,
discovers every Codex session ID retained in that Rodex lineage, and authenticates each
rollout's internal `session_meta` identity. It copies only the final newline-complete
prefix to a private 0600 temporary file and reauthenticates its SHA-256 before publishing.

The worker creates a fresh in-memory `CodexProtocolLibrary`, loads the authenticated
copies, calculates statistics, then closes it. The analyzer owns calculation only;
Rodex owns watching, scheduling, source provenance, retries, health, and persistence.
One transaction publishes a Rodex-owned monotonic revision, the session projection,
the complete `(Codex source, turn_id)` projection set, and exact source descriptors.
Codex-session-ID and prior-revision fences reject stale workers. Health failures commit
separately and never overwrite the last good snapshot.

Fixed statistics are typed scalar columns. The genuinely repeating values are normalized
as seven distribution rows, bounded category/name/count rows, and ordered audit-limit
rows at session or turn scope. No JSON statistics document is persisted: `_stats --json`
reconstructs the analyzer shape deterministically from indexed SQL rows. This keeps every
base count directly available to `SUM`, `GROUP BY`, filtering, and joins.

Analytics is fail-open: import, parsing, calculation, process, or database failure
cannot change the Codex TUI's behavior or exit status. `_stats NAME [--json]`, `_stats
NAME --turn TURN_ID [--source CODEX_SESSION_ID] [--json]`, and `_stats-status NAME` enforce
normal ownership but query SQLite without requiring live Codex, tmux, or analyzer
processes. A runtime started before this feature must end and resume before it has an
analytics sidecar.

## Named reattachment

- `./rodex <cool-name-or-codex-uuid>` resolves a canonical Codex UUID first, then a
  display name, through the same owned integer identity. An unmatched value retains
  ordinary Codex passthrough.
- If its stored tmux endpoint is live, Rodex first verifies its registered Rodex session
  ID, registry ID, and Codex session ID. A missing or mismatched marker fails closed
  without attach or rename.
- If an exact registered runtime was renamed outside Rodex, one unambiguous marker
  match repairs the endpoint; multiple matches are refused.
- If it has ended, Rodex starts a fresh tmux/app-server and asks Codex to resume the
  stored Codex session ID; the observed ID must match before the endpoint is replaced.
- Concurrent opens of one ended name serialize through a private per-session lock and
  converge on one runtime. A short old-writer shutdown handoff is retried in place;
  persistent or wrong-thread writer conflicts remain hard failures.
- If Codex explicitly reports that the ID was never saved, Rodex starts an empty
  Codex runtime and atomically relinks its new ID to the existing Rodex identity.
  Other resume failures and ID mismatches remain hard failures.
- Both routes preserve the Rodex identity and update `last_accessed_at_utc`.
- `_detach <cool-name-or-codex-uuid>` follows the same attach, resume, or recovery
  decision without attaching the caller and prints the active identities as JSON.
- `./rodex _running` lists verified runtimes and reports unverified or unregistered
  sessions separately.
- `./rodex _alias SESSION NAME` sets its portable preferred name; `--force` replaces
  it. A live effective-name change sends exactly one verified `RODEX_AUTO_INFO`
  prompt to the session's single Codex thread, regardless of how many tmux clients
  are attached. Offline and unchanged names do not send one.
- `_send`, idle-based `_wait`, and `_tail` remain compatibility commands.
- `_inspect --json` reports the exact active turn. `_start` and `_steer` accept an
  optional caller-owned `--dispatch ID`, while `_dispatch-status` observes that ID in
  exact App Server thread history. Exact `_wait`, `_interrupt`, and `_result` use a
  caller-supplied turn ID. Every machine command emits one schema-v1 envelope and
  requires the persisted runtime UUID. Results stay App Server-owned rather than
  becoming a second SQLite conversation store.
- A resumed runtime intentionally uses the caller's current working directory. This
  lets a moved home/project/worktree location travel with the human who resumes it;
  Rodex does not permanently pin session identity to its original workspace path.

## Lifecycle

- `Ctrl-b d` detaches while Codex, its app-server, and tmux continue running.
- With the default `C-b` prefix, Rodex displays `CTRL-B MODE` while tmux awaits the
  following command key. It uses tmux's per-client prefix state without intercepting
  input; a fast `Ctrl-b d` therefore still detaches normally. A custom prefix or
  user-owned root `C-b` binding is not replaced.
- In a shared session, one `Ctrl-C` is held as an accidental-exit guard and points to
  `Ctrl-b d` as the detach-only route. The same client must press `Ctrl-C` again within
  two seconds to send the interrupt to Codex, where it may end the TUI for every
  attached client. A private session retains Codex's native `Ctrl-C` behavior.
- A second attached client triggers a five-second shared-arrival animation. Returning
  to one client triggers its private-session counterpart. Both run in a separate
  one-shot process and restore the ordinary status bar without delaying the TUI.
- Exiting the Codex TUI ends its supervisor and private app-server; its cool name can
  transparently resume the saved Codex session later.
- A completely empty Codex TUI may not have saved history. Its cool name recovers by
  starting empty again and replacing only the linked Codex session ID.
- A failure before SQL registration stops the exact new tmux session and leaves no
  partial database row. A host whose pending registration is never confirmed exits;
  one exact matching pending runtime can finish an interrupted confirmation on the
  next command, including when it was launched under a temporary tmux name.
- Scrollback settings apply when a pane is created. A runtime started by an older
  Rodex version must end and resume, or be replaced by a new session, to gain the
  larger history and inline TUI rendering.
- The runtime uses `$XDG_RUNTIME_DIR/rodex` when suitable—normally
  `/run/user/<uid>/rodex`—otherwise `/tmp/rodex-<uid>`. Unix sockets stay there because
  long project paths can exceed Linux socket limits.
- While a session host is alive, it refreshes the runtime root, shared tmux socket, and
  its private sockets and log hourly. A refresh failure ends that runtime rather than
  leaving a detached session that cannot be addressed. Normal cleanup eligibility
  resumes when the live hosts exit.

Exact tmux targets and compensated name transitions preserve the recorded endpoint.
