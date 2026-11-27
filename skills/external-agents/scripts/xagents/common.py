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
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

# A panel of reviewers: `slot; slot; ? slot`, each slot `backend[:model] | backend[:model]`.
ENV_REVIEWERS = "EXTERNAL_REVIEWERS"
# The coding agent: `backend[:model]`.
ENV_CODER = "EXTERNAL_CODER"
# Extra variables, comma-separated, that an agent process receives, such as a provider key
# with an unusual name.
ENV_PASSTHROUGH = "EXTERNAL_AGENTS_ENV_PASSTHROUGH"
# `1` runs an `amp` reviewer only when a provider connection linked to the Amp account
# serves its model, never on Amp credits.
ENV_AMP_NO_CREDITS = "EXTERNAL_AGENTS_AMP_NO_CREDITS"

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
    """Whether a path relative to a tree root looks like a secret or is VCS metadata, such
    as a submodule's `.git` file.

    The match is on common names, ignoring case; it is best-effort and misses secrets
    stored under others."""
    if any(part in VCS_DIRS for part in relative.parts):
        return True
    name = relative.name.lower()
    if name in SECRET_ALLOWED:
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_PATTERNS)


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
# Provider credentials each backend can authenticate with: its own provider, plus the cloud
# platforms that serve that provider's models. An agent never receives another provider's
# keys; EXTERNAL_AGENTS_ENV_PASSTHROUGH adds any other name.
CLOUD_ENV_PREFIXES = ("AWS_", "GOOGLE_", "VERTEX_", "CLOUD_ML_", "AZURE_")
BACKEND_ENV_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_", "CLAUDE_", *CLOUD_ENV_PREFIXES),
    "codex": ("OPENAI_", "CODEX_", "AZURE_"),
    "amp": ("AMP_",),
    "vibe": ("MISTRAL_",),
}
# opencode serves many providers: the model's provider decides, as `provider/model`.
OPENCODE_PROVIDER_ENV_PREFIXES: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_",),
    "anthropic": ("ANTHROPIC_",),
    "google": ("GOOGLE_", "GEMINI_"),
    "google-vertex": ("GOOGLE_", "VERTEX_"),
    "amazon-bedrock": ("AWS_",),
    "azure": ("AZURE_",),
    "openrouter": ("OPENROUTER_",),
    "mistral": ("MISTRAL_",),
    "deepseek": ("DEEPSEEK_",),
}


def passthrough_names() -> set[str]:
    return {name.strip() for name in os.environ.get(ENV_PASSTHROUGH, "").split(",") if name.strip()}


def credential_prefixes(backend: str, model: str | None) -> tuple[str, ...]:
    if backend == "opencode":
        provider = (model or "").partition("/")[0]
        return ("OPENCODE_", *OPENCODE_PROVIDER_ENV_PREFIXES.get(provider, ()))
    return BACKEND_ENV_PREFIXES[backend]


def credential_env(backend: str, model: str | None = None) -> dict[str, str]:
    """The parent's credentials for `backend` and its model's provider, plus the passthrough names."""
    prefixes = credential_prefixes(backend, model)
    passthrough = passthrough_names()
    return {name: value for name, value in os.environ.items() if name.startswith(prefixes) or name in passthrough}


def agent_env(cwd: Path, backend: str, model: str | None = None) -> dict[str, str]:
    """The allowlisted parent environment plus the credentials of the backend's providers."""
    base = {name: value for name, value in os.environ.items() if name in BASE_ENV or name.startswith(BASE_ENV_PREFIXES)}
    return base | credential_env(backend, model) | {"PWD": str(cwd)}


def installed(command: str) -> bool:
    return shutil.which(command) is not None


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def write_generated(path: Path, content: str) -> None:
    """Replace any entry of the same name, a directory included; a copied symlink must not
    be followed. Undecodable bytes carried as surrogates are written back unchanged."""
    remove(path)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as file:
        file.write(content)


def replace_atomically(path: Path, content: str) -> None:
    """Write `content` to `path` so that a reader sees the old file or the new one, never part."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(content)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def bind_unix(path: Path, backlog: int) -> socket.socket:
    """A listening Unix socket at `path`. It binds by a relative name from inside the
    directory, which keeps long paths under the 108-byte socket path limit; the working
    directory is process-wide, so call it before any thread that uses relative paths starts."""
    server = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        server.bind(path.name)
    finally:
        os.chdir(cwd)
    server.listen(backlog)
    return server


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# How long output is still collected after the CLI exits, from descendants that inherited it.
DRAIN_SECONDS = 1.0


def run_with_timeouts(
    command: list[str],
    cwd: Path,
    total: float,
    idle: float | None,
    env: dict[str, str],
    log: Path | None = None,
    on_idle: Callable[[str, float], bool] | None = None,
) -> tuple[int, str]:
    """Kill the run if it exceeds `total` seconds or, when `idle` is set, is silent that long.

    When the idle deadline passes, `on_idle`, given the output so far and the seconds left
    before the total deadline, may return True to grant another idle period instead, for a
    CLI whose progress shows elsewhere; it must return within those seconds.
    Returns the exit code (124 on a timeout) and the combined output without ANSI escapes.
    The raw output is also appended to `log` as it arrives, so a run that is killed
    leaves a record of what it was doing."""
    sink = open(log, "ab") if log is not None else None  # noqa: SIM115 - closed in finally
    try:
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
    except BaseException:
        if sink is not None:
            sink.close()
        raise
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    chunks: list[bytes] = []
    start = last = time.monotonic()
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)

    def keep(chunk: bytes) -> None:
        chunks.append(chunk)
        if sink is not None:
            sink.write(chunk)
            sink.flush()

    def text() -> str:
        return ANSI.sub("", b"".join(chunks).decode(errors="replace"))

    closed = False
    try:
        while True:
            now = time.monotonic()
            idle_expired = idle is not None and now - last > idle
            if idle_expired and now - start < total and on_idle is not None and on_idle(text(), total - (now - start)):
                last = time.monotonic()
                continue
            now = time.monotonic()
            if now - start > total:
                idle_expired = False  # the total deadline passed, whatever else happened
            if now - start > total or idle_expired:
                proc.kill()
                proc.wait()
                keep(f"\n[killed: {'idle' if idle_expired else 'total'} timeout]".encode())
                return 124, text()
            if proc.poll() is not None:
                # The CLI exited. A descendant may still hold the pipe open, and may keep
                # writing to it, so what is left is collected for a bounded time only.
                drain_until = time.monotonic() + DRAIN_SECONDS
                while not closed and time.monotonic() < drain_until and selector.select(timeout=0.05):
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    keep(chunk)
                break
            if closed:
                # The CLI closed its output but may keep running; the deadlines still apply.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1.0)
                continue
            # Read raw chunks: a line read would block on output without a newline.
            if selector.select(timeout=1.0):
                chunk = os.read(fd, 65536)
                if not chunk:
                    closed = True
                    selector.unregister(fd)
                    continue
                keep(chunk)
                last = time.monotonic()
        return proc.wait(), text()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        selector.close()
        proc.stdout.close()
        if sink is not None:
            sink.close()
