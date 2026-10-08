# Rodex

Durable Codex TUI sessions in isolated tmux servers, with named reattachment,
exact-turn local automation, and rollout-derived analytics.

Development mode: **ALPHA** — internal Linux pre-release; breaking changes are allowed.
Start with [installation](INSTALL.md), then [CLI workflows](docs/CLI.md).
The package version and dependencies live in [pyproject.toml](pyproject.toml);
[current-contract tests](tests/test_current_contracts.py) pin compatibility generations.

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
