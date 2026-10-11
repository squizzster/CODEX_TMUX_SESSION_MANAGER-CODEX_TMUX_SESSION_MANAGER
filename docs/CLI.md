# CLI workflows

Install using [INSTALL.md](../INSTALL.md). Use `./rodex` from the checkout or `rodex`
through the installed shim.

```bash
rodex _help
rodex _version
rodex _running
```

`_help` is Rodex's command reference and does not resolve Codex, tmux, or the database.
`_version` reports Rodex's active contracts. Native `--help` and `--version` belong
to Codex. The authoritative grammar is
[`COMMAND_SPECS`](../src/rodex/command_contract.py), not a copied option table.

## Invocation routing

```bash
rodex
rodex 'Review this project'
rodex -- 'resume'
rodex SESSION
rodex resume SESSION
rodex SESSION --force-old
rodex _detach SESSION
```

- No arguments or characterized interactive Codex options with at most one prompt
  create a managed runtime. `-- TOKEN` forces a prompt, bypassing selector/subcommand
  interpretation. A recognized initial prompt passes through [prompt admission](PROMPT_SUBMISSION_FLOW.md).
- A matching permanent name, alias, or linked Codex UUID opens the same Rodex session.
  An unresolved ordinary bare token becomes a prompt; unmatched explicit
  `resume SELECTOR` is delegated unchanged to Codex.
  `--force-old` is consumed only for a recognized owned selector (bare or explicit
  `resume`); otherwise the original invocation passes unchanged to Codex. Native
  commands, option values, and literal prompts after `--` retain their meaning.
- An unregistered canonical Codex UUID is checked through a transient App Server.
  A persisted, non-ephemeral thread can be adopted into a new Rodex session. An exact
  missing-thread result makes the UUID text a prompt; other errors are not absence.
- Native subcommands, external remote options, multiple positionals, and uncertain or
  malformed option forms pass through with native streams, signals, and exit status.
  The characterized grammar is [Codex 0.151.0](../src/codex_cli_contract/v0_151_0.py),
  not a promise to intercept every later Codex option. Exact underscore commands are local.

Routing authority: [`UnifiedRodexApplicationPipeline`](../src/rodex/application_pipeline.py).

## Command-policy check

Managed runtimes offer a native `PreToolUse` hook for all shell/unified-exec
commands. The current capability-test policy denies an exact integer `sleep N` when
`N` is below 10, returning `Only sleeping 10 or more seconds is accepted.` to Codex.
It allows other commands and shows `Rodex policy allowed: COMMAND` in that session's
tmux status bar for five seconds from interception, not from command completion. A
command such as `sleep 10` can finish after its status disappears. This text is not a
model message, observer message, or persistent command log. Busy status
updates coalesce; safety warnings take priority. "Allowed" does not mean executed:
Codex approvals, sandbox restrictions, and other hooks still apply.

Before starting the remote TUI, Rodex reads Codex's native hook inventory. When the
exact injected Rodex hook is enabled, needs trust, and is the sole enabled hook, Rodex adds
`--dangerously-bypass-hook-trust` for that managed TUI invocation only. It writes no
trust record. This covers both `untrusted` and `modified` native states: an older
retained helper path may have been trusted before an installation upgrade. A
previously trusted exact Rodex hook needs no bypass.

If another enabled user, project, managed, or plugin hook is present, Rodex does not
add the bypass. Run `rodex _policy-setup`, review the Rodex `PreToolUse` hook through
`/hooks`, trust that individual hook, exit, and start a new managed session. An
explicit caller bypass or effective native bypass configuration retains its native
meaning; an explicit `-c bypass_hook_trust=false` prevents Rodex's automatic bypass.
Disabled hooks stay disabled. A missing, unexpected, duplicated, malformed, or
unverifiable Rodex hook starts with policy inactive and a five-second tmux notice
instead of waiting on a pre-attachment trust prompt. The normal bar then returns;
the inactive metadata remains available so a later attach can present the reminder.

