# Prompt configuration and submission

Codex owns its editor, turns, and transcript. Rodex owns ordered text transformations
and one-use preparation receipts; the gateway owns the child PTY and release of Enter.
This is not terminal scrollback surgery or a replacement editor API.

## Rule files

[`UserPromptHook`](../src/rodex/user_prompt_hook.py) loads:

- Frozen defaults from the installation's
  [conf/hooks/user_prompt_substitutions.yaml](../conf/hooks/user_prompt_substitutions.yaml).
  Editing checkout defaults affects the next implementation, not live runtimes.
- Optional user rules at
  `$XDG_STATE_HOME/rodex/conf/hooks/user_prompt_substitutions.yaml`, otherwise
  `~/.local/state/rodex/conf/hooks/user_prompt_substitutions.yaml`. The path is frozen
  from the launching caller's environment, even in a shared daemon.

Both files are ordered YAML lists. The shipped greeting example is:

```yaml
- name: 'Add enthusiasm to Hello'
  match: '^Hello$'
  replace: 'Hello!'
  flags: 'i'
```

Named rules require `name`, `match`, and `replace`; `flags` defaults to empty.
Names are case-sensitive identities, unique within each file. A matching user name
replaces the global rule in place; new user rules append in order. Patterns/replacements
use Python regex syntax, including `\1` and `\g<name>`. Single-quoted YAML preserves
escapes. Flags are `i`, `m`, `s`, and `g`; without `g`, replace only the first match
per text input item. Rules run sequentially. The shipped file also owns the `push`
expansion; do not duplicate its workflow text in documentation.

Legacy `/pattern/replacement/flags` and `s/pattern/replacement/flags` strings use the
same compiler; escape `/` as `\/` only in that shorthand. They are unnamed, append
in order, and cannot override named rules. Empty YAML/`[]` adds no rules; an absent
user file means globals only. Missing global defaults are an error.

On submission, [`file_stat_sha512`](../src/rodex/file_stat_sha512.py) fingerprints
mode, inode, device, uid, gid, size, mtime-ns, and ctime-ns, not file content. Unchanged
metadata reuses cached rules; changed files alone are read/compiled. Re-stat after
reading detects concurrent saves; three unstable reads refuse the submission. One
runtime lock owns the combined cache. There is no hook-file polling. A rewrite
preserving all eight metadata fields is undetectable by this contract.

Unreadable files, invalid YAML/rules, or unstable reads fail closed, not with stale
rules or a partial configuration. The connection remains usable and receives a
correlated error. Display-only notices are deduplicated per bad file version after
successful delivery; stat failures share a notice until accessibility changes. Fix
the user file and submit again: changed valid rules replace the cached failure without
a runtime restart. Hooks are synchronous; avoid expensive regexes on the input path.

## Enter handoff

```text
verified native draft → transform → edit child PTY → confirm native composer
                                                      ↓
App Server ← structured input ← consume receipt ← admit receipt + release Enter
```

[`TerminalInputInterceptor`](../src/rodex/terminal_input.py) holds Enter before Codex
receives it. The ordered contract is verify original → transform → edit → confirm
canonical output → admit receipt → release Enter. DEL and bracketed paste edit the
same native composer through the child PTY. Queuing replacement and Enter together
would establish byte order, not proof that Codex accepted the edit.

[`native_composer`](../src/rodex/native_composer.py) verifies ordinary `›` and Ultra
`»`, complete continuation rows, wrapping, dimensions, and end cursor in a
capability-fenced tmux snapshot. Changed cursor/width, copy mode, partial drafts, or
collapsed pastes cannot authorize the handoff. The gateway drains child output and
its display queue before confirming; this proves native rendered state, not that
every remote client has painted pixels.

The handoff has a 300 ms deadline with rechecks at up to 10 ms relay-wait intervals;
individual tmux calls retain their own deadlines. This runs only after Enter, not
while an idle draft waits. Other keyboard input is held during handoff. A timeout
keeps canonical text unsubmitted and admits no receipt; Enter retries confirmation
without applying transformations again. Keys alone cannot revoke preparation: only
positive verification of a different tracked draft permits normal admission again.
An unavailable snapshot proves neither state. After untracked editor changes, clear
and retype to recover a verifiable candidate.

A text rule cannot inject terminal controls, empty the whole submission, or acquire
slash/shell-command authority. Production does not use `tmux send-keys`.

## Other routes and limits

- Managed initial prompt: characterized argv → transform/admit → native TUI;
  the primary protocol request consumes that receipt. Initial whitespace is preserved.
- Verified unchanged draft: admit without unnecessary editing. Changed drafts use
  the two-confirmation path; receipt matching follows Codex's submission trimming.
- Unverified native draft: forward Enter, then transform the structured protocol
  input. This fallback cannot guarantee matching optimistic TUI history.
- `_start`, `_steer`, and queued/control input: structured admission; these routes
  cannot consume the primary TUI's receipt.
- Configured local commands are handled by the interceptor before prompt admission.
  Native commands, specialized review/goal fields, internal agent traffic, approvals,
  attachments, routing, settings, and other RPC fields remain outside text rewriting.

[`protocol_input_text`](../src/rodex/protocol_input_text.py) preserves RPC intent and
rebases annotations over unchanged text to UTF-8 byte offsets; annotations overlapping
rewritten spans are removed. [Interaction ownership](INTERACTION_PATHS.md) governs
delivery, not a second prompt-transform implementation.

## Regression evidence

- [Terminal input](../tests/test_terminal_input.py): ordering, exact-once retry,
  intent/control-byte refusal, and route-specific whitespace.
- [Native composer](../tests/test_native_composer.py) and
  [interceptor presentation](../tests/test_input_interceptor_presentation.py): glyphs,
  wrapping, cursor/dimensions, and snapshot fences.
- [Gateway](../tests/test_terminal_gateway.py): canonical output reaches a real
  outer PTY before Enter or receipt admission.
- [Live handoff](../tests/test_prompt_handoff_live.py): installed Codex/tmux ordinary,
  Ultra, and wrapped multiline cases with a non-idempotent rule. These submit model
  prompts; see [test gates](DEVELOPMENT.md#validation-gates).
