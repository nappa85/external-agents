"""Run an external agent CLI as a coding agent inside bubblewrap.

Confinement:
- Files. Read-only: /usr and the system library and binary directories, a short list of
  /etc files, the agent's own installation, its login file, and whatever presets and
  `--ro` add, and the directories of the question and proxy sockets. Read-write: the
  worktree, a fresh per-run HOME, and whatever presets and `--rw` add. Nothing else of the user's home. In the worktree, untracked or
  ignored secret-like files (`.env*`, keys, ...) are masked by an empty file, nested
  `.git` and `.jj` directories by an empty tmpfs, and the worktree's own `.git` file is
  read-only, so the agent cannot point the host's next git command at a repository it
  planted. A main checkout, whose `.git` is a directory, is refused.
- Network. A new, empty network namespace. Inside, 127.0.0.1:3128 forwards to a Unix
  socket served by this process on the host: an HTTPS proxy that allows CONNECT only to
  the egress allowlist on port 443 and checks the TLS server name. Host loopback, the LAN
  and abstract sockets are not reachable.
- Process. All other namespaces are new, nested user namespaces are disabled, the
  caller's environment is replaced by an allowlist, and a seccomp filter applies (x86_64).
- CPU. Optional affinity, and nice 10. A process inside could widen its own affinity, so
  the limit is cooperative.

Mailbox: the agent runs `ask "question"`, which blocks on a Unix socket that this
process serves; the coordinator reads questions with `question [--wait]` and replies
with `answer` over a second socket the sandbox cannot see (see mailbox.py). Question
text is untrusted data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import mailbox, seccomp
from .backends import (
    CODER_CREDENTIALS,
    CODER_MODEL_REQUIRED,
    coder_command,
    coder_env,
    coder_files,
    coder_hosts,
    parse_agent,
)
from .common import ENV_CODER, UsageError, credential_env, is_secret
from .proxy import Proxy

HOME = Path.home()
# Inside the sandbox: the `ask` helper and the proxy forwarder.
TOOL_DIR = "/opt/xagents"
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64")
ETC = (
    "resolv.conf", "hosts", "nsswitch.conf", "passwd", "group", "ld.so.cache", "ld.so.conf",
    "ld.so.conf.d", "localtime", "ssl", "ca-certificates", "pki", "crypto-policies", "alternatives",
    "python-exec",
)  # fmt: skip
PROXY_PORT = 3128

FORWARDER = (
    r"""
import os, socket, sys, threading
def pipe(a, b):
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass
def serve(client):
    upstream = socket.socket(socket.AF_UNIX)
    try:
        upstream.connect("/proxy/sock")
    except OSError:
        client.close(); return
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    pipe(upstream, client)
listener = socket.create_server(("127.0.0.1", @PORT@))
if os.fork():
    listener.close()
    os.execvp(sys.argv[1], sys.argv[1:])
while True:
    client, _ = listener.accept()
    threading.Thread(target=serve, args=(client,), daemon=True).start()
"""
).replace("@PORT@", str(PROXY_PORT))

PREAMBLE = """\
You are a coding agent in a sandbox. Work only in {worktree}. Version control does not
work here: do not try to commit; the coordinator reviews and commits your changes. The
network reaches only your model provider, so downloads fail: work with what is installed.
To ask the coordinator a question, run `ask "your question"`; it waits up to an hour for
the answer. When you finish, summarize what you changed and what you verified.

Task:

