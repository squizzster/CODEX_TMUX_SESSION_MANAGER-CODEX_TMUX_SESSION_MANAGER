# Rodex

Rodex makes Codex CLI sessions durable and memorable by running them inside tmux.
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

## Common commands

| Command | Behaviour |
|---|---|
| `./rodex` | Create and attach to a new Rodex/Codex session. |
| `./rodex automatic-beluga` | Attach if live; otherwise resume its Codex session. |
| `./rodex running` | List this POSIX user's running Rodex sessions. |
| `./rodex alias automatic-beluga edgar-work` | Assign a preferred display name. |
| `./rodex alias -f automatic-beluga new-name` | Replace an existing display name. |

`running` and `alias` also accept `--running` and `--alias`. Names use 1–80 ASCII
letters, digits, underscores, or hyphens and begin with a letter or digit.

## Local data

The default registry is `.rodex/rodex.sqlite3` beneath the directory where Rodex is
launched. Set `RODEX_DATABASE_PATH` to select another database. Short-lived Unix
sockets and app-server logs use the current user's runtime directory or `/tmp`.

The current Rodex registry stores identities, ownership, timestamps, names, and tmux
endpoints. It does not persist conversation content. Codex remains responsible for its
own session history.

## Documentation

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
