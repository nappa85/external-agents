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
`codex` and `claude` use their own configured default; `opencode` and `amp` always need
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
| `amp`      | a generated `external-reviewer` mode with only file tools, permissions limited to the snapshot  |

The snapshot prevents accidental exposure and edits. It is not a sandbox against a
reviewer that follows instructions injected into the reviewed code:

- `claude`, `opencode`, and `amp` deny reads and writes outside the snapshot.
- `codex --sandbox workspace-write` limits writes to the snapshot and blocks network,
  but its shell can still read files elsewhere on the host.

Project configuration in the snapshot that a backend would load and that can run code
is not used: `claude` loads only user settings, and the wrapper removes `.codex/` for
`codex`, `.opencode/` and `opencode.jsonc` for `opencode`, and `.amp/plugins/` for
`amp`. The diff still shows changes to those files.

The reviewer process receives only an allowlisted environment: basic variables such as
`PATH`, `HOME`, locale, proxy, and CA settings, plus provider credential prefixes such as
`OPENAI_` and `ANTHROPIC_`. Only `amp` also receives `AMP_` variables. List any other
variable a backend needs, such as `MISTRAL_API_KEY`, in
`EXTERNAL_AGENTS_ENV_PASSTHROUGH`.

## Amp without another CLI

The `amp` backend runs any model that Amp serves, so no other CLI or provider login is
needed: run `amp login` once, or export `AMP_API_KEY`. List model IDs with
`amp plugins show-agent-options`; the review entry takes the `provider/model` ID, not
the mode key that `amp --mode` uses.

Amp's built-in modes have shell and edit tools, so the wrapper never uses them for a
review. It writes a project plugin into the snapshot that registers an
`external-reviewer` mode whose agent has only `Read`, `create_file`, and `edit_file`,
and a settings file whose permissions allow those tools inside the snapshot and reject
every other call, including paths with `..` segments. Each review creates a private Amp
thread.

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

Run it in the background next to your own subagent. Slots run in parallel, so a panel
takes about as long as its slowest slot; a review can take several minutes.

| Exit | Meaning                                                                         |
| ---- | ------------------------------------------------------------------------------- |
| 0    | every required slot wrote a review                                              |
| 3    | a required slot, or every slot, had no available reviewer; its file says why    |
| 2    | usage or setup error                                                            |

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
not apply to `claude` and `amp`, which print only at the end. `--timeout` (default
1800 s) bounds every run.

Models whose ID ends in `-free` are treated as free tiers: they have daily limits and
are throttled when runs overlap, often by opening a stream that never returns a byte.
The wrapper runs at most one free model at a time on the machine, and a free model that
stalls or fails is skipped for the rest of the UTC day. The ledger lives in
`$XDG_STATE_HOME/external-agents/limits.json` (default `~/.local/state`); delete an
entry to retry sooner.
