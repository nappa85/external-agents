"""Confined reviews by external agent CLIs on sanitized snapshots of a Git checkout.

A panel is a list of slots separated by `;`. Slots run in parallel, each on its own
snapshot. Within a slot, reviewers separated by `|` or `,` are tried in order until one
produces a review. A slot prefixed with `?` is optional. A reviewer may change files only
inside its snapshot, for example to add a regression test; those changes are returned as
a patch next to its review.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .backends import (
    AMP_PLUGINS,
    AMP_SETTINGS,
    BACKENDS,
    INSTALLERS,
    PROJECT_CONFIG,
    REVIEW_MODEL_REQUIRED,
    VIBE_QUIET,
    parse_agent,
)
from .common import (
    ENV_AMP_NO_CREDITS,
    ENV_REVIEWERS,
    UsageError,
    agent_env,
    installed,
    remove,
    replace_atomically,
    run_with_timeouts,
    write_generated,
)
from .snapshot import Changes, base_diff, check_includes, filter_patch, snapshot

PROMPT_FILE = "REVIEW_PROMPT.md"
DIFF_FILE = "REVIEW_DIFF.patch"
FILES_FILE = "REVIEW_FILES.txt"
HANDOFF_FILES = (Path(PROMPT_FILE), Path(DIFF_FILE), Path(FILES_FILE))
INSTRUCTION = (
    f"You are a reviewer working in a disposable copy of a repository. Read {PROMPT_FILE} in the "
    "current directory and follow it. You may add or change files in the current directory, for "
    "example a regression test that demonstrates a finding; your changes are returned as a patch. "
    "Do not read or write files outside the current directory."
)
UNAVAILABLE = re.compile(
    r"quota|credit|rate.?limit|insufficient|unauthori[sz]ed|forbidden|\b429\b|\b402\b|"
    r"exceeded|billing|not available|model not found|not logged in",
    re.IGNORECASE,
)
# A line a CLI prints for a provider failure, as opposed to review prose.
ERROR_LINE = re.compile(r"^\W*(?:error|api error|fatal)\b", re.IGNORECASE)
# How much of a failed reviewer's output the run log keeps.
FAILURE_TAIL = 1000
OPENCODE_REVIEWER = {
    "$schema": "https://opencode.ai/config.json",
    "agent": {
        "reviewer": {
            "mode": "primary",
            "description": "Reviewer confined to its working directory",
            # The permission allowlist leaves only read, search, list, todo, and edit tools,
            # including for tools that later opencode versions add; external_directory keeps
            # reads and edits inside the snapshot. websearch could carry repository text off
            # the machine; task and skill could start agents with other permissions. Edit
            # tools must not be listed here: a `tools` entry becomes a permission rule that
            # the `*` deny below would override.
            "tools": {"bash": False, "webfetch": False, "websearch": False, "task": False, "skill": False},
            "permission": {
                "*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
                "todowrite": "allow", "todoread": "allow", "edit": "allow", "bash": "deny",
                "webfetch": "deny", "websearch": "deny", "task": "deny", "skill": "deny",
                "external_directory": "deny",
            },
        }
    },
}  # fmt: skip
AMP_MODE = "external-reviewer"
# A project plugin that registers a mode whose agent can only read, create, and edit
# files. Amp offers no grep or glob tool to plugin agents; Read also lists directories.
# apply_patch is left out because its paths are inside the patch text, where
# permission rules cannot match them.
AMP_FILE_TOOLS = ("Read", "create_file", "edit_file")
AMP_PLUGIN = """\
// @amp-agent-mode {{"key": "{mode}", "label": "{mode}"}}
import type {{ PluginAPI }} from '@ampcode/plugin'

