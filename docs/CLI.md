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

Managed runtimes offer a native shell-command hook. It evaluates direct `find`/`rg`
workflows before the narrow rule denying standalone integer sleeps below 10 seconds.
Exact decisions and interception scope are defined in
[native command policy](SECURITY.md#native-command-policy).

`Rodex policy allowed: COMMAND` appears in the session's tmux status bar for five
seconds from interception. A command such as `sleep 10` can finish after the notice
disappears. "Allowed" is a policy decision, not execution/completion evidence.
Rapid updates coalesce; safety warnings and sharing notices take priority. Policy
rows preserve `CTRL-B MODE` and then restore the normal bar. They are not chat
messages or a persistent command log.

The sole enabled Rodex hook normally qualifies for
[automatic admission](SECURITY.md#hook-admission-and-resume). If it still needs
trust and another enabled native hook is present:

1. Run `rodex _policy-setup` to open the native review UI.
2. Use `/hooks`, review and trust the individual Rodex `PreToolUse` hook, then exit.
3. Start a fresh managed runtime to use the reviewed configuration.

Unsupported, disabled, unexpected, or unverifiable Rodex hooks remain inactive.
The inactive notice also lasts five seconds; metadata remains so a later attach can
repeat the reminder. Successful admission clears that stale state. For a saved
session that times out before attachment, use the
[resume diagnosis](OPERATIONS.md#resume-failures); fresh-start success alone does
not verify native resume preflight.

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
idle upgrade. When missing history prevents an idle live-runtime upgrade, the refusal
includes this reconnect command. Ended runtimes require normal resume, where missing
history remains an error. [Retained-runtime handoff](RUNTIME_ISOLATION.md#retained-runtime-upgrade)
defines admission and ownership.

`_detach` follows the create/open/resume flow without attaching and reports identities
as JSON. `_running` reports unverified or unregistered sessions separately; it does
not silently adopt or delete them. [Lifecycle authority](RUNTIME_ISOLATION.md) explains
why a timeout cannot be treated as a dead runtime.

- `Ctrl-D` and `Ctrl-b d` detach only the invoking client and leave the runtime alive.
- With the default prefix, `Ctrl-b m` toggles mouse mode for the current managed
  session and briefly displays `Mouse: ON/OFF` with tmux's message styling. Rodex
  installs that fallback only when `prefix/m` is unbound or still tmux's stock
  pane-mark action; any other user binding wins.
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
Post-commit refresh errors identify the committed name. An uncertain announcement
retains its dispatch identity and reports an exact `_dispatch-status` command.
Repeating the same alias does not retry the announcement.

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
An unavailable primary event source or lost stream continuity ends a wait explicitly,
including a wait without a timeout. Query the exact turn result or start a new wait
after reconnecting; an observation failure is not a reason to resubmit model work.
An invalid success acknowledgement after a mutation was sent is indeterminate,
including a missing turn ID or a non-object result. The error retains the method,
dispatch ID, thread and expected turn where known. Reconcile that dispatch before
deciding whether to submit again; native rejections remain definitive errors.

Machine envelopes are defined by
[`MACHINE_ENVELOPE_SCHEMA_VERSION`](../src/rodex/machine_commands.py); App Server
version admission is defined by [`CODEX_APP_SERVER`](../src/rodex/app_server_contract.py).
There is no managed `_notice` or `_exec` command, or generic API for executing native
TUI slash commands. [Local notices](INTERACTION_PATHS.md#model-input-and-local-notices)
use an existing verified runtime's Python interaction API.
