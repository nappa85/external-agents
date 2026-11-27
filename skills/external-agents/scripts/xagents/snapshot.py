"""Sanitized repository snapshots, filtered diffs, and patches of what an agent changed."""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .common import UsageError, is_secret, remove, write_generated


def git(source: Path, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None) -> str:
    """Run git in `source`. The output keeps its line endings, and bytes that are not UTF-8,
    in file names or contents, survive as surrogates."""
    result = subprocess.run(["git", "-C", str(source), *args], input=stdin, capture_output=True, env=env)
    if result.returncode != 0:
        raise UsageError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode(errors="surrogateescape")


def source_files(source: Path) -> list[Path]:
    """Tracked plus untracked non-ignored files."""
    listing = git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({Path(entry) for entry in listing.split("\0") if entry})


class Exclusions:
    """Files and directories to leave out of a review's inputs, such as its own output."""

    def __init__(self, paths: Iterable[Path]) -> None:
        self.paths = {Path(os.path.realpath(path)) for path in paths}

    def __contains__(self, path: Path) -> bool:
        real = Path(os.path.realpath(path))
        # `.name.*` is a temporary copy that `replace_atomically` renames over `name`.
        return (
            real in self.paths
            or any(parent in self.paths for parent in real.parents)
            or any(p.parent == real.parent and real.name.startswith(f".{p.name}.") for p in self.paths)
        )


def copy_entry(source: Path, dest: Path, relative: Path) -> None:
    origin = source / relative
    target = dest / relative
    if not origin.is_symlink() and not origin.is_file():
        return  # deleted in the working tree but still in the index
    root = source.resolve()
    if Path(os.path.realpath(origin.parent)) != root / relative.parent:
        # A directory in the index replaced by a link: Git still lists its old paths, which
        # now name other files, outside the checkout or in its VCS metadata.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if origin.is_symlink():
        # Keep only relative links to non-secret files in the tree; an absolute link
        # would still point into the checkout.
        link = os.readlink(origin)
        resolved = (origin.parent / link).resolve()
        if not os.path.isabs(link) and resolved.is_relative_to(root) and not is_secret(resolved.relative_to(root)):
            os.symlink(link, target)
        return
    # A file the review itself writes, such as its status, may be renamed away meanwhile.
    with contextlib.suppress(FileNotFoundError):
        shutil.copy2(origin, target)


def check_includes(source: Path, extra: list[Path]) -> list[Path]:
    """The `--include` paths relative to `source`; a usage error for any outside it, secret-like,
    or not a file."""
    root = source.resolve()
    relatives = []
    for path in extra:
        resolved = (path if path.is_absolute() else source / path).resolve()
        if not resolved.is_relative_to(root):
            raise UsageError(f"--include {path} is outside the source tree")
        relative = resolved.relative_to(root)
        if is_secret(relative):
            raise UsageError(f"refusing to include secret-like file {relative}")
        if not (source / relative).is_file():
            raise UsageError(f"--include {relative} is not a file in the source tree")
        relatives.append(relative)
    return relatives


def snapshot(source: Path, dest: Path, extra: list[Path], exclude: Iterable[Path] = ()) -> None:
    """Copy the reviewable files of `source` into a new `dest`, plus the `extra` ignored
    files, without the `exclude` paths."""
    includes = check_includes(source, extra)
    dest.mkdir(parents=True)
    excluded = Exclusions(exclude)
    for relative in source_files(source):
        if not is_secret(relative) and source / relative not in excluded:
            copy_entry(source, dest, relative)
    for relative in includes:
        if source / relative not in excluded:
            copy_entry(source, dest, relative)
    leftovers = [p for p in dest.rglob("*") if is_secret(p.relative_to(dest)) and not p.is_dir()]
    if leftovers:
        raise UsageError(f"secret-like file survived the snapshot: {leftovers[0]}")


# A path Git quotes as a C-style string because it holds special or non-ASCII bytes.
QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def unquote(token: str) -> str:
    """A path token as Git writes it, quoted or not, as a path. A quoted token with an
    invalid escape raises ValueError."""
    if QUOTED.fullmatch(token):
        raw = codecs.escape_decode(token[1:-1].encode(errors="surrogateescape"))[0]
        return raw.decode(errors="surrogateescape")
    return token


def path_token(token: str, prefix: str) -> str | None:
    if token.startswith('"'):
        if not QUOTED.fullmatch(token):
            return None
        token = unquote(token)
    elif '"' in token:
        return None
    return token[len(prefix) :] if token.startswith(prefix) else None


def header_candidates(line: str) -> list[tuple[str, str]]:
    """Every reading of a `diff --git a/OLD b/NEW` line. Either path may be quoted, and an
    unquoted path may itself contain ` b/`, so a header can have several readings; none
    means the header cannot be parsed."""
    rest = line.removeprefix("diff --git ")
    readings = []
    for index, char in enumerate(rest):
        if char == " ":
            old, new = path_token(rest[:index], "a/"), path_token(rest[index + 1 :], "b/")
            if old is not None and new is not None:
                readings.append((old, new))
    return readings


