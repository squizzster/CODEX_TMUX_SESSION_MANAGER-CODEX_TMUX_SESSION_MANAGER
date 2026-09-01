# Codex, Rodex, and tmux

Rodex is a local session harness: user-facing Codex compatibility comes first, and the
same durable runtime then bridges human tmux attachment with authorized observation and
exact-turn automation. Each identity keeps its own meaning:

- **Rodex session:** a random 64-bit ID rendered as exactly 16 lowercase hex
  characters, plus internal `rodex_sessions.id`.
- **Rodex registry:** a separate random 64-bit ID for one database.
- **Rodex runtime:** a distinct random 64-bit ID, rendered as exactly 16 lowercase hex
  characters, for one current live incarnation.
- **Codex thread/session tree:** separate App Server `thread.id` and `thread.sessionId`;
  they are equal for the managed root thread but must not be conflated for forks.
- **tmux runtime capability:** the exact socket, random server incarnation, immutable
  `$session_id`, primary `%pane_id`, and runtime ID; registered authority also carries
  Rodex session, registry, internal SQL-row, and Codex identities.

Rodex session, Rodex registry, and Codex session IDs remain explicitly named and typed.
The tmux endpoint is a
separate operational row joined by `rodex_sessions_id`. Identities are never presented
or stored as another domain's identity.

`rodex _context` is the single machine-readable self-identification pipeline. It uses
the calling process's inherited `TMUX` and `TMUX_PANE` values only to address the live
pane, then authenticates the advertised Rodex, registry, and Codex session IDs against the
current Linux user's persisted runtime. It reports the current display and tmux names,
permanent and optional user-defined names, registry/database provenance, Codex session ID,
exact tmux socket/session/window/pane address, registration state, and attached-client
snapshot as JSON. Missing, foreign, stale, or mismatched identity fails closed rather
than being inferred or adopted. Private proxy/event sockets and runtime logs remain
implementation details rather than agent context. The result includes `runtime_id` and
whether it matches the persisted incarnation.

Every process invocation passes through one application control plane. The Rodex command
contract claims exact local commands first; the declarative Codex 0.150.1 CLI contract
then classifies native interactive syntax or direct passthrough once. The selected direct,
selector, or runtime preparation branch supplies one domain executor. `rodex.cli` only
composes these contracts and process dependencies. A matching bare selector becomes one
typed owned-session identity before lifecycle work; an unregistered canonical Codex UUID
becomes a distinct candidate for the transient App Server persistence check.

## Basic launch pipeline

1. Bare `./rodex`, explicit `./rodex _create`, or characterized native interactive
   Codex syntax validates the `codex` and `tmux` executables.
2. tmux starts a small supervisor directly; Rodex never types with `send-keys`.
3. The supervisor starts one private Codex app-server and the Rodex WebSocket proxy on
   short Unix sockets, then connects the inline Codex TUI through it with
   `--no-alt-screen` and Codex's interactive startup updater disabled.
4. Rodex asks that private app-server for its one loaded Codex session ID.
5. Under the unregistered immutable Rodex session-ID transition lock, one SQLite
   transaction creates the Rodex/runtime identities, canonical root-thread membership,
   name, user/log, and tmux rows.
6. While still holding that lock, Rodex confirms the complete registered capability,
   renames tmux to the unique display name, and configures status/input safety. A competing
   selector cannot attach to the intermediate row.
7. Rodex carries that capability to tmux's immutable `$session_id` and attaches
   to the ordinary Codex prompt; an initial prompt and interactive options have already
   reached that TUI unchanged and exactly once.

The checkout launcher and installed shim execute the project's `.venv/bin/rodex`
entrypoint directly. At the process boundary, `rodex.process_environment` compares an
active `VIRTUAL_ENV` with Rodex's own interpreter prefix. An exact match is bootstrap
state: Rodex removes it, its prompt and uv recursion marker, and every matching `.venv/bin`
entry from `PATH`. A different caller-owned virtualenv is preserved. Direct Codex
process replacement and transient App Server checks use this same prepared environment.

