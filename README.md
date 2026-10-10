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
- **Take the controls.** Start, steer, interrupt, and collect exact-turn results
  from another shell or agent.
- **Understand the work.** Inspect durable local statistics, agent relationships,
  and metadata-only traces without creating a second conversation store.
- **Make it yours.** Change prompts, presentation, and session behaviour in Python,
  without rebuilding Codex. Simple prompt rules live in YAML.

Rodex is an **ALPHA** in-house Linux pre-release; interfaces may change.

## Requirements and quick start

Rodex requires Linux with `/proc` and `pidfd` support, Python 3.12 or newer, tmux,
`uv`, and an authenticated stable Codex CLI whose App Server is version 0.151.0 or
newer. No numeric minimum tmux or `uv` CLI version is currently asserted. See the
[verified prerequisites](INSTALL.md#prerequisites) for the exact boundary.

From a checkout:

```bash
uv sync --locked
./rodex
```

Detach with `Ctrl-D` or `Ctrl-b d`. Return with `./rodex NAME`.

## Documentation

### Install and operate

- [Installation and updates](INSTALL.md) — prerequisites, per-user setup, retained
  installations, upgrades, relocation, and removal.
- [CLI workflows](docs/CLI.md) — routing, names and aliases, detach/resume, observation,
  exact-turn control, statistics, and traces.
- [Operations](docs/OPERATIONS.md) — local state, runtime files, terminal settings,
  CPU diagnosis, and failed-runtime handling.
- [Version tracker](VERSION_TRACKER.md) — pending changes and verified package milestones.

### Understand the system

- [Design map](DESIGN.md) — the shortest route to the authoritative technical guide.
- [Architecture](docs/ARCHITECTURE.md) — system shape and domain owners.
- [Interaction paths](docs/INTERACTION_PATHS.md) — input/output, views, observers,
  overload recovery, and the effect audit.
- [Prompt submission flow](docs/PROMPT_SUBMISSION_FLOW.md) — prompt rules, interception,
  validation, and submission.
- [Runtime isolation](docs/RUNTIME_ISOLATION.md) — identity, capabilities, lifecycle,
  compatibility, and retained-runtime handoff.
- [Analytics](docs/ANALYTICS.md) — rollout admission, scheduling, traces, coverage, and health.
- [SQL schema](docs/SQL_SCHEMA.md) — persistence methodology, identity, lineage, and transactions.
- [Security and privacy](docs/SECURITY.md) — trust boundaries, local transports,
  executable assumptions, and content handling.

### Develop and automate

- [Development and validation](docs/DEVELOPMENT.md) — test gates, focused test selection,
  CPU regression checks, and documentation validation.
- [Rodex session-control agent instructions](.agents/skills/rodex-session-control/SKILL.md) —
  authoritative repository-local workflow for identifying, observing, and controlling
  one exact live session and turn.
