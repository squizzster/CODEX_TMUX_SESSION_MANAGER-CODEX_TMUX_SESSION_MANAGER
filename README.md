# Rodex

Use `rodex` wherever you would use `codex` to run a durable Codex TUI inside a
verified tmux runtime. Detach, reattach by name, resume the linked Codex thread, or
control one exact turn from another local process.

| Field | Current value |
| --- | --- |
| Development mode | `ALPHA` — internal Linux pre-release; breaking changes are allowed |
| Rodex release | `0.14.0a1` |
| SQLite catalog | generation `20`, `rodex-v20.sqlite3` |
| tmux ownership | `rodex-isolated-tmux-v4` |
| Runtime peer identity | `rodex-runtime-peer-v4` |
| Machine envelope | generation `4` |
| Agent trace | `rodex-agent-trace-v3` |
| Statistics | `rodex-statistics-v8` |

Only these generations are supported. Rodex does not migrate earlier catalogs,
adopt earlier runtimes, or translate earlier wire formats. Codex owns its transcripts.

## Requirements

- Linux with `/proc` and `pidfd` support.
- Python 3.12 or newer with SQLite 3.53.1 or newer.
- [`uv`](https://docs.astral.sh/uv/) 0.12.1 or newer.
- tmux 3.2a or newer on `PATH`.
- An installed and authenticated stable Codex CLI 0.151.0 or newer.

## Start

```bash
git clone https://github.com/squizzster/CODEX_TMUX_SESSION_MANAGER-CODEX_TMUX_SESSION_MANAGER.git rodex
cd rodex
uv sync
./rodex
```

Examples:

```bash
./rodex
./rodex 'Review this project'
./rodex --model gpt-5.6-sol 'Review this project'
./rodex automatic-beluga
./rodex resume automatic-beluga
```

The first three forms create a managed session. A matching generated name, alias, or
Codex UUID opens the existing Rodex session and either reattaches its live runtime or
resumes its saved Codex thread.

## Invocation routing

| Input shape | Route |
| --- | --- |
| No arguments | Create and attach to a managed session |
| Current interactive Codex options with zero or one prompt | Create and attach; forward arguments unchanged |
| One Rodex name, alias, or Codex UUID | Reattach, resume, recover, or adopt that session |
| `resume SELECTOR` with a matching selector | Use the same managed selector route |
| `resume SELECTOR` without a match | Pass the original command to Codex |
| `-- TOKEN` | Create with `TOKEN` as the prompt; skip selector and subcommand interpretation |
| Other Codex subcommands, help, version, external remote, malformed or unknown option forms | Pass arguments, streams, signals, and exit status to Codex unchanged |

Rodex owns exact underscore-prefixed commands. Its characterized Codex grammar is
0.151.0; uncertain syntax remains Codex-owned.

## Terminal lifecycle

| Input | Shared session | Private session |
| --- | --- | --- |
| `Ctrl-C` | Detach the invoking tmux client | End the exact runtime after the ownership and topology guard passes |
| `Ctrl-D` | Detach the invoking client | Detach the invoking client |
| `Ctrl-b d` | Native tmux detach | Native tmux detach |

Each runtime uses a separate tmux server and immutable server incarnation. Rodex
verifies the session, primary pane, runtime, registry, and Codex identities before
attach, read, control, or cleanup. One `rodexd` process owns all runtime services and the
single serialized analytics pipeline beneath a runtime root. Linux process monitors show
that daemon as a version-derived task name such as `rodexd_v0_14a1` rather than a generic
Python process. Existing daemons retain their loaded code, so stop their runtimes and
daemon after installing a new Rodex version.

## Commands

### Session and terminal

| Command | Contract |
| --- | --- |
| `_help` | Print Rodex help |
| `_create [NAME] [-- CODEX_ARGS...]` | Create and attach to a managed session |
| `_detach [SESSION\|CODEX_ARGS...]` | Create, resume, or recover without attaching |
| `_running` | List running sessions |
| `_context [--json]` | Report this pane's verified live identity and sharing context |
| `_alias [--force] SESSION NAME` | Assign a preferred display name |
| `_wait SESSION` | Wait until the running session is idle |
| `_cat SESSION` | Print retained terminal output |
| `_tail [-f] [-n NUM] SESSION` | Print recent settled text and follow changes |
| `_events SESSION` | Stream filtered live protocol events as JSON Lines |
| `_mouse SESSION [MODE]` | Read or set `on`, `off`, `toggle`, or `inherit` |

Names contain 1–80 ASCII letters, digits, underscores, or hyphens and start with an
ASCII letter or digit. Reserved Codex command names are case-insensitive.

### Exact machine control

| Command | Precondition and result |
| --- | --- |
| `_inspect SESSION --json` | Read one verified live thread and active turn |
| `_start SESSION [--dispatch ID] --stdin --json` | Start work only when the thread is idle |
| `_steer SESSION --turn ID [--dispatch ID] --stdin --json` | Steer the exact active turn |
| `_dispatch-status SESSION --dispatch ID --json` | Read exact acceptance evidence |
| `_wait SESSION --turn ID [--timeout DURATION] --json` | Wait for one exact turn without interrupting it |
| `_interrupt SESSION --turn ID --json` | Interrupt the exact active turn |
| `_result SESSION --turn ID --json` | Read its bounded live result |

Mutations resolve the selector again while holding the session transition lock, verify
the durable runtime and connected peer, and send the exact turn guard. JSON responses
use the generation-4 envelope. Supply `--dispatch` when the caller needs stable
correlation after lost output; an indeterminate mutation must be inspected rather than
blindly retried.

| Exit | Meaning |
| ---: | --- |
| `2` | Invalid input or unknown session |
| `3` | Runtime, identity, or version contract failure |
| `4` | Non-interrupting wait timeout |
| `5` | Interrupted turn |
| `6` | Failed turn |
| `7` | Control failure or indeterminate dispatch |

### Analytics and trace

| Command | Contract |
| --- | --- |
| `_stats SESSION [--turn ID] [--thread CODEX_THREAD_ID] [--json]` | Read the latest persistent statistics projection |
| `_stats-status SESSION` | Read source coverage and that runtime's shared-coordinator health |
| `_agents SESSION [--json]` | Read durable root/sub-agent lineage |
| `_trace SESSION [--follow \| --include-bodies] [--limit N] [--json]` | Follow metadata or re-authenticate bodies in one snapshot |

Trace storage contains typed identities, provenance, coordinates, hashes, byte counts,
and metrics. Codex remains the owner of message, command, tool, reasoning, and output
bodies. `--include-bodies` re-reads authenticated rollout prefixes; follow mode remains
metadata-only.

## Managed presentation

The `/rodex` input menu selects a configured presentation policy:

| Value | Effect |
| --- | --- |
| `light` | Show structurally selected root commentary while the native TUI continues processing |
| `dark` | Restore the complete current native Codex projection |
| `dusk` | Reserved placeholder |

This local presentation does not start a turn or alter Codex execution, logs, or
events. Approval and other unrecognized modal controls stay native.

An exact `subAgentActivity(kind=started)` opens an input-disabled observer in the top
third of the window. It stays open while tracked agent work is active, uses typed
App Server and authenticated trace identities, and closes when that work finishes.

## Local state

| Resource | Path |
| --- | --- |
| Durable catalog | `$XDG_STATE_HOME/rodex/rodex-v20.sqlite3` |
| Durable catalog fallback | `~/.local/state/rodex/rodex-v20.sqlite3` |
| Runtime root | `$XDG_RUNTIME_DIR/rodex` |
| Runtime fallback | `/tmp/rodex-<uid>` |
| Optional runtime override | `RODEX_RUNTIME_DIR` |
| Shared daemon socket | `<runtime-root>/rodexd-v1.sock` |
| Per-runtime tmux socket | `<runtime-root>/tmux-v4-<runtime-id>.sock` |
| Per-runtime service sockets | `<runtime-root>/{app,proxy,events}-<runtime-id>.sock` |

The catalog and runtime paths are private to the current Linux user. Rodex exposes no
network listener. Its security boundary is the operating-system user account; processes
sharing that account are not separate hostile tenants.

## Development validation

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --require-live-startup --cov --cov-report=term-missing
uv build
```

Coverage must remain at least 70%. The required live-startup gate uses installed,
authenticated Codex plus real isolated tmux, SQL, and Codex history paths; missing
prerequisites fail that gate.

## Documentation

- [Installation](INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Runtime isolation](docs/RUNTIME_ISOLATION.md)
- [Codex, Rodex, and tmux boundaries](docs/CODEX_RODEX_TMUX.md)
- [Interaction paths](docs/INTERACTION_PATHS.md)
- [Security model](docs/SECURITY.md)
- [Code concepts](docs/CODE_CONCEPTS.md)
- [SQL schema](docs/SQL_SCHEMA.md)
