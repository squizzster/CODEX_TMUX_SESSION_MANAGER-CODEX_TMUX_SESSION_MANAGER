# Rodex design

Codex owns the editor, model interaction, approvals, and conversation. Rodex's Python
layer adds durable session identity, tmux hosting, presentation, and local automation.

Each runtime has its own tmux server; retained installations keep running sessions
on their original implementation. Equivalent operations share an authoritative
pipeline, with identity checked before effects.

Pick the topic relevant to your task. Each guide links to its source and tests;
there is no need to read the whole set.

## Design map

| Working on | Read |
| --- | --- |
| System shape and domain owners | [Architecture](docs/ARCHITECTURE.md) |
| Input/output, views, observer, and overload recovery | [Interaction paths](docs/INTERACTION_PATHS.md) |
| Prompt rules and submission | [Prompt flow](docs/PROMPT_SUBMISSION_FLOW.md) |
| Runtime identity, lifecycle, and upgrades | [Runtime isolation](docs/RUNTIME_ISOLATION.md) |
| Rollout statistics, traces, and coverage | [Analytics](docs/ANALYTICS.md) |
| Persistence, schema, and lineage | [SQL](docs/SQL_SCHEMA.md) |
| Executable trust and content privacy | [Security](docs/SECURITY.md) |

## Using and changing Rodex

- [Install and update](INSTALL.md) — prerequisites and retained installations.
- [CLI](docs/CLI.md) — invocation, names, detach/resume, and commands.
- [Automation skill](.agents/skills/rodex-session-control/SKILL.md) — control one verified session and turn.
- [Operations](docs/OPERATIONS.md) — local state, terminal settings, and diagnosis.
- [Development](docs/DEVELOPMENT.md) — validation and focused test selection.
- [Version tracker](VERSION_TRACKER.md) — pending changes and verified package milestones.

Release and dependency facts live in [pyproject.toml](pyproject.toml);
[current-contract tests](tests/test_current_contracts.py) pin compatibility generations.
