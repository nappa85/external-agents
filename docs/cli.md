# CLI Reference

All functionality is in one script, standard library only:

```bash
python3 -I skills/external-agents/scripts/external_agents.py <command> [options]
```

`-I` keeps the user's Python environment out; the script finds its own modules anyway.

## Environment variables

| Variable                          | Used by           | Meaning                                                                       |
| --------------------------------- | ----------------- | ----------------------------------------------------------------------------- |
| `EXTERNAL_REVIEWERS`              | `review`, `detect`, `install` | reviewer panel; see [Panel syntax](#panel-syntax)                 |
| `EXTERNAL_CODER`                  | `code`, `detect`, `install`   | coding agent, `backend[:model]`                                   |
| `EXTERNAL_AGENTS_ENV_PASSTHROUGH` | `review`, `code`  | extra variable names, comma-separated, passed to agents                       |
| `EXTERNAL_AGENTS_PIN`             | install scripts   | `1` replaces an installed CLI of another version with the pinned one          |
| `XDG_STATE_HOME`                  | `review`          | where the free-tier ledger lives (default `~/.local/state`)                   |

Agents otherwise receive only basic variables (`PATH`, `HOME`, locale, proxy, CA
settings) and provider credentials (`OPENAI_*`, `ANTHROPIC_*`, `CLAUDE_*`, `CODEX_*`,
`OPENCODE_*`, `OPENROUTER_*`, `AWS_*`, `GOOGLE_*`, `GEMINI_*`, `VERTEX_*`, `AZURE_*`,
and `AMP_*` for amp only).

## Backends and models

An agent is `backend[:model]`:

| Backend    | Model part                                                    | Model required |
| ---------- | ------------------------------------------------------------- | -------------- |
| `codex`    | OpenAI model ID; default: Codex's configured model            | no             |
| `claude`   | Claude model or alias, such as `sonnet`; default: configured  | no             |
| `opencode` | `provider/model`, such as `openai/gpt-6.1-sol`                | yes            |
| `amp`      | reviewer: `provider/model`; coder: an Amp mode key            | reviewer only  |

## Panel syntax

```
panel    = slot { ";" slot }
slot     = [ "?" ] reviewer { ( "|" | "," ) reviewer }
reviewer = backend [ ":" model ]
```

Slots run in parallel and produce one review each. Within a slot, reviewers are tried in
order until one produces a review. A `?` slot is optional. Reviewers whose CLI is not
installed are skipped.

## `detect`

Prints JSON: installed backends, the resolved reviewer panel, the coder and whether
bubblewrap can create namespaces, and `missing` (configured but absent, with the script
that installs each).

## `install [targets...] [--best-effort]`

Targets: `claude`, `codex`, `opencode`, `amp`, `bubblewrap`, `all`. Without targets,
installs the backends the configuration names, plus bubblewrap when `EXTERNAL_CODER` is
set. Exit 2 when an install fails, unless `--best-effort`.

| Target       | Method                                                                     |
| ------------ | -------------------------------------------------------------------------- |
| `opencode`   | pinned GitHub release archive, verified against a committed SHA-256        |
| `claude`     | `npm install -g @anthropic-ai/claude-code@<pin>`                           |
| `codex`      | `npm install -g @openai/codex@<pin>`                                       |
| `amp`        | `npm install -g @sourcegraph/amp@<pin>`                                    |
| `bubblewrap` | apt, dnf, zypper, pacman, apk, or emerge; then a namespace check           |

## `review`

Runs the panel on a snapshot of a Git checkout.

| Option                    | Meaning                                                                         |
| ------------------------- | ------------------------------------------------------------------------------- |
| `--prompt-file FILE`      | required; becomes `REVIEW_PROMPT.md` in the snapshot                            |
| `--output-dir DIR`        | one `review-<n>.md` per slot (or `--output FILE` for a one-slot panel)          |
| `--source DIR`            | checkout to snapshot (default: current directory)                               |
| `--base REV`              | diff from `REV` to the working tree, untracked files included, as `REVIEW_DIFF.patch` |
| `--diff-file FILE`        | use a precomputed Git-format diff instead                                       |
| `--include PATH`          | add an ignored file, such as a plan; repeatable                                 |
| `--reviewers PANEL`       | override `EXTERNAL_REVIEWERS` for this run                                      |
| `--reviewer AGENT`        | override with one slot; repeat for fallbacks                                    |
| `--expect REGEX`          | a review must match, or it counts as incomplete and the next reviewer runs      |
| `--timeout S`             | per-attempt limit (default 1800)                                                |
| `--idle-timeout S`        | kill after S seconds of silence (default 300; not for `claude`, `amp`)          |
| `--no-preflight`          | skip the "Reply with exactly: PONG" check before each reviewer                  |

The snapshot also holds `REVIEW_FILES.txt`, the changed paths with a status letter
(`A`, `M`, `D`, `R`). A reviewer that changes files leaves `review-<n>.patch`, which
applies with `git apply`.

Exit: 0 every required slot produced a review; 3 a required slot had none (its file
says why); 2 usage error.

## `code`

Runs a coding agent in bubblewrap. Arguments after `--` run in the same sandbox instead
of the agent, to check it: `-- <backend> --version` checks that the agent starts.

| Option             | Meaning                                                                     |
| ------------------ | --------------------------------------------------------------------------- |
| `--worktree DIR`   | required; a git worktree or jj workspace, never a main checkout             |
| `--state DIR`      | required; runs and caches, outside the worktree                             |
| `--task FILE`      | the task; a short sandbox preamble is prepended                             |
| `--raw-task`       | send the task without the preamble                                          |
| `--agent AGENT`    | override `EXTERNAL_CODER`                                                   |
| `--allow-host H`   | add an egress host (port 443); repeatable                                   |
| `--hosts H,H`      | replace the backend's default egress hosts                                  |
| `--log-hosts`      | log every `CONNECT` the agent makes                                         |
| `--preset rust`    | expose the Rust toolchain and Cargo caches; offline builds                  |
| `--ro PATH`        | bind a path read-only at the same place; repeatable                         |
| `--rw PATH`        | bind a path read-write at the same place; repeatable                        |
| `--env KEY=VALUE`  | set a variable inside; repeatable                                           |
| `--cpus N`         | pin to N CPUs (cooperative)                                                 |
| `--timeout S`      | stop the agent after S seconds (default 7200)                               |
| `--no-seccomp`     | allow running without the seccomp filter on non-x86_64 hosts                |

Exit: the agent's own status; 124 on timeout; 2 usage error.

## `question --state DIR [--wait] [--timeout S]`

Prints the coding agent's oldest unanswered question on stdout and
`xagents: question <id>` on stderr.

Exit: 0 a question was printed; 1 none waiting (or `--timeout` passed); 4 no active run,
or the run ended during `--wait`.

## `answer --state DIR [--to ID] TEXT`

Answers the oldest question. With `--to`, only if it is still question `ID`.

Exit: 0 delivered; 2 nothing waiting, or `ID` is no longer the oldest; 4 no active run.

## Files

| Path                                      | Content                                                  |
| ----------------------------------------- | -------------------------------------------------------- |
| `<state>/latest`                          | link to the newest run                                   |
| `<state>/run-<time>-<pid>/home/`          | the agent's fresh HOME                                   |
| `<state>/run-<time>-<pid>/questions.log`  | JSON lines: every question, answer, and withdrawal       |
| `<state>/run-<time>-<pid>/control.sock`   | the coordinator's socket while the run is active         |
| `<state>/target/`                         | Cargo build cache with `--preset rust`                   |
| `$XDG_STATE_HOME/external-agents/limits.json` | free models skipped for the rest of the UTC day      |
