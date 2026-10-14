# Find/rg command policy

Rodex's native `PreToolUse(Bash)` broker evaluates every proposed Bash command through
the production `find`/ripgrep (`rg`) customer policy before later rules such as the
short-sleep policy. A rejected direct search becomes a native denial with the policy's
specific feedback. An allowed or non-applicable search continues through the ordered
policy pipeline.

The policy never executes the supplied command, reads file contents, consults history,
or writes runtime state. It has no SQLite database, command runner, generated replay,
or dependency on the temporary `find_rg_checker.zip` corpus.

## Runtime composition

The authoritative route is:

```text
Codex PreToolUse(Bash)
  → retained Rodex helper
  → private command-policy broker
  → boundary-aware find/rg extraction
  → restricted shell and option parsing
  → static policy
  → bounded metadata census, only if approval remains possible
  → first search rejection denies
  → otherwise continue to the short-sleep and later policies
  → native allow when no policy denies
```

[`evaluate_command_policy`](../src/rodex/command_policy.py) owns rule ordering and
native allow/deny adaptation. The production search rule calls
[`evaluate_find_rg_source`](../src/rodex/pre_tool_use_customer_library/find_rg.py)
with the hook request's exact command, working directory, and environment. Only a
native `allow` means the complete ordered pipeline admitted the proposed command.

Internally, the search library retains four precise outcomes:

- `allowed`: every applicable extracted search passed static and live checks;
- `rejected`: a direct search did not earn approval;
- `needs_filesystem`: static diagnostics deliberately omitted required metadata;
- `not_applicable`: no direct supported `find` or `rg` workflow was identified.

Production enables filesystem verification, so `needs_filesystem` is principally a
deterministic diagnostic/testing outcome. Both `rejected` and any unexpected
`needs_filesystem` result map to native denial. `allowed` and `not_applicable` continue
to the next customer policy. Native denial reasons are capped at the command-policy
text bound before encoding.

## Boundary-aware extraction

`extract_search_workflows` splits source at top-level shell-list and command
boundaries. It retains pipelines so a downstream `rg` keeps its producer context.
Unescaped newlines delimit workflows; backslash-newline and a newline following `|`
continue the same workflow. Supported launch wrappers remain attached to their
effective `find` or `rg` command.

A candidate must place `find` or `rg` at a real command position after optional
assignments or supported wrappers. Quoted mentions, backticks, command/process
substitutions, heredoc bodies, and arguments to unrelated commands do not become
candidates. An extracted decision applies only to that direct workflow; it does not
model state changes made by preceding commands in the larger shell source.

Obvious hazards stop before filesystem access. For example, `find /` produces:

```text
Rejected: Stage 1: Directory input '/' has no maximum depth. Name a smaller subtree and an explicit depth bound; output limits and name filters do not bound traversal.
```

## Rules and work bounds

[`find_rg.toml`](../src/rodex/pre_tool_use_customer_library/find_rg.toml) owns:

- traversal, workflow, root, entry, file, byte, pattern, thread, stage, and census limits;
- denied `find` capabilities;
- ripgrep capabilities requiring external input, decompression, link following, or a
  different regular-expression CPU policy;
- exact user-facing messages.

[`find_rg.py`](../src/rodex/pre_tool_use_customer_library/find_rg.py) owns literal
shell grammar, native option arity, ordered option semantics, extraction, decision
composition, and filesystem inspection. These are executable syntax contracts rather
than tunable values. The TOML file is an installed package resource and participates
in retained-installation identity; running sessions keep the rules they started with.

Current conservative behavior includes:

- explicit depth plus a live width census for directories;
- actual file types rather than filename-suffix guesses;
- combined candidate-file and byte budgets for content searches;
- no symlink following, dynamic shell expansion, mutation, delegated execution,
  external pattern/config files, decompression, PCRE2, or unknown options;
- independent checking of each direct search workflow and each stage in its pipeline;
- restricted stdin-only `head`, `tail`, `sort`, `wc`, and numeric `sed` consumers;
- rejection of ambient ripgrep configuration unless `--no-config` is explicit.

Output limits, name/type/glob filters, `head`, and `timeout` do not become traversal
budgets. A shallow directory may still be too wide. Historical observations cannot
override present scope.

## Deadlines and honest limits

The production adapter admits at most 64 direct search workflows in one Bash request,
gives the complete search policy 0.5 seconds, and gives an individual metadata census
at most 0.25 seconds within that total. The helper/private broker transport can wait
up to five seconds, while the established native hook timeout remains three seconds.
The native boundary is therefore the effective user-visible ceiling; increasing the
private timeout prevents the former one-second cutoff from discarding a response that
returns before that ceiling.

The metadata census uses in-process `lstat` and `scandir`, never reads contents or
follows symlinks, and checks its deadline between metadata operations. A single kernel
call blocked on pathological local or remote storage cannot be interrupted by this
in-process design. In that case the advisory hook can still fail open after its outer
timeout. Codex remains the execution, approval, and sandbox authority. Filesystem
state can also change after inspection, and installed tool versions can change option
semantics.
An approval therefore describes the parsed request and observed inputs at inspection
time, not a runtime guarantee.

This policy is not a complete Bash interpreter or an execution sandbox. Indirect
searches such as `bash -c 'find ...'`, shell variables, functions, aliases, generated
arguments, and conditional shell grammar remain outside the direct-workflow contract.
Unknown syntax fails closed only after a direct workflow has been identified.

## Evidence

Focused validation lives in
[`test_pre_tool_use_find_rg.py`](../tests/test_pre_tool_use_find_rg.py), while native
composition and broker behavior live in
[`test_command_policy.py`](../tests/test_command_policy.py):

```bash
uv run python -m pytest tests/test_pre_tool_use_find_rg.py tests/test_command_policy.py
uv run ruff check src/rodex/pre_tool_use_customer_library src/rodex/command_policy.py
```

Tests pin the exact `find /` feedback and prove that static rejection performs no
metadata call. They cover all four internal outcomes, bounded files/directories,
pipelines, mutation and external-input rejection, symlinks, budgets, hidden ripgrep
configuration, non-execution, boundary extraction, multiline continuations, static
replay, policy ordering, reason bounds, and native decisions.

The untracked ZIP corpus was used for a read-only repeatability audit on 2026-10-14.
From 47,430 source records, the boundary produced 50,691 workflows, including 74
multiline workflows and 22 with backslash-newline continuation. Two filesystem-free
passes produced zero exceptions and the same complete-output digest,
`f497317cab01509541955f8a9eaed6b07d4bc63b25fba72ae2ed00c16d6bedec`. Of those
workflows, 40,623 needed filesystem evidence, 10,065 were structurally rejected, and
three metadata-only requests were allowed. The private corpus and audit adapter are
not runtime, package, or maintained test data.
