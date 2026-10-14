# Prompt configuration and submission

Codex owns its editor, turns, and transcript. Rodex owns ordered text transformations
at structured submission; the gateway owns the child PTY and local interception.
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

## One structured submission boundary

Native draft text and managed initial-prompt arguments reach Codex unchanged.
Codex owns editing, local commands, history recall, queuing, and whether an Enter
actually produces an RPC. Rodex transforms supported structured submissions only:

```text
native editor / initial argv / exact control
                  ↓
        structured input RPC → transform text once → App Server
```

[`SessionInteractionPipeline`](../src/rodex/interaction_pipeline.py) applies one
rule snapshot per `turn/start`, `turn/steer`, `thread/queue/add`, or
`thread/queue/update` request, including all text items in a multipart submission.
Primary and control connections use the same boundary. There is no preparation
cache, text-match exemption, prefix exemption, or state to expire on reconnect.
An Enter without an RPC performs no transformation and leaves no permission for
a later submission. Each subsequent RPC is independently transformed; this is not
deduplication of native retries or a guarantee of exactly-once turn execution.

**Intentional ALPHA behavior change:** the native draft and local recall history
retain the user's original input, while App Server/model input contains transformed
text. Optimistic native display can therefore differ from persisted server history.
Rodex does not rewrite the composer or reconcile these two displays. Recalling the
original input and resubmitting applies the current rules to that original again.
Rules match the actual submitted text, including any native-added IDE context;
they do not match an inferred draft stripped of that context.

Invalid rules refuse the structured request before upstream delivery. Enter may
already have changed native editor/history state; Rodex does not promise draft
restoration. Correct the rules and explicitly resubmit. The correlated failure and
display-only notice remain separate from model dispatch; Rodex never auto-resends.

Configured local commands still use the terminal interceptor's verified takeover
and release contract. [`native_composer`](../src/rodex/native_composer.py) and the
gateway's bounded confirmation are needed for those commands, not prompt rewriting.
Terminal framing treats CRLF as one Enter, preserving paired-LF suppression without
coupling it to prompt transformation or local-command takeover. Other native input
bytes pass through unchanged.

## Content authority and limits

[`protocol_input_text`](../src/rodex/protocol_input_text.py) owns exact text edits
and UTF-8 annotation rebasing. Annotations overlapping rewritten spans are removed.
Other RPC fields, request IDs, routing, settings, attachments, and control intent
remain unchanged. Unknown frames pass through without interpreting new text fields.
Native commands, specialized review/goal fields, internal agent traffic, approvals,
and tool output remain outside this user-input transformation contract.

A replacement is JSON text, never terminal input: slash commands, shell escapes,
and terminal-control-looking text cannot gain editor or command authority through
this hook. Invalid UTF-8 or malformed edits refuse the submission.
[Interaction ownership](INTERACTION_PATHS.md) governs delivery, not a second
prompt-transform implementation.

## Regression evidence

- [Terminal input](../tests/test_terminal_input.py): no-RPC Enter, rapid/repeated
  requests, history edits, prefixes, paired CRLF, and unchanged local takeover.
- [Gateway](../tests/test_terminal_gateway.py): real PTY/editor fixtures preserve
  the native draft without executing the prompt hook.
- [Proxy integration](../tests/test_interaction_integration.py): non-idempotent rules
  across all four RPC methods, native/control connections, reconnect and multipart
  input; correlated rejection without upstream delivery.
- [Offline native submission](../tests/test_prompt_submission_native.py): installed
  Codex/tmux with an isolated local model, terminal/initial-argv submission and native
  recall. No credentials or model quota.
- [Live submission](../tests/test_prompt_handoff_live.py): ordinary/Ultra/multiline
  model-input and recall checks. These submit authenticated model prompts; see
  [test gates](DEVELOPMENT.md#validation-gates).
