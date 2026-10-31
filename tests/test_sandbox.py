from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SCRIPT, backends, common, proxy, sandbox


def client_hello(server_name: str) -> bytes:
    """The first flight of a real TLS client naming `server_name`."""
    context = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def worktree(root: Path) -> Path:
    """A git worktree whose `.git` is a file, next to its main checkout."""
    main = root / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.email", "test@example.com")
    git(main, "config", "user.name", "Test")
    (main / "a.txt").write_text("a\n")
    (main / ".env.example").write_text("TOKEN=\n")
    (main / "fixtures").mkdir()
    (main / "fixtures/test.pem").write_text("tracked test certificate\n")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "base")
    git(main, "worktree", "add", "-q", str(root / "wt"))
    return root / "wt"


class TlsName(unittest.TestCase):
    def test_the_server_name_is_read_from_a_real_client_hello(self) -> None:
        self.assertEqual(proxy.client_hello_sni(client_hello("ChatGPT.com")), "chatgpt.com")

    def test_a_truncated_or_foreign_first_flight_has_no_name(self) -> None:
        hello = client_hello("chatgpt.com")
        self.assertIsNone(proxy.client_hello_sni(hello[:40]))
        self.assertIsNone(proxy.client_hello_sni(b"GET / HTTP/1.1\r\n\r\n"))


class Egress(unittest.TestCase):
    def test_only_allowlisted_hosts_on_port_443(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = proxy.Proxy(Path(tmp, "sock"), ["chatgpt.com"], False)
            self.assertTrue(server.allowed("chatgpt.com:443"))
            self.assertTrue(server.allowed("ChatGPT.com:443"))
            for target in ("chatgpt.com:80", "example.com:443", "chatgpt.com", "x.chatgpt.com:443"):
                self.assertFalse(server.allowed(target), target)
            server.server.close()

    def tunnel(self, request: bytes, *after: bytes) -> tuple[bytes, list[tuple[str, int]]]:
        """Send `request` and the `after` chunks through a proxy allowing chatgpt.com; return
        the reply and the upstream addresses the proxy tried to reach."""
        dialled = []

        def fake_connect(address, timeout=None):
            dialled.append(address)
            raise OSError("no network in tests")

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(proxy.socket, "create_connection", fake_connect),
        ):
            server = proxy.Proxy(Path(tmp, "sock"), ["chatgpt.com"], False)
            server.start()
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            client.connect(str(Path(tmp, "sock")))
            client.sendall(request)
            reply = client.recv(64)
            for chunk in after:
                client.sendall(chunk)
            try:
                client.recv(64)
            except OSError:
                pass
            client.close()
            server.server.close()
        return reply, dialled

    def test_a_tunnel_whose_tls_name_differs_is_closed_before_any_upstream_connection(self) -> None:
        reply, dialled = self.tunnel(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n", client_hello("attacker.example"))
        self.assertTrue(reply.startswith(b"HTTP/1.1 200"))
        self.assertEqual(dialled, [])

    def test_a_tunnel_naming_the_allowed_host_is_dialled(self) -> None:
        _, dialled = self.tunnel(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n", client_hello("chatgpt.com"))
        self.assertEqual(dialled, [("chatgpt.com", 443)])

    def test_a_header_with_a_malformed_field_is_refused(self) -> None:
        reply, dialled = self.tunnel(b"CONNECT chatgpt.com:443 HTTP/1.1\r\nno colon\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 405"))
        self.assertEqual(dialled, [])

    def test_a_trickling_client_is_dropped_at_the_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(proxy.Proxy, "HEADER_SECONDS", 1):
            server = proxy.Proxy(Path(tmp, "sock"), ["chatgpt.com"], False)
            server.start()
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            client.connect(str(Path(tmp, "sock")))
            started = time.monotonic()
            try:
                for byte in b"CONNECT chatgpt.com:443 HTTP/1.1\r\n":
                    client.sendall(bytes([byte]))
                    time.sleep(0.1)
            except OSError:
                pass
            self.assertLess(time.monotonic() - started, 3.0)
            client.close()
            server.server.close()

    def test_an_oversized_or_malformed_request_is_refused_without_dialling(self) -> None:
        reply, dialled = self.tunnel(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n" + b"X" * 9000)
        self.assertTrue(reply.startswith(b"HTTP/1.1 431"))
        reply, _ = self.tunnel(b"CONNECT chatgpt.com:443 HTTP/9\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 405"))
        self.assertEqual(dialled, [])

    def test_connections_beyond_the_limit_are_dropped_and_the_proxy_keeps_serving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = proxy.Proxy(Path(tmp, "sock"), ["chatgpt.com"], False)
            server.start()
            idle = []
            for _ in range(proxy.Proxy.MAX_TUNNELS):
                sock = socket.socket(socket.AF_UNIX)
                sock.connect(str(Path(tmp, "sock")))
                idle.append(sock)
            extra = socket.socket(socket.AF_UNIX)
            extra.settimeout(5)
            extra.connect(str(Path(tmp, "sock")))
            self.assertEqual(extra.recv(64), b"")  # dropped
            extra.close()
            for sock in idle:
                sock.close()
            threading.Event().wait(0.3)
            again = socket.socket(socket.AF_UNIX)
            again.settimeout(5)
            again.connect(str(Path(tmp, "sock")))
            again.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            self.assertTrue(again.recv(64).startswith(b"HTTP/1.1 403"))
            again.close()
            server.server.close()

    def test_a_refused_host_gets_403_without_a_connection(self) -> None:
        reply, dialled = self.tunnel(b"CONNECT 127.0.0.1:22 HTTP/1.1\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"))
        self.assertEqual(dialled, [])

    def test_opencode_egress_follows_the_model_provider(self) -> None:
        self.assertIn("chatgpt.com", backends.coder_hosts("opencode", "openai/gpt-6.1-sol"))
        self.assertEqual(backends.coder_hosts("opencode", "unknown/model"), ())


