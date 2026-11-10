# Architecture

## Components

```
skills/external-agents/scripts/
  external_agents.py   CLI: argument parsing, detect, install
  xagents/
    common.py          secret names, environment allowlist, run-with-deadlines
    backends.py        per-backend facts: installers, commands, egress, login files
    snapshot.py        sanitized snapshots, diff filtering, change recording
    review.py          panels, preflight, fallback, free-tier ledger
    sandbox.py         bubblewrap arguments, binding agent installations, the run
    proxy.py           HTTPS CONNECT proxy with host and TLS-name checks
    mailbox.py         question and answer sockets
    seccomp.py         the classic-BPF filter
  install/             idempotent installers
```

## Reviews

1. **Snapshot.** `git ls-files --cached --others --exclude-standard` lists tracked and
   untracked non-ignored files. Each is copied unless its name looks like a secret
   (`.env*`, `.envrc`, `*.key`, `*.pem`, `id_*`, `credentials*.json`, `.npmrc`,
   `audit.jsonl`, ...) or it sits in `.git` or `.jj`. Only relative symlinks to non-secret
   files inside the tree are kept, and a path whose directory resolves outside the tree is
   skipped. A final scan fails the run if a secret-like file survived.
2. **Handoff.** The prompt, the diff (filtered the same way, header by header; an
   unparsable header drops its file), and a changed-path manifest are written into the
   snapshot.
3. **Per-backend confinement.** Project configuration that a backend would load and
   that can run code (`.codex/`, `.opencode/`, `opencode.jsonc`, `.amp/plugins/`) is
   removed; Claude Code is told to load user settings only. Then each backend gets a
   file-tools-only configuration: `claude -p` with `Read,Glob,Grep,Edit,Write`,
   `dontAsk`, and edits allowed only under the snapshot; a generated opencode agent with
   a deny-by-default permission list and `external_directory: deny`; a generated Amp
   plugin mode with three file tools and path-matching permission rules; `codex exec
   --sandbox workspace-write`.
4. **Run.** The CLI runs in its own session with an allowlisted environment. A total
   deadline and an idle deadline (for CLIs that stream output) kill the whole process
   group. Output is read in raw chunks, so a run that prints without a newline or closes
   its output early cannot defeat the deadlines.
5. **Classification.** A run is usable when it exits 0 with output, unless the output is
   short and has an error line naming a provider failure (quota, rate limit,
   authorization). `--expect` adds a required pattern. A run killed for silence is
   *stalled*.
6. **Fallback and free tiers.** Each slot tries its reviewers in order, each on a fresh
   copy of the same pristine snapshot. Models ending in `-free` run one at a time per
   machine under a file lock, and one that stalls or fails is recorded in a daily
   ledger and skipped until the next UTC day.
7. **Changes.** Before and after each attempt, the snapshot is recorded in a private
   Git repository next to it (no user or system configuration; attributes that would
   convert content are unset). The diff, minus handoff files, generated configuration,
   and secret-like files, becomes `review-<n>.patch`.

## Coding sandbox

### Mounts

bubblewrap starts from an empty root:

- `/usr`, `/bin`, `/sbin`, `/lib*` read-only (symlinks recreated on merged-usr
  systems), plus a short list of `/etc` entries for name resolution, users, the
  loader, time zones, and CA certificates.
- Fresh `/proc`, `/dev`, and `/tmp`.
- A fresh HOME, created per run under the state directory. The backend's login file is
  bound read-only into it at its usual place.
- The agent's installation, read-only: a native binary as a single file; for an npm
  package, the package and its installed dependency closure (optional dependencies
  included, resolved as Node does); for a Node launcher, the Node installation that
  `node -p process.execPath` reports, which sees through version-manager shims.
- Presets and `--ro`/`--rw` paths. A path that contains the home directory, or `/`, is
  refused.