export default function (amp: PluginAPI) {{
	const agent = amp.createAgent({{
		model: {model},
		tools: {{ include: ['Read', 'create_file', 'edit_file'], exclude: ['tool_search', 'code_exec'] }},{effort}
		instructions: 'You are a reviewer. You can read and change files in your working directory but cannot run commands.',
	}})
	amp.registerAgentMode({{ key: '{mode}', label: '{mode}', agent: agent.definition }})
}}
"""

# Vibe reads `VIBE_<SETTING>` variables over the user's and the project's config, so these
# hold even when `~/.vibe/config.toml` allows more. With the `accept-edits` policy only
# file edits run without approval, and a headless run denies anything that needs it.
# `outside_workdir` denies file tools outside the snapshot, through symlinks too. Shell,
# web and subagent tools are refused outright, and the JavaScript tool `run_typescript`
# loses its process and subagent APIs; what it can still call goes through the same checks.
VIBE_REVIEWER_ENV = {
    **VIBE_QUIET,
    "VIBE_OUTSIDE_WORKDIR": "deny",
    "VIBE_BYPASS_TOOL_PERMISSIONS": "false",
    "VIBE_ENABLE_AUTO_APPROVE": "false",
    "VIBE_ENABLE_SUBAGENTS": "false",
    "VIBE_ENABLE_BACKGROUND_PROCESSES": "false",
    "VIBE_SESSION_LOGGING__ENABLED": "false",
    "VIBE_TOOLS": json.dumps({
        tool: {"permission": "never"}
        for tool in ("bash", "git_bash", "powershell", "web_fetch", "web_search", "task")
    }),
}  # fmt: skip


def snapshot_roots(snap: Path) -> list[str]:
    """Both spellings of the snapshot path: a temporary directory can sit behind a symlink,
    such as `/var` on macOS."""
    return list(dict.fromkeys((str(snap.absolute()), str(snap.resolve()))))


def amp_settings(snap: Path) -> dict[str, object]:
    """Permissions that allow the file tools inside the snapshot and reject every other call.

    Amp matches the path before normalizing it, so `..` segments are rejected first."""
    paths = [pattern for root in snapshot_roots(snap) for pattern in (root, f"{root}/*")]
    rules: list[dict[str, object]] = []
    for tool in AMP_FILE_TOOLS:
        rules.append({"tool": tool, "matches": {"path": r"/(^|\/)\.\.(\/|$)/"}, "action": "reject"})
        rules.append({"tool": tool, "matches": {"path": paths}, "action": "allow"})
    return {"amp.permissions": [*rules, {"tool": "*", "action": "reject"}]}


# Reasoning efforts a reviewer spec may end in, as `@effort`. codex, opencode and amp pass
# any of them to the provider, which accepts a subset per model; Claude Code accepts only
# its own list and silently ignores anything else, so the wrapper checks that one.
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
BACKEND_EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max")}
# A model at a high effort can think for minutes without printing a byte, which the idle
# timeout would take for a stall; `--idle-timeout` is the silence allowed up to `medium`.
EFFORT_IDLE_FACTOR = {"high": 2, "xhigh": 3, "max": 3}


@dataclass(frozen=True)
class Reviewer:
    backend: str
    model: str | None
    effort: str | None = None

    @property
    def label(self) -> str:
        label = f"{self.backend}:{self.model}" if self.model else self.backend
        return f"{label}@{self.effort}" if self.effort else label

    @property
    def idle_timeout_applies(self) -> bool:
        # `claude -p` prints nothing until the review is complete; `amp` and `vibe` stream
        # JSON events.
        return self.backend != "claude"

    @property
    def free(self) -> bool:
        """A free tier: daily limits, throttled when runs overlap."""
        return bool(self.model and self.model.endswith("-free"))

    @property
    def model_label(self) -> str:
        """`backend[:model]`: free-tier limits apply to the model at any effort."""
        return f"{self.backend}:{self.model}" if self.model else self.backend

    def idle_timeout(self, base: float) -> float:
        return base * EFFORT_IDLE_FACTOR.get(self.effort or "", 1)

    def command(self, snap: Path, instruction: str, last_message: Path) -> list[str]:
        model = ["--model", self.model] if self.model else []
        if self.backend == "codex":
            effort = ["-c", f'model_reasoning_effort="{self.effort}"'] if self.effort else []
            return [
                "codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check",
                "--color", "never", "--cd", str(snap),
                "--output-last-message", str(last_message), *model, *effort, instruction,
            ]  # fmt: skip
        if self.backend == "claude":
            # No `Read` rule: an unscoped one also allows reads outside the working directory,
            # while dontAsk denies those once the rule is absent. An `Edit` rule covers every
            # file-editing tool; `//` starts an absolute path.
            edits = [f"Edit(/{root}/**)" for root in snapshot_roots(snap)]
            return [
                # User settings only: project settings in the reviewed tree could add hooks.
                "claude", "-p", "--tools", "Read,Glob,Grep,Edit,Write", "--permission-mode", "dontAsk",
                "--allowedTools", *edits, "--strict-mcp-config", "--setting-sources", "user",
                "--no-session-persistence", *model, *(["--effort", self.effort] if self.effort else []),
                "--", instruction,
            ]  # fmt: skip
        if self.backend == "amp":
            return [
                # Streamed events show progress, so a stalled model request trips the idle
                # timeout, and a killed run's log shows what it was doing.
                "amp", "--settings-file", str(snap / AMP_SETTINGS), "--mode", AMP_MODE,
                "--visibility", "private", "--no-notifications", "--no-ide", "--stream-json",
                "-x", instruction,
            ]  # fmt: skip
        if self.backend == "vibe":
            # Vibe calls an effort a thinking level, and "no reasoning" `off`.
            thinking = ["--thinking", "off" if self.effort == "none" else self.effort] if self.effort else []
            return [
                # Not `--trust`: an untrusted folder's project config is ignored.
                "vibe", f"--prompt={instruction}", "--workdir", str(snap), "--agent", "default",
                "--approval-policy", "accept-edits", "--output", "streaming", *model, *thinking,
            ]  # fmt: skip
        # opencode calls an effort a variant.
        effort = ["--variant", self.effort] if self.effort else []
        # opencode takes its project from `$PWD`, not the process cwd; pin it with --dir.
        return [
            "opencode", "run", "--dir", str(snap), "-m", str(self.model), "--agent", "reviewer", *effort, instruction,
        ]  # fmt: skip

    def env(self, snap: Path) -> dict[str, str]:
        extra = VIBE_REVIEWER_ENV if self.backend == "vibe" else {}
        return agent_env(snap, self.backend, self.model) | extra

    @property
    def owned_paths(self) -> tuple[Path, ...]:
        """Paths the wrapper removes or generates for this backend, never the reviewer's changes."""
        generated = {"opencode": (Path("opencode.json"),), "amp": (AMP_PLUGINS, AMP_SETTINGS)}
        return (*PROJECT_CONFIG.get(self.backend, ()), *generated.get(self.backend, ()))

    def prepare(self, snap: Path) -> None:
        """Write the backend's confined agent configuration into the snapshot.

        Project configuration that the backend would load from the reviewed tree, such as
        plugins that run code, is removed first. The diff still shows changes to it."""
        for relative in PROJECT_CONFIG.get(self.backend, ()):
            remove(snap / relative)
        if self.backend == "opencode":
            write_generated(snap / "opencode.json", json.dumps(OPENCODE_REVIEWER, indent=2))
        elif self.backend == "amp":
            # The reviewed tree may hold `.amp` as a file or a link; the generated mode
            # must land in a real directory inside the snapshot.
            amp_dir = snap / AMP_PLUGINS.parent
            if amp_dir.is_symlink() or not amp_dir.is_dir():
                remove(amp_dir)
            plugins = snap / AMP_PLUGINS
            plugins.mkdir(parents=True)
            model = json.dumps(self.model)
            effort = f"\n\t\treasoningEffort: {json.dumps(self.effort)}," if self.effort else ""
            plugin = AMP_PLUGIN.format(mode=AMP_MODE, model=model, effort=effort)
            write_generated(plugins / f"{AMP_MODE}.ts", plugin)
            write_generated(snap / AMP_SETTINGS, json.dumps(amp_settings(snap), indent=2))


