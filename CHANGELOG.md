# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- An `amp` agent in the bubblewrap sandbox, a coding agent or a sandboxed reviewer, received
  every `AMP_` variable, including an Amp orb's identity variables, one of which mints
  tokens that other services trust. Its shell could read them. In the sandbox it now
  receives only `AMP_API_KEY` and `AMP_URL`, plus the `EXTERNAL_AGENTS_ENV_PASSTHROUGH` names.
- The `amp` reviewer's confinement no longer relies on `amp.permissions` rules, which Amp
  removes on 2026-12-04. The generated plugin's `tool.call` hook allows only its file tools,
  on absolute paths whose real location, links resolved, is inside the snapshot. It also
  refuses writes to `.amp/`, so the reviewer cannot rewrite the hook.
- A `codex` reviewer could write the configuration of the repository that records its
  changes, next to its snapshot in `/tmp`, and make the wrapper's own `git` run commands on
  the host (`core.fsmonitor`, filter drivers). The repository now lives under the state
  directory, its configuration is rewritten before every git command, and no hook runs.
- A coding agent could repoint a submodule's `.git` file, or `git init` an uninitialised
  submodule, so the host's next `git status` ran its commands. `code` refuses worktrees
  with submodules, and masks nested `.git` files.
- `--ro` over the state directory let the agent answer its own questions through the
  control socket, and `--rw` over the main checkout let it write hooks; both are refused.
- The diff filter let secrets through in plain `diff -u` patches, CRLF patches, and headers
  whose path contains ` b/`; it now reads hunks by their line counts and drops any text
  it cannot attribute to a file, such as a `diff -c` section. A
  `--diff-file` inside the checkout, and earlier review output, no longer reach the
  snapshot unfiltered. A tracked directory replaced by a link to `.git` is no longer copied.
- Agents receive only their own provider's credentials, not every provider's; the
  `amp` routing check no longer gets the full parent environment. Secret names match
  regardless of case.
- The agent could exhaust the connection slots it shared with the coordinator and lock
  `question` and `answer` out; each socket has its own limit.
- The seccomp filter also denies `pidfd_getfd` and `process_madvise`. The SNI parser checks
  every length and reads a ClientHello split across records. The proxy escapes the
  agent's text in its log.

### Fixed

- A reviewer thinking for longer than `--idle-timeout` was killed as stalled. The timeout
  grows with `@effort`, a retry allows twice the silence, and a silent `amp` run is checked
  on the server: a turn still in flight earns more time, and one that finished is taken as
  the review.
- One slot's wrapper error discarded every other slot's finished review; it now ends only
  that slot. A tracked `.amp` file or a directory where a generated file goes no longer
  crashes a review.
- Non-UTF-8 file names and contents crashed `review`; CRLF in hunks was turned into LF;
  `--base` with a subdirectory `--source` gave paths that did not match the snapshot; a
  rename to a non-ASCII name dropped that file from the diff.
- A descendant that kept writing after the CLI exited turned a finished review into a
  timeout. A stale `review-<n>.patch` survived a rerun. A preflight passed when the CLI only
  echoed the prompt. Unexpected `amp` event shapes and `--output` in a missing directory
  ended in a traceback. The free-tier ledger now applies to a model at every effort.
- `--output-dir` at the checkout root no longer hides the checkout, a committed review
  file stays out of `--base`, an untracked repository without commits no longer aborts
  `--base`, and a refused `--include` exits 2 again.
- `answer --to` takes the `run-...:N` token `question` prints, so a late answer cannot reach
  a newer run. A malformed, infinite or NaN `--timeout` gets an error instead of a false "run ended"
  or a spinning mailbox.
- `--env` now overrides inherited credentials; `--hosts ""` allows no host and spaces in
  `--hosts` are ignored; a signal-killed agent exits 128+N; `detect` and the bubblewrap
  installer probe the flags `code` uses, and the installer no longer needs sudo when bwrap
  is already installed.
- `code` could fail at start with `OSError: [Errno 16] Device or resource busy`: it moved
  the seccomp filter to descriptor 9 with `dup2`, which fails when another thread is
  opening a file into that number at the same moment. bwrap now reads the filter from the
  descriptor it was opened on (`--seccomp N`).

### Added

- `review --background` and `review-status`. Nothing told a coordinator that ran a review in
  the background when it ended. `--background` starts the run detached and returns once it
  is under way; every run records each slot's outcome in `review-status.json` as the slot
  ends; `review-status --wait` blocks until the run ends, prints each slot's reviewer or
  why it has none, and exits with the run's exit code, or 4 when the run was killed.
  A second run into the same outputs is refused.
- `EXTERNAL_AGENTS_REVIEW_SANDBOX=1` runs reviewers in the bubblewrap sandbox that `code`
  uses, with a shell, so they can search and run the tests; `codex` is confined there too.
  Without a working bwrap they keep file tools only.