def plain_path(line: str, prefix: str) -> str | None:
    """The path of a `--- ` or `+++ ` line, or None for /dev/null."""
    token = line[4:]
    token = unquote(match.group()) if (match := QUOTED.match(token)) else token.split("\t", 1)[0]
    if token == "/dev/null":
        return None
    return token.removeprefix(prefix)


HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """The patch's preamble, then one section per file: `git` for a `diff --git` header,
    `plain` for any other `diff` line or a bare `---`/`+++` pair, as `diff -u` writes.

    A hunk's body is consumed by the line counts in its `@@` header, so a removed `-- x` or
    added `++ y` line is never taken for a file header. A hunk that runs past its counts
    stays with its file: a misread then drops lines rather than detaching them. Text after a
    file's hunks that starts no hunk and no file goes to an `unknown` section, which is
    dropped: nothing reaches the reviewer unless it is attributed to a file."""
    sections: list[tuple[str, list[str]]] = [("preamble", [])]
    old = new = 0  # lines of the current hunk still to come
    had_hunk = False  # whether the current section has reached its hunks
    for index, line in enumerate(lines):
        if old > 0 or new > 0:
            sections[-1][1].append(line)
            if line.startswith(" "):
                old, new = old - 1, new - 1
            elif line.startswith("-"):
                old -= 1
            elif line.startswith("+"):
                new -= 1
            elif not line.startswith("\\"):
                old = new = 0  # not a hunk line: the counts were wrong
            continue
        bare_pair = (
            line.startswith("--- ")
            and index + 2 < len(lines)
            and lines[index + 1].startswith("+++ ")
            and HUNK_HEADER.match(lines[index + 2]) is not None
        )
        if line.startswith("diff "):
            sections.append(("git" if line.startswith("diff --git ") else "plain", [line]))
            had_hunk = False
        elif bare_pair and (sections[-1][0] != "git" or had_hunk):
            # A git section's own `---`/`+++` lines come before its first hunk.
            sections.append(("plain", [line]))
            had_hunk = False
        elif had_hunk and not HUNK_HEADER.match(line) and not line.startswith("\\"):
            sections.append(("unknown", [line]))
            had_hunk = False
        else:
            sections[-1][1].append(line)
            if match := HUNK_HEADER.match(line):
                had_hunk = True
                old, new = (int(count) if count is not None else 1 for count in match.groups())
    return sections


# Lines that would carry file content in a diff format the filter does not parse, such as
# `diff -c` or an `Index:` header: a preamble that holds one is dropped.
FOREIGN_HEADERS = ("*** ", "--- ", "+++ ", "@@", "Index: ", "==== ")


def judge(kind: str, lines: list[str]) -> tuple[bool, str | None]:
    """Whether to keep a section, and its `STATUS<TAB>path` manifest line. A section is
    kept only when every path it names is known and none is secret-like: fail closed."""
    if kind == "preamble":
        # A commit message before the first file, as `git format-patch` writes, is kept.
        return not any(line.startswith(FOREIGN_HEADERS) for line in lines), None
    if kind == "unknown":
        return False, None
    try:
        return judge_file(kind, lines)
    except ValueError:  # a quoted path with an invalid escape
        return False, None


def judge_file(kind: str, lines: list[str]) -> tuple[bool, str | None]:
    header = [line.rstrip("\r\n") for line in lines]
    if "@@" in (line[:2] for line in header):
        header = header[: [line[:2] for line in header].index("@@")]
    paths: list[str] = []
    name, status = None, "M"
    if kind == "git":
        readings = header_candidates(header[0])
        if not readings:
            return False, None
        paths += [path for reading in readings for path in reading]
        name = next((new for old, new in readings if old == new), readings[-1][1])
    for line in header[1:] if kind == "git" else header:
        value = line.partition(" ")[2]
        if line.startswith("new file mode"):
            status = "A"
        elif line.startswith("deleted file mode"):
            status = "D"
        elif line.startswith(("rename from ", "copy from ")):
            status = "R" if line.startswith("rename") else status
            paths.append(unquote(value.partition(" ")[2]))
        elif line.startswith(("rename to ", "copy to ")):
            name = unquote(value.partition(" ")[2])
            paths.append(name)
        elif line.startswith(("--- ", "+++ ")):
            path = plain_path(line, "a/" if line.startswith("-") else "b/")
            if path is not None:
                paths.append(path)
                name = name if kind == "git" else path
            elif kind == "plain":
                status = "A" if line.startswith("-") else "D"
    keep = bool(paths) and name is not None and not any(is_secret(Path(path)) for path in paths)
    return keep, f"{status}\t{name}" if keep else None


def filter_patch(patch: str) -> tuple[str, str]:
    """Drop secret-like files from a patch and list the remaining paths.

    Accepts Git patches and plain unified diffs, with any line endings. Returns the
    filtered patch and a `STATUS<TAB>path` manifest."""
    lines = [line for line in re.split(r"(?<=\n)", patch) if line]
    kept: list[str] = []
    names: list[str] = []
    for kind, section in split_sections(lines):
        keep, name = judge(kind, section)
        if keep:
            kept += section
        if name:
            names.append(name)
    return "".join(kept), "".join(f"{name}\n" for name in names)


