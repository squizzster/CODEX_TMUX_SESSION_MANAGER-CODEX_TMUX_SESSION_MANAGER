# Codex, Rodex, and tmux

Rodex is a match-maker and launcher. Each identity keeps its own meaning:

- **Rodex session:** our secure UUID and internal `rodex_sessions.id`.
- **Codex session:** the real UUID allocated by Codex.
- **tmux session:** the exact tmux server socket path plus tmux session name.

Rodex and Codex UUIDs remain explicitly named on the owning session row. The tmux
endpoint is a separate operational row joined by `rodex_sessions_id`. Identities are
never presented or stored as another domain's identity.

## Basic launch pipeline

1. `./rodex` validates the `codex` and `tmux` executables.
2. tmux starts a small supervisor directly; Rodex does not type commands with
   `send-keys`.
3. The supervisor starts one private Codex app-server and a transparent Rodex
   WebSocket proxy on short Unix sockets, then connects the normal TUI through it.
4. Rodex asks that private app-server for its one loaded Codex session UUID.
5. One SQLite transaction creates the Rodex identity, permanent cool name, user/log
   rows, and tmux match.
6. tmux is renamed to the cool name and displays `Rodex: <cool-name>` in its status.
7. Rodex attaches to the ordinary Codex prompt; arguments and slash commands work.

The proxy forwards protocol frames unchanged in both directions. It counts unique
tool-like `item/started` notifications and publishes `Tools: <count>` through a tmux
user option. The count covers the current live runtime and resets when it is resumed.

## Named reattachment

- `./rodex <cool-name>` resolves the name through its integer identity.
- If its stored tmux endpoint is live, Rodex attaches to it directly.
- If it has ended, Rodex starts a fresh tmux/app-server and asks Codex to resume the
  stored Codex UUID; the observed UUID must match before the endpoint is replaced.
- Both routes preserve all Rodex/Codex identity and update `last_accessed_at_utc`.
- `./rodex running` (`--running`) lists the current POSIX user's live runtimes.
- `./rodex alias SESSION NAME` sets its portable preferred name; `-f` replaces it.

## Lifecycle

- `Ctrl-b d` detaches while Codex, its app-server, and tmux continue running.
- Exiting the Codex TUI ends its supervisor and private app-server; its cool name can
  transparently resume the saved Codex session later.
- A failure before SQL registration stops the exact new tmux session and leaves no
  partial database row.
- The runtime uses `$XDG_RUNTIME_DIR` when suitable, otherwise `/tmp/rodex-<uid>`.
  Unix sockets stay there because long project paths can exceed Linux socket limits.

Exact tmux targets and compensated name transitions preserve the recorded endpoint.
