# Rodex

Use `rodex` wherever you would use `codex` to run a durable Codex TUI inside a
verified tmux runtime. Detach, reattach by name, resume the linked Codex thread, or
control one exact turn from another local process.

See [Prompt submission flow](docs/PROMPT_SUBMISSION_FLOW.md) for the end-to-end
Rodex → Codex → app workflow and its transformation boundaries.

| Field | Current value |
| --- | --- |
| Development mode | `ALPHA` — internal Linux pre-release; breaking changes are allowed |
| Rodex release | `0.14.0a2` |
| SQLite catalog | generation `20`, `rodex-v20.sqlite3` |
| tmux ownership | `rodex-isolated-tmux-v4` |
| Runtime peer identity | `rodex-runtime-peer-v5` |
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

While attached, the invoking Rodex client uses the full display-name-derived process
title, such as `rodex_cyan_mackerel`. After tmux returns it prints
`Rodex detach [cyan-mackerel].` when the runtime remains live, or
`Rodex exited [cyan-mackerel].` when the runtime ended.

Each runtime uses a separate tmux server and immutable server incarnation. Rodex
verifies the session, primary pane, runtime, registry, and Codex identities before
attach, read, control, or cleanup. One `rodexd` process per exact implementation owns
the runtime services and serialized analytics pipeline it created beneath a runtime root.
Different installed implementations coexist at SHA-256-named daemon endpoints, so a new
Rodex can create sessions without taking ownership from still-running older sessions.
Linux process monitors show
that daemon as a version-derived task name such as `rodexd_v0_14a2` rather than a generic
Python process. Existing processes retain their loaded code. Daemon and runtime handshakes
require the exact first-party implementation fingerprint, so changed code is rejected
rather than silently mixed. Stop old runtimes and their daemon after installing or
editing Rodex.

Each reservation carries its absolute caller workspace into both native child processes;
`PWD` alone is not a working directory. Native directory options remain unchanged.
Stop acknowledges `stopping` or `terminal`; a running worker retains its TTY until its
own finalizer completes. Cancelled reservations cannot start. The daemon retains at most
1,024 small completion receipts, with no caller environment; evicted operation identities
expire and cannot restart work.

When the registered main turn terminates with App Server
`codexErrorInfo: "serverOverloaded"`, Rodex schedules `Continue...` through the same
exact model-input pipeline after 30 seconds. Any terminal input cancels that pending
continuation. Repeated overloads within five minutes double the delay through 60, 120,
240, 480, and 960 seconds; later repeats remain at 960 seconds. A gap longer than five
minutes resets the sequence to 30 seconds. The terminal `systemError` state produced by
that failure remains startable only when Codex reports direct input is accepted. This
recovery is always active.
Cancellation remains effective through hooks, session-lock and transport waits until
atomic dispatch admission. It does not undo or interrupt an already admitted request.

## Submitted prompt hooks

The installation's `conf/hooks/user_prompt_substitutions.yaml` supplies global rules.
User rules live at `$XDG_STATE_HOME/rodex/conf/hooks/user_prompt_substitutions.yaml`,
falling back to `~/.local/state/rodex/conf/hooks/user_prompt_substitutions.yaml`.
Both are ordered YAML lists. The supplied global example turns `Hello` (any letter
case) into `Hello!`:

```yaml
- name: 'Add enthusiasm to Hello'
  match: '^Hello$'
  replace: 'Hello!'
  flags: 'i'
```

Each named rule requires `name`, `match`, and `replace`; `flags` defaults to empty.
Names must be unique within each file. A user rule with the same name replaces the
global rule at its original position; new user rules run afterward in user-file order.
Names are case-sensitive identities and diagnostic labels, never regex patterns.
Patterns use Python regular
expressions and replacement backreferences (`\1`, `\g<name>`), without slash delimiters.
Flags are `i` (ignore case), `m` (multiline anchors), `s` (dot matches newline),
and `g` (replace every match). Without `g`, each rule replaces the first match in each
text input item. Rules run in file order. Single-quoted YAML strings preserve escapes.
The file also includes the configured standalone-line `push` expansion. The earlier
`/pattern/replacement/flags` and `s/pattern/replacement/flags` strings remain accepted
through the same rule compiler; in that shorthand only, escape a slash as `\/`.
These unnamed rules append in file order and cannot override a named rule.

