# Phase II current boundary and remaining proposals

This document separates the application that exists now from capabilities that have not
been started. It is a review surface, not a committed roadmap. Every proposal below
requires explicit agreement before implementation.

## Current application

Rodex keeps ordinary Codex invocation and TUI behavior inside a durable, named tmux
runtime. A person may attach and intervene while an authorized caller:

- observes terminal progress with `_tail` and protocol activity with `_events`;
- reads durable lineage and typed activity with `_agents` and `_trace`;
- inspects, starts, steers, interrupts, waits for, and reads one exact turn;
- correlates dispatch acceptance without assuming server-side deduplication; and
- addresses sessions through verified durable and live identities.

The exact-turn coordinator is the only mutation-policy owner. The subscribed primary
connection owns lifecycle and user-input delivery when a short-lived mutation client
disconnects. The behavior is characterized in
[the live App Server evidence](APP_SERVER_0_147_LIVE_EVIDENCE.md) and revalidated against
installed Codex 0.150.1.

A new or resumed runtime uses the caller-selected working directory at launch. Rodex
does not follow a database, runtime root, or protected parent that moves while the
application is running; that is a terminal condition requiring a controlled restart.

## Unimplemented proposals

1. Add explicit machine lifecycle/discovery commands such as resumable `_sessions`,
   create-only `_spawn`, resume-only `_resume`, controlled restart/rehome, and workspace
   freshness fields.
2. Add a `_doctor` command, refresh exact generated-schema evidence for selected Codex
   releases, and define durable pending-attention and runtime-death reconciliation.
3. Bound duplicate cold lineage discovery across independent analytics workers. Twenty
   cold workers can still each perform the same bounded `N`-source metadata discovery;
   any shared solution must preserve authenticated provenance without restoring the
   rejected on-disk snapshot/cache, filesystem surveillance, or watcher machinery.
4. Bound very large analytics publications without splitting their atomic
   registry/session/runtime/Codex identity fence. A single publication can currently hold
   one SQLite writer transaction for an unbounded number of prepared trace/statistics rows.
5. Add an opt-in real-TUI reconnect replay and a prolonged CPU, tmux-process-rate, WAL,
   and disk-write soak. Deterministic transport, recovery, and resource-bound tests
   already cover the corresponding code paths.
6. Consider multi-agent task/message orchestration only after its authority, workspace
   ownership, approval routing, and retry semantics are explicitly designed. It must
   extend the existing exact-session pipeline rather than create a second runtime or
   conversation model.

## Continuing boundaries

- Transport remains private Unix-domain WebSockets; no TCP listener is planned.
- SQLite stores Rodex identity, provenance, coordination, and bounded outcome metadata,
  never a second conversation history.
- Cold analytics discovery remains process-local and uncached on disk; there is no
  snapshot, surveillance, inotify, or IDS mechanism.
- Names remain display/discovery aids. Mutation requires verified durable and live IDs.
- No destructive lifecycle action gains implied authority from an automation command.
- Database or protected-path movement is never treated as relocation authority.
- A future capability must enter through the existing canonical owner for its
  responsibility; it must not leave and later rejoin the pipeline through a side path.