"""


def fail(message: str) -> None:
    raise UsageError(message)


def is_system(path: str) -> bool:
    return any(path == top or path.startswith(top + "/") for top in SYSTEM_DIRS)


@dataclass
class Mounts:
    """What the sandbox sees beyond the system directories, in mount order."""

    # Login files relative to HOME, bound read-only into the fresh HOME.
    credentials: list[str] = field(default_factory=list)
    ro: list[str] = field(default_factory=list)
    path: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    rw: list[tuple[str, str]] = field(default_factory=list)

    def add_ro(self, path: str) -> None:
        if not is_system(path) and path not in self.ro:
            self.ro.append(path)


def check_bind(path: str) -> str:
    """An absolute path that does not expose the root or the whole home."""
    real = os.path.realpath(path)
    if not os.path.exists(real):
        fail(f"{path} does not exist")
    if real == "/" or Path(HOME).resolve().is_relative_to(real):
        fail(f"refusing to bind {path}: it contains the home directory")
    return real


def package_root(real: str) -> str:
    """The npm package directory that holds `real`, such as .../node_modules/@scope/name."""
    prefix, _, rest = real.rpartition("/node_modules/")
    parts = rest.split("/")
    name = "/".join(parts[:2]) if parts[0].startswith("@") else parts[0]
    return f"{prefix}/node_modules/{name}"


def node_prefix() -> tuple[str, str]:
    """The installation prefix and bin directory of the `node` on PATH, through any shim."""
    node = shutil.which("node")
    if node is None:
        fail("the agent is a Node.js script but node is not on PATH")
    executable = subprocess.run(
        [node, "-p", "process.execPath"], capture_output=True, text=True, check=True
    ).stdout.strip()
    real = os.path.realpath(executable)
    bindir = os.path.dirname(real)
    prefix = os.path.dirname(bindir) if os.path.basename(bindir) == "bin" else bindir
    return check_bind(prefix), bindir


def npm_closure(root: str) -> list[str]:
    """An installed npm package and its installed runtime dependencies, recursively.

    Optional dependencies matter: CLIs such as Codex ship their native binary in a
    platform package beside the launcher's. A dependency is looked up the way Node
    resolves it, from the package's own node_modules up through each enclosing one."""
    found: list[str] = []
    pending = [root]
    while pending:
        package = pending.pop()
        if package in found:
            continue
        found.append(package)
        try:
            manifest = json.loads(Path(package, "package.json").read_text())
        except (OSError, ValueError):
            continue
        names = {**manifest.get("dependencies", {}), **manifest.get("optionalDependencies", {})}
        for name in names:
            directory = Path(package)
            while True:
                candidate = directory / "node_modules" / name
                if candidate.is_dir():
                    pending.append(os.path.realpath(candidate))
                    break
                if directory.parent == directory:
                    break
                directory = directory.parent
    return found


def bind_executable(command: str, mounts: Mounts) -> str:
    """Bind an agent CLI with what it needs to start, and return its real path.

    A native binary is bound as a single file; a file in an npm package brings the
    package and its installed dependencies; a Node.js launcher also brings the Node
    installation."""
    found = shutil.which(command)
    if found is None:
        fail(f"{command} is not installed")
    real = os.path.realpath(found)
    for path in npm_closure(package_root(real)) if "/node_modules/" in real else [real]:
        mounts.add_ro(path)
    with open(real, "rb") as file:
        head = file.read(256)
    if head.startswith(b"#!"):
        words = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
        if words and os.path.basename(words[0]) == "env":
            words = [word for word in words[1:] if not word.startswith("-")]
        if not words:
            fail(f"{command} has an empty #! line")
        interpreter = os.path.basename(words[0])
        if interpreter == "node":
            prefix, bindir = node_prefix()
            mounts.add_ro(prefix)
            mounts.path.append(bindir)
        elif not is_system(os.path.realpath(shutil.which(interpreter) or words[0])):
            fail(f"{command} runs under {interpreter}, outside the system directories; bind it with --ro")
    return real


def python_for_forwarder(mounts: Mounts) -> str:
    real = os.path.realpath(sys.executable)
    if not is_system(real):
        mounts.add_ro(check_bind(os.path.realpath(sys.base_prefix)))
    return real


def preset_rust(mounts: Mounts, state: Path, cpus: int | None) -> None:
    for part in (".rustup", ".cargo/bin", ".cargo/registry", ".cargo/git"):
        if (HOME / part).exists():
            mounts.add_ro(str(HOME / part))
    target = state / "target"
    target.mkdir(exist_ok=True)
    # The sandbox binds it read-write: it must be a real directory, not a link elsewhere.
    if target.is_symlink() or not target.is_dir() or target.resolve() != target:
        fail(f"{target} must be a plain directory")
    mounts.rw.append((str(target), "/target"))
    mounts.path.append(str(HOME / ".cargo/bin"))
    mounts.env |= {"CARGO_TARGET_DIR": "/target", "CARGO_NET_OFFLINE": "true"}
    if cpus:
        mounts.env["CARGO_BUILD_JOBS"] = str(cpus)