class OpencodeCoder(unittest.TestCase):
    def test_reads_outside_the_worktree_are_denied_not_asked(self) -> None:
        # "ask" has no one to answer in `opencode run`: it is auto-rejected and ends the
        # run. "deny" returns an error and the agent goes on.
        permission = backends.OPENCODE_CODER["agent"]["coder"]["permission"]
        self.assertEqual(permission.get("external_directory"), "deny")


class Binding(unittest.TestCase):
    def test_a_file_in_an_npm_package_binds_the_package(self) -> None:
        self.assertEqual(
            sandbox.package_root("/p/lib/node_modules/@openai/codex/bin/codex.js"), "/p/lib/node_modules/@openai/codex"
        )
        self.assertEqual(sandbox.package_root("/p/node_modules/amp/dist/main.js"), "/p/node_modules/amp")

    def test_an_npm_package_brings_its_installed_dependencies_from_any_enclosing_node_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp).resolve() / "lib/node_modules"
            packages = {
                "@openai/codex": {
                    "optionalDependencies": {"@openai/codex-linux-x64": "1", "@openai/codex-darwin": "1"}
                },
                "@openai/codex-linux-x64": {"dependencies": {"shared": "1"}},
                "@openai/codex/node_modules/shared": {},
                "unrelated": {},
            }
            for name, manifest in packages.items():
                (modules / name).mkdir(parents=True)
                (modules / name / "package.json").write_text(json.dumps(manifest))

            closure = sandbox.npm_closure(str(modules / "@openai/codex"))

            # `shared` is nested under codex, so the platform package cannot resolve it
            # from its own location, and the darwin package is not installed.
            self.assertEqual(
                sorted(closure), [str(modules / "@openai/codex"), str(modules / "@openai/codex-linux-x64")]
            )

    def test_a_node_launcher_brings_the_node_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp, "lib/node_modules/@scope/agent")
            (package / "bin").mkdir(parents=True)
            launcher = package / "bin/agent.js"
            launcher.write_text("#!/usr/bin/env -S node --no-warnings\n")
            launcher.chmod(0o755)
            (Path(tmp) / "bin").mkdir()
            (Path(tmp) / "bin/agent").symlink_to(launcher)
            mounts = sandbox.Mounts()
            with (
                mock.patch.dict(os.environ, {"PATH": f"{tmp}/bin:{os.environ['PATH']}"}),
                mock.patch.object(sandbox, "node_prefix", return_value=("/opt/node", "/opt/node/bin")),
            ):
                real = sandbox.bind_executable("agent", mounts)
            self.assertEqual(real, str(launcher.resolve()))
            self.assertEqual(mounts.ro, [str(package.resolve()), "/opt/node"])
            self.assertEqual(mounts.path, ["/opt/node/bin"])

    def test_a_bind_that_would_expose_the_home_is_refused(self) -> None:
        for path in ("/", str(Path.home())):
            with self.assertRaisesRegex(common.UsageError, "contains the home"):
                sandbox.check_bind(path)


