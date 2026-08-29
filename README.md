# Rodex - whenever you type `codex` try instead `rodex`

Rodex makes a durable tmux-hosted Codex session feel like Codex itself. Start normally
with `./rodex`, work in the ordinary Codex TUI, detach when needed, and return later by
a memorable name such as `automatic-beluga`. Rodex-specific commands live in an
underscore namespace. Current Codex interactive options and an optional initial prompt
run inside managed Rodex; Codex subcommands and syntax outside Rodex's managed
interactive grammar pass through unchanged.

> **Development status: ALPHA.** Rodex is an in-house Linux pre-release. The application
> described here is complete for its current scope, but interfaces may still change
> before a stable release.

## Why Rodex

Rodex's first job is to accommodate the person at the terminal: replacing `codex` with
`rodex` should preserve the familiar interface, arguments, input, output, signals, and
exit status. Durability is added around that experience rather than in place of it.

One managed session is also a local bridge between an interactive Codex worker and
authorized automation. The human can attach through tmux, handle approvals, and
intervene directly; another shell or agent can discover the same verified runtime,
observe readable terminal output or structured protocol events, and control one exact
turn without typing into the TUI.
Both sides retain the same Rodex, runtime, Codex, and workspace context.

## What Rodex does

- Opens the ordinary interactive Codex TUI inside a private tmux runtime.
- Accepts the current native `codex [OPTIONS] [PROMPT]` shape and forwards its arguments
  unchanged into that managed TUI.
- Keeps Rodex and Codex session identities distinct and linked.
- Gives each Rodex session and runtime incarnation its own exact 16-character
  lowercase hexadecimal ID.
- Assigns every session a permanent, unique two-word name.
- Reattaches a live session or transparently resumes its saved Codex session.
- Suppresses Codex's blocking startup updater in managed sessions and reports a newer
  stable release as a native, scrollable TUI warning without updating Codex or starting
  a model turn.
- Adopts a persisted standalone Codex UUID into a newly named Rodex session.
- Recovers an empty, unsaved Codex session under the same Rodex identity.
- Atomically reserves an optional user-defined display name with session creation
  without losing the generated name.
- Shows the Rodex name, tool-call count, effective mouse mode, live context fill, and
  private/shared state in the tmux bar.
- Preserves 50,000 lines of conversation scrollback with keyboard copy-mode access.
- Animates shared arrival and final departure for five seconds without blocking the TUI.
- Sends work to, waits for, or reads a running session from another shell.
- Streams settled, readable terminal output without replaying terminal clear-screen or
  transient composer redraws.
- Starts, steers, interrupts, and changes mouse state through one exact-turn coordinator
  that blocks stale selectors and runtime incarnations before mutation. App Server
  mutations also send the caller's exact turn ID as their guard.
- Applies each alias through one serialized durable/tmux transition and uses the same
  exact-turn policy for its live name notification.
- Waits for and reads bounded results from one exact Codex turn without copying result
  bodies into SQLite.
- Maintains queryable session and exact-turn statistics from authenticated rollouts.
- Tracks root/sub-agent lineage and typed rollout-derived messages, commands, tools,
  contexts, token usage, rate limits, and agent activity in a contract-validated durable
  agent trace.
- Opens and reuses a noninteractive top-third agent observer on an exact live
  `subAgentActivity(kind=started)` event, correlates the current Codex collaboration
  tool by exact call ID, then shows invocation semantics, collaboration prompt when
  exposed, root-turn request context, agent-authored prose, and durable outcomes while
  leaving Codex focused below.
- Never adopts unregistered tmux sessions and verifies Rodex and Codex identities
  before attaching to or controlling a live runtime.
- Serializes concurrent opens of one ended name and tolerates the bounded Codex-writer
  shutdown handoff without creating duplicate runtimes.

