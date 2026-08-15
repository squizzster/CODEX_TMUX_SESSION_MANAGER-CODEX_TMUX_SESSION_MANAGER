# Codex, Rodex, and tmux

Rodex is a match-maker and launcher. Each identity keeps its own meaning:

- **Rodex session:** our secure UUID and internal `rodex_sessions.id`.
- **Codex session:** the real UUID allocated by Codex.
- **tmux session:** the exact tmux server socket path plus tmux session name.

The relationships are explicit SQL rows joined by `rodex_sessions_id`. A Rodex UUID
must never be presented or stored as a Codex UUID, or vice versa.

## Basic launch pipeline

1. `./rodex` validates the `codex` and `tmux` executables.
2. tmux starts a small supervisor directly; Rodex does not type commands with
   `send-keys`.
3. The supervisor starts one private Codex app-server on a short Unix socket and then
   starts the normal TUI with `codex --remote unix://...`.
4. Rodex asks that private app-server for its one loaded Codex session UUID.
5. One SQLite transaction creates the Rodex session, user/log rows, Codex match, and
   tmux match.
6. Rodex attaches the user's terminal to the ordinary Codex prompt. Codex arguments
   and slash commands such as `/status` continue to work normally.

## Lifecycle

- `Ctrl-b d` detaches while Codex, its app-server, and tmux continue running.
- Exiting the Codex TUI ends its supervisor and private app-server.
- A failure before SQL registration stops the exact new tmux session and leaves no
  partial database row.
- The runtime uses `$XDG_RUNTIME_DIR` when suitable, otherwise `/tmp/rodex-<uid>`.
  Unix sockets stay there because long project paths can exceed Linux socket limits.

The current PROTO milestone is the reliable new-session path. Reattachment commands,
recovery policy, richer navigation, and enterprise logging will be designed from real
use rather than assumed in advance.
