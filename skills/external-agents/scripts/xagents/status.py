"""The progress of a `review` run, for a coordinator that runs it in the background.

A run writes a status file next to its reviews: `review-status.json` in `--output-dir`,
or `<name>.status.json` next to `--output`. It names each slot's outcome as the slot
finishes, and the run's exit code at the end. For its whole life the run holds a lock on
its first slot's log, so `review-status` tells a run that is still going from one that was
killed before it could record its end.

`review --background` starts the run detached, with its own output in `review-run.log`
(`<name>.run.log`), and returns once the run has written its status. `review-status`
prints that status; with `--wait`, it blocks until the run ends.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from .common import UsageError, replace_atomically

RUNNING, FINISHED = "running", "finished"
# `review-status`: the run is still going; no run, or it ended without recording its end.
EXIT_RUNNING = 1
EXIT_NO_RUN = 4
POLL = 1.0
# Passed to the detached run with an ID that `--background` chose for it, as a hidden
# argument rather than a variable a shell could leave behind. The run records the ID in its
# status, so `--background` never takes an earlier run's status, even one whose pid the
# system reused, for this run's; and the run does not detach again.
# No prefix of it a user would type, such as `--d` for `--diff-file`, becomes ambiguous.
DETACHED_RUN = "--xagents-detached-run"
# The exit codes `review` returns only after it recorded its run and ran its slots.
RAN = (0, 3)


def status_path(output: Path | None, output_dir: Path | None) -> Path:
    return output.with_suffix(".status.json") if output is not None else Path(output_dir or ".") / "review-status.json"


def run_log_path(output: Path | None, output_dir: Path | None) -> Path:
    return output.with_suffix(".run.log") if output is not None else Path(output_dir or ".") / "review-run.log"


def lock_path(output: Path | None, output_dir: Path | None) -> Path:
    """The first slot's log, which exists for as long as the run."""
    return output.with_suffix(".log") if output is not None else Path(output_dir or ".") / "review-1.log"


def run_files(reviews: list[Path]) -> list[Path]:
    """The status and run log files a run with these review files may find next to them,
    under either layout, so a reviewer sees no earlier run's status either."""
    files = []
    for review in reviews:
        files += [review.with_suffix(".status.json"), review.with_suffix(".run.log")]
        files += [status_path(None, review.parent), run_log_path(None, review.parent)]
    return list(dict.fromkeys(files))


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _try_lock(fd: int, mode: int = fcntl.LOCK_EX) -> bool:
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def hold(lock: Path) -> Iterator[None]:
    """Hold the run's lock, or refuse a second run into the same outputs."""
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # A `review-status` that is checking the lock holds it for a moment.
        for _ in range(20):
            if _try_lock(fd):
                break
            time.sleep(0.05)
        else:
            raise UsageError(f"a review is already running with these outputs; it holds {lock}")
        yield
    finally:
        os.close(fd)