class Masks(unittest.TestCase):
    def test_untracked_secrets_and_nested_vcs_directories_are_hidden_but_tracked_files_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = worktree(Path(tmp))
            for name in (".env", "deep/target/.env.local", "certs/server.pem", "a/.envrc", "a/notes.txt"):
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                (tree / name).write_text("DUMMY=1\n")
            (tree / "vendor/lib/.git").mkdir(parents=True)
            (tree / "ws/.jj").mkdir(parents=True)
            (tree / "link.pem").symlink_to(tree / "a.txt")

            files, directories = sandbox.masks(tree)

            self.assertEqual(
                [p.relative_to(tree).as_posix() for p in files],
                [".env", "a/.envrc", "certs/server.pem", "deep/target/.env.local"],
            )
            self.assertEqual([p.relative_to(tree).as_posix() for p in directories], ["vendor/lib/.git", "ws/.jj"])


class Arguments(unittest.TestCase):
    def test_the_sandbox_shares_nothing_and_binds_only_what_it_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run, tree = Path(tmp, "run-1"), Path(tmp, "wt")
            tree.mkdir()
            (tree / ".git").write_text("gitdir: elsewhere\n")
            mounts = sandbox.Mounts(
                credentials=[".codex/auth.json"], ro=["/opt/agent"], rw=[(f"{tmp}/target", "/target")]
            )
            args = sandbox.bwrap_args(tree, run, mounts, ([tree / ".env"], [tree / "sub/.git"]))
        for flag in (
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--clearenv",
            "--new-session",
            "--die-with-parent",
        ):
            self.assertIn(flag, args)
        self.assertNotIn("--share-net", args)
        home = str(sandbox.HOME)
        sources = {args[i + 1] for i, a in enumerate(args) if a in ("--bind", "--ro-bind")}
        self.assertNotIn(home, sources)
        writable = {args[i + 1] for i, a in enumerate(args) if a == "--bind"}
        self.assertEqual(writable, {f"{run}/home", f"{tmp}/target", str(tree)})
        # The sockets' directories are read-only: the agent can connect but not replace them.
        for directory, inside in ((f"{run}/ask", "/ask"), (f"{run}/proxy", "/proxy")):
            position = args.index(directory)
            self.assertEqual(args[position - 1 : position + 2], ["--ro-bind", directory, inside])
        worktree_bind = args.index(str(tree))
        mask = args.index(str(tree / ".env"))
        self.assertEqual(args[mask - 2 : mask], ["--ro-bind", f"{run}/empty"])
        self.assertGreater(mask, worktree_bind, "the mask must come after the worktree bind")
        self.assertGreater(args.index(str(tree / "sub/.git")), worktree_bind)
        git_file = len(args) - 1 - args[::-1].index(str(tree / ".git"))
        self.assertEqual(args[git_file - 2 : git_file + 1], ["--ro-bind", str(tree / ".git"), str(tree / ".git")])
        # A login file is bound read-only at the same place in the fresh HOME.
        credential = args.index(f"{home}/.codex/auth.json")
        self.assertEqual(
            args[credential - 1 : credential + 2], ["--ro-bind", f"{home}/.codex/auth.json", f"{home}/.codex/auth.json"]
        )


class Refusals(unittest.TestCase):
    def namespace(self, tree: Path, state: Path, **extra: object) -> argparse.Namespace:
        values = dict(
            worktree=tree, task=None, state=state, agent="codex", cpus=None, timeout=10, allow_host=[], hosts=None,
            log_hosts=False, preset=[], ro=[], rw=[], env=[], raw_task=False, no_seccomp=False,
        )  # fmt: skip
        return argparse.Namespace(**(values | extra))

    def refusal(self, args: argparse.Namespace) -> str:
        with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/true"):
            with self.assertRaises(common.UsageError) as raised:
                sandbox.code(args, ["true"])
        return str(raised.exception)

    def test_a_main_checkout_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp, "tree/.git")).mkdir(parents=True)
            self.assertIn("not a main checkout", self.refusal(self.namespace(Path(tmp, "tree"), Path(tmp, "state"))))

    def test_a_state_dir_inside_the_worktree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = worktree(Path(tmp))
            self.assertIn("outside the worktree", self.refusal(self.namespace(tree, tree / ".state")))

    def test_a_target_link_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = worktree(Path(tmp))
            state = Path(tmp, "state")
            state.mkdir()
            (state / "target").symlink_to(tmp)
            self.assertIn("plain directory", self.refusal(self.namespace(tree, state, preset=["rust"])))


