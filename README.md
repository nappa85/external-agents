# external-agents

**Let your coding agent call in a second opinion, or a second pair of hands.**

`external-agents` is an [agent skill](skills/external-agents/SKILL.md) that teaches any
coding agent (Claude Code, Codex, Amp, opencode, or anything that can read a skill and
run a shell command) to use *other* agent CLIs in two roles:

- 🔍 **Reviewers.** Have models from other families review a plan or a change. Different
  models catch different bugs. Reviewers work on a throwaway copy of your repository with
  secrets stripped out and can only read and write files there.
- 🛠️ **Coding agents.** Hand a task to another agent and let it work in a git worktree
  inside a [bubblewrap](https://github.com/containers/bubblewrap) sandbox. It can't see
  the rest of your home directory, can't reach the network except its own model API, and
  asks your main agent when it is stuck.

Missing CLIs are installed on demand, from pinned versions, so the same setup works on
your laptop, in a container, in CI, or in a cloud agent's setup script.

## How it works

```
you ──▶ your agent (the coordinator)
          │
          ├─ review ──▶ copy of the repo without secrets ──▶ codex / claude / opencode / amp
          │                                                   └──▶ review-1.md, review-2.md, …
          │
          └─ code ────▶ git worktree ──▶ bubblewrap sandbox ──▶ agent CLI
                                          ├─ only network exit: proxy ──▶ its model API
                                          └─ `ask "…"` ──▶ your agent answers
```

Your agent stays in charge: it decides when to ask for a review, writes the task,
answers the coding agent's questions, reads the resulting diff, and commits.

## Requirements

- Python 3.11 or later and git.
- At least one agent CLI with credentials: [Codex](https://github.com/openai/codex),
  [Claude Code](https://www.anthropic.com/claude-code), [opencode](https://opencode.ai),
  or [Amp](https://ampcode.com). The skill can install them; logging in is up to you.
- For coding agents: Linux with bubblewrap and unprivileged user namespaces. Reviews
  also work on macOS.
- Node.js and npm to install Claude Code, Codex, or Amp.

## Install the skill

Clone the repository and link the skill into the skills directory your agent reads:

```bash
git clone https://github.com/nappa85/external-agents.git
# Claude Code, for every project:
ln -s "$PWD/external-agents/skills/external-agents" ~/.claude/skills/external-agents
# or, for one project, in the shared `.agents/skills/` location:
cp -r external-agents/skills/external-agents your-project/.agents/skills/
```

If your agent has no skill support, point it at
[`skills/external-agents/SKILL.md`](skills/external-agents/SKILL.md) and ask it to follow
it.

## Configure

Tell the skill which agents to use, for example in your shell profile:

```bash
# Reviewers: `;` separates reviewers that run in parallel, `|` lists fallbacks,
# and `?` marks one as optional.
export EXTERNAL_REVIEWERS="codex:gpt-6.1-sol | amp:openai/gpt-6.1-sol; claude:sonnet; \
  ? opencode:opencode/deepseek-v4.1-flash-free"

# Coding agent
export EXTERNAL_CODER="opencode:openai/gpt-6.1-sol"
```

Then check and install what is missing:

```bash
python3 -I skills/external-agents/scripts/external_agents.py detect
python3 -I skills/external-agents/scripts/external_agents.py install
```

Nothing runs unless you configure it. Without `EXTERNAL_REVIEWERS`, your agent reviews
with its own subagents as usual.

> **Your code goes to the providers you name.** Only list providers whose terms you
> accept for your source code. Free tiers may keep or train on what they receive.

## Use it

Mostly, you just talk to your agent:

- *"Implement the parser change, then get it reviewed by the external panel."*
- *"Have the external coder write the migration in a worktree; answer its questions and
  show me the diff."*

The agent follows the skill: it creates a worktree, writes the task, runs the coder in
the background, waits on its questions with `question --wait`, and reviews the result
before committing.

You can also run the commands yourself:

```bash
X=skills/external-agents/scripts/external_agents.py

# Review the changes since main with the configured panel
python3 -I $X review --prompt-file prompt.md --output-dir reviews/ --base main

# Run a coding agent on a fresh worktree
git worktree add -b feature ../wt/feature main
python3 -I $X code --worktree ../wt/feature --task task.md --state ~/.cache/xagents/feature &
python3 -I $X question --wait --state ~/.cache/xagents/feature   # prints its question
python3 -I $X answer --to 1 --state ~/.cache/xagents/feature "Use the existing parser."
```

## Safety, in short

| Concern                      | Reviewers                                          | Coding agents                                                          |
| ---------------------------- | -------------------------------------------------- | ---------------------------------------------------------------------- |
| Your secrets                 | `.env`, keys and similar files are left out        | untracked secret files are masked; the rest of your home is invisible  |
| Changing your files          | only a disposable copy; edits come back as a patch | only the worktree; you review the diff before running anything         |
| Network                      | the CLI's own access to its model                  | only the model API, through an allowlisting proxy                      |
| Running code on your machine | file tools only (Codex: its own sandbox)           | inside the sandbox only; review the diff before you build or test it   |

The details, and the limits of each protection, are in
[docs/architecture.md](docs/architecture.md).

## Documentation

- [docs/cli.md](docs/cli.md): every command, option, variable, and exit code.
- [docs/architecture.md](docs/architecture.md): how reviews and the sandbox work, and
  the threat model.
- [skills/external-agents/](skills/external-agents/): the skill itself, the
  instructions your agent reads.

## Development

The scripts use only the Python standard library.

```bash
python3 -m unittest discover -s tests       # the live sandbox tests need bwrap
uvx ruff check . && uvx ruff format --check .
```

Installer pins live at the top of each script in
[`skills/external-agents/scripts/install/`](skills/external-agents/scripts/install/).
