# Terminal display synchronization trace — 2026-09-20

Status: diagnosis only; no application fixes applied. Project mode remains ALPHA.
Examined application commit: `5f221a14140b7f86e5ef2787035a542b90bfb8d4`.
Environment: Rodex `0.14.0a2`, installed Codex `0.155.1`, tmux `3.2a`, pyte `0.8.2`.

## Conclusion

The reported intermittent Working-counter overlap was **not reproduced** with real
Codex in this investigation. The user also could not reproduce it during the
requested Hello-agent exercise. There is no evidence sufficient to identify its
historical root cause or declare it fixed by the latest update.

Two separate terminal-presentation defects were reproduced and merit correction:

1. The internal terminal model can retain an invalid cursor and misplace screen
   rows after shrinking the pane.
2. Light presentation consumes terminal queries without forwarding them or
   returning the terminal model's response to the child.

Neither defect is established as the cause of the original report. In particular,
the live Codex samples recovered after resize, and no cursor-position query was
observed during their resize sequences.

## One pipeline, with a presentation branch

Both Rodex display policies share the Codex session, protocol stream, child PTY,
`TerminalSessionGateway`, interaction pipeline, native terminal projection and
output writer. They do not create separate session or execution pipelines.

The branch is inside `TerminalSurfaceRenderer.native_output()`:

- Dark updates the native projection and forwards the original terminal bytes,
  with any local menu restoration/painting.
- Light updates the same projection, then generates a commentary/composer frame
  instead of forwarding those original bytes.

The branches return through `_queue_rendered_surface()` and `_flush()` in the same
gateway. Policy-specific presentation is intentional; differences in terminal
control behavior or validity of the shared terminal state are the defects.
These policy names mean `/rodex light` and `/rodex dark`, not terminal colour themes.

## Confirmed finding 1: resize does not maintain valid native terminal state

Source: [terminal_surface.py](../src/rodex/terminal_surface.py),
`NativeTerminalProjection`, `paintable`, `TerminalSurfaceRenderer.resize` and
`_semantic_frame`; installed `pyte/screens.py`, `Screen.resize`.

`resize()` directly delegates to `pyte.Screen.resize()`. On height reduction, pyte
unconditionally removes the height difference from the top of its buffer and
retains the previous cursor row. Real tmux in the fixture removed only enough
rows to keep the cursor in bounds. Rodex's `paintable` check validates the cursor
column but does not validate its row before permitting another generated frame.

The deterministic real-tmux fixture began at 80 columns by 23 rows, with a Working
line at row 18 and the composer/cursor at row 20. The same top 33% split used by
the observer reduced the main pane to 15 rows. All row numbers below are one-based:

| State after split | Real tmux native display | Rodex native model |
| --- | --- | --- |
| Cursor | Row 15, within the pane | Row 20, outside the pane |
| Composer | Row 15 | Row 12 |
| Original Working line | Row 13 | Row 10 |

The fixture then emitted a column reset, two-row relative cursor movement, and
`UPDATED WORKING COUNTER`. Dark updated the existing Working line at row 13.
The internal model wrote outside its visible buffer instead. After expansion to
23 rows, that previously invisible write appeared at row 18 below the retained
composer at row 12 in light mode. This is a reproduced display-state defect using
actual tmux, not a screenshot inference or a model of tmux implemented with pyte.

The internal-model divergence also exists while dark is selected. It becomes
visible when Rodex renders from that model, as light and mode restoration do.
Actual Codex commonly performs a resize redraw that repairs the state; both
direct-gateway live runs and both managed live runs recovered in the sampled cases.
The deterministic child intentionally exposes the relative-update case without
assuming that every child output is a complete repaint.

Correction requirements, not implemented: maintain coherent cursor, buffer and
resize/reflow semantics at the shared terminal-model boundary; verify them against
real tmux. Clamping only the cursor would leave the independently demonstrated
row displacement unresolved. Include relative updates and shrink/expand sequences
in regression coverage, rather than depending on a subsequent Codex full redraw.

## Confirmed finding 2: light consumes terminal query/response traffic

Source: [terminal_surface.py](../src/rodex/terminal_surface.py),
`NativeTerminalProjection.__init__`, `feed` and `native_output`; installed
`pyte/screens.py`, `report_device_status`, `report_device_attributes` and
`write_process_input`.

In light mode the original native bytes are consumed by the projection, and only
generated screen output reaches tmux. A cursor-position query (`ESC [ 6 n`) is
therefore not forwarded. Pyte parses the query, but its default
`Screen.write_process_input()` is a no-op, and Rodex has installed no response sink.
The child consequently receives no answer through either route.

