# Prompt submission flow

Rodex owns prompt admission. Codex owns its editor, optimistic history rendering, request
construction, and attachments. The App Server owns turns, persistence, and model events.
The preparation receipt joins the pre-Codex text transformation to the later structured
request without applying a non-idempotent YAML rule twice.

```mermaid
flowchart TB
    subgraph TERM[Terminal and tmux transport]
        U[User keyboard] --> TC[Terminal emulator<br/>and tmux client]
        TC --> TS[tmux server<br/>routes keys to the pane]
        TS --> GI[Raw pane TTY<br/>Rodex gateway input]
    end

    subgraph EARLY[Interactive pre-submit admission]
        GI --> TD[Decode UTF-8, paste,<br/>control keys and replies]
        TD -- Printable text or paste --> FP[Track bounded candidate<br/>and forward unchanged]
        FP --> TUI[Native Codex TUI and editor]
        TD -- Enter: CR 0x0D or LF 0x0A --> HOLD[Hold the original Enter<br/>cursor remains after current draft]
        HOLD --> V{Visible composer is exactly<br/>the candidate with cursor at end?}
        SCREEN -. Pane snapshot<br/>for example: › Hello + end cursor .-> V
        V -- Yes --> H[Prompt admission<br/>stat and load YAML snapshot<br/>apply ordered text edits once]
        H --> ROUTE{Admission source?}
        ROUTE -- Verified terminal draft --> REWRITE[Queue editor replacement<br/>DEL × draft length<br/>bracketed paste canonical text<br/>original Enter last]
        V -- No, empty, slash command,<br/>or shell escape --> PASS[Forward original Enter unchanged]
        REWRITE --> TUI
        PASS --> TUI
    end

    I[Managed initial prompt argv] --> CI[Characterized Codex CLI parse]
    CI --> H
    ROUTE -- Managed initial prompt --> ARGV[Canonical prompt argv]
    ARGV --> TUI

    TUI -- After Enter or initial prompt --> KIND{Codex input classification}
    KIND -- Model or queued input --> RPC[turn/start, turn/steer,<br/>queue/add, or queue/update]
    KIND -- Native command, shell<br/>or another local dialog --> GO
    RPC --> PX[Rodex protocol-input pipeline]
    C[Rodex control routes<br/>_start, _steer and queued input] --> PX
    PX --> R{Matching preparation receipt?}
    R -- Yes --> ONCE[Consume receipt<br/>do not transform again]
    R -- No --> FH[Structured prompt-hook fallback<br/>for unverified and control routes]
    FH --> ONCE
    ONCE --> AS[Codex App Server]
    AS --> P[Turn lifecycle<br/>persistence and model]

    subgraph OUTPUT[One output and display path]
        P --> EV[App Server events]
        EV --> PO[Rodex protocol-output pipeline]
        PO --> TUI
        TUI -- Child PTY ANSI output --> GO[Rodex TERMINAL_OUTPUT<br/>native projection and surface renderer]
        GO --> TS
        TS -- Screen updates --> SCREEN[tmux pane and attached<br/>terminal surface shown to user]
    end
```

Ordinary draft characters cross tmux and Rodex immediately, so Codex has already rendered
the visible composer text when Enter arrives. The pane is raw: tmux transports CR or LF
but does not echo a visible Enter character. Rodex holds that byte while it drains pending
editor output and confirms the exact visible prefix and end cursor. At this point the
cursor remains after the draft on the current composer line; no submitted-history row or
new composer line exists yet.

For a verified draft, Rodex queues deletion, canonical bracketed-paste text, and the
user's original Enter in that order. This is an input-order guarantee, not a promise that
the canonical editor state will appear as a separately observable screen frame before
Codex consumes Enter. Codex nevertheless classifies, optimistically renders and submits
the canonical text. Its following primary request consumes the matching preparation
receipt rather than applying a potentially non-idempotent rule twice.

The former path began at `Codex TUI → RPC → structured prompt hook`. Codex had already
added the raw draft to optimistic history before the proxy could rewrite the RPC, so
display, model input and persistence could disagree. The corrected verified path moves
the edit ahead of native Enter. The structured boundary remains the fallback for
unverified editor state and the primary admission point for exact-control traffic.