def parse_reviewer(spec: str) -> Reviewer:
    """`backend[:model][@effort]`. Only a known effort counts as a suffix, so a model ID
    containing `@`, such as a dated Vertex model, keeps its `@`."""
    head, at, effort = spec.strip().rpartition("@")
    if not at or effort not in EFFORTS:
        return Reviewer(*parse_agent(spec, REVIEW_MODEL_REQUIRED))
    backend, model = parse_agent(head, REVIEW_MODEL_REQUIRED)
    allowed = BACKEND_EFFORTS.get(backend, EFFORTS)
    if effort not in allowed:
        raise UsageError(f"{backend} takes no reasoning effort {effort!r}; expected one of {', '.join(allowed)}")
    return Reviewer(backend, model, effort)


@dataclass(frozen=True)
class Slot:
    """One panel member: the reviewers to try in order until one produces a review."""

    reviewers: tuple[Reviewer, ...]
    optional: bool = False


def parse_panel(spec: str) -> list[Slot]:
    """Parse `slot; slot; ? slot`, where each slot is `reviewer | reviewer` (or `,`)."""
    slots = []
    for text in spec.split(";"):
        text = text.strip()
        optional = text.startswith("?")
        specs = [part for part in re.split(r"[|,]", text.removeprefix("?")) if part.strip()]
        if specs:
            slots.append(Slot(tuple(parse_reviewer(part) for part in specs), optional))
        elif optional:
            raise UsageError(f"optional slot {text!r} names no reviewer")
    return slots


def resolve_panel(cli: list[str] | None, cli_panel: str | None, env: str | None) -> list[Slot]:
    """The configured panel without uninstalled reviewers; a slot may end up empty.

    Nothing configured means no external reviewer: the host's own subagent reviews alone."""
    if cli:
        slots = [Slot(tuple(parse_reviewer(spec) for spec in cli))]
    elif cli_panel is not None:
        slots = parse_panel(cli_panel)
    elif env and env.strip():
        slots = parse_panel(env)
    else:
        return []
    if not slots:
        raise UsageError("the reviewer configuration names no reviewer")
    return [Slot(tuple(r for r in slot.reviewers if installed(r.backend)), slot.optional) for slot in slots]


