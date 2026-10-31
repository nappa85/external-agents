"""Sanitized repository snapshots, filtered diffs, and patches of what an agent changed."""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from .common import UsageError, is_secret, remove


def git(source: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise UsageError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def source_files(source: Path) -> list[Path]:
    """Tracked plus untracked non-ignored files."""
    listing = git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({Path(entry) for entry in listing.split("\0") if entry})


def copy_entry(source: Path, dest: Path, relative: Path) -> None:
    origin = source / relative
    target = dest / relative
    if not origin.is_symlink() and not origin.is_file():
        return  # deleted in the working tree but still in the index
    target.parent.mkdir(parents=True, exist_ok=True)
    if origin.is_symlink():
        # Keep only relative links to non-secret files in the tree; an absolute link
        # would still point into the checkout.
        link = os.readlink(origin)
        resolved = (origin.parent / link).resolve()
        root = source.resolve()
        if not os.path.isabs(link) and resolved.is_relative_to(root) and not is_secret(resolved.relative_to(root)):
            os.symlink(link, target)
        return
    shutil.copy2(origin, target)


def snapshot(source: Path, dest: Path, extra: list[Path]) -> None:
    """Copy the reviewable files of `source` into a new `dest`, plus the `extra` ignored files."""
    dest.mkdir(parents=True)
    for relative in source_files(source):
        if not is_secret(relative):
            copy_entry(source, dest, relative)
    root = source.resolve()
    for path in extra:
        resolved = (path if path.is_absolute() else source / path).resolve()
        if not resolved.is_relative_to(root):
            raise UsageError(f"--include {path} is outside the source tree")
        relative = resolved.relative_to(root)
        if is_secret(relative):
            raise UsageError(f"refusing to include secret-like file {relative}")
        if not (source / relative).is_file():
            raise UsageError(f"--include {relative} is not a file in the source tree")
        copy_entry(source, dest, relative)
    leftovers = [p for p in dest.rglob("*") if is_secret(p.relative_to(dest)) and not p.is_dir()]
    if leftovers:
        raise UsageError(f"secret-like file survived the snapshot: {leftovers[0]}")


DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
# Git quotes paths with special or non-ASCII bytes as C-style strings.
QUOTED_DIFF_HEADER = re.compile(r'^diff --git "a/((?:[^"\\]|\\.)*)" "b/((?:[^"\\]|\\.)*)"$')


def header_paths(line: str) -> tuple[str, str] | None:
    if match := QUOTED_DIFF_HEADER.match(line):
        old, new = (codecs.escape_decode(p.encode())[0].decode(errors="replace") for p in match.groups())
        return old, new
    if '"' not in line and (match := DIFF_HEADER.match(line)):
        return match.group(1), match.group(2)
    return None


def filter_patch(patch: str) -> tuple[str, str]:
    """Drop secret-like files from a Git-format patch and list the remaining paths.

    Returns the filtered patch and a `STATUS<TAB>path` manifest."""
    kept: list[str] = []
    names: list[str] = []
    keep = True
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            paths = header_paths(line.rstrip("\n"))
            # An unparsable header is dropped with its file: fail closed.
            keep = paths is not None and not any(is_secret(Path(p)) for p in paths)
            if keep and paths:
                names.append(f"M\t{paths[1]}")
        elif keep and names:
            if line.startswith("new file mode"):
                names[-1] = "A" + names[-1][1:]
            elif line.startswith("deleted file mode"):
                names[-1] = "D" + names[-1][1:]
            elif line.startswith("rename from"):
                names[-1] = "R" + names[-1][1:]
        if keep:
            kept.append(line)
    return "".join(kept), "".join(f"{name}\n" for name in names)


def base_diff(source: Path, base: str) -> str:
    """Diff from `base` to the working tree, including untracked non-ignored files."""
    diff = git(source, "diff", "--no-color", base, "--")
    untracked = git(source, "ls-files", "-z", "--others", "--exclude-standard")
    for entry in filter(None, untracked.split("\0")):
        # `git diff --no-index` exits 1 when the files differ, which is always the case here.
        result = subprocess.run(
            ["git", "-C", str(source), "diff", "--no-color", "--no-index", "--", "/dev/null", entry],
            capture_output=True,
            text=True,
        )
        diff += result.stdout
    return diff


# Unset every attribute that converts content between the work tree and Git.
RAW_ATTRIBUTES = "* -text !eol -filter -ident !working-tree-encoding\n"


class Changes:
    """Records snapshot trees in a private Git repository to diff an agent's changes.

    Wrapper-owned paths and secret-like files are left out, so the patch applies to the
    original checkout. Git runs without user or system configuration, and repository
    attributes override the tree's own, so no filter, driver, or line-ending conversion
    alters the recorded bytes."""

    def __init__(self, git_dir: Path, work_tree: Path, owned: tuple[Path, ...]) -> None:
        remove(git_dir)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(work_tree),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        self.work_tree = work_tree
        self.owned = owned
        self.git("init", "-q")
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "attributes").write_text(RAW_ATTRIBUTES)

    def git(self, *args: str, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(["git", *args], cwd=self.work_tree, env=self.env, input=stdin, capture_output=True)
        if result.returncode != 0:
            raise UsageError(f"cannot record the agent's changes: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def is_owned(self, relative: Path) -> bool:
        return is_secret(relative) or any(relative == path or path in relative.parents for path in self.owned)

    def tree(self) -> str:
        # A repository the agent created, such as a test fixture, would otherwise be
        # recorded as a bare gitlink or fail to add; record its files instead. The walk does
        # not follow symbolic links, so nothing outside the snapshot is removed.
        for directory, subdirectories, files in os.walk(self.work_tree):
            if ".git" in subdirectories or ".git" in files:
                remove(Path(directory) / ".git")
                with contextlib.suppress(ValueError):
                    subdirectories.remove(".git")
        self.git("read-tree", "--empty")
        self.git("add", "--all", "--force", "--", ".")
        entries = [entry for entry in self.git("ls-files", "-z").split(b"\0") if entry]
        owned = [entry for entry in entries if self.is_owned(Path(os.fsdecode(entry)))]
        if owned:
            self.git("update-index", "--force-remove", "-z", "--stdin", stdin=b"\0".join(owned) + b"\0")
        return self.git("write-tree").decode().strip()

    def diff(self, before: str, after: str) -> bytes:
        return self.git("diff", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", before, after)