PRESETS = {"rust": preset_rust}


def tracked_files(worktree: Path) -> set[str]:
    """Tracked paths, which are source rather than secrets; empty when git cannot tell."""
    result = subprocess.run(["git", "-C", str(worktree), "ls-files", "-z"], capture_output=True)
    if result.returncode != 0:
        return set()
    return {os.fsdecode(entry) for entry in result.stdout.split(b"\0") if entry}


def masks(worktree: Path) -> tuple[list[Path], list[Path]]:
    """Untracked secret-like files to cover with an empty file, and VCS directories to
    cover with an empty tmpfs. Every file is walked, build output included; links are
    neither followed nor masked, since their targets outside the tree are not visible."""
    tracked = tracked_files(worktree)
    files, directories = [], []
    for root, subdirectories, names in os.walk(worktree):
        for name in list(subdirectories):
            if name in (".git", ".jj"):
                directories.append(Path(root, name))
                subdirectories.remove(name)
        for name in names:
            path = Path(root, name)
            relative = path.relative_to(worktree)
            if path.is_symlink() or relative == Path(".git"):
                continue
            if is_secret(Path(name)) and relative.as_posix() not in tracked:
                files.append(path)
    return sorted(files), sorted(directories)


def bwrap_args(worktree: Path, run: Path, mounts: Mounts, hidden: tuple[list[Path], list[Path]]) -> list[str]:
    home = str(HOME)
    args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--unshare-user", "--disable-userns"]
    if (run / "seccomp.bpf").exists():
        args += ["--seccomp", "9"]
    for top in SYSTEM_DIRS:
        if os.path.islink(top):
            args += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            args += ["--ro-bind", top, top]
    for name in ETC:
        if os.path.exists(f"/etc/{name}"):
            args += ["--ro-bind", f"/etc/{name}", f"/etc/{name}"]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    args += ["--bind", str(run / "home"), home]
    for relative in mounts.credentials:
        args += ["--ro-bind", str(HOME / relative), f"{home}/{relative}"]
    for path in mounts.ro:
        args += ["--ro-bind", path, path]
    for source, dest in mounts.rw:
        args += ["--bind", source, dest]
    for name in ("ask", "ask.py", "forward.py"):
        args += ["--ro-bind", str(run / "tools" / name), f"{TOOL_DIR}/{name}"]
    # Connecting to a socket needs no write access to its mount, so the agent can use
    # these sockets but cannot replace them.
    args += ["--ro-bind", str(run / "ask"), "/ask"]
    args += ["--ro-bind", str(run / "proxy"), "/proxy"]
    args += ["--bind", str(worktree), str(worktree)]
    # After the worktree bind, so they cover what it exposes.
    secret_files, vcs_directories = hidden
    for path in secret_files:
        args += ["--ro-bind", str(run / "empty"), str(path)]
    for path in vcs_directories:
        args += ["--tmpfs", str(path)]
    if (worktree / ".git").is_file():
        args += ["--ro-bind", str(worktree / ".git"), str(worktree / ".git")]
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    env = {
        "HOME": home,
        "USER": os.environ.get("USER", "agent"),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PATH": ":".join([TOOL_DIR, *dict.fromkeys(mounts.path), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        # Node's built-in fetch ignores the proxy variables without this.
        "NODE_USE_ENV_PROXY": "1",
        **mounts.env,
    }
    args += ["--chdir", str(worktree), "--clearenv"]
    for key, value in env.items():
        args += ["--setenv", key, value]
    return args


def prepare(state: Path, backend: str | None, python: str) -> Path:
    """A fresh run directory under `state`; launcher-owned files are written before the
    sandbox exists, so the agent never had a chance to plant links in it."""
    run = state / f"run-{dt.datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
    run.mkdir()
    for sub in ("home", "ask", "proxy", "tools"):
        (run / sub).mkdir()
    for relative, content in (coder_files(backend) if backend else {}).items():
        (run / "home" / relative).parent.mkdir(parents=True, exist_ok=True)
        (run / "home" / relative).write_text(content)
    (run / "empty").touch(mode=0o444)
    (run / "tools/ask").write_text(f'#!/bin/sh\nexec {python} -I {TOOL_DIR}/ask.py "$@"\n')
    (run / "tools/ask").chmod(0o755)
    (run / "tools/ask.py").write_text(mailbox.ASK)
    (run / "tools/forward.py").write_text(FORWARDER)
    (state / "latest").unlink(missing_ok=True)
    (state / "latest").symlink_to(run.name)
    return run


