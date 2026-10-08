# Development and validation

ALPHA means internal breaking changes are allowed, not that ownership or privacy
checks are optional. [Architecture](ARCHITECTURE.md) identifies owners;
[current-contract tests](../tests/test_current_contracts.py) pin public generations.

## Validation gates

Run from the project root. [pyproject.toml](../pyproject.toml) owns Ruff, pytest,
coverage, and build configuration; [conftest.py](../tests/conftest.py) defines the
live-gate option and [managed-startup tests](../tests/test_managed_startup.py) check
its prerequisites. No repository CI workflow or Markdown/link checker is configured.

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest --require-live-startup --cov --cov-report=term-missing
uv build
```

Coverage has a configured 70% floor. The full required live gate needs authenticated
Codex and real tmux. Missing prerequisites fail when `--require-live-startup` is set.
Live tests isolate SQL, tmux, and Codex history from user sessions.

Choose the gate deliberately:

- `uv run pytest -m 'not live_startup'` excludes live-startup cases, not every
  external-process integration test. Real tmux tests can still run.
- `uv run pytest tests/test_managed_startup.py --require-live-startup` exercises
  actual TUI startup, detach/reopen, and isolated-runtime reuse without model prompts.
- `uv run pytest -m live_startup --require-live-startup` also includes
  [prompt-handoff tests](../tests/test_prompt_handoff_live.py). Those submit real
  ordinary/Ultra/multiline prompts and can consume model quota. The marker is not
  synonymous with a no-model smoke test.

Do not put a temporary virtualenv inside the checkout: its extra symlinks can make
the installed shim's trust scan reject the project. Long in-project `--basetemp`
paths can also overflow Unix-socket limits; use pytest's normal temporary roots.

## Test selection

- CLI routing and compatibility: [command contract](../tests/test_command_contract.py),
  [current contracts](../tests/test_current_contracts.py).
- Installation and lifecycle: [retained installations](../tests/test_installation.py),
  [daemon](../tests/test_rodex_daemon.py), [runtime isolation](../tests/test_runtime_isolation.py),
  [runtime adoption](../tests/test_runtime_registration_adoption.py).
  [Retained handoff admission](../tests/test_runtime_upgrade.py) checks idle-only
  upgrades and attachment without replacement; installation tests verify a busy turn
  completes across `--force-old` reconnect before its idle runtime is upgraded.
- Input and display: [terminal input](../tests/test_terminal_input.py),
  [gateway](../tests/test_terminal_gateway.py), [native composer](../tests/test_native_composer.py),
  [observer safety](../tests/test_observer_runtime_safety.py).
  [Working status](../tests/test_working_status.py) checks root activity and tmux-owned
  dots in both the bar and terminal window title; live prompt-handoff tests verify
  the title cycle and idle reset alongside the native elapsed timer advancing without input.
- Persistence: [SQLite adversarial boundaries](../tests/adversarial/test_round3_sqlite_boundaries.py)
  and the registry/analytics suites beside the relevant implementation tests.
- New effect-bearing paths: update the classification in
  [test_interaction_path_ownership.py](../tests/adversarial/test_interaction_path_ownership.py)
  and the [interaction inventory](INTERACTION_PATHS.md#effect-audit).

## CPU regressions

```bash
uv run python tests/benchmark_terminal_cpu.py --baseline REVISION
```

[benchmark_terminal_cpu.py](../tests/benchmark_terminal_cpu.py) compares parser and
native-presentation CPU with a local Git revision using deterministic synthetic input.
It also checks output, control replies, and terminal state across randomized read
splits. Timings are diagnostic, not portable thresholds or production workload proof.
Regression tests enforce bounded parser feeds, no hidden-view wakes, and correct
visible-history invalidation. Pair them with [interval runtime measurements](OPERATIONS.md#cpu-diagnosis).

## Documentation changes

[README.md](../README.md) directly indexes every maintained Markdown file, including
root installation guidance and the automation skill. Keep implementation details in
source/tests; link to canonical explanations instead of copying them. Documentation
describes current behavior and constraints; historical investigations remain in Git.

For docs-only changes, check relative links/anchors, source paths, README coverage,
and `git diff --check`; run relevant existing contract tests. Verify command examples
against definitions or safe help execution, without launching quota-using tests merely
to validate prose. State which live or installation checks were not performed.
