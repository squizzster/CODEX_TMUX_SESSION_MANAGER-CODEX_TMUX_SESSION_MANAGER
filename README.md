# Rodex - whenever you type `codex` try instead `rodex`

Rodex creates a durable tmux-hosted Codex session when invoked with no arguments and
gives its own commands an underscore namespace. Start normally with `./rodex`, detach
when needed, and return later using a generated name such as `automatic-beluga`.

> **Development status: ALPHA.** Rodex is a Linux/POSIX pre-release under active
> validation. Interfaces may still change, but identity, lifecycle, installation,
> security, and primary workflows have automated boundary coverage.

## What Rodex does

- Opens the ordinary interactive Codex TUI inside a private tmux runtime.
- Keeps Rodex and Codex session identities distinct and linked.
- Assigns every session a permanent, unique two-word name.
- Reattaches a live session or transparently resumes its saved Codex session.
- Recovers an empty, unsaved Codex session under the same Rodex identity.
- Supports an optional user-defined display name without losing the generated name.
- Shows the Rodex name, tool-call count, effective mouse mode, and live private/shared
  state in the tmux bar.
- Preserves 50,000 lines of conversation scrollback with keyboard copy-mode access.
- Animates shared arrival and final departure for five seconds without blocking the TUI.
- Keeps the in-TUI `/rodex` command implementation available but disabled for now.
- Sends work to, waits for, or follows a running session from another shell.
- Maintains queryable session and exact-turn statistics from authenticated rollouts.
- Refuses unregistered tmux name collisions and verifies Rodex and Codex identities
  before attaching to or controlling a live runtime.
- Serializes concurrent opens of one ended name and tolerates the bounded Codex-writer
  shutdown handoff without creating duplicate runtimes.

Rodex does not replace or reinterpret nonempty Codex CLI invocations. With no
arguments it creates and attaches to a managed session. Exact underscore Rodex
commands stay local, an existing Rodex name opens that session, and every other
nonempty invocation replaces itself with Codex while preserving arguments, terminal
streams, signals, and exit status.

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
| `./rodex _running` | List this POSIX user's running Rodex sessions. |
| `./rodex _context` | Emit this pane's verified Rodex, Codex, tmux, and sharing context as JSON. |
| `./rodex _alias automatic-beluga edgar-work` | Assign a preferred display name. |
| `./rodex _alias --force automatic-beluga new-name` | Replace an existing display name. |
| `./rodex _send edgar-work "Run the tests"` | Start or steer work in a running session. |
| `./rodex _wait edgar-work` | Wait until the running session is idle. |
| `./rodex _tail edgar-work` | Follow structured live protocol events as JSON lines. |
| `./rodex _stats edgar-work` | Show the latest successful aggregate statistics. |
| `./rodex _stats edgar-work --json` | Emit the snapshot and freshness metadata as JSON. |
| `./rodex _stats edgar-work --turn TURN_ID` | Show one exact turn from the latest snapshot. |
| `./rodex _stats edgar-work --turn TURN_ID --source CODEX_UUID --json` | Qualify a turn ID across resumed Codex sources. |
| `./rodex _stats-status edgar-work` | Show source coverage and analytics worker health. |
| `./rodex _mouse edgar-work toggle` | Toggle mouse handling for one verified live session. |
| `./rodex _mouse edgar-work inherit` | Remove the session override and inherit the tmux global value. |

Rodex flags include `_alias --force` and `_stats NAME --turn ID --source UUID --json`.
`_context` is the machine-facing self-identification route for Codex and local tooling.
It resolves the inherited tmux pane, verifies its live Rodex, registry, and Codex markers
against the current user's database row, and fails closed outside a matching managed
session. The display name is read on every invocation, so an agent need not cache it.
Arguments after `_create` or
`_detach` are forwarded to the managed Codex TUI; use `--` when an explicit boundary
improves clarity. Stop `_tail` with `Ctrl-C`; the Rodex session keeps running. Names use 1–80
ASCII letters, digits, underscores, or hyphens and begin with a letter or digit. The
existing reserved-name vocabulary remains case-insensitive and includes Codex
top-level commands and aliases.

No arguments is the default managed-create route and is equivalent to `_create`. A
single argument is the other exception to ordinary Codex passthrough: if it matches an
existing Rodex name, Rodex opens that session. Otherwise it—and every other non-Rodex
invocation—is passed unchanged to Codex, unless that bare name collides with an
unregistered session on Rodex's private tmux server. Such a collision fails explicitly
and is shown by `_running`; Rodex never adopts or deletes it automatically. An existing Rodex name wins even if a later
Codex release introduces a command with the same spelling.

The in-TUI `/rodex` command and its completion ribbon are temporarily disabled through
`RODEX_TMUX_SLASH_ENABLED` in `rodex.runtime`. The implementation remains in the
codebase for later re-enablement; input currently passes directly to the Codex TUI.

## Local data

The durable per-user registry defaults to
`$XDG_STATE_HOME/rodex/rodex.sqlite3`, or
`~/.local/state/rodex/rodex.sqlite3` when `XDG_STATE_HOME` is unset. Set
`RODEX_DATABASE_PATH` to select another database.

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
distribution and named-count rows, so metrics remain directly queryable and JSON output
can be rebuilt deterministically. It never stores copied prompts, responses, commands,
tool output, raw events, or redundant statistics JSON. Canonical rollout paths and
SHA-256 digests are sensitive local metadata, so the database remains private to its
POSIX user. Codex remains responsible for raw history.

## Documentation

- [Installation](INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Codex, Rodex, and tmux boundaries](docs/CODEX_RODEX_TMUX.md)
- [Security model](docs/SECURITY.md)
- [Code concepts](docs/CODE_CONCEPTS.md)
- [SQL schema methodology](docs/SQL_SCHEMA.md)

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov --cov-report=term-missing
uv build
```

The alpha coverage floor is 70%. Tests include real-tmux boundary coverage for
scrollback retention, inherited mouse configuration, rename, identity markers, and
status configuration.
