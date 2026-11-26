# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

[0.1.2]: https://github.com/nappa85/external-agents/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nappa85/external-agents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nappa85/external-agents/releases/tag/v0.1.0
