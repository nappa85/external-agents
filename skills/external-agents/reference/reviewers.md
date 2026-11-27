# External Reviewers

`review` runs confined reviews through other agent CLIs. For each panel slot, it copies
the tracked and untracked non-ignored files of a Git checkout into a temporary snapshot,
without secret-like files or VCS metadata, then runs the first available reviewer of
that slot. The supplied diff gets the same secret filtering. The filter matches common
secret file names (`.env*`, keys, certificates, `credentials*.json`, ...); it is
best-effort, so it misses secrets committed under other names.

The snapshot comes from `git ls-files`, so a Jujutsu repository must be colocated with
Git.

## Choose reviewers

`EXTERNAL_REVIEWERS` holds a panel: slots separated by `;`, run in parallel, each
producing one review. Within a slot, reviewers separated by `|` (or `,`) are tried in
order until one passes preflight and produces a review. A slot that starts with `?` is
optional: the panel still succeeds without it.

```bash
# One reviewer: Codex, or GPT through Amp when Codex is unavailable
export EXTERNAL_REVIEWERS="codex:gpt-6.1-sol | amp:openai/gpt-6.1-sol"

# Two required reviewers plus an optional free one
export EXTERNAL_REVIEWERS="codex:gpt-6.1-sol; claude:sonnet; \
  ? opencode:opencode/deepseek-v4.1-flash-free"
```

Each entry is `backend[:model]` with the backend's full model ID. Without a model,
`codex`, `claude`, and `vibe` use their own configured default; `opencode` and `amp` always need
one, because `opencode` may default to a free tier and `amp` serves several model
families. Reviewers whose CLI is not installed are skipped. Prefer models from a family
other than the host agent's.

`--reviewers "<panel>"` overrides the variable for one run; `--reviewer backend[:model]`,
repeatable, overrides it with one slot that tries those reviewers in order.

## Confinement

| Backend    | Confinement                                                                                     |
| ---------- | ----------------------------------------------------------------------------------------------- |
| `codex`    | `codex exec --sandbox workspace-write`                                                          |
| `claude`   | `claude -p` with only file tools, `dontAsk` permissions, and edits allowed only in the snapshot |
| `opencode` | a generated `reviewer` agent with only file tools, and `external_directory` denied              |
| `amp`      | a generated `external-reviewer` mode with file tools that a plugin hook keeps in the snapshot   |
| `vibe`     | `accept-edits` policy, `outside_workdir = "deny"`, shell, web, and subagent tools refused        |

The snapshot prevents accidental exposure and edits. It is not a sandbox against a
reviewer that follows instructions injected into the reviewed code:

- `claude`, `opencode`, `amp`, and `vibe` deny reads and writes outside the snapshot.
- `codex --sandbox workspace-write` limits writes to the snapshot and blocks network,
  but its shell can still read files elsewhere on the host.

### Sandboxed reviewers

With `EXTERNAL_AGENTS_REVIEW_SANDBOX=1`, each reviewer runs inside the bubblewrap sandbox
that `code` uses, on its snapshot, and gets a shell: it can search with `rg` and run the
tests to confirm a finding. The sandbox sees the snapshot, the CLI and its login file, and
reaches only the model provider. The wrapper's generated configuration stays read-only,
and `codex` drops its own sandbox, which cannot start inside bubblewrap. Without a working
bwrap (macOS, or a Linux host that refuses user namespaces) reviewers keep the file tools
above. An `amp` reviewer also keeps them when there is no `AMP_API_KEY` or amp login file,
since it could not sign in inside the sandbox. Files the repository ignores that a run
creates, such as test caches, are left out of the returned patch.

Project configuration in the snapshot that a backend would load and that can run code
is not used: `claude` loads only user settings, and the wrapper removes `.codex/` for
`codex`, `.opencode/` and `opencode.jsonc` for `opencode`, `.amp/plugins/` for `amp`,
and `.vibe/` for `vibe`. The diff still shows changes to those files.

The reviewer process receives only an allowlisted environment: basic variables such as
`PATH`, `HOME`, locale, proxy, and CA settings, plus the credential prefixes of its own
provider: `ANTHROPIC_` for `claude`, `OPENAI_` for `codex`, `AMP_` for `amp`, `MISTRAL_` for
`vibe`, and the model's provider for `opencode`. A sandboxed `amp` reviewer receives only
`AMP_API_KEY` and `AMP_URL`: other `AMP_` variables, such as an Amp orb's identity, would be
readable from its shell. List any other variable a backend needs in
`EXTERNAL_AGENTS_ENV_PASSTHROUGH`.

## Amp without another CLI

