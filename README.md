# Rodex - whenever you type `codex` try instead `rodex`

Rodex creates a durable tmux-hosted Codex session when invoked with no arguments and
gives its own commands an underscore namespace. Start normally with `./rodex`, detach
when needed, and return later using a generated name such as `automatic-beluga`.

> **Development status: PROTO.** Rodex is a Linux/POSIX prototype under active
> development. Breaking changes and disposable database resets are expected.

## What Rodex does

- Opens the ordinary interactive Codex TUI inside a private tmux runtime.
- Keeps Rodex and Codex session identities distinct and linked.
- Assigns every session a permanent, unique two-word name.
- Reattaches a live session or transparently resumes its saved Codex session.
- Recovers an empty, unsaved Codex session under the same Rodex identity.
- Supports an optional user-defined display name without losing the generated name.
- Shows the Rodex name, tool-call count, and live private/shared state in the tmux bar.
- Preserves 50,000 lines of conversation scrollback with mouse and keyboard access.
- Animates shared arrival and final departure for five seconds without blocking the TUI.
- Keeps the in-TUI `/rodex` command implementation available but disabled for now.
- Sends work to, waits for, or follows a running session from another shell.

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
`Ctrl-b d`. Scroll with the mouse wheel, or enter tmux copy mode with `Ctrl-b [` and
leave it with `q`.

To expose this checkout as the system-wide `rodex` command, follow the
[installation guide](INSTALL.md).

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
| `./rodex _alias automatic-beluga edgar-work` | Assign a preferred display name. |
| `./rodex _alias --force automatic-beluga new-name` | Replace an existing display name. |
| `./rodex _send edgar-work "Run the tests"` | Start or steer work in a running session. |
| `./rodex _wait edgar-work` | Wait until the running session is idle. |
| `./rodex _tail edgar-work` | Follow structured live protocol events as JSON lines. |

Only `_alias` accepts a Rodex flag: `--force`. Arguments after `_create` or `_detach`
are forwarded to the managed Codex TUI; use `--` when an explicit boundary improves
clarity. Stop `_tail` with `Ctrl-C`; the Rodex session keeps running. Names use 1–80
ASCII letters, digits, underscores, or hyphens and begin with a letter or digit. The
existing reserved-name vocabulary remains case-insensitive and includes Codex
top-level commands and aliases.

No arguments is the default managed-create route and is equivalent to `_create`. A
single argument is the other exception to ordinary Codex passthrough: if it matches an
existing Rodex name, Rodex opens that session. Otherwise it—and every other non-Rodex
invocation—is passed unchanged to Codex. An existing Rodex name wins even if a later
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

Each live session host refreshes every required runtime pathname hourly. This protects
weeks-long detached sessions from age-based temporary-file cleanup without making dead
sockets persistent; refreshes stop when the owning session ends.

The current Rodex registry stores identities, ownership, timestamps, names, and tmux
endpoints. Live protocol sockets are advertised only by the running tmux session. Rodex
does not persist conversation content; Codex remains responsible for its own history.

## Documentation

- [Installation](INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Codex, Rodex, and tmux boundaries](docs/CODEX_RODEX_TMUX.md)
- [Code concepts](docs/CODE_CONCEPTS.md)
- [SQL schema methodology](docs/SQL_SCHEMA.md)

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov --cov-report=term-missing
uv build
```

The prototype coverage floor is 70%. Tests include real-tmux boundary coverage for
scrollback retention, mouse copy mode, rename, and status configuration.
