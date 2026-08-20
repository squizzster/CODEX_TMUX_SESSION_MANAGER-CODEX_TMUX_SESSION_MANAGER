# Rodex - whenever you type `codex` try instead `rodex`

Rodex makes a durable tmux-hosted Codex session feel like Codex itself. Start normally
with `./rodex`, work in the ordinary Codex TUI, detach when needed, and return later by
a memorable name such as `automatic-beluga`. Rodex-specific commands live in an
underscore namespace; other nonempty invocations pass to Codex unchanged.

> **Development status: ALPHA.** Rodex is a Linux/POSIX pre-release under active
> validation. Interfaces may still change, but identity, lifecycle, installation,
> security, and primary workflows have automated boundary coverage.

## Why Rodex

Rodex's first job is to accommodate the person at the terminal: replacing `codex` with
`rodex` should preserve the familiar interface, arguments, input, output, signals, and
exit status. Durability is added around that experience rather than in place of it.

That user-first foundation also creates the direction of travel: one managed session
becomes a local bridge between an interactive Codex worker and authorized automation.
The human can attach through tmux, handle approvals, and intervene directly; another
shell or agent can discover the same verified runtime, observe readable terminal output
or structured protocol events, and control one exact turn without typing into the TUI.
Both sides retain the same Rodex, runtime, Codex, and workspace context.

## What Rodex does

- Opens the ordinary interactive Codex TUI inside a private tmux runtime.
- Keeps Rodex and Codex session identities distinct and linked.
- Gives each Rodex session and runtime incarnation its own exact 16-character
  lowercase hexadecimal ID.
- Assigns every session a permanent, unique two-word name.
- Reattaches a live session or transparently resumes its saved Codex session.
- Adopts a persisted standalone Codex UUID into a newly named Rodex session.
- Recovers an empty, unsaved Codex session under the same Rodex identity.
- Supports an optional user-defined display name without losing the generated name.
- Shows the Rodex name, tool-call count, effective mouse mode, live context fill, and
  private/shared state in the tmux bar.
- Preserves 50,000 lines of conversation scrollback with keyboard copy-mode access.
- Animates shared arrival and final departure for five seconds without blocking the TUI.
- Keeps the in-TUI `/rodex` command implementation available but disabled for now.
- Sends work to, waits for, or reads a running session from another shell.
- Streams settled, readable terminal output without replaying terminal clear-screen or
  transient composer redraws.
- Starts, steers, waits for, interrupts, and reads results by exact Codex turn ID.
- Maintains queryable session and exact-turn statistics from authenticated rollouts.
- Refuses unregistered tmux name collisions and verifies Rodex and Codex identities
  before attaching to or controlling a live runtime.
- Serializes concurrent opens of one ended name and tolerates the bounded Codex-writer
  shutdown handoff without creating duplicate runtimes.

Rodex does not generally reinterpret ordinary nonempty Codex CLI invocations. With no
arguments it creates and attaches to a managed session. Exact underscore Rodex commands
stay local, an existing Rodex name opens that session, and a canonical Codex UUID opens
its linked Rodex session or adopts the persisted standalone thread. Every other nonempty
invocation replaces itself with Codex while preserving arguments, terminal streams,
signals, and exit status.

## Requirements

- Linux or a compatible POSIX system; Windows is not supported.
- Python 3.12 or newer.
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

At the `›` prompt, use Codex normally. Detach without ending the session with
`Ctrl-b d`. With the default prefix, `Ctrl-b` shows `CTRL-B MODE` while tmux waits for
the command key, without delaying it, so fast sequences still work. Enter copy mode with
`Ctrl-b [`, navigate with the keyboard, and leave it with `q`. Rodex inherits your tmux
mouse preference instead of overriding it. In a shared session, one `Ctrl-C` warns that
another press may end the session for everyone and offers `Ctrl-b d` as the detach-only
route; the same client must press it again within two seconds to pass the key to Codex.
Custom prefixes and user-owned root `C-b` bindings are left unchanged.

The Rodex identity is blue (`#1402D8`). The context indicator shows a rounded whole
percentage using the same last-token-usage divided by model-context-window calculation as
Rodex analytics. It is blue below 70%, tmux yellow from 70%, and tmux red from 75%.
These are empirical bands rather than a claim that Codex compacts at one exact
percentage. While the App Server reports
a live context compaction item, the same slot animates `COMPACTING`; it returns to the
freshest post-compaction percentage, or `Context: --` until one arrives.

