# Runtime isolation

Session isolation is a foundational invariant. A selector, display name, socket
pathname, client count or matching Codex thread is insufficient authority on its own.
Reads, input, protocol control and cleanup must act on the intended live incarnation.

## Current contracts

| Boundary | Current generation |
| --- | --- |
| Rodex package and subprocess version | 0.13.0a1 |
| SQLite registry | 20 (`rodex-v20.sqlite3`) |
| tmux ownership protocol | `rodex-isolated-tmux-v3` |
| WebSocket peer identity | `rodex-runtime-peer-v3` |
| Observer frames | `rodex-agent-observer-v3` |
| Machine envelopes | 4 |
| Agent trace | `rodex-agent-trace-v3` |
| Statistics projection | `rodex-statistics-v8` |

These contracts form a breaking ALPHA release. Old servers, schemas and wire shapes
are not adopted or migrated. Codex transcripts remain separately owned by Codex.

## Ownership pipeline

1. The launcher allocates a runtime ID and an independent server nonce before creation.
   It claims only a completely unmarked, empty tmux server at
   `tmux-v3-<runtime-id>.sock`. Each runtime has a separate server, so native pane
   movement cannot cross runtime boundaries.
2. The staged primary pane receives its runtime marker before the host starts. The host
   verifies its original server nonce and pane identity before claiming any endpoints
   or launching its App Server. Failure cleanup retains the original creation receipt;
   rediscovering an incumbent never grants a failed creator destruction authority.
3. Durable adoption compares the expected previous incarnation in one SQLite
   transaction. Clock readings record time but do not choose the winner. Exact complete
   tuple retries are idempotent. Pending readers may finish only an already committed
   incumbent. The same reentrant session-transition lock serializes participating
   readers, resume and rename operations.
4. Discovery compares session metadata with a guarded snapshot from the actual primary
   pane. Its only permitted concurrent transition is exact pending-to-registered
   completion. Endpoints must have that runtime's canonical names.
5. Rodex protocol and interaction connections require the runtime ID and server nonce
   at WebSocket admission, then verify the response on the connection actually used.
   The proxy verifies its native App Server peer against the retained live child process
   tree using Unix credentials and pinned process identities. Native TUI admission is
   restricted to the owning host's live process tree.
6. App-server, proxy, event and observer endpoint lifetimes share exclusive ownership
   locks and retain their bound socket inodes. A losing contender cannot unlink an
   incumbent; old cleanup cannot remove a replacement pathname. Stable lock files remain
   across listener restarts. Runtime keepalive similarly retains path descriptors.
7. Whole-runtime destruction requires the exact primary, the sole session and every
   affected pane's runtime marker. Unowned panes, extra sessions and unmarked observer
   candidates revoke destruction authority.

For recorded incarnations, `session_exists` reports whether the runtime is reachable
at its recorded endpoint. Read and resume callers retain the durable runtime ID
through that lookup; the name-only bootstrap probe cannot substitute for it.
A successful inventory without that runtime, a missing socket, or a refused owned Unix
socket establishes unreachability. Timeout, executor unavailability, malformed inventory,
ambiguous identity, invalid file type and permission failure remain errors. Negative
liveness is never cleanup authority and does not prove that externally orphaned children
have exited. Durable incarnation comparison and Codex's active-writer admission still
govern replacement.

## Client and observer lifecycle

Shared `Ctrl-C` detaches only the originating client. Private `Ctrl-C` ends the runtime
under the full destruction guard. tmux admits the key against its actual client object
and executes the nonblocking native command chain without a shell helper, deferred
confirmation, warning token or expiry callback. Membership is evaluated at native
dispatch, not assigned a hypothetical physical-key timestamp. `Ctrl-D` also detaches.

Observer creation publishes an authoritative operation receipt in tmux before splitting.
Competing or replacement coordinators reconcile that same operation. The immutable pane
launch command carries its operation ID, allowing recovery when the split response is
lost. Registration checks the candidate's actual membership and ownership, publishes
the binding last, and verifies it. An unknown split outcome cannot authorize another
split. Close retries retain their exact original pane; renewed work waits for uncertain
retirement rather than reusing a pane that an older command may still destroy.
Retirement retains a generation tombstone. New admission compares that predecessor,
so a delayed old creation command cannot recreate a retired pane or replace a newer one.

Observer frames and event subscriptions carry the complete runtime/server identity.
Pane IDs alone are insufficient because separate servers can both contain `%0`.

## Validation and review record

The original investigation independently established ownership, Ctrl-C, registration
and observer counterexamples. The missing offline patch was not available and its
reported test totals were not verified. The historical initiating pane-exchange action
remains unresolved; reproducing a reachable failure did not establish that it happened.

Three reviewers covered identity/registration, Ctrl-C, and observer/topology, then
compared their boundaries. Cross-review added creation-collision cleanup, connected
peer identity, competing observer coordinators and cleanup-after-lock-release cases.
Implementation replaced the initially proposed native confirmation with shared detach
to remove its deferred client-admission ambiguity.

### Corrections and reasons

