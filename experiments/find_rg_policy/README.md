# Find/rg policy experiment

This independent experiment decides whether one literal `find` or ripgrep (`rg`)
workflow fits a small, explicit work budget. It returns an immediate, specific
explanation for the first rule that prevents approval. It never runs the supplied
command. A boundary-aware extractor can also isolate every direct top-level search
workflow from single-line or multiline shell text while retaining complete pipelines.

The experiment is intentionally outside Rodex's runtime and hook packages. It has no
history model, SQLite database, command runner, generated replay, or dependency on the
temporary `find_rg_checker.zip` extraction. Its policy and messages belong to this
experiment.

## Run it

Use Python 3.11 or newer from the repository root:

```bash
experiments/find_rg_policy/cli.py --bash "find /"
experiments/find_rg_policy/cli.py --bash "find src -maxdepth 2 -type f"
experiments/find_rg_policy/cli.py --bash "rg --max-depth 2 needle src"
experiments/find_rg_policy/cli.py --static --bash "rg needle README.md"
experiments/find_rg_policy/cli.py --extract --static --json \
  --bash $'printf ignored\nfind /\nrg needle README.md'
```

`--cwd /absolute/path` selects the directory used to resolve literal relative paths.
It defaults to the caller's current directory. `--json` emits the structured result.

The text contract is deliberately small:

- `true`, exit 0: the understood workflow and presently verified inputs fit policy.
- `Rejected: ...`, exit 2: a search was understood but did not earn approval.
- `Needs filesystem: ...`, exit 2: static rules passed, but approval requires a
  metadata census that `--static` deliberately disabled.
- `Not applicable: ...`, exit 3: no direct `find` or `rg` command was present.
- Configuration/input errors use stderr and exit 3.

Only `decision.allowed is True` means search admission succeeded. Rejected,
needs-filesystem, and not-applicable decisions are false in boolean context, but
remain distinct structured outcomes.

`--extract` emits one result for each extracted workflow in source order. Its JSON
form reports ordinals, source spans, and line numbers without echoing command content.
Plain output uses tab-separated ordinal, starting line, and result. Exit 0 requires
at least one extracted workflow and every result to be allowed.

## Fast decision pipeline

The single authoritative path is:

```text
literal command
  → restricted shell lexer
  → find/rg option parser
  → static policy
  → bounded metadata census, only if approval is still possible
  → allowed | rejected | needs_filesystem | not_applicable
```

Static inspection branches after the static policy and returns `needs_filesystem`
instead of invoking the census.

This stage is effectively deterministic: equivalent command text, cwd, policy,
relevant environment, and observed filesystem metadata produce the same outcome and
message. No elapsed timing is placed in the result. A census at the configured
deadline can still conservatively reject under unusually slow storage or host load.
For a filesystem-independent replay, use static inspection with an explicit
environment mapping; command text, cwd, policy, and environment then fully determine
the result.

## Boundary-aware extraction

`extract_search_workflows` splits source only at top-level shell-list and command
boundaries. It retains pipelines so a downstream `rg` keeps its producer context.
Unescaped newlines delimit workflows; backslash-newline and a newline following `|`
continue the same workflow. Supported launch wrappers remain attached to their
effective `find` or `rg` command.

A candidate must place `find` or `rg` at a real command position after optional
assignments or supported wrappers. Quoted mentions, backticks, command/process
substitutions, and arguments to unrelated commands do not become candidates. An
extracted decision applies only to that standalone workflow; it never approves the
larger script or models state changes made by omitted commands.

Obvious hazards stop before filesystem access. For example, `find /` produces:

```text
Rejected: Stage 1: Directory input '/' has no maximum depth. Name a smaller subtree and an explicit depth bound; output limits and name filters do not bound traversal.
```

The static decision does not call `find`, `rg`, or a subprocess. A potentially safe
file or bounded-directory request receives an in-process `lstat`/`scandir` census.
The census never reads file contents or follows symlinks, and stops between metadata
operations on its configured time, entry, file-count, and byte budgets.

## Policy ownership

[`policy.toml`](policy.toml) owns:

- traversal, root, entry, file, byte, pattern, thread, stage, and census limits;
- denied `find` capabilities;
- ripgrep capabilities requiring external input, decompression, link following, or
  a different regular-expression CPU policy;
- exact user-facing messages.

[`policy_engine.py`](policy_engine.py) owns literal shell grammar, native option
arity, ordered option semantics, the decision pipeline, and filesystem inspection.
Those are executable syntax contracts rather than tunable policy values.

Current conservative behavior includes:

- explicit depth plus a live width census for directories;
- actual file types rather than filename suffix guesses;
- combined candidate-file and byte budgets for content searches;
- no symlink following, dynamic shell expansion, mutation, delegated execution,
  external pattern/config files, decompression, PCRE2, or unknown options;
- independent checking of every search producer in supported pipelines and lists;
- restricted stdin-only `head`, `tail`, `sort`, `wc`, and numeric `sed` consumers;
- rejection of ambient ripgrep configuration unless `--no-config` is explicit.

Output limits, name/type/glob filters, `head`, and `timeout` do not become traversal
budgets. A shallow directory may still be too wide. Fast historical observations do
not exist in this experiment and therefore cannot override present scope.

## Honest limits

This is an admission experiment, not a shell, execution sandbox, runtime prediction,
or proof that a regex is useful. Unknown syntax fails closed only after a direct
`find`/`rg` workflow is identified. Indirect searches such as `bash -c 'find ...'`,
shell variables, functions, aliases, and generated arguments are outside the direct
workflow contract.

The live census is intentionally in-process to minimize the successful-path overhead.
Its deadline is checked between metadata operations; it cannot interrupt a single
kernel call blocked on slow or remote storage. A future Rodex integration would need
an isolated, lifecycle-bounded worker and a deadline coordinated with the native hook.
This experiment does not solve that integration problem prematurely.

Filesystem state can change after inspection. Tool versions can also change option
semantics. An approval therefore means only that the parsed request and observed
inputs fit the current configured rules at inspection time.

## Focused validation

```bash
uv run python -m pytest experiments/find_rg_policy/tests
uv run ruff check experiments/find_rg_policy
uv run ruff format --check experiments/find_rg_policy
```

The tests pin the exact `find /` message and prove that this static rejection performs
no metadata call. They also cover the four outcomes, bounded files/directories,
pipelines, mutation and external-input rejection, symlinks, budgets, hidden ripgrep
configuration, non-execution, boundary extraction, multiline continuations, static
replay, and CLI output.

The untracked ZIP corpus was also used for a read-only repeatability audit on
2026-10-14. From 47,430 source records, the boundary produced 50,691 workflows,
including 74 multiline workflows and 22 with backslash-newline continuation. Two
filesystem-free passes produced zero exceptions and the same complete-output digest,
`f497317cab01509541955f8a9eaed6b07d4bc63b25fba72ae2ed00c16d6bedec`. Of those
workflows, 40,623 needed filesystem evidence, 10,065 were structurally rejected, and
three metadata-only requests were allowed. The private corpus and its audit adapter
are not runtime/package data or maintained test fixtures.