To expose this checkout as a per-user `rodex` command, follow the
[installation guide](INSTALL.md).

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
| `./rodex _create` | Create and attach to a new Rodex/Codex session. |
| `./rodex _create project_1234` | Create a session with a preferred display name. |
| `./rodex _detach` | Create without attaching and print expanded identity JSON. |
| `./rodex automatic-beluga` | Attach if live; otherwise resume or recover its Codex session. |
| `./rodex 01a015f4-f27c-7592-8060-d12313e8d0ce` | Open its linked Rodex session, or verify and adopt the persisted standalone Codex thread. |
| `./rodex _running` | List this POSIX user's running Rodex sessions. |
| `./rodex _context` | Emit this pane's verified Rodex, Codex, tmux, and sharing context as JSON. |
| `./rodex _alias automatic-beluga edgar-work` | Assign a preferred display name. |
| `./rodex _alias --force automatic-beluga new-name` | Replace an existing display name. |
| `./rodex _send edgar-work "Run the tests"` | Start or steer work in a running session. |
| `./rodex _wait edgar-work` | Wait until the running session is idle. |
| `./rodex _cat edgar-work` | Print all retained terminal output for use directly or in a Unix pipeline. |
| `./rodex _cat edgar-work \| tail -n 10` | Print the last ten retained terminal lines. |
| `./rodex _tail edgar-work` | Print the latest ten terminal lines, then follow settled readable output. |
| `./rodex _tail -n 25 edgar-work` | Select a different initial line count and continue following. |
| `./rodex _events edgar-work` | Stream filtered live protocol events as JSON lines. |
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
`_context` is the machine-facing self-identification route for Codex and local tooling.
It resolves the inherited tmux pane, verifies its live Rodex, registry, and Codex markers
against the current user's database row, and fails closed outside a matching managed
session. Its JSON includes registry/database provenance, permanent and user-defined
names, the complete tmux socket/session/window/pane address, and sharing state. The
display name is read on every invocation, so an agent need not cache it.
New runtimes also expose a random 64-bit `runtime_id` and its durable-match state. The
exact control commands emit a schema-v2 success/error envelope containing separate Rodex
session, runtime, Codex thread, Codex session-tree, and turn identities. They require
stdin prompts and a runtime created by this Rodex version. `_inspect` reports the live
App Server thread working directory so callers can verify workspace scope before mutation.
Resuming from a different caller working directory intentionally relocates the runtime;
session identity follows the human rather than permanently pinning the original path.
Exact start/steer responses expose `data.dispatch.id` and a structured
`data.recommended_next.command`. Pipelines should supply `--dispatch` before mutation so
they retain the ID even if command output is lost; Rodex generates one when omitted.
`_dispatch-status` reports `accepted`, `not_observed`, or `ambiguous` evidence and
recommends the next exact wait/result/status command without executing it.
`_result` caps final-answer text at 64 KiB, reports its original UTF-8 byte count and
truncation state, and includes at most 100 completed file-change paths.
Exit status `2` means invalid
input or unknown session, `3` means runtime/identity/compatibility failure, `4` is a
non-interrupting wait timeout, `5` an interrupted turn, `6` a failed turn, and `7` a
control or indeterminate-dispatch failure. `dispatch_indeterminate` is deliberately not
retryable: execute `data.recommended_next.command`, then let the controller decide. For
start/steer this queries `_dispatch-status`; an indeterminate interrupt instead
recommends `_result` for its already-known turn. `not_observed` is not proof of
rejection. Legacy `_send` and idle-based `_wait` remain available.
The repository-local [Rodex control skill](.agents/skills/rodex-session-control/SKILL.md)
keeps agent use on this exact identity-and-turn workflow.
When `_alias` changes the effective name of a live session, Rodex sends one verified
prompt to that session's Codex thread:
`RODEX_AUTO_INFO: Rodex session <16-hex-id> is now named '<name>'.` All attached tmux
clients share that thread, so Rodex does not broadcast per client. Offline sessions
and unchanged names produce no prompt. If delivery fails, Rodex reports the failure
without rolling back the already committed name change.
Arguments after `_create` or
`_detach` are forwarded to the managed Codex TUI; use `--` when an explicit boundary
improves clarity. `_cat` is a finite snapshot, so standard tools such as `head`, `tail`,
and `grep` compose with it normally. `_tail` prints a familiar initial line selection
and then remains open. It publishes rows as soon as they enter tmux history and publishes
stable visible-pane changes after three 0.4-second observations. The live `Working`
status region and composer are excluded so timer frames and partially typed prompts do
not become duplicate transcript lines. `_events` is the distinct machine-readable
stream: it remains open and emits selected future protocol events as JSON lines until
interrupted. Names use 1–80
ASCII letters, digits, underscores, or hyphens and begin with a letter or digit. The
existing reserved-name vocabulary remains case-insensitive and includes Codex
top-level commands and aliases.

