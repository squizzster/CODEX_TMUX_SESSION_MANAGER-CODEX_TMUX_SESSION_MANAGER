# Security and privacy

## Trust boundary

Rodex is a local, single-Linux-user tool. Processes sharing its uid are not hostile
tenants; use separate OS accounts for that boundary. Rodex exposes no TCP/network
listener: control and protocol endpoints are private Unix sockets. Its checks prevent
misaddressing, stale ownership, and accidental executable drift, not deliberate edits
by the owning user. It is not an IDS or filesystem/tmux surveillance service.

[Runtime isolation](RUNTIME_ISOLATION.md) is authoritative for capability tuples,
connected peer/process checks, destructive topology guards, endpoint cleanup, and
compatibility. Names, compact IDs, inherited tmux context, or matching thread IDs
alone never authorize an operation. An orphan is safer than attaching to or deleting
the wrong runtime; unverifiable sessions are reported, not auto-adopted or removed.

## Executable admission

The [installed shim](../usr/local/bin/rodex) scans the checkout, complete virtualenv,
site packages, and bytecode before executing the preinstalled entrypoint. It rejects
untrusted ownership and group/world-write access. Symlinks are rejected except the
specific environment interpreter aliases and `lib64` case handled by the shim;
merely pointing inside the environment is not sufficient. `.git`, `.venv/.lock`, and
nonexecuting pytest/Ruff cache contents have explicit exemptions. Ignored scratch
directories are otherwise included. The absolute interpreter is separately resolved
and checked as a root/current-user-owned regular file without group/world write access.

The shim never syncs dependencies. Root execution against a user-owned checkout is
rejected; shared system commands need a root-owned installation. The repository-local
[launcher](../rodex) is a direct development entrypoint, not the installed shim's scan.
[`enter_fixed_installation`](../src/rodex/installation.py) then pins code/dependencies/
defaults before helpers start. [Installation](../INSTALL.md) owns update, retention,
and relocation procedures; do not bypass admission by rewriting manifests or markers.

## Files, sockets, and child environment

Runtime roots are real current-user mode-`0700` directories under an accepted private
or root-owned sticky parent. Sockets/logs are mode `0600`. Daemon endpoints and start
locks are implementation-namespaced; same-uid peer checks and bounded protocol decoding
keep live transport access bound to the owning build. The explicit
[idle upgrade handoff](RUNTIME_ISOLATION.md#retained-runtime-upgrade) runs through that
owner's retained interpreter before the current build starts a replacement incarnation.
[Runtime lifetimes](RUNTIME_ISOLATION.md#resource-lifetime)
cover descriptor transfer, endpoint aliases, receipts, and PID-reuse protection.

[SQLite storage admission](SQL_SCHEMA.md#storage-and-transactions) owns no-follow
opens, retained descriptors, cooperative locks, and synchronous identity checks.
It does not promise detection of every move between transactions or protection from
same-uid direct SQLite access that ignores the lock.

tmux's global environment is not caller authority. Startup stages an inert pane,
fences the runtime, installs byte-escaped caller values over tmux stdin, marks
global-only names removed, and only then starts the bridge. General environment
payload does not enter process arguments; cwd remains an explicit control argument.
[`environment_exec`](../src/rodex/environment_exec.py) removes names outside the
caller/tmux contract at host and observer exec boundaries. Same-uid concurrent
dynamic-loader mutation is outside this Python-launcher boundary.

Update notices terminate at a private downstream-only proxy endpoint: no extra upstream
connection, subscriber event, thread content, or model turn. Version checking is
read-only and fail-open; a nonblocking file claim chooses one refresh owner. Future
cache timestamps are stale, not a way to extend the 24-hour lifetime.

## Content and privacy

Codex owns message, command, tool, reasoning, and output bodies. Rodex SQLite retains
typed identities, source coordinates/hashes, sizes, capture state, and metrics, not
duplicate plaintext bodies. The [trace reader](../src/rodex_registry/agent_trace_reader.py)
reads SQL metadata; [`_attach_authenticated_rollout_bodies`](../src/rodex/agent_trace_commands.py)
expands an explicit snapshot request after re-authenticating recorded prefixes across
current/historical memberships. Follow mode remains metadata-only. That command
adapter's `_safe_event_body` and `_redact_message_content` exclude hidden reasoning;
[`agent_trace_privacy`](../src/rodex/agent_trace_privacy.py) supplies the shared
encrypted-value classifier/redactor for normalization and expansion.

Analytics reads only authenticated files under its configured root; its resident
append trust assumption is explicit in [analytics](ANALYTICS.md#source-admission-and-append-work).
The separate live context follower accepts an absolute exact-thread rollout, reads
bounded tails/appends for token counts, and retains no bodies. Metadata checks precede
bounded fingerprints; idle waits back off to two seconds and exact-thread activity
wakes it early. See the [proxy's rollout follower](../src/rodex/protocol_proxy.py).

The observer permits only:

- Completed `agentMessage` items authored by tracked children.
- The current App Server's explicit collaboration `prompt`, tied to the exact call.
- The latest completed root user message when that same exact turn requests the
  collaboration, separately labelled as provenance, not the delegated payload.

Text is bounded before encoding, stripped of terminal controls, and sent after pane
startup through private length-framed messages, not process arguments or a SQLite
plaintext copy. Missing/encrypted plaintext stays unavailable; never infer it from
child behavior. Other roots/turns, system/developer instructions, hidden reasoning,
commands, arbitrary tool arguments, and output bodies are excluded. Correlation and
pane state belong to [the observer pipeline](INTERACTION_PATHS.md#observer-state).

`_cat`/`_tail` read verified primary-pane plain text without creating another durable
conversation log. `_events` is a verified live protocol surface, not the restricted
observer view; do not assume all observation commands share the observer's filter.
Interaction outcome buffers contain metadata, not prompt bodies. Analytics failure
logs retain exception type/code locations, never exception bodies or frame locals.
