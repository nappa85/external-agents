# Setup

## Make the skill visible to a harness

The skill is the `external-agents/` directory holding `SKILL.md`. Copy or symlink it into
the skills directory your harness reads, for example `.claude/skills/` for Claude Code,
or `.agents/skills/` for harnesses that follow the shared convention. A project copy
travels with the repository; a user-level copy (such as `~/.claude/skills/`) serves
every project. A harness without skill support can follow `SKILL.md` as plain
instructions.

## Install what the configuration names

```bash
python3 -I <skill-dir>/scripts/external_agents.py install              # what is configured
python3 -I <skill-dir>/scripts/external_agents.py install codex claude # named targets
python3 -I <skill-dir>/scripts/external_agents.py install all --best-effort
```

Targets are `codex`, `claude`, `opencode`, `amp`, `bubblewrap`, or `all`. Without
targets, it installs the backends that `EXTERNAL_REVIEWERS` and `EXTERNAL_CODER` name,
plus bubblewrap when a coder is configured. Every script is idempotent.

| Target       | How                                                                                           |
| ------------ | --------------------------------------------------------------------------------------------- |
| `opencode`   | the pinned release archive from GitHub, checked against a committed SHA-256; no remote script |
| `claude`     | `npm install -g @anthropic-ai/claude-code@<pinned>`                                           |
| `codex`      | `npm install -g @openai/codex@<pinned>`                                                       |
| `amp`        | `npm install -g @sourcegraph/amp@<pinned>`                                                    |
| `bubblewrap` | the system package manager (apt, dnf, zypper, pacman, apk, emerge), then a namespace check    |

npm verifies packages against the registry's integrity hashes. An installed CLI of
another version is kept; set `EXTERNAL_AGENTS_PIN=1` to replace it with the pinned
version, as a reproducible environment should. To upgrade a pin, edit the version (and
for opencode the checksums) at the top of its script in `scripts/install/`.

Installers never log in. Each prints what is missing: `opencode auth login`,
`claude setup-token` then `CLAUDE_CODE_OAUTH_TOKEN`, `codex login`, `amp login`, or a
provider API key variable.

## Environments

**Your own Linux or macOS machine.** Run `install` once. Reviews work on both; the
coding sandbox needs Linux.

**Ubuntu 23.10 and later.** `kernel.apparmor_restrict_unprivileged_userns=1` strips
capabilities from unconfined user namespaces. The apparmor package ships a
`bwrap-userns-restrict` profile for `/usr/bin/bwrap`; the bubblewrap installer's check
tells you when it is not loaded.

**Containers and devcontainers.** Reviews work. Most container runtimes block the user
namespaces bubblewrap needs, so run coding agents on the host or in a VM. Install at
image build time:

```dockerfile
COPY .agents/skills/external-agents /opt/external-agents
RUN EXTERNAL_AGENTS_PIN=1 python3 -I /opt/external-agents/scripts/external_agents.py install codex claude
```

**Cloud agents and remote environments with a setup script** (for example Amp orbs, or
the setup step of a hosted coding agent). Setup often builds a snapshot shared by
everyone on the project and does not see personal variables, so it cannot tell whose
configuration names which backend. Install every backend anyone uses, and never let a
failed install fail the environment:

```bash
EXTERNAL_AGENTS_PIN=1 python3 -I .agents/skills/external-agents/scripts/external_agents.py \
  install codex claude opencode amp --best-effort
```

Each person then sets `EXTERNAL_REVIEWERS` and credentials as personal secrets. An
environment restored from an older snapshot may still lack a CLI; `detect` lists it
under `missing`.

**CI.** Run the same `install` with `EXTERNAL_AGENTS_PIN=1`, and pass provider keys as
secrets. Only send source to providers your organization's agreement covers.

## Requirements

- Python 3.11 or later, standard library only, and git.
- Node.js 18 or later with npm for the `claude`, `codex`, and `amp` installers.
- `curl`, `tar` or `unzip`, and `sha256sum` or `shasum` for the opencode installer.
- For coding agents: Linux, bubblewrap, and unprivileged user namespaces.
