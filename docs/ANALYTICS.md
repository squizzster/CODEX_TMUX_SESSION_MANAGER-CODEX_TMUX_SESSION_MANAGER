# Analytics and rollout provenance

Rodex derives projections from Codex-owned rollouts; it does not own a second
conversation store. Analytics failures are fail-open for the TUI. They preserve
last-good projections rather than inventing successful results.

## Work ownership

[`SharedAnalyticsCoordinator`](../src/rodex/analytics.py) runs every runtime's
`AnalyticsRolloutWorker.poll_once` on one daemon thread. Each worker retains separate
source cursors, analyzer/trace state, event buffers, health, and retry deadlines.
The [scheduler](../src/rodex/analytics_scheduler.py) fairly coalesces protocol activity
until 0.5 seconds of quiet or five seconds of continuous work; overflow requests
reconciliation, not an unbounded event queue.

[`StatefulCodexProtocolAnalyticsAdapter`](../src/rodex/analytics_analyzer.py) owns
resident calculation. The full-replay `CodexProtocolAnalyticsAdapter` remains the
independent parity oracle. [pyproject.toml](../pyproject.toml) pins the analyzer
dependency; concrete loading stays behind the guarded worker factory so help/native
delegation do not import its private implementation.

## Source admission and append work

[`AnalyticsSourceCatalog`](../src/rodex/analytics_source_catalog.py) and the
[source reader](../src/rodex/analytics_source_reader.py) authenticate current-user
regular files under the configured [sessions root](OPERATIONS.md#local-state), using
no-follow, nonblocking opens, exact thread metadata, and complete-record boundaries.

Cold recovery follows exact thread UUIDs from lifecycle/activity or durable lineage
first. Only when historical activity lacks a child UUID does a startup-only fallback
inspect JSONL metadata in the root UUIDv7 three-day window. It reads first metadata
lines and authenticates parent closure; resident wakes never repeat that scan.
Parent topology is staged before a child. Clean children may omit inheritance markers;
their cutoff is zero. Inherited parent records are excluded while original physical
coordinates remain beside the filtered bytes, so trace/body references do not shift.

Resident reads consume newline-complete suffixes and extend the accepted-prefix
digest from new bytes. Cold startup, clean replay, and explicit body expansion re-hash
the accepted prefix. This assumes trusted append-only Codex rollouts; it is not
continuous authentication against hostile same-uid rewrites. Clean replay invalidates
cached lineage metadata with reader state, not just the byte cursor.

[`agent_trace`](../src/rodex/agent_trace.py) normalizes response metadata and typed
facts. Canonical lifecycle turn IDs stay strict UUIDs. Synthetic labels such as
`auto-compact-1` remain thread-scoped with gapped coverage rather than replacing an
active canonical turn. Exact spawn facts bind child to parent turn; durable lineage
is the restart fallback. First-linked timestamps apply only to legacy histories with
neither fact; contradictory exact evidence fails closed instead of guessing.
Message roles preserve Codex's `assistant`, `developer`, `user`, and `system`
identities; `unknown` remains the explicit fallback for absent or unsupported roles.

## Publication and recovery

The [registry analytics boundary](../src/rodex_registry/analytics_registry.py)
prepares a publication once, then commits accepted checkpoints, lineage, changed
statistics, trace suffix, and health in one identity-fenced transaction. SQL retries
reuse that immutable publication without rerunning analysis or source I/O under a
writer lock. [SQL publication rules](SQL_SCHEMA.md#publication-and-durability) define
independent statistics/trace compare-and-set heads and cumulative coverage.

A publication-sequence race discards the in-memory cursor and reloads SQL before
accepting more bytes. Deterministic failures park by authenticated source fingerprint;
I/O and SQLite operational errors remain eligible for delayed retry. Health-only
publication retries use the same coordinator without replaying rollouts. Failure
logging records exception type/code locations, not bodies or frame locals.

[`AnalyticsRecoveryBudget`](../src/rodex/analytics_recovery.py) owns a separate
failed-work deadline. Events, source growth, or retirement cannot reset it. Delay is
the larger of an exponential 30-second floor capped at 900 seconds and 99 times the
failed attempt's measured duration. The latter is uncapped and targets at most 1%
repeated failed-work duty per runtime, not a total daemon CPU ceiling. Successful
acceptance clears the budget; normal suffix processing has no such failure delay.

## Health and coverage

`rodex _stats-status SESSION` already emits JSON. Its fields are defined by
[`execute_statistics_command`](../src/rodex/statistics_commands.py):

- `worker_state` describes worker progress, not statistical completeness.
  `up_to_date` can coexist with `coverage_state=gapped`.
- `catching_up` with `rollout_not_found` means expected source coverage is unresolved.
  Zero `consecutive_failures` does not turn missing sources into healthy/complete data;
  good partial projections can still publish.
- `next_retry_at_utc` describes scheduled recovery. A null value does not mean no
  source/protocol wakes or no remaining work.
- Publication sequences and `calculated_at_utc` describe accepted snapshots, not the
  live turn or current CPU use. A health-only failure does not replace those snapshots.

`_stats`, `_stats-status`, `_agents`, and `_trace` require owned durable identity,
not live Codex/tmux/analyzer processes. `_trace --include-bodies` is an explicit
snapshot re-authentication path; follow is metadata-only. Body privacy and observer
plaintext limits are in [security](SECURITY.md#content-and-privacy).

## Retirement limits

Orderly retirement preserves accepted events on the same worker, allowing five
seconds of cooperative effort with a 0.5-second append-settle interval. Shared-worker
backlog counts against that budget. Only quiesced producers plus a final generation
can establish completion; otherwise the result is inactive or explicitly incomplete.
Health stores diagnostics when available; daemon logs cover storage failure.

Daemon shutdown waits up to ten seconds for runtime workers, then five seconds for
analytics. These caller budgets cannot preempt executing analyzer/SQL work. Later
activation requests reconciliation, but this is not a crash-durable repair queue or
a final-projection durability guarantee.

Evidence: [coordinator](../tests/test_rodex_analytics.py),
[scheduler](../tests/test_rodex_analytics_scheduler.py),
[source catalog](../tests/test_rodex_analytics_source_catalog.py),
[source reader](../tests/test_rodex_analytics_source_reader.py),
[analyzer parity](../tests/test_rodex_analytics_analyzer.py),
[recovery](../tests/test_rodex_analytics_recovery.py), and
[retirement](../tests/test_analytics_retirement.py).
