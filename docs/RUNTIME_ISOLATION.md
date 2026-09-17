# Runtime isolation

Every operation must address one proven live incarnation. A selector, display name,
socket path, client count, pane ID, or Codex thread is insufficient authority alone.

## Contract manifest

| Boundary | Current generation |
| --- | --- |
| Rodex package and subprocess | `0.13.0a1` |
| SQLite registry | `20` (`rodex-v20.sqlite3`) |
| tmux ownership | `rodex-isolated-tmux-v3` |
| WebSocket peer identity | `rodex-runtime-peer-v3` |
| Observer frames | `rodex-agent-observer-v3` |
| Machine envelopes | `4` |
| Agent trace | `rodex-agent-trace-v3` |
| Statistics projection | `rodex-statistics-v8` |

Rodex adopts no earlier runtime, schema, or wire generation. Codex separately owns its
transcripts.

## Ownership pipeline

1. The launcher allocates a runtime ID and server nonce, then claims only a completely
   unmarked, empty tmux server at `tmux-v3-<runtime-id>.sock`. A separate server for each
   runtime prevents native pane movement across runtime boundaries.
2. The staged primary pane receives its runtime marker before the host starts. The host
   verifies the original server nonce and pane identity before opening endpoints or its
   App Server. Failure cleanup retains the original creation receipt and cannot acquire
   an incumbent's destruction authority.
3. Durable adoption compares the expected prior runtime ID in one SQLite transaction.
   An exact complete-tuple retry is idempotent; wall-clock order never selects a winner.
   One reentrant session-transition lock serializes participating reads, resume, rename,
   publication, and registration.
4. Discovery compares durable metadata with a guarded snapshot from the actual primary
   pane. Only exact pending-to-registered completion may occur concurrently. Every
   endpoint must use that runtime's canonical name.
5. WebSocket admission and its response both prove the runtime ID and server nonce on
   the connection in use. Unix peer credentials and pinned process identities restrict
   native App Server and TUI admission to the owning host's live process tree.
6. App Server, proxy, event, observer, and keepalive lifetimes retain exclusive locks,
   bound socket inodes, or path descriptors. A contender cannot unlink an incumbent,
   and stale cleanup cannot remove a replacement endpoint.
7. Destruction requires the exact primary, the sole session on its server, and the
   runtime marker on every affected pane. An extra session or unowned pane rejects it.

## Liveness classification

`session_exists` preserves the durable runtime ID through its endpoint query.

| Observation | Result |
| --- | --- |
| Successful inventory lacks the recorded runtime | Unreachable |
| Canonical socket is absent | Unreachable |
| Connection to an owned Unix socket is refused | Unreachable |
| Timeout or executor unavailable | Error |
| Malformed or ambiguous inventory | Error |
| Invalid file type or permission failure | Error |

Only a proven unreachable result is negative liveness. It is not cleanup authority and
does not prove orphaned child processes exited. Durable incarnation comparison and the
Codex active-writer admission still govern replacement.

## Client lifecycle

- Shared `Ctrl-C` detaches the originating tmux client.
- Private `Ctrl-C` destroys the exact runtime only after the full guard passes.
- `Ctrl-D` and `Ctrl-b d` detach the originating client.
- tmux evaluates membership on the actual client during native dispatch. No shell
  helper, deferred confirmation, warning token, or expiry timer mediates `Ctrl-C`.

## Observer lifecycle

Observer creation publishes an operation receipt before splitting. Its immutable launch
command carries that operation ID, so another coordinator can reconcile a lost split
reply without authorizing a second split. Registration verifies membership and ownership,
publishes the binding last, and verifies it again.

Uncertain retirement keeps the original pane target and generation tombstone. New work
waits for that pane's outcome, preventing a delayed old command from recreating a retired
pane or replacing a newer one. Observer frames and subscriptions carry runtime and server
identity because separate tmux servers may both contain pane `%0`.

## App Server process lifecycle

Managed and transient App Servers run in their own process sessions. Cleanup sends
ordinary termination, then after three seconds kills only the creation-owned process
group and reaps its leader. This releases a stalled native Codex writer without targeting
another runtime.

Cold App Server readiness has the same bounded 30-second allowance as managed startup.
An unregistered exact resume that encounters the saved thread's active writer stops its
failed TUI and retries inside the existing bounded handoff window. Unrelated errors and
completed registrations cannot enter that path.

## Verification map

| Contract | Executable evidence |
| --- | --- |
| Server and topology ownership, discovery, cleanup, liveness | `tests/test_runtime_isolation.py` |
| Connected runtime, server, and process identity | Runtime-peer and protocol-peer tests |
| Expected-incarnation publication and coherent reads | `tests/test_runtime_registration_adoption.py` |
| Cross-process and reentrant transitions | `tests/test_session_transition_lock.py` |
| Native originating-client `Ctrl-C` behavior | `tests/test_tmux_shared_ctrl_c.py` |
| Observer socket, operation, pane, and retirement safety | `tests/test_observer_runtime_safety.py` |
| Installed Codex and isolated live lifecycle | `tests/test_managed_startup.py` |
| Process-group shutdown and writer release | `tests/test_app_server_shutdown.py` |
| Exact bounded resume retry | `tests/test_runtime_writer_handoff.py` |

Run the complete release gate:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest --require-live-startup --cov --cov-report=term-missing
uv build
```

Rodex isolates cooperating runtimes within one Linux user account. It does not isolate
arbitrary hostile processes sharing that account; use separate OS users for that boundary.