The `amp` backend runs any model that Amp serves, so no other CLI or provider login is
needed: run `amp login` once, or export `AMP_API_KEY`. List model IDs with
`amp plugins show-agent-options`; the review entry takes the `provider/model` ID, not
the mode key that `amp --mode` uses.

Without a suffix, Amp runs the reviewer at its default reasoning effort, `medium`. A model
that does not support it fails preflight with `Reasoning effort "medium" is not
supported`. Append `@effort` with one of the efforts that `amp plugins show-agent-options
--json` lists for the model, such as `amp:deepseek/deepseek-v4.1-flash@high`.

Every backend takes the `@effort` suffix: `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, or `max`. `claude` accepts only `low` through `max`, for example
`claude:sonnet@high`.

Amp serves a model through a provider key or subscription linked to the Amp account,
such as a ChatGPT subscription, when one covers it, and bills Amp credits otherwise. With
`EXTERNAL_AGENTS_AMP_NO_CREDITS=1`, each `amp` reviewer first runs `amp config
model-providers check-access` on its model and effort, even with `--no-preflight`. When no
linked connection serves the model, or the check fails, the reviewer is unavailable: the
slot tries its next reviewer and the log says why. The check is a small inference, so it
costs a few Amp credits when credits would serve the model. `check-access` associates that
inference with an Amp thread you own, from `AMP_THREAD_ID`; outside an Amp thread, set it
to any thread you own, or the check fails and every `amp` reviewer is unavailable.

Amp's built-in modes have shell and edit tools, so the wrapper never uses them for a
review. It writes a project plugin into the snapshot that registers an
`external-reviewer` mode whose agent has only `Read`, `create_file`, and `edit_file`,
and a settings file whose permissions allow those tools inside the snapshot and reject
every other call, including paths with `..` segments. Each review creates a private Amp
thread.

## Mistral Vibe

The `vibe` backend runs Mistral's Vibe CLI with the key in `MISTRAL_API_KEY` or in
`~/.vibe/.env`. A key that `vibe --setup` keeps in the system keyring is out of reach:
the reviewer's environment has no session bus. The model part is
an alias from Vibe's model list (by default `mistral-large-4-0` and `mistral-medium-3.5`,
plus any in `~/.vibe/config.toml`); without one, Vibe uses its `active_model`. `@effort`
becomes `--thinking`, with `none` passed as `off`; each model lists the levels it
supports in its `thinking_levels`.

Vibe has no file-tools-only mode: every session also offers `bash` and `run_typescript`,
a JavaScript tool that calls the other tools. The wrapper sets the limits as `VIBE_*`
variables, which override `~/.vibe/config.toml`, and runs the `accept-edits` policy, so
only edits run without approval and a headless run refuses anything that needs it.
`outside_workdir = "deny"` refuses file tools outside the snapshot, including through a
symlink; shell, web, and subagent tools are set to `never`; `bypass_tool_permissions`,
background processes, subagents, MCP servers, connectors, telemetry, and session logs
are off. `run_typescript` has no network or file access of its own, so its calls go
through the same checks. The run uses `--output streaming`, one JSON event per tool call
and message, so the idle timeout applies.

## Data sharing

The snapshot is the user's source. Send it only to providers the user's agreement
covers. A free or promotional tier may retain or train on prompts; naming one in
`EXTERNAL_REVIEWERS` is the user's explicit permission to use it. Never add a free tier
on your own, through `--reviewer`, `--reviewers`, or the configuration, unless the user
permits it for that review.

## Run a review

Write the reviewer prompt to a file. Besides the handoff inputs, tell the reviewer:

- The review target is `REVIEW_DIFF.patch`, and `REVIEW_FILES.txt` lists the changed
  paths with a status letter. There is no VCS in the snapshot.
- Which review guidance in the repository to follow, if the repository has any.
- The output format you need, such as a verdict line or finding JSON.

```bash
python3 -I <skill-dir>/scripts/external_agents.py review \
  --prompt-file "$PROMPT" --output-dir "$OUT_DIR" --base main
