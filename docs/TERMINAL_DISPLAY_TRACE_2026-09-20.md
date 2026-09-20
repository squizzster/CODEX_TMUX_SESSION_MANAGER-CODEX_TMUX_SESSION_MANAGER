# Terminal display synchronization trace — 2026-09-20

Status: source corrections implemented and release verification passed; see
[final release verification](#final-release-verification).
The original diagnosis below records the pre-fix application at
`5f221a14140b7f86e5ef2787035a542b90bfb8d4`. Project mode remains ALPHA.
Environment: Rodex `0.14.0a2`, installed Codex `0.155.1`, tmux `3.2a`, pyte `0.8.2`.

## Live incident: mutable helper imports crossed a running installation

During this work the user reproduced display-only corruption in the live
`sepia-harrier` chat and supplied `/tmp/example_1.png` and its red-box annotation.
The application remained usable and the layout later recovered. A subsequent tmux
message named `rodex.tmux_sharing_coordinator` and ended with `returned 1`, although
the command redirected both stdout and stderr. These are separate observations;
they do not establish that one terminal byte sequence caused both symptoms.

The live daemon was Rodex `0.14.0a2`, PID 3475475, started at 20:10:14 UTC. Its exact
loaded first-party fingerprint was
`853d2f4e41c86d6fedf7d2f2ced79f20fd56f487b4c28ac2857f7344ba8edb4a`.
The affected runtime was `27bb7bcfb5f3f8c3`, server nonce
`291e995c0f2c4040617fc6aade0f971b`, tmux session `$0`, primary pane `%0`.
The screenshot clock showed 21:01; uploaded image modification times are not exact
failure timestamps. App-server and daemon log files were empty. The Codex rollout
and tmux messages retained activity, but no raw terminal stream at the failure instant.

The source trace establishes this causal chain:

1. The running daemon retained its old imported implementation and owned its
   SHA-named endpoint. Its tmux hook command retained the mutable checkout's
   `.venv/bin/python` path, rather than a retained installation.
2. Editing that checkout changed what the next hook or observer subprocess imported.
   A fresh helper computed the edited implementation fingerprint. Merely retaining
   the daemon's already-loaded Python modules did not retain those later imports.
3. Before a protocol bump, the fresh sharing helper addressed the edited SHA's
   daemon socket. `_notify_runtime_resize` suppressed connection failures, so the
   original daemon could silently miss the pane-size transition.
4. After tmux protocol 4 was bumped to 5, a fresh helper rejected the still-running
   protocol-4 server before enumerating its roster and returned 1. tmux itself
   reports a failed `run-shell -b` job in the pane; shell stdout/stderr redirection
   does not suppress tmux's own failure display.
5. Observer launches used the same mutable interpreter/source boundary and could
   likewise import a different runtime contract. Fixing only one hook command,
   adding a repaint or redirecting more output would leave that boundary broken.

The in-place edits during this investigation exposed this fault. The original
checkout and virtual environment were restored to the exact loaded 0.14 fingerprint;
the matching live hook then returned 0 and the outer/child PTY dimensions agreed.
The user's database and live session were preserved. All new changes moved to the
separate `CODEX_TMUX_SESSION_MANAGER_RELEASE_0_15` worktree.

The unsolicited failure message is explained directly by the verified command,
protocol mismatch and tmux job-reporting path. Missed resize delivery is a verified
mechanism relevant to the red-box display drift; its exact historical contribution
remains unresolved without the missing bytes/timing. In steady dark presentation
without a local overlay, Rodex forwards native output: the internal pyte resize
fault alone cannot explain corruption of that raw visible stream.

The durable correction publishes a verified local installation before composing
CLI/runtime services. Code, Python dependencies and shipped defaults have one
fingerprint; each installation retains its own interpreter outside the replaceable
bootstrap environment. Daemons, observers and every hook use it with `-I`.
Concurrent publication is locked and atomic, changed copies are rejected, and
external editable dependencies cannot silently escape the copy. User rule overrides
remain external and reload on submission. No older wire/catalog adapter is added.

`tests/test_installation.py` replaces a development checkout's release, protocol,
catalog, dependency and defaults while retaining an earlier installation. It checks
that both installations keep their own contents even after the bootstrap is removed.
A separate regression sends the retained helper through a real tmux roster and the
real daemon wire handler to a recording resize recipient after replacement. That
callback is delivered; an explicitly misdirected newer identity is rejected without
another callback. These tests isolate the protocol/lifecycle mechanism without a model.

## Additional confirmed resize-delivery defect

A negative-control fixture deliberately separated the daemon resize callback from
its pane's foreground `SIGWINCH`. It revealed another source fault independent of
version drift: tmux can run `after-resize-window` before applying the new dimensions
to the outer kernel PTY. Rodex consumed that wake by copying the old `TIOCGWINSZ`
value, cleared its pending flag, and received no later daemon wake when tmux finally
updated the kernel size.

A real tmux 3.2a hook probe measured a logical 80×15 pane while the hook's kernel
query still returned 80×23. The kernel changed approximately 240 ms after the resize
command returned. The early callback left the child at 23 rows even after the outer
PTY reached 15; a second callback then corrected it. The original signal-driven
fixture had received the later foreground `SIGWINCH`, hiding this daemon-specific
ordering. Earlier live samples happened to receive enough later hooks to recover.

The source fix supplies `TerminalSessionGateway` with the primary pane owner's
capability-fenced `#{pane_width}|#{pane_height}` read. Those dimensions describe the
tmux grid already rendering the output. The gateway uses them for initial sizing
and every resize callback, updates the native model and sets the child PTY size
through the same path. No arbitrary sleep or periodic size polling is needed.
Non-tmux gateways retain their ordinary kernel-size provider. A failed capability
read cannot substitute a stale size or authorize another pane.

The real-PTY regression disables foreground `SIGWINCH` repair, confirms that a
withheld callback leaves native coordinates stale, then delivers the callback and
checks correct child size, cursor reply and relative Working-counter update. Both
policies' real-terminal query/resize tests now use that independent callback route.
This proves the lost-final-size mechanism and its correction; it still cannot
reconstruct the exact bytes absent from the user's historical red-box frame.

## Missing geometry events and the foreground bridge

Real tmux tests also found that `next-layout`, `previous-layout` and natural observer
process exit can resize the primary pane without any of the old command-specific
hooks firing. Natural observer disappearance is especially relevant to the reported
split/close sequence: `after-kill-pane` runs for that command, not every pane exit.
The generic `window-layout-changed` hook fired for all five previously covered
geometry commands as well as these missing routes, so it replaces those five hooks.
Sharing-related client hooks remain.

That generic event still does not cover every operation on tmux 3.2a. Swapping two
unequal panes changed the primary from 15 rows to 7 without a layout event; this tmux
version also rejects `after-swap-pane`. The prior terminal bridge handed its TTY to
the daemon, then exec'd `cat` to hold the lifetime socket. That discarded the normal
foreground `SIGWINCH` path which would have covered the swap and completed deferred
kernel resizing.

The bridge now retains a small blocking signal/lifetime loop. `SIGWINCH` wakes a pipe;
normal control flow sends a current-contract resize hint to the same pinned daemon.
The bridge never reads or writes the pane TTY. The gateway remains its only I/O owner
and revalidates current tmux geometry. The bridge exits when the daemon closes the
retained connection. Pending resize signals coalesce; there is no idle polling and
no additional helper process for each signal.

`tests/test_terminal_bridge.py` performs an actual kernel PTY resize after descriptor
handoff, verifies the exact daemon wake request, proves typed bytes remain available
to the handed-off descriptor, and checks bridge exit on lifetime-socket closure.
`tests/test_tmux_resize_notifications.py` exercises split/close, both layout-navigation
commands, resize-pane/window, select-layout and natural observer exit using the
production hook set. With foreground notifications enabled it also verifies unequal
pane swaps. Missing callbacks, stale kernel-size reads and mutable helper imports
are independent contributors; the correction covers all three at their owners.

## Concurrent and initial resize admission

The dimension provider introduced a blocking, authoritative tmux read. Review then
verified an existing flag-ordering race made visible by that read: a second callback
could set `_resize_pending` during the first read, only for `_apply_resize` to clear
it afterward. A real PTY probe left the child at 15 rows while the newest geometry
was 7, even after draining the wake pipe. The gateway now claims the pending wake
before reading; a callback during the read or application remains pending for the
next pass. A permanent concurrent-provider regression verifies the child reaches 7.
Identical dimensions produce no renderer operation, so duplicate client/layout/kernel
hints cannot erase an active overlay awaiting its next native update.

There was also a startup subscription gap: the bridge could submit a hint after the
gateway's first size read but before the daemon registered its resize callback.
The daemon now registers controls first, then schedules one reconciliation. The
initial authoritative read and this subscribe-then-reconcile step cover both sides
of the handoff without depending on another user action. Daemon tests assert that
initial callback occurs before explicit subsequent wake requests.

## Original trace conclusion

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

## Original resize-notification observations

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

## Fix verification

The following supersedes the original diagnosis's "not implemented" status.
The original intermittent report remains unproven; these corrections address the
independently reproduced defects and related failures found during their review.

### Shared terminal state

[NativeTerminalScreen](../src/rodex/native_terminal_screen.py) replaces direct
`pyte.Screen.resize()` delegation. Height reduction removes rows below the cursor
first, then scrolls only the remaining reduction. Expansion restores eligible
native history. Width changes reflow soft-wrapped logical lines, preserving styles,
wide/combining characters and cursor position. History is bounded to 50,000 rows;
native screen/history clears end restoration eligibility. The visible buffer and
cursor are updated together before subsequent relative terminal operations.

The model remains shared by light, dark and local menus. It never receives Rodex's
own display frames. The paintability contract also checks the cursor's row. A tiny
semantic viewport with no transcript space omits the transcript instead of letting
Python's `[-0:]` slice render every item and scroll the composer.

Expectations in [resize tests](../tests/test_native_terminal_resize.py) come from
real tmux captures and cursor reports, including shrink/expand, relative Working
counter updates, simultaneous height/width changes, Unicode and native clears.
Independent real-tmux probes additionally checked sparse cursor positions, regional
scrolling, wrapped-line erasure, tabstops and saved cursor restoration. Those agreed
with tmux. A one-column viewport containing a wide glyph remains a degenerate
difference: tmux itself exposes an out-of-bounds cursor there; widening recovered
agreement. These checks do not claim universal terminal-emulator equivalence.

### Shared terminal protocol

[NativeTerminalProjection](../src/rodex/native_terminal_projection.py) frames native
output before presentation selection. Cursor/status queries produce one local reply
using the native state at the query's exact position in the byte stream. This includes
origin-relative coordinates, delayed autowrap, private CPR and zero-padded numeric
parameters. Those queries are consumed in both modes, preventing a second reply
from the semantic display's different cursor position.

Device capabilities and opaque OSC/DCS controls continue to the real terminal in
both modes; pyte's default VT102 reply does not substitute for actual capabilities.
The gateway queues locally generated replies directly to the child PTY, outside
keyboard interception and prompt hooks, with backpressure when the child cannot
consume them. Actual-terminal replies retain the existing input decoder's opaque
terminal-reply route.

The renderer returns ordered stream bytes, a replaceable frame and child replies
as separate effects. The gateway coalesces only frames. Policy changes wait until
UTF-8, escape/control strings and synchronized-update blocks are complete. This
also fixes dropping an OSC terminator or synchronized-update closer during a mode
change. Grouped and zero-padded `2026` parameters follow the same boundary logic.

[Protocol tests](../tests/test_native_terminal_protocol.py) exercise both modes and
every byte split for queries, opaque controls, UTF-8 and mode transitions. Real child
PTYs verify received cursor/status/capability replies across resize and mode changes.
[Gateway tests](../tests/test_terminal_gateway.py) cover partial writes, pending-frame
replacement, reply backpressure and keyboard-hook isolation. Independent review
also passed 90 queue/handoff combinations with one-byte writes.

### Validation result

The user subsequently supplied `example_1.png` and `example_1_red_box.png`, confirming
the original symptom: stray `52`, Working/composer rows and tool-interaction text
were visibly mixed. The user then reported recovery; a read-only capture of the
same `sepia-harrier` pane showed the ordinary layout again. That pane's daemon
started at 20:10:14 UTC, before these fixes, and `/proc/<pid>/comm` identified
`rodexd_v0_14a2`. This is user-observed evidence in the older implementation, not a
reproduction in the corrected runtime. No byte trace was captured at the failure
instant, so the causal link to either corrected defect remains unproven.

Managed live checks of the terminal fix used real Codex, three observer-controller
split/close cycles and four client resizes per policy. Dark recorded 878 native
reads and 14 resize applications; light recorded 725 reads and 13 applications
(including initial/duplicate resize hints). Both completed the bounded model turn,
with zero invalid cursor, buffer-row or buffer-column states. Light returned to
dark and accepted/cleared a new draft. Some early post-split captures were briefly
blank and recovered; asynchronous outer/child size intervals remain observable.
These instrumented checks do not establish absence of every display race. The
fixture's shutdown logged incomplete analytics retirement; its runtime processes,
copied authentication/configuration and generated Codex temporary trees were removed.

At the user's request the release is `0.15.0a1`, with all Rodex compatibility
generations advanced: SQLite 21, tmux 5, runtime peer 6, daemon 3, process receipts 3,
observer 4, machine envelopes 5, agent trace 4 and statistics 9. No migration or
compatibility adapter was added. Dependency resolution also updated coverage,
ruff and wcwidth within the declared requirements.

### Final release verification

- `uv run ruff format --check .`: passed, 220 Python files.
- `uv run ruff check .`: passed.
- `uv run pytest --require-live-startup --cov --cov-report=term-missing -ra`:
  **2,231 passed, 3 skipped, 84.99% coverage**, in 247.17 seconds.
- Two skips are explicitly opt-in App Server model/user-input integrations. The third
  is an exact Codex fixture-version check: installed Codex is 0.155.1. Required managed
  startup and all three real prompt-handoff cases passed. One existing Python warning
  concerns the writer-handoff test's use of `forkpty` in a threaded test process.
- `uv build`: source distribution and wheel built successfully.
- Built wheel installed in a clean Python 3.12.13 environment: **2 live startup tests
  passed** in 62.34 seconds, including native typing, light/dark selection, detach,
  reopen, runtime reuse and saved-thread adoption, with isolated SQL/tmux/Codex state.
  That startup gate submits no model prompt. Temporary verification environments and
  copied authentication/configuration were removed after their processes stopped.

The first full run exposed four stale argv assertions missing the new helper `-I`
flag and a live prompt fixture that started typing after the banner, before requiring
its composer. The assertions now check the isolated helper invocation and the fixture
waits for the actual editor. Affected tests and the entire release gate subsequently
passed; no application assertion was weakened.

Permanent regressions cover native resize/reflow against real tmux, exact terminal
replies across policies and byte splits, stream/frame queue ordering, geometry hooks,
foreground SIGWINCH forwarding, concurrent and startup resize admission, and retained
helper/code/dependency/default isolation across an update. The historical red-box
frame remains unattributed at byte level; these are proved source mechanisms and
regressions, not a claim to have reconstructed missing historical evidence.

Fresh 0.15 launches enter the fixed installation. Pre-0.15 live processes still require
their original checkout/environment to remain intact; this fix cannot retrofit them.
The user's original 0.14 checkout remains clean at its recorded fingerprint, with its
live database and sessions preserved. The release work is isolated on
`fix/terminal-state-release` in `CODEX_TMUX_SESSION_MANAGER_RELEASE_0_15`.

Additional local evidence is in the release worktree's ignored `.tmp.HJk9mg/`
(validation logs) and `.tmp.resize-review.KLk4PE/` (tmux timing/event/race probes).
Earlier screenshot and timeline evidence remains in the original checkout's ignored
`.tmp.B69GNT/`. Temporary evidence may expire; the tests and this report are versioned.
