"""What the wrapper knows about each agent CLI, for both modes."""

from __future__ import annotations

import json
from pathlib import Path

from .common import SCRIPTS_DIR, UsageError

BACKENDS = ("codex", "claude", "opencode", "amp")
# Backends that need an explicit model: opencode serves many providers and may default to a
# free tier; an amp reviewer runs a generated agent that names its model. A coding amp
# agent takes an optional mode key instead.
REVIEW_MODEL_REQUIRED = ("opencode", "amp")
CODER_MODEL_REQUIRED = ("opencode",)
# Scripts that install a backend's pinned CLI; `bubblewrap` is the coding sandbox.
INSTALLERS = {
    "claude": SCRIPTS_DIR / "install" / "claude-code.sh",
    "codex": SCRIPTS_DIR / "install" / "codex.sh",
    "opencode": SCRIPTS_DIR / "install" / "opencode.sh",
    "amp": SCRIPTS_DIR / "install" / "amp.sh",
    "bubblewrap": SCRIPTS_DIR / "install" / "bubblewrap.sh",
}
# Project configuration each backend loads from its working directory and that can run
# code. `claude` skips project settings through `--setting-sources` instead.
AMP_PLUGINS = Path(".amp/plugins")
AMP_SETTINGS = Path(".amp/settings.json")
PROJECT_CONFIG = {
    "codex": (Path(".codex"),),
    "opencode": (Path(".opencode"), Path("opencode.jsonc")),
    "amp": (AMP_PLUGINS,),
}


def parse_agent(spec: str, model_required: tuple[str, ...]) -> tuple[str, str | None]:
    """`backend[:model]` into its parts, checking the backend and the model requirement."""
    backend, _, model = (part.strip() for part in spec.partition(":"))
    if backend not in BACKENDS:
        raise UsageError(f"unknown agent backend {backend!r}; expected one of {', '.join(BACKENDS)}")
    if backend in model_required and not model:
        raise UsageError(f"{backend} needs an explicit model, for example {backend}:openai/gpt-6.1-sol")
    return backend, model or None


# --- Coding mode -------------------------------------------------------------------------

# Hosts a coding agent may reach through the egress proxy, by backend. opencode's depend on
# the model's provider. These are the API and login-refresh hosts observed or documented
# for each CLI; `--log-hosts` shows what a run actually asks for.
OPENAI_HOSTS = ("chatgpt.com", "auth.openai.com", "api.openai.com")
CODER_HOSTS: dict[str, tuple[str, ...]] = {
    "claude": ("api.anthropic.com", "console.anthropic.com", "platform.claude.com"),
    "codex": OPENAI_HOSTS,
    "amp": ("ampcode.com",),
}
OPENCODE_PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "openai": OPENAI_HOSTS,
    "anthropic": ("api.anthropic.com", "console.anthropic.com"),
    "opencode": ("opencode.ai",),
    "openrouter": ("openrouter.ai",),
    "google": ("generativelanguage.googleapis.com", "oauth2.googleapis.com"),
    "mistral": ("api.mistral.ai",),
    "deepseek": ("api.deepseek.com",),
}


def coder_hosts(backend: str, model: str | None) -> tuple[str, ...]:
    if backend == "opencode":
        provider = (model or "").partition("/")[0]
        return OPENCODE_PROVIDER_HOSTS.get(provider, ())
    return CODER_HOSTS[backend]


# Login files, relative to HOME, that a coding sandbox binds read-only when they exist.
# Environment credentials (provider prefixes and the passthrough list) are passed as well.
CODER_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "opencode": (".local/share/opencode/auth.json",),
    "claude": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
    "amp": (".local/share/amp/secrets.json",),
}

# Inside the sandbox the agent may run commands: the sandbox, not the agent's own
# permission system, is the boundary. Reads outside the working directory are denied, not
# asked: nobody answers a question in `opencode run`, so an "ask" ends the run.
OPENCODE_CODER = {
    "$schema": "https://opencode.ai/config.json",
    "agent": {
        "coder": {
            "mode": "primary",
            "description": "Coding agent in a bubblewrap sandbox; asks the coordinator with `ask`.",
            "tools": {
                "write": True, "edit": True, "bash": True, "read": True, "glob": True, "grep": True,
                "list": True, "webfetch": False, "websearch": False, "task": False, "skill": False,
            },
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny", "external_directory": "deny"},
        }
    },
}  # fmt: skip


def coder_files(backend: str) -> dict[str, str]:
    """Configuration files, relative to the sandbox HOME, that the launcher writes."""
    if backend == "opencode":
        return {".config/opencode/opencode.json": json.dumps(OPENCODE_CODER, indent=2)}
    return {}


def coder_env(backend: str) -> dict[str, str]:
    """Fixed variables for the agent process inside the sandbox."""
    if backend == "claude":
        # The sandbox is the boundary, so permission prompts are skipped; Claude Code asks
        # for IS_SANDBOX to allow that. Telemetry and update checks would only be refused.
        return {"IS_SANDBOX": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
    return {}


def coder_command(backend: str, executable: str, model: str | None, worktree: Path, task: str) -> list[str]:
    if backend == "opencode":
        return [executable, "run", "--dir", str(worktree), "-m", str(model), "--agent", "coder", task]
    if backend == "claude":
        chosen = ["--model", model] if model else []
        return [executable, "-p", "--dangerously-skip-permissions", "--no-session-persistence", *chosen, "--", task]
    if backend == "codex":
        chosen = ["--model", model] if model else []
        return [
            executable, "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
            "--color", "never", "--cd", str(worktree), *chosen, task,
        ]  # fmt: skip
    # For amp the model part names an agent mode, such as `smart`.
    chosen = ["--mode", model] if model else []
    return [executable, "--dangerously-allow-all", "--no-notifications", "--no-ide", *chosen, "-x", task]