The inventory decision is the startup snapshot immediately before the TUI creates
its thread. As accepted for this ALPHA path, Rodex does not lock Codex hook files
across that interval. Every policy decision status lasts five seconds, policy rows
preserve tmux's `CTRL-B MODE` indicator, and successful admission clears retained
inactive metadata. Native hook and
managed-only restrictions remain authoritative. Unmanaged Codex is unaffected.
`write_stdin` continuations and hosted tools such as web search are outside this
shell-command hook. See [security](SECURITY.md#native-command-policy).

## Open, detach, and resume

A live selector reattaches only after exact identity verification. An ended runtime
resumes the saved Codex thread. Only an explicit never-saved-thread response permits
empty-session recovery with a replacement Codex ID; other failures remain errors.
The Rodex identity and permanent name survive. A resumed runtime uses the resumer's
current working directory, not a permanently pinned original workspace.

After an implementation update, opening a supported retained live session upgrades
that session only when its thread is idle, preserving its thread, cwd, and name.
Busy turns report commands to retry once idle or reconnect without upgrading:

```bash
rodex SESSION --force-old
rodex resume SESSION --force-old
```

This attaches through the retained installation, including during a busy turn,
without stopping, restarting, or sending model input. It also skips an available
idle upgrade. Ended runtimes require normal resume, and missing history remains an
error. [Retained-runtime handoff](RUNTIME_ISOLATION.md#retained-runtime-upgrade)
defines admission and ownership.

`_detach` follows the create/open/resume flow without attaching and reports identities
as JSON. `_running` reports unverified or unregistered sessions separately; it does
not silently adopt or delete them. [Lifecycle authority](RUNTIME_ISOLATION.md) explains
why a timeout cannot be treated as a dead runtime.

- `Ctrl-D` and `Ctrl-b d` detach only the invoking client and leave the runtime alive.
- Shared `Ctrl-C` detaches that client. Private `Ctrl-C` ends the exact runtime only
  after the ownership/topology guard passes. It is not a normal Codex interrupt key.
- Exiting the Codex TUI ends its runtime; opening the name can resume later.

Attach/detach/exit banners use the current display name. The attached client process
title is `rodex_<display_name>` with hyphens replaced by underscores.

`_alias SESSION NAME` assigns a preferred name; `--force` replaces an existing alias.
An effective live-name change attempts one `RODEX_AUTO_INFO` prompt: start only when
idle, or steer the observed active turn. Offline/unchanged names send none. Notification
failure does not roll back the committed name. This is a model-input side effect,
not merely a display rename; see
[`ExactTurnMutationCoordinator.alias_transition`](../src/rodex/exact_turn_mutation.py).

## Observe without guessing completion

- `_context --json` verifies the invoking pane's identity and attached-client snapshot.
  Inherited `TMUX`/`TMUX_PANE` are addresses, not authority; inspect the thread for cwd.
- `_cat SESSION` is a finite plain-text snapshot of the immutable primary pane.
- `_tail SESSION` follows by default: committed history emits promptly; visible text
  settles before emission. It excludes the live composer/status region. This is
  sampled terminal text, not an authoritative turn event stream.
- `_events SESSION` follows selected future protocol events as JSON Lines.
- `_stats`, `_stats-status`, `_agents`, and `_trace` read durable projections without
  requiring a live runtime. Read [analytics coverage limits](ANALYTICS.md#health-and-coverage)
  before treating them as complete. Trace body expansion is snapshot-only;
  `--include-bodies` cannot be combined with `--follow`.

tmux owns scrollback. `Ctrl-b [` enters copy mode and `q` exits. `_mouse SESSION
inherit` removes only that session's override. Terminal settings are in
[operations](OPERATIONS.md#terminal-settings).

## Exact-turn automation

Use the [session-control skill](../.agents/skills/rodex-session-control/SKILL.md) for
inspection, stdin prompts, dispatch reconciliation, waiting, and interruption.
`_wait SESSION` is a human idle wait; exact automation supplies `--turn TURN_ID`.
Terminal output alone never proves completion. Approval and user-input requests
remain with the subscribed primary connection, normally the managed TUI.

Machine envelopes are defined by
[`MACHINE_ENVELOPE_SCHEMA_VERSION`](../src/rodex/machine_commands.py); App Server
version admission is defined by [`CODEX_APP_SERVER`](../src/rodex/app_server_contract.py).
