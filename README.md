# Rodex

**Whenever you type `codex`, try `rodex` instead.**

Rodex wraps the real Codex TUI in a durable, named tmux session. You keep Codex's
editor, tools, approvals, and conversation while Rodex adds detach/reconnect,
live agent visibility, resilient overload recovery, and exact local automation.

It is useful when work should survive a terminal disconnect, continue unattended,
or be inspected and controlled safely from another shell or agent. Retained
installations keep already-running sessions on the implementation that started them.

- **Let Codex cook.** Detach, leave it running on its host, and come back by a
  memorable name like `automatic-beluga`.
- **Watch the crew.** Your agents' progress and replies unfold in a live pane
  above Codex. Keep working below.
- **Less babysitting.** When a turn fails from server overload, Rodex waits and
  sends `Continue...` for you. Repeated overloads back off; typing cancels a pending retry.
- **Keep long turns oriented.** At an observed 70% context crossing, Rodex schedules
  a guarded reminder for the active main turn; cancellation and refusal remain explicit.
- **Take the controls.** Start, steer, interrupt, and collect exact-turn results
  from another shell or agent.
- **Understand the work.** Inspect durable local statistics, agent relationships,
  and metadata-only traces without creating a second conversation store.
- **Make it yours.** Change prompts, presentation, and session behaviour in Python,
  without rebuilding Codex. Simple prompt rules live in YAML.
- **Bound expensive searches.** A native pre-tool policy explains unsafe direct
  `find`/`rg` requests before execution while preserving Codex approvals and sandboxing.

Rodex **0.17a** is an **ALPHA** in-house Linux pre-release; interfaces may change.
Prompt rules transform structured submissions; native drafts and recall retain the
original input. See the [submission contract](docs/PROMPT_SUBMISSION_FLOW.md) and
[release changes](VERSION_TRACKER.md#017a--2026-10-14--current-package-identifier).

## Requirements and quick start

Rodex requires Linux with `/proc` and `pidfd` support, Python 3.11 or newer, tmux, and
an authenticated stable Codex CLI whose App Server is version 0.151.0 or newer.
Checkout setup additionally requires `uv`; an installed shim can use another existing
Rodex environment without it. No numeric minimum tmux or `uv` CLI version is currently
asserted. See the [verified prerequisites](INSTALL.md#prerequisites) for the exact
boundary. A system-installed `rodex` shim is a resolver, not a bundled application:
each user also needs an [accessible Rodex environment](INSTALL.md#command-and-environment-are-separate).

From a checkout:

```bash
uv sync --locked
./rodex
```

Detach with `Ctrl-D` or `Ctrl-b d`. With the default tmux prefix, `Ctrl-b m`
toggles mouse mode and briefly confirms `Mouse: ON/OFF` unless the user's tmux
configuration defines another `m` action.
Return with `./rodex NAME`.

## Find the right authority

Start with the row matching your task; follow its source/test links as needed.
Current contracts belong to the topic guides, not the dated reports.

| Task | Start here |
| --- | --- |
| Install, update, provision another user, or relocate | [Installation](INSTALL.md) |
| Run commands, reconnect, or diagnose a session | [CLI workflows](docs/CLI.md) → [Operations](docs/OPERATIONS.md) |
| Inspect or control one exact live turn | [Session-control agent instructions](.agents/skills/rodex-session-control/SKILL.md) |
| Change implementation and select tests | [Change guide](DESIGN.md) → [domain owners](docs/ARCHITECTURE.md) → [validation gates](docs/DEVELOPMENT.md) |
| Change prompt rules or understand the expansion notice | [Prompt submission](docs/PROMPT_SUBMISSION_FLOW.md) |
| Trace forwarding, local notices, or automatic model input | [Interaction ownership](docs/INTERACTION_PATHS.md), including the [70% reminder](docs/INTERACTION_PATHS.md#context-threshold-steering) |
| Diagnose startup or change runtime lifetime/upgrade behavior | [Runtime isolation](docs/RUNTIME_ISOLATION.md), including [primary discovery](docs/RUNTIME_ISOLATION.md#primary-conversation-discovery) |
| Interpret statistics/trace coverage or change persistence | [Analytics](docs/ANALYTICS.md) → [SQL authority](docs/SQL_SCHEMA.md) |
| Review trust/privacy or command-policy decisions | [Security](docs/SECURITY.md) → [find/rg policy](docs/FIND_RG_COMMAND_POLICY.md) |
| Consult historical evidence | [Version tracker](VERSION_TRACKER.md), [dated native interaction findings](docs/INTERACTION_PIPELINE_REPORT.md) |
