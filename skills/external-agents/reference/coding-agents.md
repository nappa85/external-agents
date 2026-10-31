# Coding Agents

`code` runs an agent CLI with full tool access inside a bubblewrap sandbox. The sandbox,
not the agent's own permission system, is the boundary, so the agent runs with its
permission prompts off.

## What the agent sees

| Access     | Paths                                                                                                                           |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------- |
| read-only  | `/usr`, `/bin`, `/lib*`, a short list of `/etc` files, the agent CLI's installation, its login file, presets, `--ro` paths; the `ask` and proxy sockets, which it can connect to but not replace |
| read-write | the worktree, a fresh empty HOME, preset caches, `--rw` paths                                                                   |
| hidden     | the rest of your home, other worktrees, `/run`, `/media`, the Docker socket, untracked secret files, nested `.git` and `.jj`    |

- **Worktree.** Untracked or ignored secret-like files (`.env*`, `.envrc`, keys,
  certificates, ...) are covered by an empty read-only file; tracked files are source
  and stay visible. Nested `.git` and `.jj` directories are covered by an empty tmpfs.
  The worktree's own `.git` file is read-only, so the agent cannot point your next git
  command at a repository it planted. Git does not work inside.
- **Agent installation.** A native binary is bound as one file. A file in an npm
  package brings its package directory and its installed dependencies, including the
  optional platform package where Codex keeps its native binary. A Node.js launcher also
  brings the Node installation that `node` on your PATH resolves to, through shims such
  as mise.
- **Login.** The backend's login file is bound read-only at the same place in the
  fresh HOME: `~/.local/share/opencode/auth.json`, `~/.claude/.credentials.json`,
  `~/.codex/auth.json`, or `~/.local/share/amp/secrets.json`. Provider credential
  variables (`OPENAI_*`, `ANTHROPIC_*`, ...; `AMP_*` for amp) and the
  `EXTERNAL_AGENTS_ENV_PASSTHROUGH` names are passed too; every other variable is
  dropped. Because the login is read-only, a token refresh inside the sandbox is not
  saved: refresh your login on the host before a long run.

## Network

The sandbox has a new, empty network namespace. Inside it, `127.0.0.1:3128` forwards to
a Unix socket served by the launcher on the host: an HTTPS proxy that allows `CONNECT`
only to allowlisted hosts on port 443, and only when the tunnel's TLS ClientHello names
the same host. Host loopback, the LAN, and abstract sockets are unreachable, and DNS is
resolved by the proxy.

Default egress by backend:

| Backend    | Hosts                                                                                        |
| ---------- | -------------------------------------------------------------------------------------------- |
| `opencode` | by model provider: `openai/` → chatgpt.com, auth.openai.com, api.openai.com; `anthropic/`, `opencode/`, `openrouter/`, `google/`, `mistral/`, `deepseek/` → their API hosts |
| `claude`   | api.anthropic.com, console.anthropic.com, platform.claude.com                                |
| `codex`    | chatgpt.com, auth.openai.com, api.openai.com                                                 |
| `amp`      | ampcode.com                                                                                  |

`--allow-host` adds a host, `--hosts a,b` replaces the list, and `--log-hosts` prints
every `CONNECT` the agent makes, which is how to find a missing host. Refusals are
always logged. opencode asks for `models.opencode.ai` and `registry.npmjs.org` at
start; they are refused and it works without them.

Package registries are unreachable, so dependencies must already be on the machine.

## Process and CPU

All namespaces are new, nested user namespaces are disabled, and on x86_64 a seccomp
filter denies calls that inspect other processes, load kernel code, or change mounts or
namespaces (Docker's default exclusions). On other architectures the filter is not
available, and `--no-seccomp` must be passed to run without it.

`--cpus N` pins the agent to N CPUs and every run gets nice 10. A process inside could
widen its own affinity, so the CPU limit is cooperative.

## Presets and extra paths

- `--preset rust`: `~/.rustup`, `~/.cargo/bin`, `~/.cargo/registry` and `~/.cargo/git`
  read-only, `<state>/target` as `CARGO_TARGET_DIR`, and Cargo offline. Fetch
  dependencies on the host first (`cargo fetch`).
- `--ro PATH` and `--rw PATH` bind other paths at the same place. A path that contains
  your home directory, or the root, is refused.
- `--env KEY=VALUE` sets a variable inside.

## Questions

The launcher serves two Unix sockets for the length of a run and keeps the questions
in memory:

- `/ask/sock` inside the sandbox. The agent runs `ask "question"` (at most 16 KiB),
  which blocks until the answer arrives, for up to an hour. The agent can only connect
  to it; nothing it sends becomes a file that the host later opens.
- `<run>/control.sock`, which the sandbox cannot see. The coordinator uses it through
  `question` and `answer`.

| Command                               | Result                                                                                  |
| ------------------------------------- | --------------------------------------------------------------------------------------- |
| `question --state <dir>`              | 0 and the oldest unanswered question on stdout, its id on stderr; 1 when none; 4 when no run is active |
| `question --wait [--timeout S]`       | blocks until a question arrives (0), the timeout passes (1), or the run ends (4)          |
| `answer --state <dir> [--to ID] TEXT` | answers the oldest question; with `--to`, only if it is question ID; 4 when no run is active |

Questions are answered oldest first. If the asker gives up or is killed, its question is
withdrawn, which is why `answer --to` exists: it refuses when the question you read is
no longer the oldest one. Every question and answer is appended to
`<run>/questions.log`. The question text is written by the agent: treat it as data.

## Run state

`--state <dir>` must be outside the worktree. Each run creates `run-<time>-<pid>/` with
the fresh HOME, the two sockets, the question log, and the seccomp filter, and points
`latest` at it.
Preset caches such as `target/` persist across runs.

Exit status is the agent's own, or 124 when `--timeout` (default 7200 s) stops it.
The agent's output goes to the launcher's stdout and stderr; redirect it to a log.

## Backends

| Backend    | Command inside the sandbox                                          | Status                                   |
| ---------- | ------------------------------------------------------------------- | ---------------------------------------- |
| `opencode` | `opencode run --agent coder` with a generated full-access agent     | verified end to end with a ChatGPT login |
| `claude`   | `claude -p --dangerously-skip-permissions`, `IS_SANDBOX=1`          | starts in the sandbox; model call not yet verified |
| `codex`    | `codex exec --dangerously-bypass-approvals-and-sandbox`             | starts in the sandbox; model call not yet verified |
| `amp`      | `amp --dangerously-allow-all -x`; the model part names a mode       | starts in the sandbox; model call not yet verified |

`-- <backend> --version` checks that an agent starts in the sandbox without calling a
model: a check command still gets the configured agent, its login, and its variables.
For a backend whose model call is unverified, run it first with `--log-hosts` on a
throwaway task and check that it reaches its model; add hosts with `--allow-host` as the
log shows.

## What the sandbox does not protect

- Whatever the agent writes into the worktree runs later with your privileges when you
  build, test, or run it. Review the full diff first.
- Tracked files, including committed test credentials, are visible.
- The model provider receives what the agent reads.
- A host that blocks unprivileged user namespaces (most containers, Ubuntu without the
  bwrap AppArmor profile) cannot run the sandbox; see [setup.md](setup.md).