One declarative Codex 0.150.1 CLI contract owns this boundary. Exact underscore Rodex
commands stay local. Native interactive options and an optional prompt create and attach
to a managed session, while a sole bare token first gets the chance to resolve an
existing Rodex name or persisted Codex UUID. Current Codex subcommands, help/version,
external `--remote`, malformed shapes, and unknown option-shaped syntax replace Rodex
with Codex while preserving arguments, terminal streams, signals, and exit status.

## Requirements

- Linux is required; Rodex's fail-closed SQLite path uses `/proc/self/fd`,
  `O_NOFOLLOW`, and `flock`. Other POSIX systems and Windows are not supported.
- Python 3.12.13, whose managed build loads SQLite 3.53.1. Older SQLite
  builds are unsupported.
- [`uv`](https://docs.astral.sh/uv/).
- `tmux` available on `PATH` for managed launches, underscore commands, and stored
  Rodex sessions.
- An installed and authenticated `codex` CLI.

## Quick start

```bash
git clone https://github.com/squizzster/CODEX_TMUX_SESSION_MANAGER-CODEX_TMUX_SESSION_MANAGER.git rodex
cd rodex
uv sync
./rodex
```

Start with an initial prompt exactly as you would with Codex:

```bash
./rodex 'Project: CODEX_TMUX_SESSION_MANAGER'
./rodex --model gpt-5.6-sol 'Review this project'
```

At the `›` prompt, use Codex normally. Detach without ending the session with
`Ctrl-b d`. With the default prefix, `Ctrl-b` shows `CTRL-B MODE` while tmux waits for
the command key, without delaying it, so fast sequences still work. Enter copy mode with
`Ctrl-b [`, navigate with the keyboard, and leave it with `q`. Rodex inherits your tmux
mouse preference instead of overriding it. In a shared session, one `Ctrl-C` warns that
another press may end the session for everyone and offers `Ctrl-b d` as the detach-only
route; the same client must press it again within two seconds to pass the key to Codex.
Custom prefixes and user-owned root `C-b` bindings are left unchanged.

Managed launches disable Codex's own interactive startup update check because it can
appear before runtime registration and block Rodex attachment. Immediately before each
attach, Rodex compares the installed Codex version with a cached npm release lookup. The
npm lookup has a three-second bound and each successful result is cached for 24 hours;
future-dated cache metadata is treated as stale rather than extending that window. One
nonblocking cross-process claim owns refresh for an exact cache, while contenders use the
latest valid cached value without waiting. All lookup and delivery failures are non-fatal.
When a newer stable release exists, the proxy gives the primary TUI a native warning:

```text
Rodex: Codex update available: 0.149.1 -> 0.150.1 (run 'codex update' outside Rodex)
```

The notice enters ordinary TUI scrollback but is not sent to the App Server, persisted in
the Codex thread, or treated as a model turn. Rodex never installs the update.

The Rodex identity is blue (`#1402D8`). The context indicator shows a rounded whole
percentage using the same last-token-usage divided by model-context-window calculation as
Rodex analytics. It is blue below 70%, tmux yellow from 70%, and tmux red from 75%.
These are empirical bands rather than a claim that Codex compacts at one exact
percentage. The exact primary rollout path supplied by `thread/started` provides live
token snapshots during a long turn; App Server usage notifications reconcile the same
value at their lifecycle boundary. While the App Server reports a live context compaction
item, the same slot animates `COMPACTING` with a bright cyan-to-white activity pulse; it
returns to the freshest post-compaction percentage, or `Context: --` until one arrives.
At EOF the rollout follower checks metadata before its bounded content fingerprint,
backs off from 0.25 to 2 seconds while idle, and wakes immediately on an existing primary
protocol event. Replacement and truncation still re-enter the authenticated baseline.

### Live agent observer pane

After runtime registration, an exact App Server
`item/started → subAgentActivity(kind=started)` event creates a top pane at one-third of
the window. The existing Codex pane remains active in the lower two-thirds. The top pane
directly runs Rodex's dedicated observer process, has tmux input disabled, and cannot
execute shell commands. It remains after an agent turn finishes and is reused by later
agent activity; it exits with the Rodex runtime so it cannot keep a session alive.

The App Server's exact agent UUID and path determine what is followed. Its current
`collabAgentToolCall` item names `spawnAgent`, `followupTask`, or `sendMessage` and may
carry the exact collaboration `prompt`; the matching `subAgentActivity` supplies target
and lifecycle under the same call ID. The committed trace independently retains that
call-to-tool relationship. A spawn starts a new agent thread, a follow-up starts a new
turn on the existing agent, and a message continues the current turn without queuing
another. Missing correlation remains explicitly unresolved rather than being guessed
from the generic `interacted` activity kind.

Rodex also receives the completed root `userMessage` that preceded the collaboration.
When it belongs to the same exact root turn, the observer reproduces its text unchanged
as `ROOT TURN REQUEST · exact user message` and identifies it as provenance rather than
the collaboration payload. When Codex exposes `prompt`, the pane separately renders the
exact delegated task, follow-up task, or message; otherwise it reports the authenticated
rollout payload as encrypted and unavailable. Completed tracked-child `agentMessage`
items appear as agent-attributed `AGENT UPDATE` commentary
and `AGENT ANSWER` final text. The completion block restates invocation type, work,
token breakdown, weekly-limit use when available, and the exact root-request-context
recap.
Rodex sanitizes terminal controls without fixed-width truncation and leaves wrapping to
the actual tmux pane width.

SQLite supplies exact turn model/effort, clean/inherited lineage, lifecycle, command,
file, web, query, result, compaction, and token metrics. Changed durable progress adds one
natural-width `WORK` block without cursor-up rewriting; interleaved agents and successive
turns retain independent presentation state. Multiple not-yet-observed requests for one
agent remain FIFO, so a delayed earlier turn cannot consume a later follow-up's request.
`send_message` never enters that queue, consumes a target turn, delays observer drain,
or manufactures a terminal recap.

The display excludes unrelated parent/user messages, developer and system instructions,
hidden reasoning, command text, tool payloads, and output bodies. Each runtime owns a
distinct private observer-control socket. Length-framed Unix stream messages preserve
long exact agent prose without datagram truncation, while preventing one Rodex session
from delivering lifecycle or publication events into another pane. The analytics
transaction also canonicalizes each turn-producing spawn/follow-up request and later
associates it FIFO with the target agent's next distinct observed turn. The same trace
read exposes the exact linked `send_message` invocation without creating a request row.
SQLite stores identity, encrypted-body metadata, and provenance, not a second plaintext
body.

The analytics worker sends a nonblocking wake only after its existing transaction
commits. The observer advances from an indexed opaque event cursor, then performs one
bounded read-only projection for only active or terminal-pending exact agent turns;
completed displays retire from later reads. It regards a display as drained only after
durable terminal events and an exact up-to-date worker publication. App lifecycle
updates never cause SQL reads. There is no timer-based SQL polling, and burst
notifications are coalesced before a read.

To expose this checkout as a per-user `rodex` command, follow the
[installation guide](INSTALL.md).

Installing new Rodex code does not replace modules already loaded by a running session
host. Apply updates in a maintenance window: exit each existing Rodex runtime and resume
it after installation. Detach and reattach alone does not reload Python code. Never move
the live database or its protected parent as an update mechanism.

### Modern tmux keyboard and mouse setup

Current tmux uses one `mouse` option; the older `mode-mouse`, `mouse-resize-pane`,
`mouse-select-pane`, and `mouse-select-window` options are obsolete. A compact vi-like
configuration is:

```tmux
set -g status-keys vi
setw -g mode-keys vi
bind-key m set-option -g mouse \; display-message 'Mouse: #{?mouse,ON,OFF}'
```

With no value, `set-option` toggles a boolean option. This binding replaces tmux's
default `prefix + m` mark-pane action, avoiding the misleading `M` pane flag that can
otherwise appear in the status bar. Rodex sessions inherit the global value unless an
explicit session value is set. Use `rodex _mouse NAME toggle`, `on`, or `off` for one
verified Rodex session, and `rodex _mouse NAME inherit` to return it to the global
preference. See the current [tmux manual](https://man.openbsd.org/tmux.1) and
[tmux getting-started guide](https://github.com/tmux/tmux/wiki/Getting-Started).

## Common commands

| Command | Behaviour |
|---|---|
| `./rodex _help` | Show Rodex's own command help without starting Codex or tmux. |
| `./rodex` | Create and attach to a new Rodex/Codex tmux session. |
| `./rodex 'Project: CODEX_TMUX_SESSION_MANAGER'` | Create a managed session and submit the initial prompt once through Codex's native TUI startup. |
| `./rodex --model gpt-5.6-sol 'Review this project'` | Forward current interactive options and a prompt unchanged into the managed TUI. |
| `./rodex _create` | Create and attach to a new Rodex/Codex session. |
| `./rodex _create project_1234` | Create a session with a preferred display name. |
| `./rodex _detach` | Create without attaching and print expanded identity JSON. |
| `./rodex automatic-beluga` | Attach if live; otherwise resume or recover its Codex session. |
| `./rodex 01a015f4-f27c-7592-8060-d12313e8d0ce` | Open its linked Rodex session, or verify and adopt the persisted standalone Codex thread. |
| `./rodex _running` | List this Linux user's running Rodex sessions. |
| `./rodex _context` | Emit this pane's verified Rodex, Codex, tmux, and sharing context as JSON. |
| `./rodex _alias automatic-beluga edgar-work` | Assign a preferred display name. |
| `./rodex _alias --force automatic-beluga new-name` | Replace an existing display name. |
| `./rodex _wait edgar-work` | Wait until the running session is idle. |
| `./rodex _cat edgar-work` | Print all retained terminal output for use directly or in a Unix pipeline. |
| `./rodex _cat edgar-work \| tail -n 10` | Print the last ten retained terminal lines. |
| `./rodex _tail edgar-work` | Print the latest ten terminal lines, then follow settled readable output. |
| `./rodex _tail -n 25 edgar-work` | Select a different initial line count and continue following. |
| `./rodex _events edgar-work` | Stream filtered live protocol events as JSON lines. |
| `./rodex _agents edgar-work --json` | Show the durable root/sub-agent lineage and rollout checkpoint state. |
| `./rodex _trace edgar-work --json` | Read a transactionally consistent durable agent-trace snapshot. |
| `./rodex _trace edgar-work --follow` | Follow newly committed typed trace metadata as JSON lines; body expansion is rejected in follow mode. |
| `./rodex _trace edgar-work --include-bodies` | Re-authenticate rollout prefixes and explicitly resolve command/message/tool bodies in one snapshot. |
| `./rodex _inspect edgar-work --json` | Read live thread state and its exact active turn ID. |
| `printf '%s' "$PROMPT" \| ./rodex _start edgar-work --dispatch DISPATCH_ID --stdin --json` | Start an idle thread with caller-owned correlation. |
| `printf '%s' "$PROMPT" \| ./rodex _steer edgar-work --turn TURN_ID --dispatch DISPATCH_ID --stdin --json` | Steer one exact active turn with caller-owned correlation. |
| `./rodex _dispatch-status edgar-work --dispatch DISPATCH_ID --json` | Observe where a dispatch ID appears in exact thread history. |
| `./rodex _wait edgar-work --turn TURN_ID --timeout 30m --json` | Wait for one exact turn without interrupting on timeout. |
| `./rodex _interrupt edgar-work --turn TURN_ID --json` | Interrupt one exact active turn. |
| `./rodex _result edgar-work --turn TURN_ID --json` | Read bounded live result data without copying it into SQLite. |
| `./rodex _stats edgar-work` | Show team statistics plus SQL-derived root/sub-agent lifecycle and resource totals. |
| `./rodex _stats edgar-work --json` | Emit the snapshot and freshness metadata as JSON. |
| `./rodex _stats edgar-work --turn TURN_ID` | Show one exact turn from the latest snapshot. |
| `./rodex _stats edgar-work --turn TURN_ID --thread CODEX_THREAD_ID --json` | Qualify a turn ID across exact root or sub-agent threads. |
| `./rodex _stats-status edgar-work` | Show source coverage and analytics worker health. |
| `./rodex _mouse edgar-work toggle` | Toggle mouse handling for one verified live session. |
| `./rodex _mouse edgar-work inherit` | Remove the session override and inherit the tmux global value. |

Rodex flags include `_alias --force` and
`_stats NAME --turn ID --thread CODEX_THREAD_ID --json`.

### Exact control and alias behavior

`_context` is the machine-facing self-identification route for Codex and local tooling.
It resolves the inherited tmux pane, verifies its live Rodex, registry, and Codex markers
against the current user's database row, and fails closed outside a matching managed
session. Its JSON includes registry/database provenance, permanent and user-defined
names, the complete tmux socket/session/window/pane address, and sharing state. The
display name is read on every invocation, so an agent need not cache it.
Each runtime also exposes a random 64-bit `runtime_id` and its durable-match state. The
exact control commands emit a schema-v2 success/error envelope containing separate Rodex
session, runtime, Codex thread, Codex session-tree, and turn identities. They require
stdin prompts and an exact durably registered runtime incarnation. `_inspect` reports the
live App Server thread working directory so callers can verify workspace scope before
mutation.
The checked App Server version is a minimum compatibility floor, not an exact pin:
newer stable Codex versions remain available to exact control, while older or
unrecognized versions fail with a compatibility diagnostic.

`_start`, `_steer`, and `_interrupt` enter one exact-turn mutation coordinator. The
coordinator resolves the requested name, acquires that session's transition lock,
resolves the name again after any wait, and verifies the durable runtime and live control
identity immediately before transport can send. `_start` requires an idle thread;
`_steer` and `_interrupt` require the exact active turn ID and send it as the App Server's
expected-turn guard. A moved selector or replaced runtime fails before a mutation frame;
an incompatible App Server or mismatched turn fails closed. Each mutation RPC chain has
one absolute deadline covering connection, initialization, frame delivery, and response
handling.

`_mouse` enters the same coordinator and retains that transition lock through both its
tmux mutation and readback. It resolves the verified runtime marker to tmux's immutable
server-local `$session_id`, so a concurrent rename or reuse of the old display name cannot
redirect the operation or its telemetry to another session.

Resuming from a different caller working directory intentionally relocates the runtime;
session identity follows the human rather than permanently pinning the original path.
Exact start/steer responses expose `data.dispatch.id` and a structured
`data.recommended_next.command`. Pipelines should supply `--dispatch` before mutation so
they retain the ID even if command output is lost; Rodex generates one when omitted.
`_dispatch-status` reports `accepted`, `not_observed`, or `ambiguous` evidence and
recommends the next exact wait/result/status command without executing it.
`_result` caps final-answer text at 64 KiB, reports its original UTF-8 byte count and
truncation state, and includes at most 100 completed file-change paths.
Exit status `2` means invalid input or unknown session, `3` means
runtime/identity/compatibility failure, `4` is a non-interrupting wait timeout, `5` an
interrupted turn, `6` a failed turn, and `7` a control or indeterminate-dispatch failure.
`dispatch_indeterminate` is deliberately not retryable: execute
`data.recommended_next.command`, then let the controller decide. For start/steer this
queries `_dispatch-status`; an indeterminate interrupt instead recommends `_result` for
its already-known turn. `not_observed` is not proof of rejection. The human-facing
idle-based `_wait` remains available; machine turn mutations use the exact `_start`,
`_steer`, and `_interrupt` pipeline exclusively.
The repository-local [Rodex control skill](.agents/skills/rodex-session-control/SKILL.md)
keeps agent use on this exact identity-and-turn workflow.

`_alias` is one serialized coordinator operation for durable assignment, the live tmux
name, and notification. Rodex plans the assignment from a consistent read, performs tmux
work without holding a SQLite writer transaction, and finalizes only if the durable state
still matches the plan. A failed durable finalize restores a tmux name it changed. After
a successful live change, Rodex starts an idle turn or steers the exact active turn with
one verified prompt:
`RODEX_AUTO_INFO: Rodex session <16-hex-id> is now named '<name>'.` All attached tmux
clients share that thread, so Rodex does not broadcast per client. Offline sessions
and unchanged names produce no prompt. If delivery fails, Rodex reports the failure
without rolling back the already committed name change.

### Routing and observation

Arguments after `_create` or `_detach` are forwarded to the managed Codex TUI; use `--`
when an explicit boundary improves clarity. `_cat` is a finite snapshot, so standard
tools such as `head`, `tail`, and `grep` compose with it normally. `_tail` prints a
familiar initial line selection and then remains open. It publishes rows as soon as they
enter tmux history and publishes stable visible-pane changes after three 0.4-second
observations. The live `Working` status region and composer are excluded so timer frames
and partially typed prompts do not become duplicate transcript lines. `_events` is the
distinct machine-readable stream: it remains open and emits selected subsequent protocol
events as JSON lines until interrupted. Names use 1–80 ASCII letters, digits,
underscores, or hyphens and begin with a letter or digit. The reserved-name vocabulary is
case-insensitive and includes Codex top-level commands and aliases.

No arguments is the default managed-create route and is equivalent to `_create`. The
characterized Codex 0.150.1 interactive grammar—current options plus zero or one prompt—
uses the same managed route and reaches Codex unchanged. A sole bare token first resolves
an existing Rodex name or linked Codex UUID; otherwise it becomes the initial prompt.
An unregistered canonical Codex UUID is checked through a short-lived App Server
`thread/read`; a persisted, non-ephemeral thread is resumed and adopted, while a missing
UUID becomes a normal managed prompt. `rodex -- TOKEN` forces prompt meaning without
selector or subcommand interpretation.

Current Codex subcommands and aliases pass through directly, as do help/version,
external remote connections, malformed current syntax, multiple positional arguments,
and every unknown option-shaped invocation. This preserves Codex's own parsing and
diagnostics. Current command names are reserved from Rodex aliases.

## Local data

The durable per-user registry defaults to
`$XDG_STATE_HOME/rodex/rodex-v16.sqlite3`, or
`~/.local/state/rodex/rodex-v16.sqlite3` when `XDG_STATE_HOME` is unset. Set
`RODEX_DATABASE_PATH` to select another database.

Rodex session IDs are random 64-bit values rendered only as 16 lowercase hex
characters, including leading zeroes. Rodex registry IDs use the same 64-bit wire form
as a separate identity domain. Every current runtime incarnation has a third, distinct
64-bit `runtime_id` in the same wire form. These compact values are intentional
agent-facing integrity discriminators, not bearer secrets: the 16-character form halves
the transcription burden of a UUID while retaining the full `2^64` candidate space.
Session and runtime allocation use the same bounded ten-candidate indexed-selection
pipeline. SQLite stores each Rodex-owned ID losslessly in one signed `BIGINT` and
enforces its domain uniqueness. Codex session IDs remain Codex-owned 128-bit values.
Each is stored once in the canonical `codex_threads` table across two `BIGINT` columns;
memberships, current-root selection, activities, and lineage use integer foreign keys.

The registry uses schema generation 16. An internal generation marker admits an
already-current database cheaply inside each operation's transaction. Explicit
first-use bootstrap creates a missing private registry atomically; nonempty unmarked,
incomplete, and wrong-generation databases fail closed. The explicit integrity audit is
a read-only canonical allowlist check and is not part of ordinary mutation hot paths.

Short-lived Unix sockets and app-server logs use `$XDG_RUNTIME_DIR/rodex`, normally
`/run/user/<uid>/rodex`. When `XDG_RUNTIME_DIR` is unset or that socket path would be
too long, Rodex uses the private fallback `/tmp/rodex-<uid>`. Set `RODEX_RUNTIME_DIR`
to override it.

The registry is held in a private current-user directory and must remain a regular
current-user file at mode `0600`; runtime roots are mode `0700`, and live sockets and
logs are mode `0600`. Symlink, owner, and file-type checks fail closed. SQLite uses WAL
mode with a busy timeout so analytics writes do not block consistent readers during
ordinary concurrent use. SQLite connects through the same retained no-follow descriptor
Rodex validated. Transactions recheck the retained parent, transition-lock, database, and
SQLite main-path identities before connect, `BEGIN`, and `COMMIT`, and retain a
process-local `(device, inode)` baseline between transactions. A threadless, fork-safe
process-local SQLite connection also keeps at most one exact database's WAL generation
alive between sparse writes, with bounded automatic checkpoint and journal-size policy;
it holds no SQL transaction or cooperative transition lock, switches only by validated
storage identity, and closes on clean process exit. Its validated main-file descriptor
remains open for the same process-local lifetime and is released only after SQLite closes.
Before a genuine fork, the parent closes this complete owner so the child inherits no live
SQLite state. A missing or replaced later identity fails that SQL operation with restart
guidance. Rodex has no database watcher, subscription, or polling loop; a
move-away-and-back completed entirely between transactions is not observable. Every
ordinary transaction and integrity audit holds a shared advisory transition lock and reads
committed WAL normally. The explicit maintenance lock is exclusive and is for offline
diagnostics only; live database moves or replacements are unsupported. All Rodex processes
accessing a database must use this transaction boundary; direct same-user SQLite access is
outside the cooperative-lock contract. Only explicit first-use flows may create the
database and transition lock; ordinary readers and mutations are existing-only, and
bootstrap does not recreate storage previously admitted by the same process and later
found missing.

Each live session host refreshes every required runtime pathname hourly. This protects
weeks-long detached sessions from age-based temporary-file cleanup without making dead
sockets persistent; refreshes stop when the owning session ends.

The Rodex registry also stores accepted append-stream provenance, independent analytics
worker checkpoints and health, the latest successful derived statistics snapshot, and
an append-only typed agent trace. Each session host runs a low-priority, fail-open
sidecar whose event scheduler blocks while idle, batches activity for a 0.5-second quiet
period with a five-second ceiling, and resolves only the exact Codex thread identities
named by bounded semantic wake events. A resident analyzer
consumes only newline-complete appended bytes after initial registration; it neither
recursively scans the sessions tree nor repeatedly reloads unchanged rollout prefixes.
Bounded catch-up and one clean replay cover races and recoverable faults without an
unbounded polling or retry loop. Clean replay invalidates cached verified lineage
metadata together with byte-reader and analyzer state before re-authenticating sources.

Cold lineage recovery first uses exact 128-bit child identities named by lifecycle or
parent activity. When an authenticated spawn supplies only an agent path, one
startup-only fallback enumerates regular JSONL files in the root UUIDv7 three-day window,
reads only each first `session_meta` line, and accepts only the authenticated root/parent
closure. Paths are then cached; resident append wakes never repeat this directory scan.

On worker startup or clean recovery, Rodex performs one full-prefix read to authenticate
the durable byte count and SHA-256 before analysis. The resident hot path relies on
Codex's trusted append-only rollout contract and then returns to suffix-only reads; it
does not claim hostile in-place mutation detection on every append. Unchanged semantic
wakeups perform no source-byte reads or projection write after reconciliation, and an
event-before-append race receives only the scheduler's bounded retry window. Rollout
normalization takes response-item turn identity from Codex's nested passthrough metadata,
so a function call and its `SubAgentActivity` share the same canonical parent turn.
Sequence-fence races reload their checkpoint; deterministic semantic publication conflicts
park by authenticated source fingerprint instead of repeatedly resetting every cursor.

Authenticated records are normalized into immutable typed facts before SQLite work. The
registry trace contract then canonicalizes UTC timestamps, text, identities, and typed
details; rejects duplicates; hashes each detail; and seals the complete prepared
publication. The trace writer accepts only that exact contract-issued value and only
inside an active Rodex transaction. Replaced or manually constructed prepared values and
calls without an active transaction fail before the first trace SQL statement.

Within that one writer transaction, trace membership resolution reads only the distinct
thread IDs present in the batch, in bounded chunks, after the transaction's membership
writes. Append, publication-head advancement, agent-request provenance, and FIFO
target-turn reconciliation commit or roll back together. Reconciliation is an internal
writer step rather than a separately sequenced public operation; trace snapshot,
pagination, and follow behavior belong to the read side.

Rodex persists stable thread/turn/item identity, replaceable typed statistics metrics,
normalized distribution and named-count rows, and typed trace detail tables. Model and
reasoning effort remain separate nullable turn-state facts whose stable
integer IDs reference dedicated lookup tables; their session counts are derived from
those exact turn rows. Metrics therefore remain directly queryable and JSON output can
be rebuilt deterministically. Trace SQL stores event metadata, byte counts, and
accepted rollout coordinates—not copied message, command, tool, or output bodies.
Codex thread identities retain all 128 bits once in canonical rows; unresolved
sub-agent activity targets point to those rows before verified membership exists.
Turns, Codex items, trace events, and canonical tool calls use separate opaque 128-bit
public identities, while relational joins and pagination keep compact internal integer
foreign keys. Exact-turn statistics expose opaque `turn_id` and semantic
`codex_turn_id` separately. Item aliases carry their canonical activity scope; thread
memberships, rollout-source provenance, lineage, item/tool-call aliases, and every
published typed trace detail reject update and delete. A canonical tool call may fill
its initially unknown tool name once; its identity and verified name are then final.
Trace publication totals advance incrementally rather than recounting the historical
ledger. Coverage is cumulative: a prior durable gap remains gapped, and any retained
unrecognized record prevents a complete claim.
`_trace --include-bodies` deliberately re-reads and re-hashes recorded prefixes for
current or historical thread memberships; hidden reasoning and encrypted values remain
redacted through the same privacy classifier used during normalization. It is a
one-shot operation and cannot be combined with `--follow`, whose bounded output remains
metadata-only. Canonical rollout paths and SHA-256 digests are sensitive local metadata,
so the database remains private to its operating-system user. Codex remains responsible for raw
history. Each session's
`statistics_publication_sequence` starts at one and advances only when a changed,
authenticated projection is published. It is a consistency and concurrency token, not
a turn count or retained history; only the latest successful snapshot remains in Rodex
SQL.

## Documentation

- [Installation](INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Codex, Rodex, and tmux boundaries](docs/CODEX_RODEX_TMUX.md)
- [Security model](docs/SECURITY.md)
- [Code concepts](docs/CODE_CONCEPTS.md)
- [SQL schema methodology](docs/SQL_SCHEMA.md)
- [Codex App Server live control evidence](docs/APP_SERVER_0_147_LIVE_EVIDENCE.md)
- [Current boundary and unimplemented proposals](docs/PHASE_II_PLAN.md)

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov --cov-report=term-missing
uv build
```

The coverage floor is 70%. Tests lock the exhaustive application route/preparation
matrix and thin CLI boundary, with real App Server Unix-socket and real-tmux coverage for
scrollback retention and following, inherited mouse configuration, rename, identity
markers, and status configuration.