- The handoff diff shows whole changed functions, so reviewers open fewer files.
- `amp` reviewers can search: the generated mode adds `search_files` (a regular expression
  over file contents) and `find_files` (a glob over paths), confined to the snapshot like
  its file tools. Amp has no search tool of its own, so reviewers read whole files to find
  a definition.
- `EXTERNAL_AGENTS_AMP_NO_CREDITS=1` runs an `amp` reviewer only when a provider
  connection linked to the Amp account, such as a ChatGPT subscription, serves its model.
  `amp config model-providers check-access` decides before each `amp` reviewer runs; a
  model Amp credits would pay for, or a failed check, makes the reviewer unavailable, so
  the slot falls back. Outside an Amp thread, the check needs `AMP_THREAD_ID`.

## [0.1.4] - 2026-11-23

### Added

- A `vibe` backend for [Mistral Vibe](https://github.com/mistralai/mistral-vibe), as a
  reviewer and as a coding agent, with `install vibe` pinning 3.1.4 through `uv`. A
  reviewer runs the `accept-edits` policy with `outside_workdir = "deny"` and shell, web
  and subagent tools refused, set through `VIBE_*` variables that override the user's
  config; the repository's `.vibe/` is removed from the snapshot. `@effort` becomes
  `--thinking`.
- The coding sandbox can run a CLI installed in its own Python virtual environment, such
  as a `uv tool` or pipx install.

### Changed

- `MISTRAL_*` variables are passed to agents with the other provider credentials, so
  `MISTRAL_API_KEY` no longer needs `EXTERNAL_AGENTS_ENV_PASSTHROUGH`.

## [0.1.3] - 2026-11-20

### Fixed

- An `amp` reviewer whose model request hangs without failing is now caught by `--idle-timeout`
  instead of running into the 1800 s total timeout. `amp` now runs with `--stream-json`, so it
  prints an event for each model turn and tool call. A paid reviewer that stalls is retried once
  from a fresh snapshot; free tiers are still not retried.

### Added

- Each slot's raw reviewer output, preflight included, is written to `review-<n>.log` as it
  arrives, so a running or killed reviewer shows what it was doing. When no reviewer succeeds,
  the slot's file shows an `amp` reviewer's last tool calls and its Amp thread ID.

### Changed

- A reviewer killed at the total timeout is reported as `timed out` instead of `unavailable`.
- `amp` reviews no longer start with the CLI's deprecation warning: the review comes from the
  final stream event.

## [0.1.2] - 2026-11-18

### Added

- A reviewer spec can end in `@effort` to set the reviewer's reasoning effort: `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh` or `max`. Amp otherwise runs at `medium`, which some models
  reject at preflight. `claude` accepts only `low` through `max`. A suffix that is not a known
  effort stays part of the model ID.
- This changelog.

### Changed

- The version in `pyproject.toml` now matches the release. It still read 0.1.0 when 0.1.1 was tagged.

## [0.1.1] - 2026-11-10

### Fixed

- Review diffs no longer depend on the user's git diff configuration. With `diff.noprefix` or
  `diff.mnemonicPrefix` set, reviewers got an empty diff while the run still succeeded, and
  `diff.external` aborted the review.
- Snapshots no longer follow a tracked directory that the working tree replaced with a link to
  a directory outside the checkout. Before this fix, the outside files went to the reviewer's provider.
- `question` and `answer` exit with code 4 as documented when no run directory exists yet, or
  when the latest run directory was removed. They used to print a traceback and exit 1.
- Free-tier daily limits are checked and recorded under the free-tier lock. Before this fix,
  concurrent runs could use a throttled model again and lose each other's entries in `limits.json`.
- An invalid `--expect` regex is reported as a usage error with exit 2, not as a traceback.
- The coding sandbox sets CPU affinity and niceness without a `preexec_fn`. With the proxy and
  mailbox threads running, the `preexec_fn` could deadlock the launch.

### Security

- Agent audit logs (`audit.jsonl`) count as secret-like files. They are refused in snapshots,
  diffs and `--include`.

### Added

- CI runs lint, ShellCheck and the test suite.

## [0.1.0] - 2026-11-07

### Added

- First release. Other agent CLIs (Codex, Claude Code, opencode, Amp) run as confined reviewers
  of a plan or change, or as coding agents sandboxed with bubblewrap on a git worktree. Pinned
  CLIs are installed when missing.
- MIT license.

[Unreleased]: https://github.com/nappa85/external-agents/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/nappa85/external-agents/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/nappa85/external-agents/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/nappa85/external-agents/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nappa85/external-agents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nappa85/external-agents/releases/tag/v0.1.0