The real child-PTY/tmux fixture requested its cursor position before and after the
split. Dark returned `ESC [ 20 ; 3 R` and `ESC [ 15 ; 3 R`. Light returned no bytes
within either 0.5-second observation window. The source trace identifies why no
later response is queued. Separate characterization checks also confirmed that
device-status and primary-device-attributes queries disappear in light mode.

This is terminal control traffic, distinct from App Server JSON-RPC. Live Codex
issued a startup cursor query while the session was still dark. Its tested resize
sequences did not issue another, so this defect is **not a demonstrated resize
failure in Codex 0.155.1**.

Correction requirements, not implemented: assign terminal query/response handling
to the shared terminal boundary before presentation selection. Cursor replies must
refer to the native terminal state, not to the visually rearranged light frame.
Verify a real child receives a correct reply in both modes and across a mode change.

## Resize notifications: observed behavior, not a third confirmed defect

The normal observer route was traced through `AgentObserverCoordinator`,
`ObserverPaneController`, `TmuxPaneController`, the installed tmux hooks,
`reconcile_sharing_state`, the daemon's runtime wake callback, and the gateway's
`TIOCSWINSZ` operation. Observer creation uses a detached top 33% split; normal
closure issues an exact guarded `kill-pane`. There is no policy-specific branch
in that resize-notification route.

Each fully managed test opened/closed the actual observer pane controller three
times and changed client dimensions four times. All ten transitions reached the
child PTY in both modes. The initial PTY sizing is recorded separately, giving
11 size applications per run.

Because hooks run asynchronously, child output continued briefly with different
outer-pane and child dimensions: 43 of 881 native reads in the dark run, and 49 of
799 in light. These are instrumented samples, not latency measurements. The saved
post-transition screens recovered; this observation alone does not prove that
the interval causes persistent corruption or requires a separate fix.

## Validation and limitations

- Existing focused tests: **54 passed** across `test_terminal_surface.py`,
  `test_terminal_gateway.py` and `test_tmux_sharing_coordinator.py`.
- Additional temporary characterization checks: **4 passed**, meaning the two
  defects remain present. These checks characterize failures; they are not tests
  of an implemented correction.
- Real tmux and real PTY fixture: both policies, top split, relative counter
  update, expansion, and cursor query/reply observations.
- Real Codex through the gateway: two light runs, including 12-row panes,
  width changes, split/unsplit and return to dark. No matching overlap reproduced.
- Full Rodex daemon plus real Codex: one dark and one light run, exact observer
  pane-controller operations, client resizes, and a bounded `sleep 15` model turn.
  Light's selected commentary and the native composer remained usable. The light
  run also returned successfully to the native display after completion.
- One user-requested Hello agent exercised the actual user's split/close route;
  the user reported they could not reproduce the original symptom.

The existing renderer tests commonly use pyte for both the internal and visible
screens. They can agree while both differ from real tmux. Existing gateway resize
coverage verifies dimensions and a child SIGWINCH, not this display-state contract.

Application files were not edited. Temporary instrumentation logged unmodified
gateway operations and terminal bytes; it can influence timing. The direct-gateway
fixtures use a signal-driven test host, so only the managed fixtures verify Rodex's
actual daemon/hook notification route. The tests sampled screens and byte/state
traces, not every physical display frame, and do not establish absence of every race.

## Relevant history and retained evidence

Commit `9f37ac7` introduced the semantic presentation branch on September 9,
including replacement of native bytes in light mode. Delegating resize directly
to pyte predates that branch in `90c251e`. The light/native branch and resize logic
examined here have not been corrected by the latest prompt-handoff changes.

Commit `3f2e2cb` on September 17 introduced the current split/resize wake hooks.
The latest application merge (`5f221a1`, PR #93) concerns verified prompt handoff,
including output-flush/confirmation timing. It is not evidence by itself that this
historical display symptom was fixed. A different previous runtime or timing
condition remains possible, but was not established.

Local, ignored investigation evidence is under `../.tmp.tQKHAE/`: scripts,
`fixture-results.json`, per-mode terminal captures, byte/state `events.jsonl`,
managed `operations.json` and `summary.json`. Temporary test runtimes were stopped;
copied authentication/configuration files were removed. Generated evidence remains
outside Git and may expire under the repository's temporary-work policy.

Run the focused baseline from the project root:

```bash
.venv/bin/python -m pytest -q tests/test_terminal_surface.py tests/test_terminal_gateway.py tests/test_tmux_sharing_coordinator.py
```

While the local evidence directory exists, rerun the no-model defect checks with:

```bash
.venv/bin/python -m pytest -q .tmp.tQKHAE/test_confirmed_display_findings.py
```
