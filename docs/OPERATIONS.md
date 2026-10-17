# Operations

## Local state

Keep runtime roots short: Linux Unix-socket limits can reject long checkout paths.
These locations belong to the current Linux user, not to the caller's project.

| Resource | Default and override |
| --- | --- |
| Rodex state | `$XDG_STATE_HOME/rodex`, otherwise `~/.local/state/rodex` |
| Catalog | `rodex-v22.sqlite3` under the state root; generation authority is [private_database_path.py](../src/rodex_sql/private_database_path.py) |
| Retained installations | `implementations/<installation-key>` under the state root; absolute `RODEX_INSTALLATIONS_ROOT` overrides the store |
| Runtime root | suitable `$XDG_RUNTIME_DIR/rodex`, otherwise `/tmp/rodex-<uid>`; `RODEX_RUNTIME_DIR` overrides it |
| Daemon endpoint | `<runtime-root>/<installation-key>.sock` |
| Daemon coordination | `<installation-key>.start.lock`, `<installation-key>.log`, and the mode-`0600` `<installation-key>.daemon-instance` capability under the runtime root; the capability is replaced for each daemon start |
| tmux endpoint | `<runtime-root>/tmux-v5-<runtime-id>.sock` |
| Service sockets | `<runtime-root>/{app,proxy,events,policy}-<runtime-id>.sock` |
| Codex rollouts | `RODEX_CODEX_SESSIONS_ROOT`, otherwise `$CODEX_HOME/sessions`, otherwise `~/.codex/sessions` |

