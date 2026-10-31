#!/usr/bin/env python3
"""Use other agent CLIs (Codex, Claude Code, opencode, Amp) as confined reviewers or as
sandboxed coding agents.

  detect                       what is configured, installed, and missing
  install [targets]            install pinned CLIs and bubblewrap (default: what is configured)
  review                       run the reviewer panel on a sanitized snapshot
  code                         run a coding agent in bubblewrap on a git worktree
  question / answer            the coding agent's questions, over a Unix socket

Exit codes: 0 success; 2 usage or setup error; 3 (review) a required reviewer slot had no
available reviewer; the coding agent's own exit code, or 124 on timeout, for `code`;
1 (question) none waiting; 4 (question, answer) no active run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Explicit, so the package is found under `python3 -I`, which leaves the script's
# directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xagents import mailbox, review, sandbox  # noqa: E402
from xagents.backends import BACKENDS, CODER_MODEL_REQUIRED, INSTALLERS, parse_agent  # noqa: E402
from xagents.common import ENV_CODER, ENV_REVIEWERS, UsageError, installed  # noqa: E402


def bubblewrap_works() -> bool:
    """Whether bwrap can create its namespaces here; containers and some kernels forbid it."""
    if not installed("bwrap"):
        return False
    probe = ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "true"]
    return subprocess.run(probe, capture_output=True).returncode == 0


def configured_backends() -> list[str]:
    named = {r.backend for slot in review.parse_panel(os.environ.get(ENV_REVIEWERS) or "") for r in slot.reviewers}
    if coder := os.environ.get(ENV_CODER):
        named.add(parse_agent(coder, CODER_MODEL_REQUIRED)[0])
    return [b for b in BACKENDS if b in named]


def detect(_: argparse.Namespace) -> int:
    coder = os.environ.get(ENV_CODER)
    coder_backend = parse_agent(coder, CODER_MODEL_REQUIRED)[0] if coder else None
    report = {
        "installed": [b for b in BACKENDS if installed(b)],
        "reviewers": review.detect_reviewers(),
        "coder": {
            "configured": coder,
            "installed": bool(coder_backend and installed(coder_backend)),
            "bubblewrap": bubblewrap_works(),
        },
        # Configured backends, and bubblewrap when a coder is configured, that are missing,
        # each with the script that installs it.
        "missing": {
            name: str(INSTALLERS[name])
            for name in [*configured_backends(), *(["bubblewrap"] if coder else [])]
            if not installed("bwrap" if name == "bubblewrap" else name)
        },
    }
    print(json.dumps(report, indent=2))
    return 0


def install(args: argparse.Namespace) -> int:
    targets = args.targets or [*configured_backends(), *(["bubblewrap"] if os.environ.get(ENV_CODER) else [])]
    if "all" in targets:
        targets = [*BACKENDS, "bubblewrap"]
    unknown = [t for t in targets if t not in INSTALLERS]
    if unknown:
        raise UsageError(f"unknown install target {unknown[0]!r}; expected {', '.join([*INSTALLERS, 'all'])}")
    if not targets:
        print(f"nothing to install: {ENV_REVIEWERS} and {ENV_CODER} name no backend", file=sys.stderr)
        return 0
    if shutil.which("bash") is None:
        raise UsageError("the install scripts need bash")
    failed = []
    for target in dict.fromkeys(targets):
        if subprocess.run(["bash", str(INSTALLERS[target])]).returncode != 0:
            failed.append(target)
            print(f"warning: installing {target} failed", file=sys.stderr)
    if failed and not args.best_effort:
        return 2
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("detect", help="Print what is configured, installed, and missing, as JSON")
    setup = commands.add_parser("install", help="Install pinned agent CLIs and bubblewrap")
    setup.add_argument("targets", nargs="*", help=f"{', '.join(INSTALLERS)}, or all; default: what is configured")
    setup.add_argument("--best-effort", action="store_true", help="Exit 0 even when an install fails")
    review.add_parser(commands)
    sandbox.add_parsers(commands)
    return root


def main(argv: list[str]) -> int:
    # Everything after `--` is a command to run in the coding sandbox instead of the agent.
    command = None
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    args = parser().parse_args(argv)
    try:
        if command is not None and args.command != "code":
            raise UsageError("only `code` takes a command after --")
        if args.command == "detect":
            return detect(args)
        if args.command == "install":
            return install(args)
        if args.command == "review":
            return review.review(args)
        if args.command == "code":
            return sandbox.code(args, command or None)
        if args.command == "question":
            return mailbox.question(args.state, args.wait, args.timeout)
        return mailbox.answer(args.state, args.text, args.to)
    except UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