```

- `--base <revision>` diffs that Git revision against the working tree, untracked files
  included. In a Jujutsu repository pass `--diff-file <(jj diff --git -r <revset>)`.
- `--include <path>` adds an ignored file, such as a plan.
- `--output <file>` replaces `--output-dir` when the panel has one slot.
- `--expect <regex>` requires a usable review to match, such as `(?i)verdict`. An
  output that does not match counts as incomplete: the next reviewer is tried, and the
  incomplete output is kept in the slot's file when none succeeds.

Slots run in parallel, so a panel takes about as long as its slowest slot; a review can
take several minutes. Start it with `--background` next to your own subagent, not with a
shell's `&`: `--background` returns only once the run has recorded its status, and prints a
`review-status` command whose `--run` names the run, so it never reports another run.

`review` exits:

| Exit | Meaning                                                                         |
| ---- | ------------------------------------------------------------------------------- |
| 0    | every required slot wrote a review                                              |
| 3    | a required slot, or every slot, had no available reviewer; its file says why    |
| 2    | usage or setup error, or another run is writing to the same outputs             |

### Follow a run

`--background` starts the run detached, writes its own output to `review-run.log` in the
output directory (`<name>.run.log` next to `--output`), and returns 0 as soon as the run has
recorded its status. When the run fails to start, such as on a usage error, it returns the
run's exit code instead. When the run already ended and a later run into the same outputs
replaced its status, it says so with the run's exit code and returns 0: that exit code is
the outcome, and `review-status` would describe the later run.
Any run, in the background or not, records its progress in `review-status.json`
(`<name>.status.json`): each slot's reviewer chain, and its outcome as soon as the slot
ends.

```bash
python3 -I <skill-dir>/scripts/external_agents.py review-status --wait --output-dir "$OUT_DIR" --run "$RUN_ID"
```

`$RUN_ID` is the ID in the `wait for it with:` command that `--background` printed; running
that command as printed is simplest.

`review-status` reads that file and prints the run, then one line per slot: `written by
<reviewer> to <file>`, with the patch when there is one, `missing, no reviewer available
(<why>)`, or `running (<chain>)` with its log. `--wait` blocks until the run ends;
`--timeout <s>` bounds the wait. Pass the same `--output-dir` or `--output` as the run, and
the `--run` ID that `--background` printed: when a later run into the same outputs has
replaced this one, `review-status` exits 4 instead of reporting the later run.

| Exit   | Meaning                                                                     |
| ------ | --------------------------------------------------------------------------- |
| 0, 3   | the run finished; the run's own exit code (2 if the wrapper itself failed) |
| 1      | still running (without `--wait`, or when `--timeout` expired)               |
| 4      | no run, it was killed before it finished, or (`--run`) a later run replaced it |

The run holds a lock on its first slot's log while it runs, which tells a running run
from a killed one, and refuses a second run into the same outputs (exit 2). `--output`
refuses names that `--output-dir` uses, such as `review-1.md`, so the two layouts never
share files.

A reviewer may add or change files in its snapshot. The wrapper returns them as
`review-<n>.patch`, which applies to the reviewed checkout with `git apply`; it leaves
out handoff files, generated backend configuration, and secret-like files. Each
reviewer in a slot starts from the same unchanged snapshot. Only `codex` has a shell, so
other reviewers write tests but cannot run them.

## Failures and limits

A short preflight call ("Reply with exactly: PONG") before each review detects a missing
login or an exhausted quota, either of which can otherwise hang a run. `--no-preflight`
skips it. A reviewer that exits 0 counts as unavailable only when its output is short
and has an error line naming a provider failure, such as `Error: rate limit reached`;
a review that merely discusses authorization or quotas is kept.

`--idle-timeout` (default 300 s) kills a reviewer that prints nothing that long; it does
not apply to `claude`, which prints only at the end. A model thinking hard can be silent
for minutes, so the timeout doubles at `@high` and triples at `@xhigh` and `@max`, and the
retry after a stall allows twice the silence again. An `amp` run that goes silent is
checked on the server (`amp threads export`): while its turn is still in flight it gets up
to two more idle periods, and a turn the server finished while the CLI printed nothing is
taken as the review. `vibe` prints an event for each
finished message and tool call. `amp` runs with `--stream-json`, so
it prints an event per model turn and tool call, and a model request that hangs without
failing trips the idle timeout instead of running into the total one. A paid reviewer
killed that way is retried once from a fresh snapshot; a free one is not. `--timeout`
(default 1800 s) bounds every run.

Each slot's raw output, preflight included, is appended to `review-<n>.log` (next to
`--output`, as `<name>.log`) as it arrives, so a running or killed reviewer shows what it
was doing. For `amp`, each JSON event carries `session_id`, the reviewer's Amp thread,
which shows the whole run. When no reviewer succeeds, the slot's file keeps the end of
each failed run: for `amp`, its last tool calls and its thread ID.

Models whose ID ends in `-free` are treated as free tiers: they have daily limits and
are throttled when runs overlap, often by opening a stream that never returns a byte.
The wrapper runs at most one free model at a time on the machine, and a free model that
stalls or fails is skipped for the rest of the UTC day. The ledger lives in
`$XDG_STATE_HOME/external-agents/limits.json` (default `~/.local/state`); delete an
entry to retry sooner.