Runtime creation supplies the prepared environment to the tmux client so a fresh shared
server starts clean. Every new session also overrides `PATH` and the virtualenv markers;
values absent from the caller are unset for the first host and marked removed from the
session before later panes can inherit them. This per-session contract prevents a shared
tmux server started by older Rodex code from reintroducing stale bootstrap values. The
session host passes the resulting environment explicitly to both App Server and TUI.

Immediately before attachment, Rodex compares `codex --version` with the cached result
of a bounded `npm view @openai/codex version` lookup. When a newer stable release exists,
it sends the primary TUI a native `warning` notification through a private Rodex-only
proxy endpoint. That endpoint terminates at the proxy: it opens no upstream App Server
connection, emits no protocol event to subscribers, persists no thread content, and
starts no model turn. Rodex never installs an update, and lookup or delivery failure
never prevents attachment. A nonblocking cross-process claim admits only one npm refresh
for an exact stale cache; contenders use the latest valid cached value without waiting.

The empty invocation is the default managed-create command. Current
`codex [OPTIONS] [PROMPT]` syntax follows the same managed path with its original argv.
A sole bare token first follows the name/UUID selector pipeline and becomes an initial
prompt when unresolved; `rodex -- TOKEN` bypasses selector and subcommand interpretation.
Current Codex subcommands, help/version, external remote options, malformed current
syntax, multiple positionals, and unknown option-shaped invocations use direct process
replacement, retaining native stdin, stdout, stderr, signals, and exit status.

`./rodex _help` prints the Rodex command namespace locally. It does not resolve Codex,
tmux, or the session database.

## Tmux execution boundary

`rodex.tmux_executor` is the only production boundary that starts a tmux process. An
executor binds one tmux binary and server socket; all callers use the same
`run(command_arguments, mode=..., output=...)` entry. Captured mode either returns text
or discards stdin/stdout/stderr and always has an absolute deadline: one second by
default and five seconds for ordinary runtime-launcher commands. Nonzero exits, timeout,
and process unavailability return one normalized result. Captured runtime creation may
supply an explicit environment; interactive attachment uses the same entry with direct
terminal ownership, an explicit environment, no capture, and its natural lifetime. The
asynchronous executor also exposes only `run`; deadline cancellation kills and reaps its
one child.

Status publication, input guards, observer-pane control, registration checks, animation,
rename, and attach all use that boundary. Domain
components may assemble tmux arguments or atomic `if-shell` command sequences, but they
never construct or execute the tmux process prefix themselves.

## Shared tmux authority

All managed sessions intentionally multiplex through the per-user versioned
`tmux-shared-v1.sock`. This is analogous to many clients sharing one Unix socket: the
socket selects a server but grants no session authority. Rodex records server-scope
protocol and random incarnation markers. Only creation may claim a completely unmarked
server, and only while it has no session; an unmarked nonempty or incompatible server is
left untouched.

`TmuxRuntimeCapability` binds the owning host to its socket, server incarnation, immutable
tmux `$session_id`, primary `%pane_id`, and Rodex runtime. `TmuxSessionCapability` adds
registered Rodex session, registry, SQL-row, and Codex identities. Discovery retrieves a
coherent server/session/control snapshot. The launcher mints external authority only
after a complete roster uniqueness check; async workers carry that already-minted
capability. Every terminal action repeats its applicable tuple at the exact target;
primary-pane actions also require its immutable `%pane_id`. Names and process context
are addresses only.

Under one stable per-user XDG/runtime context, Rodex uses one canonical database and one
shared tmux server. Display-name uniqueness covers every session recorded in that
database, and the live tmux name is exactly the Rodex display name. A successful
new-session transaction reserves its generated name against permanent names and aliases,
so a later session using the same database cannot receive it. A different
`XDG_STATE_HOME` is a separate database/name boundary; a different `RODEX_RUNTIME_DIR`
is a separate shared-server boundary. Rename and attach keep using `$session_id`, so
later name reuse cannot redirect an operation. The database ID remains an internal
capability field that detects stale or replaced canonical storage; it never alters the
name.