def classify(returncode: int, output: str, expect: re.Pattern[str] | None = None) -> str:
    """`ok`, `stalled` (killed after going silent), `timed out` (killed at the total
    timeout), `unavailable`, or `incomplete` (exited 0 but without the text `expect`
    requires, such as a verdict line)."""
    text = output.strip()
    if returncode == 124 and text.endswith("[killed: idle timeout]"):
        return "stalled"
    if returncode == 124 and text.endswith("[killed: total timeout]"):
        return "timed out"
    if returncode != 0 or not text:
        return "unavailable"
    # Some CLIs exit 0 after printing a provider error. Only a short output with an error
    # line that names a provider failure counts, so a review that discusses
    # authorization or quotas is kept, however short.
    if len(text) < 500 and any(ERROR_LINE.match(line) and UNAVAILABLE.search(line) for line in text.splitlines()):
        return "unavailable"
    if expect is not None and not expect.search(text):
        return "incomplete"
    return "ok"


# Free tiers have daily limits and are throttled when several runs overlap; a throttled run
# often opens a stream that never returns a byte instead of failing.
def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "external-agents"


@contextlib.contextmanager
def free_tier_lock(reviewer: Reviewer) -> Iterator[None]:
    """Hold one machine-wide lock while a free model runs, so free runs never overlap."""
    if not reviewer.free:
        yield
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir() / "free.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _today() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


def _ledger() -> dict[str, dict[str, str]]:
    try:
        return json.loads((state_dir() / "limits.json").read_text())
    except (OSError, ValueError):
        return {}


def limited_today(reviewer: Reviewer) -> bool:
    return _ledger().get(reviewer.model_label, {}).get("date") == _today()


def mark_limited(reviewer: Reviewer, reason: str) -> None:
    """Skip a free model for the rest of the UTC day; free limits reset daily."""
    state_dir().mkdir(parents=True, exist_ok=True)
    ledger = _ledger()
    ledger[reviewer.model_label] = {"date": _today(), "reason": reason}
    replace_atomically(state_dir() / "limits.json", json.dumps(ledger, indent=2))


def amp_output(code: int, raw: str) -> str:
    """The review from `amp --stream-json` output, or a readable trace of the run.

    A successful run yields its final message. Otherwise each event becomes a short line,
    so the failure tail shows the last tool calls, the Amp thread to open, and why the run
    ended; text that is not an event, such as a CLI warning or the kill marker, is kept.
    Output without any event, such as a startup failure, is returned unchanged."""
    lines: list[str] = []
    thread = None
    events = 0
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            if line.strip():
                lines.append(line)
            continue
        events += 1
        thread = event.get("session_id") or thread
        if event.get("type") == "result":
            result = str(event.get("result") or event.get("error") or "")
            if code == 0 and not event.get("is_error") and result.strip():
                return result
            lines.append(f"Error: {result}" if event.get("is_error") else f"result: {result[:500]}")
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                lines.append(f"tool {block.get('name')} {json.dumps(block.get('input'))[:200]}")
            elif block.get("type") == "text":
                lines.append(f"text: {' '.join(str(block.get('text')).split())[:200]}")
    if not events:
        return raw
    if thread:
        # Before the kill marker, which `classify` reads at the end.
        marker = lines.pop() if lines and lines[-1].startswith("[killed:") else None
        lines += [f"amp thread: {thread}", *([marker] if marker else [])]
    return "\n".join(lines)


def vibe_output(code: int, raw: str) -> str:
    """The review from `vibe --output streaming`, or a readable trace of the run.

    Each line is a finished history entry. A successful run yields its last assistant
    message; otherwise tool calls become short lines and other text, such as a denied
    call, an `Error:` line or the kill marker, is kept."""
    lines: list[str] = []
    answer = None
    events = 0
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            if line.strip():
                lines.append(line)
            continue
        events += 1
        if entry.get("type") == "message" and entry.get("role") == "assistant":
            content = entry.get("content") or []
            text = "\n\n".join(str(b.get("text")) for b in content if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                answer = text
                lines.append(f"text: {' '.join(text.split())[:200]}")
        elif entry.get("type") == "effect":
            display = (entry.get("detail") or {}).get("display") or {}
            lines.append(f"tool {entry.get('title')}: {' '.join(str(display.get('summary', '')).split())[:200]}")
    if code == 0 and answer is not None:
        return answer
    return "\n".join(lines) if events else raw


def amp_session(raw: str) -> str | None:
    """The Amp thread of a run, from the first `amp --stream-json` event that names it."""
    for line in raw.splitlines():
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict) and isinstance(event.get("session_id"), str):
                return event["session_id"]
    return None