@unittest.skipUnless(shutil.which("bwrap"), "needs bwrap")
class LiveSandbox(unittest.TestCase):
    """Runs the real launcher with a probe command: dummy secrets, no model, no network."""

    PROBE = (
        "import os, socket\n"
        "def link(f):\n"
        "    try:\n"
        "        return os.readlink('/proc/self/fd/' + f)\n"
        "    except OSError:\n"
        "        return None\n"
        "print('fds', [l for f in os.listdir('/proc/self/fd') if int(f) > 2\n"
        "              for l in [link(f)] if l and not l.startswith('/proc/')])\n"
        "print('secret', len(open('deep/target/.env').read()))\n"
        "print('tracked', len(open('fixtures/test.pem').read()) > 0)\n"
        "print('nested', os.listdir('vendor/.git'))\n"
        "try:\n"
        "    open('.git', 'w').write('gitdir: /tmp/planted\\n')\n"
        "    print('gitfile writable')\n"
        "except OSError:\n"
        "    print('gitfile read-only')\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "print('loopback22', s.connect_ex(('127.0.0.1', 22)))\n"
        "import ctypes\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "print('ptrace', libc.ptrace(0, 0, 0, 0), ctypes.get_errno())\n"
        "print('home', sorted(os.listdir(os.path.expanduser('~'))))\n"
        "open('written.txt', 'w').write('ok')\n"
    )

    def test_the_real_sandbox_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = worktree(Path(tmp))
            (tree / "deep/target").mkdir(parents=True)
            (tree / "deep/target/.env").write_text("DUMMY_SECRET=not-real\n")
            (tree / "vendor/.git").mkdir(parents=True)
            (tree / "vendor/.git/config").write_text("[core]\n")
            out = subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "code", "--worktree", str(tree), "--state", str(Path(tmp, "state")),
                 "--", sys.executable, "-I", "-c", self.PROBE],
                capture_output=True, text=True, timeout=120,
            )  # fmt: skip
            self.assertEqual(out.returncode, 0, out.stderr)
            lines = dict(line.split(" ", 1) for line in out.stdout.splitlines())
            self.assertEqual(lines["secret"], "0")
            self.assertEqual(lines["tracked"], "True")
            self.assertEqual(lines["nested"], "[]")
            self.assertEqual(lines["gitfile"], "read-only")
            self.assertNotEqual(lines["loopback22"], "0")
            if os.uname().machine == "x86_64":
                self.assertEqual(lines["ptrace"], "-1 1")
            self.assertEqual(lines["fds"], "[]")  # nothing inherited beyond stdio
            self.assertEqual(lines["home"], "[]")
            self.assertEqual((tree / "written.txt").read_text(), "ok")
            self.assertTrue((tree / ".git").read_text().startswith("gitdir: "))
            self.assertNotIn("planted", (tree / ".git").read_text())

    def test_ask_reaches_the_coordinator_and_the_sockets_cannot_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tree = worktree(Path(tmp))
            state = Path(tmp, "state")
            probe = 'echo "got $(ask "which parser?")"; rm /ask/sock 2>/dev/null || echo kept'
            launcher = subprocess.Popen(
                [sys.executable, "-I", str(SCRIPT), "code", "--worktree", str(tree), "--state", str(state),
                 "--", "sh", "-c", probe],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )  # fmt: skip

            def cli(*args: str) -> subprocess.CompletedProcess[str]:
                command = [sys.executable, "-I", str(SCRIPT), *args, "--state", str(state)]
                return subprocess.run(command, capture_output=True, text=True, timeout=60)

            # The launcher creates the state directory and the run shortly after starting.
            deadline = time.monotonic() + 30
            while not (state / "latest").is_symlink() and time.monotonic() < deadline:
                time.sleep(0.1)
            shown = cli("question", "--wait", "--timeout", "30")
            answered = cli("answer", "--to", "1", "the existing one")
            out, err = launcher.communicate(timeout=60)

            self.assertEqual(shown.stdout, "which parser?\n", shown.stderr)
            self.assertEqual(answered.returncode, 0, answered.stderr)
            self.assertEqual((launcher.returncode, out), (0, "got the existing one\nkept\n"), err)
            self.assertEqual(cli("question").returncode, 4)


if __name__ == "__main__":
    unittest.main()
