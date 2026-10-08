# Operations

## Local state

Keep runtime roots short: Linux Unix-socket limits can reject long checkout paths.
These locations belong to the current Linux user, not to the caller's project.

| Resource | Default and override |
| --- | --- |
| Rodex state | `$XDG_STATE_HOME/rodex`, otherwise `~/.local/state/rodex` |
| Catalog | `rodex-v21.sqlite3` under the state root; generation authority is [private_database_path.py](../src/rodex_sql/private_database_path.py) |
| Retained installations | `implementations/<implementation-sha256>` under the state root; absolute `RODEX_INSTALLATIONS_ROOT` overrides the store |
| Runtime root | suitable `$XDG_RUNTIME_DIR/rodex`, otherwise `/tmp/rodex-<uid>`; `RODEX_RUNTIME_DIR` overrides it |
| Daemon endpoint | `<runtime-root>/<implementation-sha256>.sock` |
| tmux endpoint | `<runtime-root>/tmux-v5-<runtime-id>.sock` |
| Service sockets | `<runtime-root>/{app,proxy,events}-<runtime-id>.sock` |
| Codex rollouts | `RODEX_CODEX_SESSIONS_ROOT`, otherwise `$CODEX_HOME/sessions`, otherwise `~/.codex/sessions` |

Path authorities: [`default_runtime_root_path`](../src/rodex/runtime.py),
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

The `/rodex` menu selects `light` (root commentary) or `dark` (native screen);
`dusk` is a reserved placeholder. This changes presentation, not execution, logging,
or model input. Native modal controls remain native. Selection and parsing constraints
are in [terminal presentation](INTERACTION_PATHS.md#terminal-and-presentation).

Managed Codex defaults to `tui.animations=false` to avoid decorative spinner/shimmer
redraw work. `rodex --config tui.animations=true` explicitly restores it. The source
of launch defaults is [`runtime.py`](../src/rodex/runtime.py), not a second Codex config.

The Rodex bar shows `Working.` through `Working....` while the main Codex thread is
active. tmux advances the fixed-width dots once per second; Rodex publishes only
working/idle transitions, with no animation frames passing through the native TUI or
terminal parser. Completion, interruption, failure, or primary disconnect clears the
indicator and restores tmux's 15-second idle status interval. Child-thread activity
does not change the main thread's indicator.

During an active main turn, Rodex also requests a native Codex redraw every three
seconds through its ordinary resize signal, preserving the actual terminal size and
composer input. This advances Codex's `Working (… • esc to interrupt)` elapsed counter
with decorative animations disabled. The gateway removes that refresh deadline as
soon as the turn becomes idle or disconnects; idle terminal relaying still blocks on
events.

tmux's 50,000-line history limit is installed before pane creation; it is not retroactive.
Managed Codex uses `--no-alt-screen` so rendered rows enter that history. Mouse behavior
inherits tmux's global value unless `_mouse` sets a session override. With the default
prefix, status shows `CTRL-B MODE` while tmux awaits the next key; custom prefix/root
bindings are not replaced. Sharing transitions have their own bounded status animation,
independent of Codex's `tui.animations` setting. See
[`TmuxStatusPipeline`](../src/rodex/tmux_status.py) for status ownership.

Rodex's update notice is display-only and may appear in Codex's F2 warning center.
A bounded read-only version lookup caches for 24 hours; lookup or notice failures do
not block attachment. Rodex never installs updates. Run `codex update` outside Rodex
when choosing to update Codex. [Installation](../INSTALL.md) covers Rodex updates.

## CPU diagnosis

Measure interval CPU deltas, not just accumulated process time or lifetime `%CPU`.
Record the exact PID/start time, implementation fingerprint, active runtimes, workload,
and sample duration. The visible name `rodexd_v0_15a1` identifies a release, not a build:
several retained implementations can share it. Never kill by that name alone.

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
not update it. Never overwrite ownership markers, replace live SQL files, or infer
destruction authority from a display name or PID alone.
