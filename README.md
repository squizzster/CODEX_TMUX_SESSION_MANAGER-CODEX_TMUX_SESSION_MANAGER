# Rodex - whenever you type `codex` try instead `rodex`

Rodex makes Running Codex CLI sessions durable and memorable by running them inside tmux.
Start normally with `./rodex`, detach when needed, and return later using a generated
name such as `automatic-beluga`.

> **Development status: PROTO.** Rodex is a Linux/POSIX prototype under active
> development. Breaking changes and disposable database resets are expected.

## What Rodex does

- Opens the ordinary interactive Codex TUI inside a private tmux runtime.
- Keeps Rodex and Codex session identities distinct and linked.
- Assigns every session a permanent, unique two-word name.
- Reattaches a live session or transparently resumes its saved Codex session.
- Supports an optional user-defined display name without losing the generated name.
- Shows the Rodex name and protocol-observed tool-call count in the tmux status line.
- Sends work to, waits for, or follows a running session from another shell.

Rodex does not replace the Codex CLI. It wraps its normal interface with local session
identity, tmux lifecycle management, and a transparent protocol proxy.

## Requirements

- Linux or a compatible POSIX system; Windows is not supported.
- Python 3.12 or newer.
- [`uv`](https://docs.astral.sh/uv/).
- `tmux` available on `PATH`.
- An installed and authenticated `codex` CLI.

## Quick start

```bash
git clone https://github.com/squizzster/CODEX_TMUX_SESSION_MANAGER-CODEX_TMUX_SESSION_MANAGER.git rodex
cd rodex
uv sync
./rodex
```

At the `›` prompt, use Codex normally. Detach without ending the session with
`Ctrl-b d`.

To expose this checkout as the system-wide `rodex` command, follow the
[installation guide](INSTALL.md).

## Common commands

| Command | Behaviour |
|---|---|
| `./rodex` | Create and attach to a new Rodex/Codex session. |
| `./rodex --create project_1234` | Create a session with a preferred display name. |
| `./rodex -d` | Create without attaching and print expanded identity JSON. |
| `./rodex automatic-beluga` | Attach if live; otherwise resume its Codex session. |
| `./rodex running` | List this POSIX user's running Rodex sessions. |
| `./rodex alias automatic-beluga edgar-work` | Assign a preferred display name. |
| `./rodex alias -f automatic-beluga new-name` | Replace an existing display name. |
| `./rodex send edgar-work "Run the tests"` | Start or steer work in a running session. |
| `./rodex wait edgar-work` | Wait until the running session is idle. |
| `./rodex tail edgar-work` | Follow structured live protocol events as JSON lines. |

Create also accepts `--c` or `-create`; `-c` remains Codex's `--config` option. Detach
accepts `-d`, `--d`, `-detach`, or `--detach`. The bare control commands accept
`--running`, `--alias`, `--send`,
`--wait`, and `--tail`. Stop `tail` with `Ctrl-C`; the Rodex session keeps running.
Names use 1–80 ASCII letters, digits, underscores, or hyphens and begin with a letter
or digit. Rodex control words and Codex top-level commands/aliases are reserved
case-insensitively; this vocabulary may grow with supported Codex versions.

A single argument keeps the natural Codex-style flow: if it matches an existing Rodex
name, Rodex opens that session; otherwise the argument is passed to the new Codex TUI.
Use `--create NAME` when the argument must become the session's display name.

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
the critical rename and status-configuration lifecycle.