def amp_thread_state(thread: str, cwd: Path, env: dict[str, str], timeout: float = 60) -> tuple[str, str | None]:
    """What the Amp server says about a run's thread, which keeps running when the CLI is
    silent or gone: `working` while a model turn is in flight, `answered` with the final
    text once a turn ended without a tool call, `stopped` after a turn that ended asking
    for a tool, which only the CLI runs, or `unknown` when the thread cannot be read."""
    if timeout <= 0:
        return "unknown", None
    try:
        result = subprocess.run(
            ["amp", "threads", "export", thread], cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
        export = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "unknown", None
    messages = export.get("messages") if isinstance(export, dict) else None
    last = messages[-1] if isinstance(messages, list) and messages else None
    if not isinstance(last, dict):
        return "unknown", None
    state = last.get("state")
    if last.get("role") != "assistant" or state is None:
        return "working", None
    if not isinstance(state, dict):
        return "unknown", None
    if state.get("type") != "complete":
        return "working", None
    content = last.get("content")
    texts = [
        b["text"] for b in (content if isinstance(content, list) else []) if isinstance(b, dict)
        and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]  # fmt: skip
    text = "\n\n".join(texts)
    if state.get("stopReason") == "end_turn" and text.strip():
        return "answered", text
    return "stopped", None


# How many idle periods an `amp` run may stay silent while its turn is still in flight on
# the server, beyond the first: a model thinking at length prints nothing until it answers,
# while a request that never returns would otherwise hold the run until the total timeout.
AMP_IDLE_EXTENSIONS = 2


class AmpWatch:
    """Consults the server when an `amp` run goes silent. A turn still in flight earns more
    time; a turn the server finished while the CLI printed nothing is taken as the answer."""

    def __init__(self, cwd: Path, env: dict[str, str], log: Path) -> None:
        self.cwd, self.env, self.log = cwd, env, log
        self.extensions = 0
        self.thread: str | None = None
        self.answer: str | None = None

    def note(self, message: str) -> None:
        with self.log.open("a") as out:
            out.write(f"\n[{message}]\n")

    def on_idle(self, output: str, remaining: float) -> bool:
        self.thread = self.thread or amp_session(output)
        if self.thread is None:
            return False
        # The check must not carry the run past its total deadline.
        state, text = amp_thread_state(self.thread, self.cwd, self.env, timeout=min(60.0, remaining))
        if state == "answered":
            self.answer = text
            self.note(f"the server finished {self.thread} while the CLI was silent; taking its answer")
            return False
        if state == "working" and self.extensions < AMP_IDLE_EXTENSIONS:
            self.extensions += 1
            self.note(f"silent, but {self.thread} is still working on the server; waiting another idle period")
            return True
        return False


def attempt(
    reviewer: Reviewer, pristine: Path, instruction: str, total: float, idle: float, log: Path
) -> tuple[int, str, bytes]:
    """Run one reviewer on a fresh copy of `pristine`, appending its raw output to `log`.

    Returns the exit code, the output, and a Git patch of the files the reviewer changed."""
    snap = pristine.parent / "snapshot"
    remove(snap)
    shutil.copytree(pristine, snap, symlinks=True)
    reviewer.prepare(snap)
    # The repository that records the changes lives under the state directory, outside
    # every directory a reviewer may write: `codex --sandbox workspace-write` allows /tmp.
    state_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="record-", dir=state_dir()) as record:
        return run_attempt(reviewer, snap, Path(record), instruction, total, idle, log)


def run_attempt(
    reviewer: Reviewer, snap: Path, record: Path, instruction: str, total: float, idle: float, log: Path
) -> tuple[int, str, bytes]:
    changes = Changes(record / "changes.git", snap, (*HANDOFF_FILES, *reviewer.owned_paths))
    before = changes.tree()
    with log.open("a") as header:
        header.write(f"\n=== {reviewer.label} at {dt.datetime.now().astimezone():%H:%M:%S %Z}: {instruction[:60]}\n")
    with tempfile.TemporaryDirectory(prefix="xagents-msg-") as tmp:
        last_message = Path(tmp) / "last-message.md"
        env = reviewer.env(snap)
        watch = AmpWatch(snap, env, log) if reviewer.backend == "amp" else None
        code, output = run_with_timeouts(
            reviewer.command(snap, instruction, last_message),
            snap,
            total,
            reviewer.idle_timeout(idle) if reviewer.idle_timeout_applies else None,
            env,
            log,
            on_idle=watch.on_idle if watch else None,
        )
        if watch is not None and watch.answer is not None:
            code, output = 0, watch.answer
        elif reviewer.backend == "amp":
            output = amp_output(code, output)
        elif reviewer.backend == "vibe":
            output = vibe_output(code, output)
        if code == 0 and last_message.is_file() and last_message.read_text().strip():
            output = last_message.read_text()
    try:
        return code, output, changes.diff(before, changes.tree())
    except UsageError as error:
        # The review is still useful; the reader learns why its changes are missing.
        return code, f"{output.rstrip()}\n\nThe wrapper could not record the reviewer's file changes: {error}\n", b""