Every managed initial prompt, native submission, `_start`, `_steer`, and queued
submission reaches the same prompt hook exactly once per request. Rodex prepares a
managed initial prompt before launching Codex. At interactive Enter, Rodex transforms a
verified native draft, updates that same Codex editor through its PTY, confirms the
canonical tmux composer, and only then admits a receipt and releases Enter; the
TUI therefore classifies, displays, queues, and submits the canonical text. Its primary
protocol request consumes the matching preparation receipt instead of applying the hook
again. Ordinary and Ultra composers are recognized, including visible wrapped text.
A rewrite confirmation timeout keeps the draft unsubmitted; Enter retries without
reapplying rules. A native draft that cannot be verified initially retains the structured
protocol fallback, which cannot guarantee matching optimistic TUI history.
See [prompt ownership and timing](docs/PROMPT_SUBMISSION_FLOW.md) for the complete flow.
Control and queued routes without a terminal draft transform at that protocol
boundary. Only text input is rewritten; images, routing, turn identity, settings,
approvals, and responses remain native. UI annotations over unchanged text are rebased
to UTF-8 byte offsets; annotations overlapping protocol-rewritten text are removed.
Native commands delegated directly to Codex, specialized command fields such as review
instructions or goal objectives, and internal agent traffic are outside this hook.

The global file belongs to the Rodex installation, independent of the caller's workspace.
The user path is frozen from the launching caller's environment, even in a shared daemon.
Only a submitted input re-stats both files, using `file_stat_sha512`: SHA-512 of mode,
inode, device, owner, group, size, mtime-ns, and ctime-ns, with no content reads. An
unchanged fingerprint uses that file's cached rules; a changed fingerprint reads and
compiles only that file before the same submission. A second call to the same stat function after reading
checks for concurrent saves; three changing reads refuse that submission. One runtime
owns the cache, including concurrent TUI/control submissions. No polling occurs.

Empty YAML or `[]` contributes no rules from that file. An absent user file uses global
rules alone; creating or removing it takes effect on the next submission. A missing
global file, unreadable files, invalid YAML, and invalid rules refuse the submission
and send descriptive, display-only Rodex notices with each file and issue. No partial
configuration is applied. The connection stays usable. An unchanged bad fingerprint is
cached and its notice is displayed only once per file after successful delivery; a changed
fingerprint allows a new notice. When stat itself fails, identical failures likewise
share one notice until the file becomes accessible. Each refused RPC still receives a
failure response so callers cannot hang. Fix the file and submit again; valid changed
rules replace the cached error. Only the supplied metadata fingerprint detects changes:
rewrites preserving all eight metadata fields cannot be detected. Hooks are synchronous.

After installing changed code, start a fresh runtime with `rodex`, or exit the old Codex
TUI before resuming its session. Detaching and reopening a live runtime retains loaded
code. Subsequent YAML edits take effect on the next submission without a restart.

## Commands

### Session and terminal

| Command | Contract |
| --- | --- |
| `_help` | Print Rodex help |
| `_version` | Print the Rodex release and active daemon/SQLite compatibility contracts |
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

Orderly retirement preserves accepted events and reconciles on the same analytics worker,
allowing a 0.5-second append-settle interval within five seconds of cooperative effort.
Outcomes are complete, inactive, or explicitly incomplete; `_stats-status` retains an
incomplete diagnostic where storage is available, and the daemon log records failures
when it is not. Daemon shutdown waits at most ten seconds for runtime workers, then five
seconds for analytics. Those caller budgets cannot preempt an executing analyzer or SQL
operation; unquiesced producers cannot be declared complete. Resume requests reconciliation;
this is best effort, not a crash-durable repair queue or final-durability guarantee.

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
| User prompt overrides | `$XDG_STATE_HOME/rodex/conf/hooks/user_prompt_substitutions.yaml` |
| User prompt overrides fallback | `~/.local/state/rodex/conf/hooks/user_prompt_substitutions.yaml` |
| Runtime root | `$XDG_RUNTIME_DIR/rodex` |
| Runtime fallback | `/tmp/rodex-<uid>` |
| Optional runtime override | `RODEX_RUNTIME_DIR` |
| Implementation daemon socket | `<runtime-root>/<implementation-sha256>.sock` |
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
- [Shared-runtime root-cause evidence and verification](docs/SHARED_RUNTIME_ROOT_CAUSES.md)