def cpu_list(requested: int | None) -> list[int] | None:
    if requested is None:
        return None
    allowed = sorted(os.sched_getaffinity(0))
    if not 1 <= requested <= len(allowed):
        fail(f"--cpus must be between 1 and {len(allowed)}")
    return allowed[:requested]


# Pins and lowers the priority of the launched sandbox, then execs it. This runs as its own
# program: a `preexec_fn` would run Python in the forked child while the proxy and mailbox
# threads are alive, which can deadlock it before exec.
CONFINE = """\
import os, sys
cpus = {int(cpu) for cpu in sys.argv[1].split(",") if cpu}
if cpus:
    os.sched_setaffinity(0, cpus)
os.nice(10)
os.execvp(sys.argv[2], sys.argv[2:])
"""


def code(args: argparse.Namespace, command: list[str] | None) -> int:
    if shutil.which("bwrap") is None:
        fail("bubblewrap is not installed; run `external_agents.py install bubblewrap`")
    spec = args.agent or os.environ.get(ENV_CODER)
    if not spec and command is None:
        fail(f"no coding agent is configured; set {ENV_CODER} or pass --agent")
    worktree = args.worktree.resolve()
    if (worktree / ".git").is_dir():
        fail("run it on a git worktree (`git worktree add`), not a main checkout: the agent could plant hooks in .git")
    if not worktree.is_dir():
        fail(f"{worktree} is not a directory")
    if HOME.resolve().is_relative_to(worktree):
        fail(f"{worktree} contains the home directory")
    if command is None and (args.task is None or not args.task.is_file()):
        fail("--task must name a file")
    state = args.state.resolve()
    if state.is_relative_to(worktree) or worktree.is_relative_to(state):
        fail("the state dir must be outside the worktree, and not contain it")
    state.mkdir(parents=True, exist_ok=True)
    cpus = cpu_list(args.cpus)

    mounts = Mounts()
    python = python_for_forwarder(mounts)
    for name in args.preset:
        PRESETS[name](mounts, state, len(cpus) if cpus else None)
    for path in args.ro:
        mounts.add_ro(check_bind(path))
    for path in args.rw:
        real = check_bind(path)
        if Path(real).is_relative_to(state) or state.is_relative_to(real):
            fail(f"--rw {path} overlaps the state dir")
        mounts.rw.append((real, real))
    for pair in args.env:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            fail(f"--env {pair!r} is not KEY=VALUE")
        mounts.env[key] = value

    backend, model = parse_agent(spec, CODER_MODEL_REQUIRED) if spec else (None, None)
    defaults = coder_hosts(backend, model) if backend else ()
    hosts = args.hosts.split(",") if args.hosts else [*defaults, *args.allow_host]
    # A check command still gets the configured agent, so `-- <agent> --version` shows
    # whether it starts in the sandbox.
    if backend is not None:
        executable = bind_executable(backend, mounts)
        if command and command[0] == backend:
            # The bound file may have another name, such as an npm package's `codex.js`.
            command = [executable, *command[1:]]
        mounts.credentials = [rel for rel in CODER_CREDENTIALS[backend] if (HOME / rel).is_file()]
        mounts.env |= credential_env(backend) | coder_env(backend)
    if command is None:
        assert backend is not None
        if not hosts:
            fail(f"no egress host is known for {spec}; name the provider's API host with --allow-host")
        if not mounts.credentials and not credential_env(backend):
            print(f"warning: {backend} has no login file or credential variable; it may fail", file=sys.stderr)
        task = args.task.read_text()
        if not args.raw_task:
            task = PREAMBLE.format(worktree=worktree) + task
        command = coder_command(backend, executable, model, worktree, task)

    run = prepare(state, backend, python)
    for relative in mounts.credentials:
        (run / "home" / relative).parent.mkdir(parents=True, exist_ok=True)
        (run / "home" / relative).touch()
    if platform.machine() == "x86_64":
        (run / "seccomp.bpf").write_bytes(seccomp.encode(seccomp.program()))
    elif not args.no_seccomp:
        fail(f"the seccomp filter supports x86_64 only; pass --no-seccomp to run on {platform.machine()} without it")
    proxy = Proxy(run / "proxy" / "sock", hosts, args.log_hosts)
    proxy.start()
    questions = mailbox.Server(mailbox.Mailbox(run / "questions.log"), run / "ask" / "sock", run / "control.sock")
    questions.start()
    argv = bwrap_args(worktree, run, mounts, masks(worktree))
    argv += [python, "-I", f"{TOOL_DIR}/forward.py", *command]
    print(f"xagents: run {run.name}, egress {', '.join(hosts) or 'none'}", file=sys.stderr)

    # bwrap reads the filter from descriptor 9 (`--seccomp 9`).
    filtered = (run / "seccomp.bpf").exists()
    if filtered:
        bpf = os.open(run / "seccomp.bpf", os.O_RDONLY)
        if bpf != 9:
            os.dup2(bpf, 9, inheritable=True)
            os.close(bpf)
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", CONFINE, ",".join(map(str, cpus or [])), *argv],
            pass_fds=(9,) if filtered else (),
        )
        try:
            code = child.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            print(f"xagents: stopped after {args.timeout} s", file=sys.stderr)
            code = 124
    finally:
        if filtered:
            os.close(9)
        proxy.server.close()
        questions.close()
    print(
        f"xagents: agent exited {code}. The worktree is untrusted until reviewed: read the whole "
        "diff before building, testing or running anything from it.",
        file=sys.stderr,
    )
    return code


