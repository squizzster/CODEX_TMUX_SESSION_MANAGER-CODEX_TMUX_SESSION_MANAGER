# Runtime isolation

A selector, display name, socket, client count, pane ID, or Codex thread is an
address or observation, not sufficient authority to act on a runtime.

## Compatibility boundary

[Current-contract tests](../tests/test_current_contracts.py) pin release, catalog,
tmux, daemon, process-receipt, peer, observer, machine, trace, and statistics versions.
Do not replicate that manifest in callers. Live handshakes also require the exact
loaded fingerprint; [retained installations](../INSTALL.md#retained-installations-and-compatibility)
keep helpers on that implementation across checkout updates.

Earlier catalogs, runtimes, and wire formats are not generally adopted or translated.
There is one deliberate exception: [`notify_legacy_runtime_resize`](../src/rodex/legacy_runtime_compat.py)
accepts the known pre-retention tmux-v4 coordinator, authenticates its v2 daemon
through a private process receipt and same-user socket, forwards resize, and pins
only its owned hook slots to a retained bridge. It cannot start, stop, adopt, access
an old catalog, or send model input. Unknown generations and foreign hooks remain fenced.

## Capability and admission

[`TmuxRuntimeCapability` and `TmuxSessionCapability`](../src/rodex/tmux_session_capability.py)
are the authority types. Runtime authority binds the absolute socket, server
incarnation, immutable tmux `$session_id`, primary `%pane_id`, and runtime ID.
Registered authority adds Rodex session, registry, SQL-row, Codex, and registration
identities. Rodex's 16-hex IDs are integrity discriminators, not bearer credentials.

[`RodexRuntimeLauncher`](../src/rodex/runtime.py) mints authority from a coherent,
uniqueness-checked roster. Every effect rechecks its applicable tuple at the exact
target; primary actions also require the pane ID. tmux predicates belong in direct
`if-shell -F` conditions, with payload-only `display-message` in the selected branch.
Rendering a predicate as display output changes literal `$`/`%` semantics.

The creation/admission sequence is:

1. Claim an entirely unmarked, empty tmux server dedicated to this runtime. Retain
   the creation nonce for cleanup; failure must not rediscover incumbent authority.
2. Reserve one runtime/operation in its implementation-scoped
   [`DaemonRuntimeManager`](../src/rodex/daemon.py). Different implementations cannot
   reconcile each other's reservations or child receipts.
3. Transfer exactly one pane TTY descriptor from the bridge. Verify same-uid peer,
   PID, pane, TTY, nonce, and reservation before ownership transfers. The daemon
   runtime becomes the sole TTY owner; a manager timeout cannot revoke it.
4. Under the session transition lock, commit the expected-incarnation SQL transition,
   then confirm registration and UI identity before releasing the lock. New runtimes
   advertise `pending`; an exact durable/pending pair can finish interrupted confirmation.
   Unconfirmed runtimes expire rather than becoming implicitly registered.
5. Admit protocol traffic only after both ends verify the runtime/server/fingerprint.
   [`RuntimePeerIdentity`](../src/rodex/runtime_peer.py) also pins native peers to the
   retained child process tree; a matching thread ID alone is insufficient.

Locks span identity-sensitive transitions, not terminal attachment. Attachment uses
immutable `$session_id`, so a concurrent alias cannot redirect it. An unambiguous
full-capability match can repair an externally renamed endpoint; ambiguous or foreign
matches cannot. Alias finalization uses compare-and-swap and compensates its own tmux
rename on failure. See [SQL incarnation rules](SQL_SCHEMA.md#identity-and-lineage).

## Liveness and replacement

`RodexRuntimeLauncher.session_exists` retains the expected incarnation through the query.

| Observation | Meaning |
| --- | --- |
| Successful inventory lacks the runtime; canonical socket absent; owned socket refuses connection | Proven unreachable |
| Timeout, unavailable executor, malformed/ambiguous inventory, invalid file type, permissions | Error, not negative liveness |

Unreachable is not destruction authority or proof that native children exited.
Replacement still requires the expected durable incarnation and Codex writer admission.
Concurrent opens serialize and converge on one runtime. Only the unregistered exact
resume path retries a departing active writer within its bounded handoff window;
unrelated failures and completed registrations cannot enter that path.

## Resource lifetime

- [`ExclusiveUnixEndpoint`](../src/rodex/runtime_endpoint.py) retains endpoint locks
  and inodes: losers cannot unlink incumbents, and stale cleanup cannot remove replacements.
  A child-created App Server rendezvous alias is allowed only after alias, private
  directory, physical socket, and listener ownership verification. Cleanup removes
  the retained alias, not the child's target.
- Daemon request decoding has a five-second absolute deadline and closes every
  rejected/truncated `SCM_RIGHTS` acquisition. An admitted bridge has the runtime's
  lifetime, not the framing timeout. Shutdown wakes incomplete decoders.
- Stop acknowledges `stopping` or `terminal`; only actual finalization proves terminal.
  Cancelled reservations cannot start. Completed contexts compact to at most 1,024
  small identity/outcome receipts without caller environments; evicted operations
  expire and cannot replay as new work.
- [`RuntimeProcessReceipts`](../src/rodex/process_receipts.py) bind native process
  groups to operation/runtime, PID, Linux start time, and uid. Parent-death guards
  protect daemon-owned children. Reconciliation verifies every field before signalling;
  termination escalates after three seconds only for the creation-owned group.
- Runtime cwd is an explicit reservation field passed to App Server and TUI.
  Setting `PWD` is not `chdir`; native directory options must not be applied twice.

## tmux and observer lifecycle

Whole-runtime destruction additionally proves the sole server session and ownership
of every affected pane. Foreign panes block it. [CLI lifecycle keys](CLI.md#open-detach-and-resume)
execute in native originating-client context; `exit-unattached` and
`destroy-unattached` are disabled so the final detach does not destroy a runtime.

Global client/layout hooks are wake-only because a detached session may no longer
exist when the hook runs. The coordinator inventories the roster and uses each
session's capability. It touches only owned hook indices, never local session hooks.
Existing foreign `C-c`/`C-d` bindings fail initialization. tmux has no atomic
bind-if-absent primitive: coordinate same-uid configuration during initialization;
readback cannot rule out a racing overwrite at the bind instant.

Resize hooks and the retained foreground bridge's `SIGWINCH` wake the same gateway.
It reads guarded logical pane dimensions because tmux's kernel PTY size can lag its
hook. Generic layout changes and pane swaps must both reach this path.

Observer creation publishes an operation receipt before splitting; a lost split
reply cannot authorize a second pane. Registration publishes its binding last.
Uncertain retirement retains the original target and generation tombstone; new work
waits for that outcome. Frames carry runtime/server identity because separate tmux
servers can both contain `%0`. [Observer state](INTERACTION_PATHS.md#observer-state)
owns visibility, not pane mechanics.

Evidence: [runtime isolation](../tests/test_runtime_isolation.py),
[daemon lifetimes](../tests/test_daemon_resource_lifetimes.py),
[runtime peers](../tests/test_runtime_peer.py),
[registration/adoption](../tests/test_runtime_registration_adoption.py),
[transition locks](../tests/test_session_transition_lock.py), and
[observer safety](../tests/test_observer_runtime_safety.py).