tmux 3.2 may lose the source session from a `client-detached` hook after that session is
destroyed. Rodex's indexed global client hooks consequently contain no authority and do
not target any session: they wake a coordinator. The coordinator verifies the server,
reads the complete registered roster, and submits each changed attachment count through
that session's full capability. Rodex changes only its owned global hook slots and never
removes session-local hooks. The shared root `C-c` guard is installed and re-read exactly;
a pre-existing non-Rodex binding or ownership change fails initialization rather than
silently disabling the guard. There is no general input interception or `pipe-pane`.

All of these checks happen at the operation boundary. Rodex does not monitor tmux or the
filesystem as an IDS and uses no inotify watcher or real-time surveillance loop. tmux has
no conditional bind-if-absent primitive, so a same-uid external key change can race the
last absence check and be overwritten by Rodex's bind. Readback detects a competing
change that wins afterward, but cannot prove absence at the bind instant; coordinate
same-uid tmux configuration while Rodex initializes.

## Scrollback ownership

Rodex configures tmux's history default before the first pane is created: each new pane
inherits a 50,000-line history limit. Mouse configuration remains user-owned and a
Rodex session inherits tmux's global value unless `_mouse` sets a session override. The managed Codex TUI runs
without the alternate screen so rendered conversation rows reach that history.
`Ctrl-b [` enters keyboard copy mode and `q` exits it. `_mouse NAME inherit` removes a
session override without changing any other session or the global configuration.

tmux—not the terminal emulator or WebSocket proxy—owns managed-session scrollback.
`_cat` reads only the capability's immutable primary Codex pane as one finite snapshot
through the verified live-session read pipeline; selecting an observer pane cannot
redirect it. Standard tools select from the result, for example
`rodex _cat NAME | head -n 10` or `rodex _cat NAME | tail -n 10`. `_tail NAME` uses
the same verified tmux source but remains open: it prints the selected recent lines,
emits rows entering committed history immediately, and emits stable visible changes
after three 0.4-second captures. Its plain-text cursor excludes the current Codex
`Working`/background status region and live composer, avoiding partial prompt fragments,
timer duplication, ANSI replay, and screen-clearing side effects. Familiar `-n`,
`--lines`, and `-NUM` selection forms change the initial output; following is the
command's default behavior.

`_events` uses the same identity-resolution boundary but reads a genuinely different
source: it remains open to emit selected future protocol events as JSON lines. `_tail`
is useful to a person or agent observing readable progress; `_events`, `_inspect`,
exact `_wait`, and `_result` carry machine lifecycle truth.
The proxy continues to forward ordinary protocol frames and selected live events
without screen-scraping, reconstructing terminal rows, or persisting conversation
content.

The proxy forwards ordinary protocol frames unchanged in both directions, counts unique
tool starts, and fans structured TUI events to bounded live subscribers. Its sole
Rodex-local exception is the update-notice endpoint: a notice becomes a downstream-only
native warning on the subscribed primary TUI and therefore a TUI-owned scrollback line
that survives redraw. For the status bar, the exact primary `thread/started` rollout path
also supplies appended token snapshots between the App Server's turn-boundary usage
notifications; the shared coordinator preserves compaction-animation priority. The
follower checks cheap metadata before its bounded boundary hash, backs idle waits off
from 0.25 to 2 seconds, and wakes early on existing exact-thread protocol events. The
subscribed primary connection, normally the managed TUI,
receives lifecycle, approval, and user-input requests; short-lived control clients do
not become subscribers by reading or mutating. Each ordinary client uses a separate
upstream App Server connection over private Unix sockets. tmux user options advertise
the sockets and live identities. Tool counts cover one runtime.

## Live agent observer pane

An exact primary-thread `item/started → subAgentActivity(kind=started)` event enters the
dedicated live agent observer pipeline. The session host creates or reuses one marked
top-third pane while preserving the lower Codex pane's focus. That pane directly runs
`rodex.agent_observer`; tmux input is disabled, so it is a presentation surface rather
than a shell. It consumes the App Server's exact agent identity, path, activity kind, and
completed tracked-agent messages plus typed turn evidence from SQLite.

