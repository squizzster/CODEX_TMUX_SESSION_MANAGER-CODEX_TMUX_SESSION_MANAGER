# Rodex

Rodex runs the native Codex TUI in durable, named tmux sessions. Work can continue
after a terminal disconnect and resume by name, with Codex retaining its editor,
tools, approvals, and conversation.

Rodex adds an agent activity pane, exact-turn automation, overload recovery, prompt
rules, native shell-command hooks, and local statistics and traces. Retained
installations keep running sessions bound to their original implementation.

Development mode is **ALPHA**: this is an internal Linux pre-release with changing
interfaces. Requirements are Python **3.12+**, Linux `/proc` and `pidfd` support,
tmux, `uv`, and an authenticated stable Codex App Server **0.151.0+**. Optional hook
support is probed separately; see [prerequisites and startup](INSTALL.md#prerequisites).

## CLI and operations

- [Installation and updates](INSTALL.md) — prerequisites, per-user setup, retained
  installations, upgrades, relocation, and removal.
- [CLI workflows](docs/CLI.md) — routing, names and aliases, detach/resume, observation,
  exact-turn control, statistics, and traces.
- [Operations](docs/OPERATIONS.md) — local state, runtime files, terminal settings,
  CPU diagnosis, and failed-runtime handling.
## Architecture and technical contracts

- [Change guide](DESIGN.md) — choose the authority and boundary for a proposed change.
- [Architecture](docs/ARCHITECTURE.md) — system shape and domain owners.
- [Interaction paths](docs/INTERACTION_PATHS.md) — input/output, views, observers,
  local notices, overload recovery, and the effect audit.
- [Prompt submission flow](docs/PROMPT_SUBMISSION_FLOW.md) — prompt rules, interception,
  validation, and submission.
- [Runtime isolation](docs/RUNTIME_ISOLATION.md) — identity, capabilities, lifecycle,
  compatibility, and retained-runtime handoff.
- [Analytics](docs/ANALYTICS.md) — rollout admission, scheduling, traces, coverage, and health.
- [SQL schema](docs/SQL_SCHEMA.md) — persistence methodology, identity, lineage, and transactions.
- [Security and privacy](docs/SECURITY.md) — native hook admission, trust boundaries,
  local transports, executable assumptions, and content handling.

## Development and agent workflow

- [Development and validation](docs/DEVELOPMENT.md) — test gates, focused test selection,
  CPU regression checks, and documentation validation.
- [Rodex session-control agent instructions](.agents/skills/rodex-session-control/SKILL.md) —
  authoritative repository-local workflow for identifying, observing, and controlling
  one exact live session and turn.

## Reports and history

- [Native interaction findings](docs/INTERACTION_PIPELINE_REPORT.md) — dated Codex
  0.162.1 notice-placement and resume evidence, with verification limits.
- [Version tracker](VERSION_TRACKER.md) — pending changes and verified package milestones.
