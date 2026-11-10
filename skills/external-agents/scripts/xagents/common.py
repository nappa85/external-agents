"""Pieces shared by the review and coding modes: errors, secret names, the agent
environment, and running a CLI under deadlines."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

# A panel of reviewers: `slot; slot; ? slot`, each slot `backend[:model] | backend[:model]`.
ENV_REVIEWERS = "EXTERNAL_REVIEWERS"
# The coding agent: `backend[:model]`.
ENV_CODER = "EXTERNAL_CODER"
# Extra variables, comma-separated, that an agent process receives, such as a provider key
# with an unusual name.
ENV_PASSTHROUGH = "EXTERNAL_AGENTS_ENV_PASSTHROUGH"

SKILL_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"


class UsageError(Exception):
    """A configuration or invocation error the caller must fix."""


SECRET_PATTERNS = (
    ".env",
    ".env.*",
    ".envrc",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.sqlite*",
    "*.db",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.keystore",
    "*.jks",
    "*.secret",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    "credentials*.json",
    # Agent audit logs, such as OpenFirma's, record tool calls with their arguments.
    "audit.jsonl",
)
SECRET_ALLOWED = (".env.example", ".env.sample", ".env.template")
# Version-control metadata: never copied, and masked in a coding sandbox, because hooks and
# configuration in it run code on the host's next VCS command.
VCS_DIRS = (".git", ".jj")


def is_secret(relative: Path) -> bool:
    """Whether a path relative to a tree root looks like a secret or sits in VCS metadata.

    The match is on common names; it is best-effort and misses secrets stored under others."""
    if any(part in VCS_DIRS for part in relative.parts[:-1]):
        return True
    name = relative.name
    if name in SECRET_ALLOWED:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)


# An agent's environment is built from an allowlist: these names and prefixes, the
# provider credential prefixes, and EXTERNAL_AGENTS_ENV_PASSTHROUGH.
BASE_ENV = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM", "LANG", "LANGUAGE", "TZ",
        "TMPDIR", "TMP", "TEMP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
        "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    }
)  # fmt: skip
BASE_ENV_PREFIXES = ("LC_", "XDG_")
# Provider credentials the backends need to authenticate.
PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_", "CLAUDE_", "OPENAI_", "CODEX_", "OPENCODE_", "OPENROUTER_",
    "AWS_", "GOOGLE_", "GEMINI_", "VERTEX_", "AZURE_",
)  # fmt: skip
# Amp authenticates with its own credentials, so only the amp backend receives them.
AMP_ENV_PREFIX = "AMP_"


def passthrough_names() -> set[str]:
    return {name.strip() for name in os.environ.get(ENV_PASSTHROUGH, "").split(",") if name.strip()}


def credential_env(backend: str) -> dict[str, str]:
    """The parent's provider credentials for `backend`, plus the passthrough names."""
    prefixes = PROVIDER_ENV_PREFIXES + ((AMP_ENV_PREFIX,) if backend == "amp" else ())
    passthrough = passthrough_names()
    return {name: value for name, value in os.environ.items() if name.startswith(prefixes) or name in passthrough}


def agent_env(cwd: Path, backend: str) -> dict[str, str]:
    """The allowlisted parent environment plus the credentials of the backend's providers."""
    base = {name: value for name, value in os.environ.items() if name in BASE_ENV or name.startswith(BASE_ENV_PREFIXES)}
    return base | credential_env(backend) | {"PWD": str(cwd)}


def installed(command: str) -> bool:
    return shutil.which(command) is not None


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def write_generated(path: Path, content: str) -> None:
    """Replace any entry of the same name; a copied symlink must not be followed."""
    path.unlink(missing_ok=True)
    path.write_text(content)


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def run_with_timeouts(
    command: list[str], cwd: Path, total: float, idle: float | None, env: dict[str, str]
) -> tuple[int, str]:
    """Kill the run if it exceeds `total` seconds or, when `idle` is set, is silent that long.

    Returns the exit code (124 on a timeout) and the combined output without ANSI escapes."""
    # A new session lets a timeout kill the CLI's child processes too, which would
    # otherwise keep the pipe open.
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    chunks: list[bytes] = []
    start = last = time.monotonic()
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)

    def text() -> str:
        return ANSI.sub("", b"".join(chunks).decode(errors="replace"))

    closed = False
    try:
        while True:
            now = time.monotonic()
            idle_expired = idle is not None and now - last > idle
            if now - start > total or idle_expired:
                proc.kill()
                proc.wait()
                return 124, text() + f"\n[killed: {'idle' if idle_expired else 'total'} timeout]"
            if closed:
                # The CLI closed its output but may keep running; the deadlines still apply.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1.0)
                if proc.returncode is not None:
                    break
                continue
            # Read raw chunks: a line read would block on output without a newline.
            if selector.select(timeout=1.0):
                chunk = os.read(fd, 65536)
                if not chunk:
                    closed = True
                    selector.unregister(fd)
                    continue
                chunks.append(chunk)
                last = time.monotonic()
            elif proc.poll() is not None:
                # The CLI exited; a lingering descendant may still hold the pipe open.
                while selector.select(timeout=0):
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                break
        return proc.wait(), text()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.stdout.close()