The current Codex App Server emits a `collabAgentToolCall` naming the exact collaboration
tool and a `subAgentActivity` carrying lifecycle and target under the same call ID. Rodex
correlates those items without treating generic `interacted` as a tool name. The view
therefore distinguishes `spawn_agent` (new thread and turn), `followup_task` (same thread,
new turn), and `send_message` (same thread and current turn). Once durable lineage
arrives, a spawn is identified as `NEW CLEAN AGENT` or `NEW INHERITED AGENT`; a follow-up
is identified as `SAME AGENT · NEW TURN`. A missing or unsupported call correlation is
reported as unresolved rather than guessed.

When the App Server supplies the collaboration `prompt`, the private live path renders
it exactly as `DELEGATED TASK`, `FOLLOW-UP TASK`, or `MESSAGE`. The authenticated rollout
and SQLite retain encrypted-body metadata rather than another plaintext copy. If live
plaintext is unavailable, the pane reports the encrypted payload as unavailable and
never reconstructs it from the child's behaviour or reply.

Separately, the primary App Server stream supplies the completed parent `userMessage`.
The session host keeps only the latest exact root-turn message in memory and, when that
same turn requests an agent, sends its unchanged text to the pane as
`ROOT TURN REQUEST · exact user message`. This is explicitly root-turn provenance, not
the collaboration payload. Without the exact same-turn message, the observer presents
`ROOT TURN REQUEST UNAVAILABLE`. Tracked-child commentary and final
responses appear as agent-attributed `AGENT UPDATE` and `AGENT ANSWER`. Message text is
bounded before transport, has no fixed display width, and wraps at the tmux pane edge;
terminal control sequences are removed.

`observer_projection` statelessly validates and bounds collaboration invocations,
sub-agent lifecycle, root-request context, and tracked-child messages. A producer
`ObserverStateReducer` is the sole owner of identity keys, active events, tombstones,
target pruning, revision, and primary-connection epoch. Its single-slot dispatcher owns
transport only and sends the newest complete bounded snapshot. The observer process uses
a consumer reducer with the same identity rules; it applies each revision once and
replaces presentation-derived state on epoch or overflow boundaries. Consequently a
retired target cannot reappear when an unrelated later snapshot arrives.
Each runtime derives a distinct private control socket from its exact protocol-event
socket. Events cross it as length-framed Unix stream messages. Projection bounds free
text before JSON encoding and caps each frame at 256 KiB; the observer rejects a
lifecycle or request event whose root identity does not match. Root request and collaboration prompt
text cross that private socket after pane startup, so it does not enter the observer
process arguments and is not duplicated as a SQLite plaintext body. The direct
event-stream subscription supplies runtime liveness only.

`AgentObserverView` owns presentation correlation and rendering only.
`ObserverPaneController` owns only validation, location, and creation of the
input-disabled tmux pane, using bounded executor calls. On loss of the primary App Server
connection, `PrimaryConnectionLifecycleCoordinator` independently resets context,
event-tap, and observer participants and completes the transition even if one participant
fails. The reducer alone advances the serialized observer epoch. This prevents one broken
reset from preserving another participant's stale connection state.

The analytics worker wakes the observer only after a durable publication commit, so
indexed cursor reads need no polling timer. The observer reads only active or
terminal-pending exact agent turns in one bounded read-only transaction and retires each
completed presentation afterward. Committed metrics summarize actions,
commands, file changes, web operations, queries, result records, compactions, and token
use; each exact `(agent thread, agent turn)` owns separate progress, request, token, and
completion state. Unbound turn-producing requests to one existing agent remain in
invocation order until their distinct turns become observable, preventing delayed
earlier events from acquiring a later follow-up's human request. A `send_message`
interaction creates no pending target turn, cannot acquire a later turn, and receives no
terminal recap. Natural-width progress blocks never depend on moving the terminal cursor
across wrapped rows. Completion repeats the invocation semantics and exact root-request
context so a short pane still leaves a useful handoff in tmux history. The pane survives
agent completion for reuse and exits when the runtime event stream closes. Parent
messages from another root or turn, developer and system
instructions, command text, tool payloads, output bodies, and hidden reasoning remain
outside the display contract.