def alive(lock: Path) -> bool:
    """Whether a run holds `lock`."""
    try:
        fd = os.open(lock, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        # Shared: probes must not see each other as a run.
        return not _try_lock(fd, fcntl.LOCK_SH)
    finally:
        os.close(fd)


def read(path: Path) -> dict | None:
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    # Anything else is not a status this script wrote.
    if not isinstance(status, dict):
        return None
    slots = status.get("slots", [])
    if not isinstance(slots, list) or not all(isinstance(slot, dict) for slot in slots):
        return None
    for slot in slots:
        for field in ("chain", "reasons"):
            value = slot.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return None
    return status


class Tracker:
    """Writes a running review's status file; slots update it from their own threads."""

    def __init__(self, path: Path, slots: list[dict], run: str | None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.status: dict = {
            "state": RUNNING,
            "pid": os.getpid(),
            "run": run,
            "started": _now(),
            "slots": [{"slot": number, **slot, "state": RUNNING} for number, slot in enumerate(slots, start=1)],
        }
        self._write()

    def _write(self) -> None:
        replace_atomically(self.path, json.dumps(self.status, indent=2) + "\n")

    def slot(self, number: int, **fields: object) -> None:
        with self.lock:
            self.status["slots"][number - 1].update(fields)
            self._write()

    def finish(self, code: int) -> None:
        with self.lock:
            self.status.update(state=FINISHED, finished=_now(), exit=code)
            self._write()


def describe(status: dict, ended: bool = False) -> list[str]:
    """One line for the run, then one per slot."""
    if status.get("state") == FINISHED:
        lines = [f"review finished at {status.get('finished')}, exit {status.get('exit')}"]
    elif ended:
        lines = ["review ended without finishing: it was killed or crashed; see its run log and each slot's log"]
    else:
        lines = [f"review running since {status.get('started')}, pid {status.get('pid')}"]
    for slot in status.get("slots", []):
        kind = "optional slot" if slot.get("optional") else "slot"
        head = f"{kind} {slot.get('slot')}"
        if slot.get("state") == "written":
            patch = f"; proposed changes in {slot['patch']}" if slot.get("patch") else ""
            lines.append(f"{head}: written by {slot.get('reviewer')} to {slot.get('review')}{patch}")
        elif slot.get("state") == "missing":
            reasons = "; ".join(slot.get("reasons") or []) or "no reason recorded"
            lines.append(f"{head}: missing, no reviewer available ({reasons}); see {slot.get('review')}")
        elif ended or status.get("state") == FINISHED:
            lines.append(f"{head}: never finished; see {slot.get('log')}")
        else:
            lines.append(f"{head}: running ({', '.join(slot.get('chain') or [])}); output so far in {slot.get('log')}")
    return lines


def _missing(path: Path) -> str:
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "does not exist"
    except OSError as error:
        return f"could not be read: {error}"
    return "exists but could not be read as a status"


def review_status(args: argparse.Namespace) -> int:
    """Print a review run's status: its exit code once it finished, EXIT_RUNNING while it
    runs (after `--timeout`, with `--wait`), EXIT_NO_RUN without a run, when the run ended
    without finishing, or, with `--run`, when a later run replaced that run."""
    path = status_path(args.output, args.output_dir)
    lock = lock_path(args.output, args.output_dir)
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    try:
        while True:
            status = read(path)
            # `--background` prints `--run` only once the run's status exists, so a missing
            # status or another run's is never this run's.
            if args.run is not None and (status is None or status.get("run") != args.run):
                print(
                    f"run {args.run} was replaced by a later run into the same outputs, or its status is gone; "
                    "its reviews and logs are not its own any more",
                    file=sys.stderr,
                )
                return EXIT_NO_RUN
            if status is not None and status.get("state") == FINISHED:
                print("\n".join(describe(status)))
                code = status.get("exit")
                # bool is an int too, but never an exit code this script wrote.
                return code if isinstance(code, int) and not isinstance(code, bool) else EXIT_NO_RUN
            running = alive(lock)
            # The probe answers for the status read before it only if nothing changed
            # meanwhile, such as the run finishing or a later run taking its place.
            if read(path) != status:
                continue
            if not running:
                if status is None:
                    print(f"no review run found: {path} {_missing(path)}", file=sys.stderr)
                else:
                    print("\n".join(describe(status, ended=True)))
                return EXIT_NO_RUN
            if not args.wait or (deadline is not None and time.monotonic() >= deadline):
                # A run that holds the lock but has no status yet is still preparing its snapshot.
                print("\n".join(describe(status)) if status is not None else "review starting")
                return EXIT_RUNNING
            time.sleep(POLL if deadline is None else max(0.0, min(POLL, deadline - time.monotonic())))
    except OSError as error:
        print(f"cannot follow the review run in {path.parent}: {error}", file=sys.stderr)
        return EXIT_NO_RUN


def background(script: Path, argv: list[str], output: Path | None, output_dir: Path | None) -> int:
    """Start `review` detached and return 0 once it has recorded its run, or once it ran
    its slots even if a later run already replaced its status; otherwise, such as on a usage
    error, return its exit code."""
    lock = lock_path(output, output_dir)
    try:
        running = alive(lock)
    except OSError as error:
        raise UsageError(f"cannot use the outputs in {lock.parent}: {error}") from error
    if running:
        raise UsageError(f"a review is already running with these outputs; it holds {lock}")
    if output is not None and not output.parent.is_dir():
        raise UsageError(f"cannot use --output {output}: {output.parent} is not a directory")
    try:
        (output_dir or Path(".")).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UsageError(f"cannot use --output-dir {output_dir}: {error}") from error
    # The run replaces an earlier run's status under its lock; one without this run's ID is
    # never taken for this run's.
    path = status_path(output, output_dir)
    run = uuid.uuid4().hex
    log = run_log_path(output, output_dir)
    # The child writes to a log of its own, a name snapshots leave out, until its status
    # shows it holds the outputs; a launch that loses a race leaves the winner's log alone.
    private = log.with_name(f".{log.name}.{run}")
    try:
        out = private.open("x")
    except OSError as error:
        raise UsageError(f"cannot write the run log {private}: {error}") from error
    with out:
        child = subprocess.Popen(
            [sys.executable, "-I", str(script), *argv, DETACHED_RUN, run],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    while True:
        # Read the exit before the status: a run that wrote its status and then ended
        # between two polls started, and its outcome is for `review-status`.
        ended = child.poll() is not None
        status = read(path)
        if ended and child.returncode in RAN and (status is None or status.get("run") != run):
            # It ran its slots, but a later run already replaced its status and reviews, so
            # `review-status` would report that run instead.
            print(
                f"review ran in the background and already ended with exit {child.returncode}; a later run into "
                f"the same outputs replaced its status and reviews, so do not run review-status for it; "
                f"its own output is in {private}"
            )
            return 0
        if status is not None and status.get("run") == run:
            os.replace(private, log)
            where = ["--output", str(output)] if output is not None else ["--output-dir", str(output_dir)]
            command = [sys.executable, "-I", str(script), "review-status", "--wait", *where, "--run", run]
            print(f"review started in the background (pid {child.pid}); its own output goes to {log}")
            print(f"wait for it with: {shlex.join(command)}")
            return 0
        if ended:
            # It ended without recording a run, such as on a usage error.
            sys.stderr.write(private.read_text())
            private.unlink(missing_ok=True)
            return child.returncode
        time.sleep(0.1)


def run_id(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("must not be empty; pass the ID --background printed")
    return text


def add_parser(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser(
        "review-status", help="Print a review run's outcome per slot; --wait blocks until the run ends"
    )
    outputs = run.add_mutually_exclusive_group(required=True)
    outputs.add_argument("--output", type=Path, help="The run's --output")
    outputs.add_argument("--output-dir", type=Path, help="The run's --output-dir")
    run.add_argument("--wait", action="store_true", help="Block until the run ends")
    run.add_argument(
        "--run", type=run_id, help="The run ID that --background printed; a later run into the same outputs exits 4"
    )
    run.add_argument("--timeout", type=float, help="With --wait, give up after this many seconds (exit 1)")
