# Rodex

Rodex keeps the native Codex TUI running in durable, named tmux sessions. Detach
without stopping work, reconnect by name, and keep Codex's editor, tools and approvals.

Watch collaborating agents, follow readable terminal output, and control exact turns
from another shell or agent. Ordered prompt rules, overload recovery, context
reminders and bounded shell-search policy reduce repetitive supervision. Local
statistics and traces retain provenance without duplicating conversation bodies.

Rodex is an **ALPHA** Linux project. It requires Python **3.11+**, tmux, Linux `/proc`
and pidfd support, and an authenticated stable Codex App Server **0.151.0+**.
Checkout setup uses `uv`; no numeric tmux, SQLite or `uv` CLI minimum is asserted.
See [prerequisites and capability limits](INSTALL.md#prerequisites) and
[current versions](VERSION_TRACKER.md). Running sessions retain their owning implementation.
Local terminal reconstruction supports verified tmux 3.2a and 3.4 profiles;
other versions retain native display and terminal-owned replies.

## Install and use

- [Installation, environment discovery and updates](INSTALL.md)
- [CLI workflows, exit/detach and observation](docs/CLI.md)
- [Operations, local state and troubleshooting](docs/OPERATIONS.md)
- [Prompt rules and submission behavior](docs/PROMPT_SUBMISSION_FLOW.md)

## Develop and automate

- [Change guide: choose the authoritative owner](DESIGN.md)
- [Architecture and domain boundaries](docs/ARCHITECTURE.md)
- [Development checks and verification limits](docs/DEVELOPMENT.md)
- [Session-control agent instructions](.agents/skills/rodex-session-control/SKILL.md)
- [Current version and compatibility authorities](VERSION_TRACKER.md)

## Contracts and gotchas

- [Interaction routes, model input and local display](docs/INTERACTION_PATHS.md)
- [Runtime identity, retained upgrades and cleanup](docs/RUNTIME_ISOLATION.md)
- [Security, native hook trust and privacy](docs/SECURITY.md)
- [Find/rg command-policy scope and limits](docs/FIND_RG_COMMAND_POLICY.md)
- [Analytics provenance, recovery and coverage](docs/ANALYTICS.md)
- [SQLite storage, identity and schema standards](docs/SQL_SCHEMA.md)