## Persistent analytics and agent trace

Each session host supervises one low-priority analytics subprocess keyed by its Rodex
session. A blocking scheduler coalesces protocol activity until 0.5 seconds of quiet or
five seconds of continuous work. Cold lineage recovery follows exact event-named thread
UUIDs first. When historical spawn output lacks a UUID, one startup-only fallback scans
regular JSONL files in the root UUIDv7 three-day window, reads only first metadata lines,
and accepts the authenticated parent closure. Cached resident sources then consume only
newline-complete suffixes; ordinary wakes never repeat discovery or reload unchanged
prefixes.

One resident analyzer and stateful trace normalizer own calculation for the worker
lifetime. Direct-parent topology is staged before a new child. Catch-up, stale
compare-and-set recovery, and clean replay have finite windows; exhausting one parks the
work for a later event instead of polling or looping. Rodex owns scheduling,
authenticated rollout provenance, bounded recovery, health, and persistence.
Response-item turn scope comes from Codex's nested passthrough metadata, keeping a
collaboration function call and its `SubAgentActivity` on one canonical parent turn.
Publication-sequence races reload SQL/cursors; deterministic semantic conflicts publish
degraded health and park by authenticated source fingerprint.

Canonical thread, turn, item, and tool-call identities outlive replaceable statistics.
Session membership, current-root selection, rollout source, and sub-agent lineage are
separate relational rows. One registry/session/runtime/Codex-fenced transaction commits
accepted source checkpoints, changed statistics metrics, the append-only typed trace
suffix, and healthy state. Statistics and trace publication heads are independent
compare-and-set domains. Trace totals advance from persisted counts rather than
recounting history; failures preserve every last-good projection and cannot affect the
TUI.

Agent requests are also canonical rows. Each joins the exact parent user-message
reference, collaboration tool request, spawn/follow-up activity, activity scope, and
target thread, with an independent opaque public identity. A separate association row
links each request FIFO to that target agent's next distinct observed turn. Reusing an
agent therefore creates another request and another turn association rather than
overwriting its first request or pretending it is a new agent.

Fixed statistics use typed scalar columns; repeating values use bounded distribution,
category/name/count, and audit-limit rows. `_stats --json` reconstructs the analyzer
shape from indexed SQL. Per-thread summaries group through canonical membership and the
separate lineage edge. Trace tables retain typed event metadata, source coordinates,
byte counts, and opaque public UUIDs without copying message, command, tool, or output
bodies. `_trace --include-bodies` explicitly re-authenticates recorded rollout prefixes
before resolving those bodies. Body expansion is snapshot-only; `--follow` remains a
bounded metadata stream and rejects `--include-bodies`.

Analytics is fail-open: source, parsing, calculation, process, or database failure
cannot change the Codex TUI's behavior or exit status. `_stats`, `_stats-status`,
`_agents`, and `_trace` enforce normal ownership but query SQLite without requiring live
Codex, tmux, or analyzer processes.

## Named reattachment

- `./rodex <cool-name-or-codex-uuid>` resolves an existing canonical Codex UUID link
  first, then a display name, through the same owned integer identity.
- An unregistered canonical UUID is checked with a short-lived App Server
  `thread/read(includeTurns=false)`. An exact persisted, non-ephemeral result enters the
  ordinary new-session pipeline with `resume <UUID>`, receives Rodex's normal 64-bit
  session/runtime identities and unique two-word name, and must load the same UUID before
  its affiliation commits.
- An exact missing-thread response cleans up the catalog process/socket/log, then treats
  the UUID text as a managed initial prompt instead of adopting it.
- If its stored tmux endpoint is live, Rodex first verifies its registered Rodex session
  ID, registry ID, and Codex session ID. A missing or mismatched marker fails closed
  without attach or rename.
- If an exact registered runtime was renamed outside Rodex, one unambiguous full-capability
  match in the expected registry repairs the endpoint; multiple matches are refused. A
  foreign registry may legitimately reuse the same Codex UUID or display name and is
  ignored rather than treated as authority or a collision.