No arguments is the default managed-create route and is equivalent to `_create`. A
single argument is the other exception to ordinary Codex passthrough. An existing Rodex
name or linked Codex UUID opens that session. An unregistered canonical Codex UUID is
checked through a short-lived App Server `thread/read`; a persisted, non-ephemeral thread
is resumed exactly, verified, assigned normal Rodex session/runtime IDs and a unique
two-word name, registered, and attached. A missing UUID and every other unmatched
invocation pass unchanged to Codex without creating a Rodex database row or tmux session.
A bare name colliding with an unregistered session on Rodex's private tmux server instead
fails explicitly and is shown by `_running`; Rodex never adopts or deletes that tmux
session automatically. An existing Rodex name wins even if a later Codex release
introduces a command with the same spelling.

The in-TUI `/rodex` command and its completion ribbon are temporarily disabled through
`RODEX_TMUX_SLASH_ENABLED` in `rodex.runtime`. The implementation remains in the
codebase for later re-enablement; input currently passes directly to the Codex TUI.

## Local data

The durable per-user registry defaults to
`$XDG_STATE_HOME/rodex/rodex-v9.sqlite3`, or
`~/.local/state/rodex/rodex-v9.sqlite3` when `XDG_STATE_HOME` is unset. Set
`RODEX_DATABASE_PATH` to select another database.

Rodex session IDs are random 64-bit values rendered only as 16 lowercase hex
characters, including leading zeroes. Rodex registry IDs use the same 64-bit wire form
as a separate identity domain. Every current runtime incarnation has a third, distinct
64-bit `runtime_id` in the same wire form. These compact values are intentional
agent-facing integrity discriminators, not bearer secrets: the 16-character form halves
the transcription burden of a UUID while retaining the full `2^64` candidate space.
Session and runtime allocation use the same bounded ten-candidate indexed-selection
pipeline. SQLite stores each Rodex-owned ID losslessly in one signed `BIGINT` and
enforces its domain uniqueness. Codex session IDs remain Codex-owned 128-bit values and
are stored losslessly across two `BIGINT` columns.

Version 8 is an incompatible ALPHA schema with no v7 reader or migration path. Rodex
leaves `rodex-v7.sqlite3` and earlier generations untouched; explicitly selecting one
fails exact schema verification rather than falling back or rewriting it.

Short-lived Unix sockets and app-server logs use `$XDG_RUNTIME_DIR/rodex`, normally
`/run/user/<uid>/rodex`. When `XDG_RUNTIME_DIR` is unset or that socket path would be
too long, Rodex uses the private fallback `/tmp/rodex-<uid>`. Set `RODEX_RUNTIME_DIR`
to override it.

The registry is held in a private directory and repaired to mode `0600`; runtime roots
are mode `0700`, and live sockets and logs are mode `0600`. Symlink, owner, and file-type
checks fail closed. SQLite uses WAL mode with a busy timeout so analytics writes do not
block consistent readers during ordinary concurrent use.

Each live session host refreshes every required runtime pathname hourly. This protects
weeks-long detached sessions from age-based temporary-file cleanup without making dead
sockets persistent; refreshes stop when the owning session ends.

The Rodex registry also stores authenticated rollout provenance, independent analytics
worker health, and the latest successful derived statistics snapshot. Each session host
runs a low-priority, fail-open sidecar that analyzes complete JSONL record prefixes in a
fresh in-memory analyzer. Rodex persists typed session/turn columns plus normalized
distribution and named-count rows. Model and reasoning effort remain separate nullable
turn facts whose stable integer IDs reference dedicated lookup tables; their session
counts are derived from those exact turn rows. Metrics therefore remain directly
queryable and JSON output can be rebuilt deterministically. Rodex never stores copied
prompts, responses, commands, tool output, raw events, or redundant statistics JSON.
Canonical rollout paths and SHA-256 digests are sensitive local metadata, so the
database remains private to its POSIX user. Codex remains responsible for raw history.
Each session's `statistics_publication_sequence` starts at one and advances whenever a
changed authenticated rollout prefix is successfully published. It is a consistency
and concurrency token, not a turn count or retained history; only the latest successful
snapshot remains in Rodex SQL.

## Documentation

- [Installation](INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Codex, Rodex, and tmux boundaries](docs/CODEX_RODEX_TMUX.md)
- [Security model](docs/SECURITY.md)
- [Code concepts](docs/CODE_CONCEPTS.md)
- [SQL schema methodology](docs/SQL_SCHEMA.md)
- [Codex App Server 0.147 live evidence](docs/APP_SERVER_0_147_LIVE_EVIDENCE.md)
- [Phase II candidates for review](docs/PHASE_II_PLAN.md)

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov --cov-report=term-missing
uv build
```

The alpha coverage floor is 70%. Tests lock the exhaustive application route/preparation
matrix and thin CLI boundary, with real App Server Unix-socket and real-tmux coverage for
scrollback retention and following, inherited mouse configuration, rename, identity
markers, and status configuration.
