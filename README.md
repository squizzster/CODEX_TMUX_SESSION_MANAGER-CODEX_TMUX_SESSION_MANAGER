# Rodex — whenever you type `codex`, try `rodex` instead

**Your favourite Codex harness. Supercharged in Python.**

Run `./rodex` and you're in the ordinary Codex TUI — editor, tools, approvals and all.
Around it, Rodex adds durable sessions, memorable names, shared access, a live view of
your agents, and exact-turn controls for automation. Keep the harness you love and
give it a whole new bag of tricks.

**Let Codex cook. Close the terminal. Come back by name.**

Kick off the refactor. Dig into that sprawling codebase. Put your agents to work.
Rodex gives your Codex sessions staying power: detach, reconnect from another
terminal, and jump back into a session named `automatic-beluga` when you're ready.
Your session stays alive on its host while you're away.

Drive from the keyboard. Coordinate from another shell. Keep a second terminal
attached to the same live session. Keep the work moving.

Even capacity hiccups get a helping hand. When Codex reports an overloaded-server
failure, Rodex waits and sends `Continue...` for you. Repeated overloads back off;
typing cancels a pending retry. A little nudge. A lot less babysitting.

Bring your familiar Codex interactive options and an initial prompt. Return by name,
alias, or Codex UUID, with or without `resume`. Rodex's extra controls live in an
underscore namespace: `_running` finds your live sessions and `_help` shows you the
ropes. Other Codex subcommands and unsupported syntax pass through unchanged.

Development mode: **ALPHA** — an in-house Linux pre-release. Interfaces and
storage/runtime contracts may change before a stable release.

## Why Rodex

**Make your favourite harness your own.** Rodex's session, interaction, and recovery
logic is written in Python. Extend the behaviour around Codex — prompt handling,
presentation, automation, recovery — without patching and rebuilding Codex itself.
Prompt substitution rules are configurable in YAML, too. The
[architecture](docs/ARCHITECTURE.md) and
[prompt guide](docs/PROMPT_SUBMISSION_FLOW.md) show where to start.

**Walk away. Come back. Carry on.** Give a task room to run, detach when life calls,
and reopen it by a permanent name you can remember. If its runtime has ended, Rodex
can resume the saved Codex conversation under that same name. Your everyday Codex
interface stays familiar, and native commands keep their arguments, terminal streams,
signals, and exit status.

**See your agents in action.** The live observer brings the crew into view above your
Codex session: what they're working on, the prompts available to Rodex, their prose,
and their outcomes. Keep typing below while the agents work above. The status bar and
terminal title show main-thread activity; scrollback and durable analytics let you
inspect the work later.

**Give your automation real controls.** An authorized shell or agent can start work,
steer an active turn, interrupt it, or wait for its exact result. You can handle
approvals and intervene directly in the TUI throughout. Both sides address the same
verified Rodex session, Codex thread, runtime, and workspace, with explicit turn
identities and structured outcomes to coordinate around. One session, ready for
hands-on work and serious automation.

## What Rodex does

- Runs the ordinary Codex TUI in a dedicated tmux server for each runtime, with
  verified ownership before attachment or control.
- Gives every session a permanent, unique two-word name. An optional display alias
  can be reserved at creation or assigned later without losing the generated name.
  Rodex session and runtime IDs are separate 16-character lowercase hexadecimal
  identities, linked to the Codex UUID.
- Reattaches live sessions, resumes saved conversations, adopts persisted standalone
  Codex UUIDs, and recovers confirmed never-saved empty sessions under the same Rodex
  identity. Concurrent opens of an ended name converge on one runtime.
- Keeps running sessions on retained copies of their code, dependencies, and defaults
  across checkout updates. Opening a supported older session upgrades it when idle;
  `--force-old` attaches to its existing runtime, including during a busy turn.
- Shows the display name, tool-call count, mouse mode, context fill, and private/shared
  state in the tmux bar. `Working.` through `Working....` marks main-thread activity;
  the terminal title pulses and periodically shows elapsed work time. Codex's native
  elapsed counter refreshes every three seconds, with decorative animations off by
  default.
- Keeps up to 50,000 lines of terminal scrollback, with keyboard copy mode and
  per-session mouse control. Sharing transitions have a bounded status animation.
- Switches between the native Codex screen and a root-commentary view through the
  `/rodex` menu while the native TUI keeps running.
- Applies configurable prompt transformations through one admission path for initial
  prompts, native submissions, and automation. See
  [prompt configuration and submission](docs/PROMPT_SUBMISSION_FLOW.md).
