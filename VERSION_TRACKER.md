# Version tracker

This file owns the concise human-readable change history. [pyproject.toml](pyproject.toml)
owns the active package version, while
[current-contract tests](tests/test_current_contracts.py) pin wire and storage
generations. Package milestones below are verified from repository history; this
repository currently has no Git release tags, so they do not imply a published release.

## Unreleased

### Added

- A daemon now creates one random 256-bit incarnation capability at each start,
  publishes it privately, and requires it alongside the retained installation identity
  on every request and response.
- The README now links every maintained project guide and repository-local agent
  instruction directly.

### Modified

- Ordinary startup recognizes an unchanged checkout and environment from lightweight
  metadata, returns a valid cached retained interpreter before taking the publication
  lock, and uses a new opaque key when it must publish a changed installation.
- Daemon protocol v4 and the terminal bridge bind clients to one exact daemon
  incarnation. Retained-runtime handoff continues to admit supported daemon-v3 and
  daemon-v4 installations.
- Documentation now distinguishes installation routing, daemon-incarnation checks,
  and the single-user security boundary.

### Removed

- Routine startup no longer hashes Rodex source, installed dependency contents, or
  shipped configuration to identify itself.
- The installed shim no longer recursively scans the checkout and virtual environment
  as an intrusion-detection policy.

## 0.15.0a1 — 2026-09-20 — current package identifier

### Added

- Private retained installations pin code, installed dependencies, configuration,
  and interpreter paths for running sessions.

### Modified

- Terminal resize delivery and retained helper boundaries were strengthened, and the
  owned runtime, daemon, process-receipt, peer, observer, machine, trace, statistics,
  and catalog contracts advanced together.

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
