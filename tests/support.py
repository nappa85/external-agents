"""Puts the skill's script directory on sys.path, as the CLI does for itself."""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "external-agents" / "scripts"
SCRIPT = SCRIPTS / "external_agents.py"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))

from xagents import backends, common, mailbox, proxy, review, sandbox, seccomp, snapshot  # noqa: E402

__all__ = ["SCRIPT", "SCRIPTS", "backends", "common", "mailbox", "proxy", "review", "sandbox", "seccomp", "snapshot"]