# The preflight reply: the last line of the output, so a CLI that only echoes the prompt
# does not pass.
PONG = re.compile(r"\W*PONG\W*", re.IGNORECASE)


def preflight(reviewer: Reviewer, pristine: Path, log: Path, timeout: float = 90, attempts: int = 2) -> bool:
    """A short call that catches missing auth or exhausted quota, which can otherwise hang."""
    for number in range(1, attempts + 1):
        code, output, _ = attempt(reviewer, pristine, "Reply with exactly: PONG", timeout, timeout, log)
        lines = [line for line in output.splitlines() if line.strip()]
        if code == 0 and lines and PONG.fullmatch(lines[-1].strip()):
            return True
        tail = " ".join(output.split())[-300:]
        print(f"{reviewer.label}: preflight {number}/{attempts} failed (exit {code}): {tail}", file=sys.stderr)
        if number < attempts:
            time.sleep(10)
    return False


def amp_credit_problem(reviewer: Reviewer, cwd: Path, log: Path, timeout: float = 120) -> str | None:
    """Why an `amp` reviewer may not run under EXTERNAL_AGENTS_AMP_NO_CREDITS, or None.

    `check-access` runs a small inference and reports what served it: a connection linked
    to the user's Amp account (`model-provider`) or Amp itself, on credits."""
    command = ["amp", "config", "model-providers", "check-access", "--provider-model", str(reviewer.model)]
    if reviewer.effort:
        command += ["--reasoning-effort", reviewer.effort]
    with log.open("a") as out:
        out.write(f"\n=== {reviewer.label}: model routing check\n")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=agent_env(cwd, "amp"),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        partial = b"".join(p if isinstance(p, bytes) else p.encode() for p in (error.stdout, error.stderr) if p)
        with log.open("a") as out:
            out.write(partial.decode(errors="replace"))
        problem = f"its model routing check timed out after {timeout:.0f} s"
    except OSError as error:
        problem = f"its model routing check could not run: {error}"
    else:
        output = result.stdout + result.stderr
        with log.open("a") as out:
            out.write(f"{output.rstrip()}\n(exit {result.returncode})\n")
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            report = None
        ok = result.returncode == 0 and isinstance(report, dict) and report.get("ok") is True
        routing = report.get("modelRouting") if isinstance(report, dict) else None
        if not isinstance(routing, dict):
            routing = {}
        kind = routing.get("kind")
        if ok and kind == "model-provider":
            return None
        if ok and kind == "upstream-model-provider":
            name = routing.get("modelProviderName") or routing.get("modelProviderID") or "Amp"
            problem = (
                f"Amp credits would pay for {reviewer.model} (served by {name}), and {ENV_AMP_NO_CREDITS} forbids them"
            )
        else:
            tail = " ".join(output.split())[-300:]
            problem = f"its model routing check failed (exit {result.returncode}, routing {kind or 'unknown'}): {tail}"
    with log.open("a") as out:
        out.write(f"{problem}\n")
    return problem


@dataclass(frozen=True)
class Options:
    total: float
    idle: float
    preflight: bool
    expect: re.Pattern[str] | None