- The worktree, read-write, then on top of it:
  - an empty read-only file over every untracked or ignored secret-like file (tracked
    files are source and stay visible);
  - an empty tmpfs over every nested `.git` and `.jj` directory;
  - the worktree's own `.git` file bound read-only over itself.

A main checkout is refused: its `.git` directory holds hooks and configuration that the
host's next git command would run.

### Network

`--unshare-all` gives the sandbox an empty network namespace. A small forwarder, started
inside before the agent, listens on `127.0.0.1:3128` and relays each connection to
`/proxy/sock`, a Unix socket served on the host. The agent sees `HTTPS_PROXY` (and
`NODE_USE_ENV_PROXY=1` for Node's fetch).

The proxy accepts only `CONNECT host:443` for allowlisted hosts. After replying `200`, it
reads the client's TLS ClientHello and requires its server name to equal the `CONNECT`
host before dialling, so an allowed address cannot be used to reach another site behind
the same CDN. It bounds concurrent tunnels, header size, header time (an absolute
deadline, so trickled bytes cannot hold a slot), and idle time.

### Process

All namespaces are new, `--disable-userns` prevents nested user namespaces,
`--die-with-parent` and `--new-session` tie the sandbox to the launcher and detach it
from the terminal, and the environment is rebuilt from an allowlist.

On x86_64, a classic-BPF seccomp filter denies calls from Docker's default exclusion
list (`ptrace`, `mount`, `unshare`, `setns`, `bpf`, `perf_event_open`, `keyctl`,
`io_uring_*`, module loading, ...) with `EPERM`; `clone` with namespace flags fails;
`clone3` returns `ENOSYS` so libc falls back to `clone`; foreign architectures and x32
calls are killed. It is a deny list behind the namespaces, not the primary boundary.

CPU affinity and `nice 10` are set before exec. Without a writable cgroup the affinity is
cooperative: a process inside could widen it.

### Questions

The launcher serves two Unix sockets for the length of the run:

- **`<run>/ask/sock`**, bound read-only at `/ask/sock`. Connecting needs no write access
  to the mount, so the agent can use it but not replace it. `ask` sends a 4-byte
  big-endian length and the UTF-8 question (16 KiB at most), then waits. The reply is a
  status byte, `A` with the answer or `E` with an error, and the launcher closes the
  connection. If the asker hangs up first, its question is withdrawn.
- **`<run>/control.sock`**, outside every mount. `question` and `answer` send one JSON
  line (`{"op": "question", "wait": true, "timeout": 30}` or
  `{"op": "answer", "text": "...", "to": 3}`) and read one JSON line. The client opens
  the run through the `latest` link without following other links, checks that
  `control.sock` is a socket, and connects by a relative name, which keeps long state
  paths under the 108-byte socket path limit.

Questions are kept in memory, answered oldest first, and appended with their ids to
`<run>/questions.log`, which only the launcher writes. Ending the run releases waiting
askers with an error and waiting `question --wait` calls with exit 4.

## Threat model

**Defended against:** an agent, or instructions injected into the code it reads, trying
to read secrets outside the worktree, read untracked secret files inside it, reach hosts
other than its model API, reach host or LAN services, tamper with the host's git
metadata, plant files the host later opens while handling questions, or persist beyond
the run.

**Not defended against:**

- Code the agent writes into the worktree. Builds, tests, and scripts run with your
  privileges once you run them. Review the diff first.
- Exfiltration through the model API itself: the provider receives what the agent reads.
- Tracked secrets, which are source to the sandbox.
- Kernel vulnerabilities reachable through the remaining system calls.
- Reviewers: the snapshot prevents accidents, not a determined reviewer. Codex's shell
  can read files elsewhere on the host; the other reviewers rely on their CLI's own
  path permissions.

**Assumptions:** unprivileged user namespaces work (most containers block them, and
Ubuntu 23.10+ needs the `bwrap-userns-restrict` AppArmor profile); login files are
read-only inside, so a token refreshed in the sandbox is not saved.
