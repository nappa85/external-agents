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
import sys
import tempfile
import time
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
    parse_agent,
)
from .common import ENV_REVIEWERS, UsageError, agent_env, installed, remove, run_with_timeouts, write_generated
from .snapshot import Changes, base_diff, filter_patch, snapshot

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
		tools: {{ include: ['Read', 'create_file', 'edit_file'], exclude: ['tool_search', 'code_exec'] }},
		instructions: 'You are a reviewer. You can read and change files in your working directory but cannot run commands.',
	}})
	amp.registerAgentMode({{ key: '{mode}', label: '{mode}', agent: agent.definition }})
}}
"""


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


@dataclass(frozen=True)
class Reviewer:
    backend: str
    model: str | None

    @property
    def label(self) -> str:
        return f"{self.backend}:{self.model}" if self.model else self.backend

    @property
    def idle_timeout_applies(self) -> bool:
        # `claude -p` and `amp -x` print nothing until the review is complete.
        return self.backend not in ("claude", "amp")

    @property
    def free(self) -> bool:
        """A free tier: daily limits, throttled when runs overlap."""
        return bool(self.model and self.model.endswith("-free"))

    def command(self, snap: Path, instruction: str, last_message: Path) -> list[str]:
        model = ["--model", self.model] if self.model else []
        if self.backend == "codex":
            return [
                "codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check",
                "--color", "never", "--cd", str(snap),
                "--output-last-message", str(last_message), *model, instruction,
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
                "--no-session-persistence", *model, "--", instruction,
            ]  # fmt: skip
        if self.backend == "amp":
            return [
                "amp", "--settings-file", str(snap / AMP_SETTINGS), "--mode", AMP_MODE,
                "--visibility", "private", "--no-notifications", "--no-ide", "-x", instruction,
            ]  # fmt: skip
        # opencode takes its project from `$PWD`, not the process cwd; pin it with --dir.
        return ["opencode", "run", "--dir", str(snap), "-m", str(self.model), "--agent", "reviewer", instruction]

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
            plugins = snap / AMP_PLUGINS
            plugins.mkdir(parents=True)
            model = json.dumps(self.model)
            write_generated(plugins / f"{AMP_MODE}.ts", AMP_PLUGIN.format(mode=AMP_MODE, model=model))
            write_generated(snap / AMP_SETTINGS, json.dumps(amp_settings(snap), indent=2))


def parse_reviewer(spec: str) -> Reviewer:
    return Reviewer(*parse_agent(spec, REVIEW_MODEL_REQUIRED))


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
    """`ok`, `stalled` (killed after going silent), `unavailable`, or `incomplete` (exited 0
    but without the text `expect` requires, such as a verdict line)."""
    text = output.strip()
    if returncode == 124 and text.endswith("[killed: idle timeout]"):
        return "stalled"
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
    return _ledger().get(reviewer.label, {}).get("date") == _today()


def mark_limited(reviewer: Reviewer, reason: str) -> None:
    """Skip a free model for the rest of the UTC day; free limits reset daily."""
    state_dir().mkdir(parents=True, exist_ok=True)
    ledger = _ledger()
    ledger[reviewer.label] = {"date": _today(), "reason": reason}
    (state_dir() / "limits.json").write_text(json.dumps(ledger, indent=2))


def attempt(reviewer: Reviewer, pristine: Path, instruction: str, total: float, idle: float) -> tuple[int, str, bytes]:
    """Run one reviewer on a fresh copy of `pristine`.

    Returns its exit code, its output, and a Git patch of the files it changed."""
    snap = pristine.parent / "snapshot"
    remove(snap)
    shutil.copytree(pristine, snap, symlinks=True)
    reviewer.prepare(snap)
    # The recording repository sits next to the snapshot, outside the reviewer's reach.
    changes = Changes(pristine.parent / "changes.git", snap, (*HANDOFF_FILES, *reviewer.owned_paths))
    before = changes.tree()
    with tempfile.TemporaryDirectory(prefix="xagents-msg-") as tmp:
        last_message = Path(tmp) / "last-message.md"
        code, output = run_with_timeouts(
            reviewer.command(snap, instruction, last_message),
            snap,
            total,
            idle if reviewer.idle_timeout_applies else None,
            agent_env(snap, reviewer.backend),
        )
        if code == 0 and last_message.is_file() and last_message.read_text().strip():
            output = last_message.read_text()
    try:
        return code, output, changes.diff(before, changes.tree())
    except UsageError as error:
        # The review is still useful; the reader learns why its changes are missing.
        return code, f"{output.rstrip()}\n\nThe wrapper could not record the reviewer's file changes: {error}\n", b""


def preflight(reviewer: Reviewer, pristine: Path, timeout: float = 90, attempts: int = 2) -> bool:
    """A short call that catches missing auth or exhausted quota, which can otherwise hang."""
    for number in range(1, attempts + 1):
        code, output, _ = attempt(reviewer, pristine, "Reply with exactly: PONG", timeout, timeout)
        if code == 0 and "PONG" in output:
            return True
        tail = " ".join(output.split())[-300:]
        print(f"{reviewer.label}: preflight {number}/{attempts} failed (exit {code}): {tail}", file=sys.stderr)
        if number < attempts:
            time.sleep(10)
    return False


@dataclass(frozen=True)
class Options:
    total: float
    idle: float
    preflight: bool
    expect: re.Pattern[str] | None


def review_with_fallback(
    reviewers: list[Reviewer], pristine: Path, options: Options
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
            if options.preflight and not preflight(reviewer, pristine):
                log.append(f"{reviewer.label}: preflight failed")
                continue
            code, output, changes = attempt(reviewer, pristine, INSTRUCTION, options.total, options.idle)
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
    slot: Slot, args: argparse.Namespace, source: Path, files: dict[str, str]
) -> tuple[Reviewer | None, str, bytes]:
    """Run one slot on its own snapshots, since backends and reviewers write into them.

    Every attempt starts from the same pristine copy, so a failed reviewer's changes do
    not reach the next one."""
    if not slot.reviewers:
        return None, "no backend is installed or configured", b""
    options = Options(args.timeout, args.idle_timeout, not args.no_preflight, args.expect)
    with tempfile.TemporaryDirectory(prefix="xagents-review-") as tmp:
        pristine = Path(tmp) / "pristine"
        snapshot(source, pristine, args.include)
        for name, content in files.items():
            write_generated(pristine / name, content)
        return review_with_fallback(list(slot.reviewers), pristine, options)


def output_paths(args: argparse.Namespace, count: int) -> list[Path]:
    if args.output is not None:
        if args.output.suffix == ".patch":
            raise UsageError("--output must not end in .patch; the reviewer's changes are written there")
        if count != 1:
            raise UsageError(f"the panel has {count} slots; pass --output-dir instead of --output")
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
    paths = output_paths(args, len(panel))
    files = {PROMPT_FILE: args.prompt_file.read_text()}
    patch = base_diff(source, args.base) if args.base else args.diff_file.read_text() if args.diff_file else None
    if patch is not None:
        files[DIFF_FILE], files[FILES_FILE] = filter_patch(patch)
    with ThreadPoolExecutor(max_workers=len(panel)) as pool:
        results = list(pool.map(lambda slot: review_slot(slot, args, source, files), panel))
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
    run.add_argument("--idle-timeout", type=float, default=300)
    run.add_argument("--no-preflight", action="store_true")
