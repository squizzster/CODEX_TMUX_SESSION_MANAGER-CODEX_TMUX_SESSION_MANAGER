# Prompt submission flow

Rodex owns prompt admission. Codex owns its editor, optimistic history rendering, request
construction, and attachments. The App Server owns turns, persistence, and model events.
The preparation receipt joins the pre-Codex text transformation to the later structured
request without applying a non-idempotent YAML rule twice.

```mermaid
flowchart LR
    U[User keyboard input] --> TG[Rodex terminal gateway]
    TG --> TD[Terminal decoder and interceptor]
    TD --> V{Native draft verified at Enter?}

    V -- Yes --> H[Prompt admission<br/>stat + load YAML snapshot<br/>apply ordered text edits]
    H --> E[Update the same Codex editor<br/>before releasing Enter]
    E --> TUI[Codex TUI<br/>classify + optimistic canonical display<br/>preserve attachments and intent]

    V -- No --> TUI

    I[Managed initial prompt argv] --> CI[Characterized Codex CLI parse]
    CI --> H

    TUI --> RPC[turn/start, turn/steer,<br/>queue/add, or queue/update]
    RPC --> PX[Rodex protocol-input pipeline]
    PX --> R{Matching preparation receipt?}
    R -- Yes --> ONCE[Consume receipt<br/>do not transform again]
    R -- No --> FH[Structured prompt-hook fallback]
    FH --> ONCE

    C[Rodex control routes<br/>_start / _steer / queued input] --> PX
    ONCE --> AS[Codex App Server]
    AS --> P[Turn lifecycle + rollout persistence + model]
    P --> EV[App Server events]
    EV --> PO[Rodex protocol-output pipeline]
    PO --> TUI
    TUI --> S[Terminal surface shown to user]
```

The former path began at `TUI → RPC → structured prompt hook`. Codex had already added
the raw draft to optimistic history before the proxy could rewrite the RPC, so display,
model input, and persistence could disagree. The corrected verified path moves the edit
to `TD → H → E`, before Codex classifies or renders the submission, while retaining the
structured boundary for exact-control traffic and editor states Rodex cannot safely
rewrite.
