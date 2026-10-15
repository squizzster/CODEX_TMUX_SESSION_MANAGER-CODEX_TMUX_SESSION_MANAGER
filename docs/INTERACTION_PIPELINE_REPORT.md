# Native interaction findings — 2026-10-11

Status: historical observations on Codex **0.162.1**, with current regression-test
pointers. The initial inspection used Rodex `0.15.0a1` at `ed0c5dc`; the resume
follow-up was fixed in `9aeaf31` later that day. These findings describe the tested
version and scenarios, not every future Codex UI or an existing retained runtime's
effective settings.

Current contracts live in [interaction paths](INTERACTION_PATHS.md),
[prompt submission](PROMPT_SUBMISSION_FLOW.md), and
[native hook admission](SECURITY.md#hook-admission-and-resume). This report keeps
the native observations and verification gaps that source inspection alone cannot
establish.

## Native notice placement

An isolated remote TUI probe injected downstream notifications on the idle initial
screen, using the [offline native fixture](../tests/command_policy_native_fixture.py),
`RodexTerminalClient` from [test_managed_startup.py](../tests/test_managed_startup.py),
and a transparent recording relay.

| Notification | Ordinary screen | F2 warning center | Model requests |
| --- | --- | --- | --- |
| `warning`, exact thread ID and message | Indicator; message absent inline | Message in Startup category | 0 |
| `deprecationNotice`, summary and details | Indicator; message absent inline | Summary/details in Deprecation category | 0 |

After Escape, the deprecation message was absent from the ordinary screen. Recorded
upstream frames contained neither injected text nor a `turn/start`, `turn/steer`, or
queue submission. `thread/start` created an empty fixture thread; it did not start
a model turn. The probe copied no user authentication files and touched no existing
user session.

The existing managed-startup helper `await_native_warning` deliberately accepts
either inline or F2 presentation. Passing that test therefore does not establish
inline placement. The separate
[proxy isolation tests](../tests/test_rodex_protocol_proxy.py) establish that local
notices do not reach App Server. The current
[notice API contract](INTERACTION_PATHS.md#model-input-and-local-notices) distinguishes
transport acceptance, semantic presentation, and durable history.

### What this probe did not establish

- It bypassed the full managed tmux/gateway route and did not test an active turn.
- `thread/read(includeTurns=true)` returned `list_turns is not supported yet`.
  Persisted transcript contents and restart behavior were not established by the probe.
- The ad-hoc display probe and its raw captures are not committed regression fixtures.
  The linked fixture/client are reusable infrastructure, not a dedicated inline-placement test.
- Switching to `deprecationNotice` or hook `systemMessage` is not evidence of a
  context-free inline notice feature. No such renderer feature was implemented by
  this investigation. Native and light views would both need an explicit display contract.

## Resume preflight

Fresh remote startup accepted the TUI's hook-trust bypass. Remote resume instead
read the saved thread and native hook inventory, then opened hook review **before
`thread/resume`**. Rodex waited for a loaded thread before attaching the terminal,
leaving the review prompt hidden until the startup timeout. Testing only
`thread/start` had missed this native lifecycle difference.

The fix admits the exact native hook key/hash in private App Server configuration
and rechecks native trust before launching the TUI. The
[security guide](SECURITY.md#hook-admission-and-resume) owns the admission rules,
configuration-parser gotcha, opt-out, and remaining native-review limits.

Evidence in [test_command_policy_native.py](../tests/test_command_policy_native.py):

- `test_remote_tui_uses_exact_temporary_hook_trust_for_every_lifecycle` exercises
  start/resume/fork with untrusted and previous-installation trust. It checks the
  actual request/response, native permission selection, unrelated hook-state
  preservation, and unchanged persisted trust.
- `test_managed_automatic_hook_admission_survives_saved_session_resume` stops the
  fixture runtime, reopens the same Rodex name with the same Codex UUID, and proves
  the policy runs in the new turn. It checks current-turn output so an old denial
  replayed in history cannot satisfy the assertion.

The real affected saved session also reopened with its original UUID. A separate
direct-Codex failure involved an unreachable shared-daemon socket; local logs
predated these hook changes, and a direct `--no-daemon` resume succeeded. The socket's
removal cause was not established. [Resume diagnosis](OPERATIONS.md#startup-and-resume-failures)
keeps these failure domains separate.

## Original validation scope

The initial report recorded 654 focused contract/interaction tests and 14 native
command-policy tests passing. Those are historical counts, not the current suite
size. Native policy tests used an offline deterministic model; the display probe
made no model request. Validation used Codex 0.162.1, tmux 3.2a, and Python 3.12.
Python emitted `forkpty()` multithreading deprecation warnings in the native fixture.

The original documentation investigation did not run the quota-using prompt-handoff
suite or full clean-installation suite. The later resume fix added the lifecycle
regressions above. Use [development gates](DEVELOPMENT.md#validation-gates) for
current validation; admission of stable App Server 0.151.0+ is not a promise that
optional hooks or warning placement match this native version.