| Finding | Implemented correction | Why this boundary matters |
| --- | --- | --- |
| F01: metadata and actual pane can disagree | Separate tmux servers, guarded primary discovery, canonical endpoint names and connected peer identity | The screen and protocol destination must belong to the same incarnation; labels alone do not establish membership. |
| F02–F04: departing client, hidden confirmation and delayed helper timing | Shared Ctrl-C detaches its originating native client; private exit uses the complete destruction guard | Removes asynchronous destructive confirmation and its divergent timers, warning state and processing-time window. |
| F05: pending creator/reader race | Complete-tuple idempotence, committed-incumbent recovery and reentrant transition locks | A reader may complete the same committed runtime without causing its creator to reject or terminate it. |
| F06: rejected durable adoption still published | Mandatory expected-incarnation comparison inside the transaction | A stale candidate cannot become usable after SQLite retains another runtime; clock rollback no longer chooses the winner. |
| F07–F08: ambiguous split and moved candidate | Authoritative operation receipts, candidate membership/provenance checks, reconciliation and retirement tombstones | A lost response cannot authorize duplicate creation, and a valid primary cannot confer ownership on an unrelated candidate. |
| F09: endpoint replacement and stale cleanup | Exclusive lifetime locks and retained socket inodes | Duplicate startup and old cleanup cannot remove a cooperating replacement's endpoint. |
| F10: unrelated malformed roster blocks a target | Per-runtime server inventories and per-server diagnostic failure containment | Another runtime's malformed roster is outside the target's resolution boundary; ambiguity within the target still fails closed. |
| F11: unknown liveness treated as absence | Separate proven endpoint unreachability from inventory, permission and execution errors | A failed query must not silently authorize replacement. |
| F12: uppercase human UUID rejected | Normalize the human selector before strict identity parsing | Human spelling is case-insensitive while machine identity remains canonical. |
| Native session switch misses sharing updates | Install the session-change hook alongside attach/detach hooks | Sharing presentation follows the actual client membership transition. |
| Guarded destructive no-op reports success | Explicit rejection branch in the native Ctrl-C command | Rejected exit remains observable on the originating client. |
| Keepalive path identity can repeat after replacement | Retain path descriptors for the complete keepalive lifetime | Pinning the original inode removes the tested unlink/recreate identity reuse. |

Cross-review also corrected failures exposed by the implementation itself: bootstrap
cleanup must retain its original nonce, empty logs must not be removed after releasing
endpoint ownership, native TUI transport requires host-process admission, and selectors
must be resolved again after waiting for their transition lock. These corrections are
covered by regression tests; they are not newly established historical incident events.

The current source was version 0.12.0a1 at the start of implementation, whereas the
supplied offline report described 0.10.1a1. Observer closure behavior therefore needed
review against the current code, rather than acceptance of the earlier line references.
The fix retries uncertain closure against the original pane and reconciles pending
creation. Unknown split outcomes without a discoverable candidate remain blocked.

Real integration also exposed a separate resume handoff problem with installed Codex
0.154.0. In a forced schedule, a second TUI reported the exact saved thread's active
writer conflict but stayed alive without loading it. It still had not loaded the
thread eight seconds after the original writer exited; restarting only that failed
TUI loaded the exact thread. The host now recognizes this error during unregistered
exact resume, stops its own failed TUI and uses the existing bounded retry window.
Unrelated errors and completed registration cannot take that path. This is an
availability correction found during validation, not evidence of the original exchange.

Stop/resume validation also found that terminating only the Codex launcher wrapper
could leave its native App Server child holding the saved thread's writer lock.
Managed and transient catalog App Servers now start in their own process sessions.
Cleanup first requests ordinary termination; after the existing three-second deadline,
it kills only that creation-owned process group before reaping its leader. This releases
a stalled native writer without extending the resume retry deadline or targeting other
runtimes. Graceful and stalled wrapper/native tests verify lock release and preservation
of an unrelated process; the real installed-CLI matrix exercises repeated stop/resume.

The clean-wheel workflow exposed a separate cold-start limit: native Codex database
initialization on the project filesystem took about 17 seconds before binding its socket.
App Server readiness now uses the same bounded 30-second allowance as managed startup.
Delayed readiness, early process exit and a missing socket have deterministic regression
coverage. The exact-resume writer-handoff deadline remains unchanged.

The executable acceptance evidence is maintained with the affected tests:

- `tests/test_runtime_isolation.py`: distinct native servers, repeated local pane IDs,
  movement attempts, foreign topology, guarded discovery, collision/timeout cleanup
  and liveness classification.
- `tests/test_tmux_shared_ctrl_c.py`: real PTY clients, delayed queues, exiting-but-still
  connected clients, terminal-name reuse, dispatch membership and changed topology.
- `tests/test_runtime_registration_adoption.py` and `tests/test_session_transition_lock.py`:
  expected-incarnation adoption, rollback, idempotence, coherent reads and fork/thread locks.
- `tests/test_observer_runtime_safety.py`: socket lifecycle, competing/replaced
  coordinators, lost replies, moved candidates and delayed pane retirement.
- Protocol peer tests: wrong runtime/server identity, same-thread endpoint substitution,
  native process ownership and absence of mutation before admission.
- `tests/test_managed_startup.py`: installed CLI, real authenticated Codex, isolated
  servers, terminal presentation, repeated attach and stop-to-resume selectors.
- `tests/test_app_server_shutdown.py` and `tests/test_runtime_writer_handoff.py`:
  native writer cleanup, unrelated-process preservation and exact bounded resume retries.

Run the complete acceptance suite with:

```bash
uv run pytest --require-live-startup --cov --cov-report=term-missing
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

Component selections overlap and are not additive. Characterization tests reproducing
old unsafe behavior are not safety passes. Bounded concurrency exploration is evidence
for the tested schedules, not proof about every possible schedule.

Rodex's OS trust boundary remains the Linux user account. Its cooperating-runtime
isolation contract does not make arbitrary hostile processes sharing that account into
separate security tenants; different OS users require separate OS permissions.
