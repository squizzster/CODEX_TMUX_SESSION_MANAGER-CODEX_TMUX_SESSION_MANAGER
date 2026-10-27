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
  external-process integration test. Real tmux tests and
  [offline native submissions](../tests/test_prompt_submission_native.py) can still
  run; the latter use an isolated local model without credentials or model quota.
- `uv run pytest tests/test_managed_startup.py --require-live-startup` exercises
  actual TUI startup, detach/reopen, and isolated-runtime reuse without model prompts.
- `uv run pytest -m live_startup --require-live-startup` also includes
  [prompt-submission tests](../tests/test_prompt_handoff_live.py). Those submit real
  ordinary/Ultra/multiline prompts and can consume model quota. The marker is not
  synonymous with a no-model smoke test.

Long in-project `--basetemp` paths can overflow Unix-socket limits; use pytest's
normal temporary roots.
Native fixtures have emitted multithreaded `forkpty()` deprecation warnings on
Python 3.12; passing offline checks does not resolve that process-launch warning.

## Test selection

- CLI routing and compatibility: [command contract](../tests/test_command_contract.py),
  [current contracts](../tests/test_current_contracts.py), and
  [CLI behavior](../tests/test_rodex_cli.py). CLI tests verify that `_tail` returns the
  requested retained lines and exits, with streaming enabled only by `-f`/`--follow`.
- Installation and lifecycle: [shim discovery](../tests/test_install_shim.py),
  [retained installations](../tests/test_installation.py),
  [daemon](../tests/test_rodex_daemon.py), [runtime isolation](../tests/test_runtime_isolation.py),
  and [runtime adoption](../tests/test_runtime_registration_adoption.py).
  [Retained handoff admission](../tests/test_runtime_upgrade.py) checks idle-only
  upgrades and attachment without replacement; installation tests verify a busy turn
  completes across `--force-old` reconnect before its idle runtime is upgraded.
  [Activation retries](../tests/test_runtime_activation.py) distinguish registered
  runtimes from pending analytics. [Owned process lifecycle](../tests/test_process_lifecycle.py)
  and [receipt reconciliation](../tests/test_process_receipts.py) exercise gated
  startup, surviving group members, failed cleanup and retained finalization authority.
  Installation and shim tests also check the single versioned preparation notice,
  the copied source's release version, and silent reuse of a cached installation.
- Input and display: [terminal input](../tests/test_terminal_input.py),
  [gateway](../tests/test_terminal_gateway.py), [native composer](../tests/test_native_composer.py),
  [observer safety](../tests/test_observer_runtime_safety.py).
  Input and gateway tests cover split escape/UTF-8 tokens, streamed paste/reply
  ownership, title deferral, partial writes, and blocking without expired deadlines.
  [Native continuation](../tests/test_native_terminal_protocol.py) checks the next
  output after presentation changes, including delayed autowrap, scroll margins,
  cursor visibility, and deferred mode transitions. [Edit/resize comparisons](../tests/test_native_terminal_resize.py)
  check real tmux against projected wide cells, wrap links, sparse blank rows, and
  shifted character extents. An immediately correct screenshot alone does not
  verify these stateful boundaries; continue output and resize after the edit.
  [Cancellation](../tests/test_terminal_cancellation.py),
  [line operations](../tests/test_terminal_line_operations.py), and
  [Unicode profiles](../tests/test_terminal_unicode_profiles.py) must also run with
  both tmux 3.2a and 3.4 on `PATH`. The fixture binds interpretation to the server
  version; selectors, joiners, padding and margin behavior differ between profiles.
  [Exit/detach tests](../tests/test_tmux_shared_ctrl_c.py) use disposable tmux clients
  for countdown choices, ownership changes, reconnects and stale timers.
  [Tail tests](../tests/test_session_tail.py) cover line-selection grammar, follow
  flags, footer filtering, one compaction opening/completion, quoted text and
  scrollback rollover.
  [Status messages](../tests/test_tmux_status_messages.py) checks actual terminal
  colours, literal text, Ctrl-B, live fields after both expiry paths, and stale timers.
  [Working status](../tests/test_working_status.py) checks root activity, tmux-owned
  bar dots, and outer-title projection. [Terminal-title tests](../tests/test_terminal_title.py)
  pin timing and elapsed formats; gateway and live prompt-handoff tests cover
  coalescing, the idle reset, and the native elapsed timer.
- Persistence: [SQLite adversarial boundaries](../tests/adversarial/test_round3_sqlite_boundaries.py)
  and [hard-link identity fences](../tests/adversarial/test_sqlite_link_identity.py),
  plus the registry/analytics suites beside the relevant implementation tests.
- Rollout statistics and token accounting: start with
  [analyzer parity](../tests/test_rodex_analytics_analyzer.py), then add
  [worker/publication](../tests/test_rodex_analytics.py) or
  [trace normalization](../tests/test_rodex_agent_trace.py) only when that boundary
  is touched.
  Changes to turn outcomes or projection generations also need
  [lifecycle consistency](../tests/test_analytics_lifecycle_consistency.py) and
  [verified normalization rebuilds](../tests/test_analytics_normalization_rebuild.py).
- New effect-bearing paths: update the classification in
  [test_interaction_path_ownership.py](../tests/adversarial/test_interaction_path_ownership.py)
  and the [interaction inventory](INTERACTION_PATHS.md#effect-audit).
- Native command policy: [contract/helper tests](../tests/test_command_policy.py),
  [find/rg parser and admission tests](../tests/test_pre_tool_use_find_rg.py),
  [trust admission](../tests/test_command_policy_configuration.py),
  [tmux status](../tests/test_command_policy_status.py), and
  [native checks](../tests/test_command_policy_native.py). The native checks use
  installed Codex/tmux with an offline deterministic model fixture, no account or
  model quota. They cover native trust reuse, start/resume/fork protocol admission,
  command security, deny/rewrite decisions, concurrent managed sessions, and content privacy.
  They also verify Rodex-only automatic hook admission, extra enabled-hook fallback,
  and fresh and resumed managed `sleep 5` denial without persisted hook trust.
  Saved-session tests require the same UUID after runtime replacement and inspect
  current-turn tool output so replayed historical denials cannot satisfy the check.

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

[README.md](../README.md) is the complete documentation index; [DESIGN.md](../DESIGN.md)
helps select a change boundary. [VERSION_TRACKER.md](../VERSION_TRACKER.md) points to
current compatibility authorities. Keep contracts and verification limits in their
topic guides; Git retains historical findings and implementation changes.

For documentation-only changes:

- Inventory maintained Markdown with `git ls-files '*.md'`, excluding fixtures,
  generated output, and vendored content. Every maintained file, including root guides
  and the repository-local skill, needs a direct README link.
- Resolve relative links from their containing document; check paths, heading anchors,
  and referenced symbols. Prefer source-file links and exact symbols to line anchors.
- Verify CLI examples against `./rodex _help`, `./rodex _version`, and definitions.
  Keep model-submitting and destructive examples out of documentation checks.
- Run relevant existing contract tests and `git diff --check`; confirm the diff is
  documentation-only. No Markdown/link checker is configured in the repository.
- Report actual checks and unverified external links/native scenarios. Dated findings
  are evidence for their stated version and scope, not current compatibility guarantees.

The smallest existing compatibility check is:

```bash
uv run pytest tests/test_current_contracts.py tests/test_command_contract.py
```