def add_parsers(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser(
        "code",
        help="Run a coding agent in bubblewrap on a git worktree",
        description="Arguments after `--` run in the same sandbox instead of the agent, to check it.",
    )
    run.add_argument(
        "--worktree", type=Path, required=True, help="A git worktree or jj workspace, never a main checkout"
    )
    run.add_argument("--task", type=Path, help="File holding the task for the agent")
    run.add_argument("--state", type=Path, required=True, help="Directory for runs and caches, outside the worktree")
    run.add_argument("--agent", help=f"backend[:model]; defaults to {ENV_CODER}")
    run.add_argument("--cpus", type=int, help="Pin to this many CPUs")
    run.add_argument("--timeout", type=float, default=7200)
    run.add_argument("--allow-host", action="append", default=[], help="Add an egress host (port 443)")
    run.add_argument("--hosts", help="Comma-separated egress hosts, replacing the backend's defaults")
    run.add_argument("--log-hosts", action="store_true", help="Log every CONNECT target to stderr")
    run.add_argument("--preset", action="append", default=[], choices=sorted(PRESETS), help="Toolchain to expose")
    run.add_argument("--ro", action="append", default=[], help="Bind a path read-only at the same place")
    run.add_argument("--rw", action="append", default=[], help="Bind a path read-write at the same place")
    run.add_argument("--env", action="append", default=[], help="KEY=VALUE for the agent")
    run.add_argument("--raw-task", action="store_true", help="Send the task without the sandbox preamble")
    run.add_argument("--no-seccomp", action="store_true", help="Allow running without the filter off x86_64")
    ask = commands.add_parser(
        "question",
        help="Print the coding agent's oldest unanswered question",
        description="Exit 0 with a question on stdout and its id on stderr; 1 when none is "
        "waiting (or --timeout passed); 4 when no run is active or the run ended.",
    )
    ask.add_argument("--state", type=Path, required=True)
    ask.add_argument("--wait", action="store_true", help="Block until a question arrives or the run ends")
    ask.add_argument("--timeout", type=float, help="With --wait, give up after this many seconds")
    reply = commands.add_parser(
        "answer",
        help="Answer the coding agent's oldest question",
        description="Exit 0 when delivered; 4 when no run is active.",
    )
    reply.add_argument("--state", type=Path, required=True)
    reply.add_argument("--to", type=int, help="The id `question` printed; refuse if that question is gone")
    reply.add_argument("text")