def review_with_fallback(
    reviewers: list[Reviewer], pristine: Path, options: Options, run_log: Path
) -> tuple[Reviewer | None, str, bytes]:
    """The first reviewer that produces a review, its output, and its file changes.

    When none does, the log names each failure and keeps every incomplete output, so a
    real review that the completeness check missed is not lost."""
    log = []
    for reviewer in reviewers:
        # The ledger is read and written under the lock, so a slot or review that waited
        # for it sees a limit the run before it just recorded.
        with free_tier_lock(reviewer):
            if reviewer.free and limited_today(reviewer):
                log.append(f"{reviewer.label}: skipped (limited earlier today)")
                print(f"{reviewer.label}: limited earlier today; trying the next reviewer.", file=sys.stderr)
                continue
            # A billing rule, not a health check, so --no-preflight does not skip it.
            if (
                reviewer.backend == "amp"
                and os.environ.get(ENV_AMP_NO_CREDITS) == "1"
                and (problem := amp_credit_problem(reviewer, pristine.parent, run_log))
            ):
                log.append(f"{reviewer.label}: unavailable ({problem})")
                print(f"{reviewer.label}: unavailable ({problem}); trying the next reviewer.", file=sys.stderr)
                continue
            if options.preflight and not preflight(reviewer, pristine, run_log):
                log.append(f"{reviewer.label}: preflight failed")
                continue
            code, output, changes = attempt(reviewer, pristine, INSTRUCTION, options.total, options.idle, run_log)
            state = classify(code, output, options.expect)
            if state == "stalled" and not reviewer.free:
                # A paid model's request can hang without failing, and a model can think
                # longer than the idle timeout: the fresh run may stay silent twice as long.
                # Free tiers stall from throttling, so they are not retried.
                print(f"{reviewer.label}: stalled (exit {code}); retrying once.", file=sys.stderr)
                code, output, changes = attempt(
                    reviewer, pristine, INSTRUCTION, options.total, 2 * options.idle, run_log
                )
                state = classify(code, output, options.expect)
            if reviewer.free and state in ("stalled", "unavailable"):
                # Free tiers rarely say "limit": a stall or a failure stops spending runs on
                # the model for the day.
                state = f"{state}, likely a free-tier limit"
                mark_limited(reviewer, state)
        if state == "ok":
            return reviewer, output, changes
        entry = f"{reviewer.label}: {state} (exit {code})"
        if state == "incomplete":
            entry += "\n--- incomplete output ---\n" + output.strip()
        elif tail := output.strip()[-FAILURE_TAIL:]:
            # Keep the output: it is the only diagnostic, and it may hold a review that
            # was misclassified.
            entry += "\n" + "\n".join(f"  {line}" for line in tail.splitlines())
        log.append(entry)
        # A later reviewer's success discards the log, so the diagnostic goes to stderr too.
        print(f"{entry}\ntrying the next reviewer.", file=sys.stderr)
    return None, "\n".join(log) or "no reviewer backend is installed or configured", b""


def review_slot(
    slot: Slot, args: argparse.Namespace, source: Path, files: dict[str, str], exclude: list[Path], run_log: Path
) -> tuple[Reviewer | None, str, bytes]:
    """Run one slot on its own snapshots, since backends and reviewers write into them.

    Every attempt starts from the same pristine copy, so a failed reviewer's changes do
    not reach the next one. Every run's raw output goes to `run_log` as it arrives. A
    failure of the wrapper itself ends only this slot."""
    if not slot.reviewers:
        return None, "no backend is installed or configured", b""
    options = Options(args.timeout, args.idle_timeout, not args.no_preflight, args.expect)
    try:
        with tempfile.TemporaryDirectory(prefix="xagents-review-") as tmp:
            pristine = Path(tmp) / "pristine"
            snapshot(source, pristine, args.include, exclude)
            for name, content in files.items():
                write_generated(pristine / name, content)
            return review_with_fallback(list(slot.reviewers), pristine, options, run_log)
    except Exception as error:
        # Any wrapper failure, a bug included: the other slots' reviews are still written,
        # and the traceback goes to this slot's log.
        with run_log.open("a") as out:
            out.write(f"\nwrapper error:\n{traceback.format_exc()}")
        return None, f"the wrapper failed while running this slot: {error}", b""


def output_paths(args: argparse.Namespace, count: int) -> list[Path]:
    if args.output is not None:
        if args.output.suffix in (".patch", ".log"):
            raise UsageError(f"--output must not end in {args.output.suffix}; the wrapper writes that file")
        if count != 1:
            raise UsageError(f"the panel has {count} slots; pass --output-dir instead of --output")
        if not args.output.parent.is_dir():
            raise UsageError(f"cannot use --output {args.output}: {args.output.parent} is not a directory")
        return [args.output]
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UsageError(f"cannot use --output-dir {args.output_dir}: {error}") from error
    return [args.output_dir / f"review-{number}.md" for number in range(1, count + 1)]


def detect_reviewers() -> dict[str, object]:
    configured = os.environ.get(ENV_REVIEWERS)
    panel = resolve_panel(None, None, configured)
    named = {r.backend for slot in parse_panel(configured or "") for r in slot.reviewers}
    return {
        "configured": configured,
        "panel": [{"optional": slot.optional, "chain": [r.label for r in slot.reviewers]} for slot in panel],
        # Configured backends whose CLI is missing, each with its install script.
        "missing": {b: str(INSTALLERS[b]) for b in BACKENDS if b in named and not installed(b)},
    }