# Override the user's diff configuration: `diff.noprefix` or `diff.mnemonicPrefix` would make
# every header unparsable, so `filter_patch` would drop every file, and `diff.external` or a
# textconv driver would replace the patch. `--relative` gives paths relative to the source
# directory, as the snapshot holds them.
DIFF_OPTIONS = (
    "--no-color", "--no-ext-diff", "--no-textconv", "--relative", "--src-prefix=a/", "--dst-prefix=b/",
)  # fmt: skip


def base_diff(source: Path, base: str, exclude: Iterable[Path] = ()) -> str:
    """Diff from `base` to the working tree, including untracked non-ignored files, without
    the `exclude` paths.

    Untracked files enter the diff as intent-to-add entries of a temporary copy of the
    index, so one `git diff` covers everything and the repository's index is untouched."""
    excluded = Exclusions(exclude)
    listing = git(source, "ls-files", "-z", "--others", "--exclude-standard")
    # An entry ending in `/` is a nested repository Git does not descend into.
    untracked = [
        entry for entry in listing.split("\0") if entry and not entry.endswith("/") and source / entry not in excluded
    ]
    # Excluded files the repository tracks, such as a committed review, leave the diff too.
    root = source.resolve()
    pathspecs = [
        f":(exclude,literal){path.relative_to(root).as_posix()}"
        for path in excluded.paths
        if path.is_relative_to(root) and path != root
    ]
    index = Path(git(source, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    with tempfile.TemporaryDirectory(prefix="xagents-index-") as tmp:
        temporary = Path(tmp, "index")
        if index.is_file():
            shutil.copyfile(index, temporary)
        env = {**os.environ, "GIT_INDEX_FILE": str(temporary)}
        if untracked:
            names = "\0".join(f":(literal){entry}" for entry in untracked).encode(errors="surrogateescape")
            git(source, "add", "--intent-to-add", "--pathspec-from-file=-", "--pathspec-file-nul", stdin=names, env=env)
        # Whole changed functions, so a reviewer opens fewer files to read the code around a change.
        return git(source, "diff", *DIFF_OPTIONS, "--function-context", base, "--", ".", *pathspecs, env=env)


# Unset every attribute that converts content between the work tree and Git.
RAW_ATTRIBUTES = "* -text !eol -filter -ident !working-tree-encoding\n"


class Changes:
    """Records snapshot trees in a private Git repository to diff an agent's changes.

    Wrapper-owned paths and secret-like files are left out, so the patch applies to the
    original checkout. Git runs without user or system configuration, and repository
    attributes override the tree's own, so no filter, driver, or line-ending conversion
    alters the recorded bytes. The repository's configuration is rewritten before every
    command: an agent that can write next to its snapshot could otherwise make the
    wrapper's own git run commands, through `core.fsmonitor` or a filter driver."""

    def __init__(self, git_dir: Path, work_tree: Path, owned: tuple[Path, ...]) -> None:
        remove(git_dir)
        self.git_dir = git_dir
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(work_tree),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        self.work_tree = work_tree
        self.owned = owned
        # Paths in the first recorded tree: an ignored file among them, such as an included
        # plan, is the reviewer's to change; one created later is build output.
        self.baseline: set[bytes] | None = None
        self.run("init", "-q")
        self.config = (git_dir / "config").read_text()

    def reset_configuration(self) -> None:
        if self.git_dir.is_symlink() or not self.git_dir.is_dir():
            raise UsageError(f"cannot record the agent's changes: {self.git_dir} was replaced")
        write_generated(self.git_dir / "config", self.config)
        for name in ("info", "hooks", "commondir", "config.worktree"):
            remove(self.git_dir / name)
        (self.git_dir / "info").mkdir()
        (self.git_dir / "info" / "attributes").write_text(RAW_ATTRIBUTES)

    def run(self, *args: str, stdin: bytes | None = None) -> bytes:
        # No hook runs, wherever one was planted: `git add` would run post-index-change.
        command = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
        result = subprocess.run(command, cwd=self.work_tree, env=self.env, input=stdin, capture_output=True)
        if result.returncode != 0:
            raise UsageError(f"cannot record the agent's changes: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def git(self, *args: str, stdin: bytes | None = None) -> bytes:
        self.reset_configuration()
        return self.run(*args, stdin=stdin)

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
        if self.baseline is None:
            self.baseline = set(entries)
        else:
            # Files the tree's own ignore rules match, such as test caches and build output.
            listing = self.git("ls-files", "-z", "--cached", "--ignored", "--exclude-standard")
            owned += [entry for entry in listing.split(b"\0") if entry and entry not in self.baseline]
        if owned:
            self.git("update-index", "--force-remove", "-z", "--stdin", stdin=b"\0".join(owned) + b"\0")
        return self.git("write-tree").decode().strip()

    def diff(self, before: str, after: str) -> bytes:
        return self.git("diff", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", before, after)