- Once an incarnation is verified, attachment resolves its runtime marker to exactly one
  immutable tmux `$session_id`; a concurrent alias cannot stale the attach target.
- If it has ended, Rodex starts a fresh tmux/app-server and asks Codex to resume the
  stored Codex session ID; the observed ID must match before the endpoint is replaced.
- Concurrent opens of one ended name serialize through a private per-session lock and
  converge on one runtime. A short old-writer shutdown handoff is retried in place;
  persistent or wrong-thread writer conflicts remain hard failures.
- If Codex explicitly reports that the ID was never saved, Rodex starts an empty
  Codex runtime and atomically relinks its new ID to the existing Rodex identity.
  Other resume failures and ID mismatches remain hard failures.
- Both routes preserve the Rodex identity and update `last_accessed_at_utc`.
- `_detach <cool-name-or-linked-codex-uuid>` follows the same existing-session attach,
  resume, or recovery decision without attaching the caller and prints the active
  identities as JSON. Bare UUID adoption is the attaching selector route.
- `./rodex _running` lists verified runtimes and reports unverified or unregistered
  sessions separately.
- `./rodex _alias SESSION NAME` sets its portable preferred name; `--force` replaces
  it. A live effective-name change sends exactly one verified `RODEX_AUTO_INFO`
  prompt to the session's single Codex thread, regardless of how many tmux clients
  are attached. Offline and unchanged names do not send one.
- Human-facing idle `_wait` remains available. Mutations use only the exact, locked
  `_start`, `_steer`, and `_interrupt` coordinator operations.
- `_inspect --json` reports the exact active turn. `_start` and `_steer` accept an
  optional caller-owned `--dispatch ID`, while `_dispatch-status` observes that ID in
  exact App Server thread history. Exact `_wait`, `_interrupt`, and `_result` use a
  caller-supplied turn ID. Every machine command emits one schema-v2 envelope with
  `runtime.runtime_id` and requires the persisted runtime ID. Results stay App
  Server-owned rather than becoming a second SQLite conversation store.
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
  attached client. A private session retains Codex's native `Ctrl-C` behavior. A foreign
  root `C-c` binding causes explicit initialization failure because silent fallback would
  remove this safety property.
- A second attached client triggers a five-second shared-arrival animation. Returning
  to one client triggers its private-session counterpart. The global hook only wakes a
  coordinator; it does not infer the detached session. The coordinator inventories the
  roster and submits the transition to the immutable primary `%pane_id` under the full
  capability. tmux atomically increments a generation, keeps only the newest pending
  transition, admits one lease owner, and schedules one 15-second recovery gate.
  Token/generation conditions prevent an old owner from releasing its successor.
- Exiting the Codex TUI ends its supervisor and private app-server; its cool name can
  transparently resume the saved Codex session later.
- A completely empty Codex TUI may not have saved history. Its cool name recovers by
  starting empty again and replacing only the linked Codex session ID.
- A failure before SQL registration stops the exact new tmux session and leaves no
  partial database row. A host whose pending registration is never confirmed exits;
  one exact matching pending runtime can finish an interrupted confirmation on the
  next command, including when it was launched under a temporary tmux name.
- Scrollback settings apply when a pane is created.
- The runtime uses `$XDG_RUNTIME_DIR/rodex` when suitable—normally
  `/run/user/<uid>/rodex`—otherwise `/tmp/rodex-<uid>`. Unix sockets stay there because
  long project paths can exceed Linux socket limits. `RODEX_RUNTIME_DIR` explicitly
  selects another runtime root and therefore another shared tmux server.
- While a session host is alive, it refreshes the runtime root, shared tmux socket, and
  its private sockets and log hourly. A refresh failure ends that runtime rather than
  leaving a detached session that cannot be addressed. Normal cleanup eligibility
  resumes when the live hosts exit.
- The host has no database watcher, subscription, or polling loop. Canonical SQL
  transactions synchronously validate the private opened storage and its process-local
  identity baseline; a missing or replaced identity fails the operation at its next SQL
  boundary with restart guidance.

Exact tmux targets and compensated name transitions preserve the recorded endpoint.
