# Version tracker

This file owns the concise human-readable change history. [pyproject.toml](pyproject.toml)
owns the active package version, while
[current-contract tests](tests/test_current_contracts.py) pin wire and storage
generations. Package milestones below are verified from repository history; this
repository currently has no Git release tags, so they do not imply a published release.

## Unreleased

No changes yet.

## 0.16a — 2026-10-14 — current package identifier

Build metadata and wheel filenames normalize this PEP 440 spelling to `0.16a0`.

### Added

- Managed runtimes provide an active-prefix `m` fallback that toggles session mouse
  mode and briefly confirms `Mouse: ON/OFF` when the key is unbound or still uses
  tmux's stock pane-mark action, while preserving any non-stock user binding during
  startup and reconciliation.
- [Native interaction findings](docs/INTERACTION_PIPELINE_REPORT.md), dated
  2026-10-11, preserve Codex 0.162.1 notice-placement evidence and its verification limits.
- Native managed-session command-policy hook with volatile tmux-only command status,
  a typed allow/deny/rewrite-command extension point, and a first capability rule
  denying standalone integer sleeps below 10 seconds with model-visible feedback.
  The matcher accepts surrounding horizontal whitespace and one terminal semicolon.
  An exact Rodex-only hook inventory gets temporary native key/hash trust;
  another enabled hook retains `_policy-setup`. Decision and inactive-reminder rows
  last five seconds, expose tmux prefix mode, and restore the normal bar by exact
  claim token; successful admission clears stale inactive state.
- Production `PreToolUse` customer-policy library for direct `find`/`rg` workflows,
  with boundary-aware multiline extraction, declarative installed TOML rules,
  deterministic static outcomes, bounded live metadata census, exact rejection
  feedback, and no command execution, history, or persistence.
- Exact main-turn context stewardship at a 70% structured-usage crossing, with one
  asynchronous fixed steer per armed crossing, compaction suppression, final
  exact-turn admission, and no automatic retry after uncertain delivery.
- A daemon now creates one random 256-bit incarnation capability at each start,
  publishes it privately, and requires it alongside the retained installation identity
  on every request and response.
- Agent traces now preserve Codex `developer` messages as a first-class role through
  normalization, contract validation, SQL storage, and trace readback.

### Modified

- `evaluate_command_policy` now checks `find`/`rg` before continuing to the existing
  standalone short-sleep rule. Search evaluation has a combined workflow/deadline
  budget, native denial reasons are bounded, and the helper/broker timeout is now
  five seconds while the native hook retains its three-second limit.
- Rodex now targets Python 3.11 or newer using equivalent pre-PEP-695 typing syntax.
  Launcher discovery includes PATH interpreters and leaves missing-package or module
  failures visible after selecting a compatible Python.
- The installed shim now resolves only existing, executable Rodex environments for the
  invoking user: explicit, checkout, active-virtualenv, uv-tool, and system-Python
  candidates. It performs no installation, synchronization, download, or recursive
  scan and reports an unsupported environment without modifying it.
- Saved-session resume no longer waits behind Codex 0.162.1's hidden hook-review
  prompt. Automatic admission verifies native trust after a provisional App Server
  restart instead of relying on the TUI bypass flag. Native start/resume/fork and
  same-name runtime replacement tests preserve UUIDs, permissions, and user hook state.
- Documentation refactor: complete README index, a change-boundary guide, consolidated
  interaction/hook contracts, dated native findings, and explicit separation of the
  launch shim from owner-provisioned checkout, uv-tool, virtualenv, or system
  environments. Missing-package, isolated-user-site, resume, notice, retry, and
  model-input gotchas are retained; application behavior is unchanged.
- The [session-control skill](.agents/skills/rodex-session-control/SKILL.md) now
  distinguishes model input from local notices, clarifies native command-policy
  status, and accounts for overload recovery starting a separate turn.
- Forward-contract checks characterize Codex 0.162.1's `partial_answer` phase
  without changing the 0.151.0 minimum: it is not a final result or light-view
  commentary, and trace metadata uses the existing `unknown` phase fallback.
- Ordinary startup recognizes an unchanged checkout and environment from lightweight
  metadata, returns a valid cached retained interpreter before taking the publication
  lock, and uses a new opaque key when it must publish a changed installation.
- Daemon protocol v5 carries the per-runtime policy endpoint; the terminal bridge
  and daemon bind clients to one exact daemon
  incarnation. Retained-runtime handoff continues to admit supported daemon-v3 and
  daemon-v4 installations, alongside daemon-v5.
- The current-only catalog is generation 22 (`rodex-v22.sqlite3`) and the agent-trace
  contract is v5. Statistics projection v10 counts each canonical response-usage
  record once while retaining cumulative snapshots as a legacy fallback. Generation
  21 remains untouched and is not implicitly migrated.

### Removed

- Routine startup no longer hashes Rodex source, installed dependency contents, or
  shipped configuration to identify itself.
- Recognized developer messages are no longer collapsed into the `unknown` role.
- The installed shim no longer recursively scans the checkout and virtual environment
  as an intrusion-detection policy.

## 0.14.0a2 — 2026-09-18 — historical package identifier

### Modified

- Server-overload recovery was bound to the correct live root session across stale
  runtime observations.

## 0.14.0a1 — 2026-09-17 — historical package identifier

### Added

- One implementation-scoped daemon replaced per-runtime Python hosts and coordinated
  runtime workers and analytics.

## 0.13.0a1 — 2026-09-17 — historical package identifier

### Added

- Runtime ownership was isolated with explicit tmux, process, peer, and persistence
  identity boundaries.

Earlier alpha milestones remain available in Git history; this tracker keeps only the
context needed to understand the current retained-runtime architecture.