Path authorities: [`default_runtime_root_path`](../src/rodex/runtime.py),
[`daemon_socket_path` and `daemon_instance_path`](../src/rodex/daemon_client.py),
[installation retention](../src/rodex/installation.py), and
[`default_codex_sessions_root`](../src/rodex/source_configuration.py).
[Prompt configuration](PROMPT_SUBMISSION_FLOW.md#rule-files) owns the user override path.

Different `XDG_STATE_HOME` values create different catalogs/name domains; different
`RODEX_RUNTIME_DIR` values create different endpoint roots. Do not assume one
machine-wide name or daemon namespace. Live runtimes refresh their runtime paths
hourly; a refresh failure ends the affected runtime rather than leaving an
unaddressable detached session. Storage relocation is an
[offline operation](../INSTALL.md#update-or-relocate).

## Terminal settings

The `/rodex` menu selects `dark` (the default native screen) or `light` (root-thread
`agentMessage` commentary only); `dusk` is a reserved placeholder. `light` is a
content filter: final answers, `partial_answer`, tool bodies, and child messages are
not its transcript items. The native composer/control region stays available, and
unknown modal states, including approvals, fall back to native rendering. Selection
changes presentation, not execution, logging, or model input.
[`PRESENTATION_POLICIES`](../src/rodex/presentation_policy.py) owns the selectors;
[local-command handling](INTERACTION_PATHS.md#local-commands) owns menu admission.

Managed Codex defaults to `tui.animations=false` to avoid decorative spinner/shimmer
redraw work. `rodex --config tui.animations=true` explicitly restores it. The source
of launch defaults is [`runtime.py`](../src/rodex/runtime.py), not a second Codex config.

The optional [command-policy check](CLI.md#command-policy-check) uses a transient
tmux status row. That guide owns notice timing, priority, inactivity, and setup.

The Rodex bar shows `Working.` through `Working....` while the main Codex thread is
active. tmux advances the fixed-width dots once per second; Rodex publishes only
working/idle transitions, with no animation frames passing through the native TUI or
terminal parser. Completion, interruption, failure, or primary disconnect clears the
indicator and restores tmux's 15-second idle status interval. Child-thread activity
does not change the main thread's indicator.

The terminal window title rests at `[---------] NAME`. During active main-thread work,
the nine-cell field pulses every 250 milliseconds. Every five seconds it shows elapsed
work time for one second:
`59m23s` below an hour, `23h20m` below a day, and `10d23h20m` once days are needed.

The gateway coalesces title frames, skips obsolete ones when busy, and stops the title
deadline at idle. It also requests a native Codex redraw every second during
active work, preserving terminal size and composer input while advancing Codex's
`Working (… • esc to interrupt)` counter. Idle relaying remains event-driven.

tmux's 50,000-line history limit is installed before pane creation; it is not retroactive.
Managed Codex uses `--no-alt-screen` so rendered rows enter that history. Mouse behavior
inherits tmux's global value until `Ctrl-b m` (with the default prefix) toggles a session
override and briefly displays `Mouse: ON/OFF`, or `_mouse` sets one explicitly;
`_mouse SESSION inherit` removes that override.
During UI initialization and reconciliation, Rodex replaces an unbound `prefix/m` or
tmux's stock `select-pane -m` action with this fallback. Any other effective user binding
is preserved. Status shows `CTRL-B MODE` while tmux awaits the next key. Rodex owns
root `Ctrl-C`/`Ctrl-D`; conflicting user bindings fail initialization rather than
being replaced. Other custom keys remain untouched. Sharing transitions have their own bounded status
animation, independent of Codex's `tui.animations` setting. See
[`TmuxStatusPipeline`](../src/rodex/tmux_status.py) for status ownership.

Rodex's update notice is display-only and may appear in Codex's F2 warning center.
A managed TUI starts with `check_for_update_on_startup=false`; Rodex's bounded
read-only version lookup caches for 24 hours. Lookup or notice failures do
not block attachment. Rodex never installs updates. Run `codex update` outside Rodex
when choosing to update Codex. [Installation](../INSTALL.md) covers Rodex updates.

## CPU diagnosis

Measure interval CPU deltas, not just accumulated process time or lifetime `%CPU`.
Record the exact PID/start time, installation key, active runtimes, workload,
and sample duration. The visible daemon name identifies a release, not a build; several
retained implementations can share it. Never kill by that name alone.

Separate these costs before changing architecture:

- [Gateway and presentation](INTERACTION_PATHS.md#terminal-and-presentation): PTY
  bytes, queries, resize, and selected-view revisions. The gateway blocks on readiness
  and explicit deadlines; its lack of an idle timer does not mean the whole daemon
  performs no periodic work.
- [Analytics](ANALYTICS.md): one coordinator per daemon, burst batching, cold replay,
  append work, and failed-attempt recovery budgets. More sessions share that worker.
- Optional observers, `_tail` clients, context followers, and status animations have
  their own work. Distinguish these from native Codex/App Server child CPU.

Use `_stats-status SESSION` (already JSON) to correlate source/retry state, not as a CPU
measurement or a completeness guarantee. `catching_up`/`rollout_not_found` must not
be described as healthy just because the failure counter is zero.
[Health semantics](ANALYTICS.md#health-and-coverage) and the
[deterministic CPU benchmark](DEVELOPMENT.md#cpu-regressions) define useful evidence.
No machine-independent daemon CPU ceiling is asserted.

## Failed or mismatched runtime

Use `_running`, `_context --json` from the affected pane, and exact inspection before
acting. [Liveness classification](RUNTIME_ISOLATION.md#liveness-and-replacement)
distinguishes unreachable endpoints from permission, timeout, or identity errors.
An incompatible live runtime belongs to its retained installation; detaching does
not update it. Opening its name through the current command upgrades a
[supported retained runtime](RUNTIME_ISOLATION.md#retained-runtime-upgrade) once its
turn is idle. Busy turns explain the interruption risk and show both commands:
retry normally once idle, or use `rodex NAME --force-old` to reconnect through the
retained installation while its current work continues.
Never overwrite ownership markers, replace live SQL files, or infer
destruction authority from a display name or PID alone.

## Startup and resume failures

Separate saved-thread admission from fresh startup. Record the exact Rodex name,
Codex UUID, runtime/installation identity, App Server version, and failing stage.
A timeout is not a native missing-thread result and does not authorize replacing
history. Use [lifecycle admission](RUNTIME_ISOLATION.md#liveness-and-replacement)
and existing-only [SQL reads](SQL_SCHEMA.md#storage-and-transactions) to reconcile
identities without mutating the catalog.

- **Rodex waits for the Codex session before attachment:** inspect the owned
  runtime log and native startup protocol. [Primary discovery](RUNTIME_ISOLATION.md#primary-conversation-discovery)
  allows 30 seconds for the main acknowledgement; a few seconds of startup is normal.
  Temporary native helpers may coexist with main and must not be counted as competing
  main conversations. A TUI modal can precede thread loading and leave stderr quiet.
  The known 0.162.1
  [hook-review boundary](SECURITY.md#hook-admission-and-resume) explains why a
  successful fresh session did not prove saved-session resume would work.
- **Direct Codex reports its shared App Server is not ready:** inspect the exact
  socket and daemon log named in that error. This is a different server from the
  private App Server that Rodex launches. A live PID does not prove its socket path
  is reachable. In the investigated 0.162.1 case, `codex --no-daemon resume UUID`
  isolated the failed background-server route and opened the saved conversation.
  Check that the UUID has no active writer before using that diagnostic.

Preserve the distinction between an observed missing socket and its unknown removal
cause. Diagnose the owning server before restarting processes; a broad daemon restart
can affect other sessions. Startup owners are `ManagedSessionLifecycle` in
[managed_session_lifecycle.py](../src/rodex/managed_session_lifecycle.py) and
`_run_runtime_service` in [runtime.py](../src/rodex/runtime.py).