- Gives work another go after a main-thread `serverOverloaded` failure by sending
  `Continue...` after an initial 30-second wait. Repeated overloads trigger increasing
  delays, and terminal input cancels a pending retry. The
  [recovery contract](docs/INTERACTION_PATHS.md#outcomes-and-recovery) defines the
  trigger, backoff, and cancellation boundaries.
- Suppresses Codex's blocking startup updater and reports an available newer stable
  release through a display-only TUI warning. Rodex never installs the update or
  starts a model turn for that notice.
- Reads snapshots, follows settled plain-text terminal output, and streams selected
  protocol events from another shell. Exact-turn commands start, steer, interrupt,
  wait for, and read results with runtime and turn checks before mutation; result
  bodies remain outside SQLite.
- Maintains queryable session and turn statistics, root/sub-agent lineage, and durable
  traces of messages, commands, tools, contexts, token usage, rate limits, and agent
  activity from authenticated rollouts. [Analytics health](docs/ANALYTICS.md#health-and-coverage)
  makes incomplete or delayed coverage visible.
- Opens an input-disabled observer in the top third of the terminal while tracked
  agents work, leaving Codex focused below. It correlates collaboration requests by
  exact call and turn identities, shows available prompts, prose, and outcomes, and
  closes when no tracked agent remains working.

Session interactions share one target-addressed pipeline for messages, explicit model
turns, pane control, presentation, and terminal/protocol input and output. The main
view and agent observer use the same interaction contract; the observer aggregates
agents rather than binding to one model thread. See the
[interaction routes and ownership](docs/INTERACTION_PATHS.md) for supported operations
and delivery outcomes.

Names and sockets locate sessions; verified runtime identity authorizes actions.
Rodex checks the registered session, runtime, server, pane, and Codex identities at
the applicable operation boundary and never adopts unregistered tmux sessions.
[Runtime isolation](docs/RUNTIME_ISOLATION.md) explains those checks and the supported
retained-runtime handoff.

The managed CLI boundary follows the repository's
[Codex 0.151.0 grammar](src/codex_cli_contract/v0_151_0.py). Native interactive options
and at most one prompt enter Rodex; a sole bare token first gets the chance to resolve
a session. Exact `resume SELECTOR` uses the same lookup. Unmatched resume commands,
other Codex subcommands, help/version, external `--remote`, malformed forms, and
unknown options delegate to Codex unchanged. Use `--` to force a literal prompt.
The [CLI guide](docs/CLI.md) covers routing and lifecycle keys.

## Current release

**Rodex 0.15.0a1**, SQL generation **21**, isolated tmux protocol **v5**. Sessions
share a `rodex-v21.sqlite3` catalog within the same state root; each live runtime has
its own tmux server at `tmux-v5-<runtime-id>.sock`. Earlier catalog generations are
not migrated.

Supported retained runtimes can upgrade when opened idle;
`rodex NAME --force-old` reconnects through their retained installation without
interrupting active work. See [installation and compatibility](INSTALL.md),
[package metadata](pyproject.toml), and the
[current-contract tests](tests/test_current_contracts.py).

## Requirements

- Linux with `/proc` and `pidfd` support.
- Python 3.12 or newer, `uv`, and tmux on `PATH`.
- An installed, authenticated stable Codex CLI **0.151.0 or newer**, as enforced by
  the [App Server contract](src/rodex/app_server_contract.py).

See [installation prerequisites](INSTALL.md#prerequisites) for dependency constraints
and validation. Rodex does not enforce numeric SQLite or tmux version floors; the
`uv_build` requirement is a build-backend constraint, not a minimum `uv` CLI version.

## Quick start

```bash
git clone https://github.com/squizzster/CODEX_TMUX_SESSION_MANAGER-CODEX_TMUX_SESSION_MANAGER.git rodex
cd rodex
uv sync --locked
./rodex
```

Start with an initial prompt as you would with Codex:

```bash
./rodex 'Review this project'
```

Note the generated name in the tmux bar. Detach with `Ctrl-D` or `Ctrl-b d`, then
return using your session's name in place of this example:

```bash
./rodex automatic-beluga
```

Discover Rodex's commands and running sessions:

```bash
./rodex _help
./rodex _running
```

Rodex uses its bootstrap `.venv` to run Rodex itself. Managed and delegated Codex
processes do not inherit that environment; a different project virtualenv activated
by the caller is preserved. Startup uses the installed dependencies without syncing
or downloading code. Follow [installation](INSTALL.md#install-the-per-user-command)
to make `rodex` available on your `PATH`.

## CLI

- [Invocation, session workflows, and command discovery](docs/CLI.md)
- [Exact-session automation skill](.agents/skills/rodex-session-control/SKILL.md)

## Architecture

- [Runtime shape and domain owners](docs/ARCHITECTURE.md)
- [Interaction routes, terminal presentation, and observer ownership](docs/INTERACTION_PATHS.md)

## Technical

- [Runtime identity, admission, and lifecycle fences](docs/RUNTIME_ISOLATION.md)
- [Prompt configuration, transformation, and Enter handoff](docs/PROMPT_SUBMISSION_FLOW.md)
- [Analytics scheduling, provenance, recovery, and coverage](docs/ANALYTICS.md)
- [SQLite boundaries, schema standards, and identity model](docs/SQL_SCHEMA.md)
- [Trust boundary, executable admission, and privacy](docs/SECURITY.md)

## Operations

- [Install, update, relocate, or remove the command](INSTALL.md)
- [Local state, terminal settings, and CPU/health diagnosis](docs/OPERATIONS.md)

## Development

- [Validation gates, test selection, and CPU benchmark](docs/DEVELOPMENT.md)