def review(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    if not args.prompt_file.is_file():
        raise UsageError(f"prompt file {args.prompt_file} not found")
    if args.diff_file and not args.diff_file.exists():
        raise UsageError(f"diff file {args.diff_file} not found")
    panel = resolve_panel(args.reviewer, args.reviewers, os.environ.get(ENV_REVIEWERS))
    if not panel:
        raise UsageError(f"no external reviewer is configured; set {ENV_REVIEWERS} or pass --reviewers")
    # Validated here, before the slots run, so a bad --include is a usage error (exit 2).
    check_includes(source, args.include)
    paths = output_paths(args, len(panel))
    # The review's own inputs and outputs stay out of what the reviewers see: the supplied
    # diff only after filtering, and no earlier review or its patch.
    exclude = [
        *([args.diff_file] if args.diff_file else []),
        *(path.with_suffix(suffix) for path in paths for suffix in (".md", ".log", ".patch")),
        *paths,
    ]
    files = {PROMPT_FILE: args.prompt_file.read_text()}
    if args.base:
        patch = base_diff(source, args.base, exclude)
    elif args.diff_file:
        patch = args.diff_file.read_bytes().decode(errors="surrogateescape")
    else:
        patch = None
    if patch is not None:
        files[DIFF_FILE], files[FILES_FILE] = filter_patch(patch)
    logs = [path.with_suffix(".log") for path in paths]
    for path, log in zip(paths, logs, strict=True):
        log.write_text("")
        # A patch left by an earlier review next to this output would look like this one's.
        path.with_suffix(".patch").unlink(missing_ok=True)
    print(f"raw reviewer output, written as it arrives: {', '.join(map(str, logs))}", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=len(panel)) as pool:
        results = list(pool.map(lambda slot, log: review_slot(slot, args, source, files, exclude, log), panel, logs))
    complete = True
    for number, (slot, path, (reviewer, output, changes)) in enumerate(
        zip(panel, paths, results, strict=True), start=1
    ):
        kind = "optional slot" if slot.optional else "slot"
        if reviewer is None:
            path.write_text(f"no reviewer available:\n{output}\n")
            print(f"{kind} {number}: no reviewer available; see {path}", file=sys.stderr)
            complete = complete and slot.optional
            continue
        text = f"reviewer: {reviewer.label}\n{output}"
        if changes:
            patch_path = path.with_suffix(".patch")
            patch_path.write_bytes(changes)
            text = f"{text.rstrip()}\n\nproposed changes: {patch_path.name}\n"
            print(f"{kind} {number}: {reviewer.label} proposed changes in {patch_path}", file=sys.stderr)
        path.write_text(text)
        print(f"{kind} {number}: review by {reviewer.label} written to {path}", file=sys.stderr)
    if not complete or all(reviewer is None for reviewer, _, _ in results):
        print("A required external reviewer is missing; record the reduced panel.", file=sys.stderr)
        return 3
    return 0


def expect_pattern(text: str) -> re.Pattern[str]:
    try:
        return re.compile(text)
    except re.error as error:
        raise argparse.ArgumentTypeError(f"not a valid regex: {error}") from error


def add_parser(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser("review", help="Run the reviewer panel on a snapshot of a Git checkout")
    run.add_argument("--source", type=Path, default=Path("."), help="Checkout to snapshot")
    run.add_argument("--prompt-file", type=Path, required=True)
    outputs = run.add_mutually_exclusive_group(required=True)
    outputs.add_argument("--output", type=Path, help="Review file, for a panel of one slot")
    outputs.add_argument("--output-dir", type=Path, help="Directory for one review-<slot>.md per slot")
    scope = run.add_mutually_exclusive_group()
    scope.add_argument("--base", help=f"Write the diff from this Git revision to {DIFF_FILE}")
    scope.add_argument("--diff-file", type=Path, help=f"Use a precomputed diff as {DIFF_FILE}")
    run.add_argument("--include", type=Path, action="append", default=[], help="Add an ignored file, such as a plan")
    chosen = run.add_mutually_exclusive_group()
    chosen.add_argument(
        "--reviewer",
        action="append",
        help=f"backend[:model] to try, repeatable in fallback order, as one slot; overrides {ENV_REVIEWERS}",
    )
    chosen.add_argument("--reviewers", help=f"A whole panel in {ENV_REVIEWERS} syntax; overrides it")
    run.add_argument("--expect", type=expect_pattern, help="Regex a usable review must match, such as a verdict line")
    run.add_argument("--timeout", type=float, default=1800)
    run.add_argument(
        "--idle-timeout",
        type=float,
        default=300,
        help="Seconds of silence before a reviewer counts as stalled, up to effort medium; "
        "high doubles it, xhigh and max triple it",
    )
    run.add_argument("--no-preflight", action="store_true")
