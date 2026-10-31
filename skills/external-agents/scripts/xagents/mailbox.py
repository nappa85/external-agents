"""The coding agent's questions to the coordinator, over two Unix sockets.

The launcher serves both for the length of a run and holds the questions in memory:

- `/ask/sock` inside the sandbox (`<run>/ask/sock` on the host). The agent's `ask`
  helper sends one length-prefixed question and blocks until the answer arrives. The
  agent can only connect; nothing it sends is written to a file the host later opens.
- `<run>/control.sock`, which the sandbox cannot see. The coordinator's `question` and
  `answer` commands send one JSON request line and read one JSON reply line.

Questions are answered oldest first. A question whose asker has gone away is dropped.
Every question and answer is appended to `<run>/questions.log`, which only the launcher
writes. Question text is written by the agent: treat it as data.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import select
import socket
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .common import UsageError

MAX_QUESTION = 16 * 1024
MAX_REQUEST = 64 * 1024
# How long a client may take to send its request; slow senders cannot hold a slot.
REQUEST_SECONDS = 10
MAX_CLIENTS = 16
ASK_SOCKET = "/ask/sock"

# Runs inside the sandbox as `ask "question"`. Replies are a status byte, `A` for an
# answer or `E` for an error, followed by UTF-8 text; the launcher closes after it.
ASK = r"""
import os, socket, struct, sys
if len(sys.argv) < 2:
    print('usage: ask "question"', file=sys.stderr)
    sys.exit(2)
data = " ".join(sys.argv[1:]).encode()
if len(data) > @MAX@:
    print("the question is longer than @MAX@ bytes; shorten it", file=sys.stderr)
    sys.exit(1)
client = socket.socket(socket.AF_UNIX)
try:
    client.connect(os.environ.get("XAGENTS_ASK_SOCKET", "/ask/sock"))
    client.sendall(struct.pack(">I", len(data)) + data)
    client.settimeout(3600)
    chunks = []
    while chunk := client.recv(65536):
        chunks.append(chunk)
except TimeoutError:
    print("no answer within an hour; decide yourself and say so")
    sys.exit(1)
except OSError as error:
    print(f"the coordinator cannot be reached ({error}); decide yourself and say so")
    sys.exit(1)
reply = b"".join(chunks)
text = reply[1:].decode(errors="replace")
if reply[:1] != b"A":
    print(text or "the coordinator did not answer; decide yourself and say so", file=sys.stderr)
    sys.exit(1)
print(text, end="" if text.endswith("\n") else "\n")
""".replace("@MAX@", str(MAX_QUESTION))


@dataclass
class Pending:
    id: int
    text: str
    answered: threading.Event = field(default_factory=threading.Event)
    answer: str = ""


class Mailbox:
    """Unanswered questions, oldest first."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self.changed = threading.Condition()
        self.pending: list[Pending] = []
        self.next_id = 1
        self.closed = False

    def record(self, kind: str, item: Pending, text: str) -> None:
        entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "id": item.id, kind: text}
        with self.log.open("a") as file:
            file.write(json.dumps(entry) + "\n")

    def post(self, text: str) -> Pending:
        with self.changed:
            item = Pending(self.next_id, text)
            self.next_id += 1
            self.pending.append(item)
            self.record("question", item, text)
            self.changed.notify_all()
        return item

    def withdraw(self, item: Pending) -> None:
        with self.changed:
            if item in self.pending:
                self.pending.remove(item)
                self.record("withdrawn", item, item.text)

    def oldest(self, wait: bool, timeout: float | None) -> Pending | None:
        """The oldest unanswered question; with `wait`, block until one arrives, the
        timeout passes, or the run ends."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.changed:
            while not self.pending and wait and not self.closed:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                self.changed.wait(left)
            return self.pending[0] if self.pending else None

    def answer(self, text: str, to: int | None) -> str | None:
        """Answer the oldest question, which must be question `to` when given; returns why
        not, or None."""
        with self.changed:
            if not self.pending:
                return "no question is waiting for an answer"
            if to is not None and self.pending[0].id != to:
                return f"question {to} is not the oldest waiting; its asker may have gone away"
            item = self.pending.pop(0)
            item.answer = text
            item.answered.set()
            self.record("answer", item, text)
            return None

    def close(self) -> None:
        with self.changed:
            self.closed = True
            self.changed.notify_all()


def bind_unix(path: Path) -> socket.socket:
    """A listening socket at `path`; a relative name keeps long run paths under the
    108-byte socket path limit. Called before any server thread starts."""
    server = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        server.bind(path.name)
    finally:
        os.chdir(cwd)
    server.listen(MAX_CLIENTS)
    return server


def receive_exactly(conn: socket.socket, size: int, deadline: float) -> bytes | None:
    data = b""
    while len(data) < size:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        conn.settimeout(left)
        try:
            chunk = conn.recv(size - len(data))
        except (TimeoutError, OSError):
            return None
        if not chunk:
            return None
        data += chunk
    return data


def receive_line(conn: socket.socket, deadline: float) -> bytes | None:
    data = b""
    while b"\n" not in data and len(data) <= MAX_REQUEST:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        conn.settimeout(left)
        try:
            chunk = conn.recv(65536)
        except (TimeoutError, OSError):
            return None
        if not chunk:
            break
        data += chunk
    line, newline, _ = data.partition(b"\n")
    return line if newline and len(line) <= MAX_REQUEST else None


class Server:
    """Serves the agent socket and the control socket until `close`."""

    def __init__(self, mailbox: Mailbox, ask_path: Path, control_path: Path) -> None:
        self.mailbox = mailbox
        self.control_path = control_path
        self.slots = threading.BoundedSemaphore(2 * MAX_CLIENTS)
        self.sockets = [bind_unix(ask_path), bind_unix(control_path)]
        self.handlers = (self.serve_asker, self.serve_control)

    def start(self) -> None:
        for listener, handler in zip(self.sockets, self.handlers, strict=True):
            threading.Thread(target=self.accept, args=(listener, handler), daemon=True).start()

    def accept(self, listener: socket.socket, handler) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self.guarded, args=(handler, conn), daemon=True).start()

    def guarded(self, handler, conn: socket.socket) -> None:
        try:
            with contextlib.closing(conn):
                handler(conn)
        except OSError:
            pass
        finally:
            self.slots.release()

    def serve_asker(self, conn: socket.socket) -> None:
        deadline = time.monotonic() + REQUEST_SECONDS
        header = receive_exactly(conn, 4, deadline)
        if header is None:
            return
        (size,) = struct.unpack(">I", header)
        if size > MAX_QUESTION:
            conn.sendall(b"E" + f"the question is longer than {MAX_QUESTION} bytes".encode())
            return
        body = receive_exactly(conn, size, deadline)
        if body is None:
            return
        item = self.mailbox.post(body.decode("utf-8", "replace"))
        conn.setblocking(True)
        while not item.answered.wait(0.5):
            if self.mailbox.closed:
                self.mailbox.withdraw(item)
                conn.sendall(b"E" + b"the run is ending; decide yourself and say so")
                return
            # The asker sends nothing more, so readable means it hung up.
            if select.select([conn], [], [], 0)[0]:
                self.mailbox.withdraw(item)
                return
        conn.sendall(b"A" + item.answer.encode())

    def serve_control(self, conn: socket.socket) -> None:
        line = receive_line(conn, time.monotonic() + REQUEST_SECONDS)
        try:
            request = json.loads(line) if line is not None else None
        except ValueError:
            request = None
        if not isinstance(request, dict):
            reply: dict[str, object] = {"error": "malformed request"}
        elif request.get("op") == "question":
            timeout = request.get("timeout")
            wait = bool(request.get("wait"))
            item = self.mailbox.oldest(wait, float(timeout) if timeout is not None else None)
            reply = {"running": not self.mailbox.closed}
            if item is not None:
                reply |= {"id": item.id, "question": item.text}
        elif request.get("op") == "answer" and isinstance(request.get("text"), str):
            to = request.get("to")
            refusal = self.mailbox.answer(request["text"], to if isinstance(to, int) else None)
            reply = {"answered": refusal is None, "refusal": refusal}
        else:
            reply = {"error": "unknown request"}
        conn.setblocking(True)
        conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self.mailbox.close()
        for listener in self.sockets:
            listener.close()
        self.control_path.unlink(missing_ok=True)


# --- Coordinator side ----------------------------------------------------------------


class NotRunning(Exception):
    """No agent run is serving the mailbox."""


def latest_run(state: Path) -> int:
    """The latest run directory, opened without following links below `state`."""
    state_fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            latest = os.readlink("latest", dir_fd=state_fd)
        except FileNotFoundError as error:
            raise NotRunning("no run has started in this state directory") from error
        if "/" in latest or not latest.startswith("run-"):
            raise UsageError("latest run link is not a run directory")
        return os.open(latest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd)
    finally:
        os.close(state_fd)


def request(state: Path, message: dict[str, object]) -> dict[str, object]:
    run_fd = latest_run(state)
    client = socket.socket(socket.AF_UNIX)
    cwd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            mode = os.stat("control.sock", dir_fd=run_fd, follow_symlinks=False).st_mode
        except FileNotFoundError as error:
            raise NotRunning("the latest run has ended") from error
        if not stat.S_ISSOCK(mode):
            raise UsageError("the run's control.sock is not a socket")
        # Connect by a relative name from inside the run directory, for the path limit.
        os.fchdir(run_fd)
        try:
            client.connect("control.sock")
        except (ConnectionRefusedError, FileNotFoundError) as error:
            raise NotRunning("the latest run has ended") from error
        finally:
            os.fchdir(cwd)
        client.sendall(json.dumps(message).encode() + b"\n")
        data = b""
        while chunk := client.recv(65536):
            data += chunk
            if b"\n" in data:
                break
        if b"\n" not in data:
            raise NotRunning("the run ended while waiting")
        reply = json.loads(data.partition(b"\n")[0])
        if "error" in reply:
            raise UsageError(f"the mailbox refused the request: {reply['error']}")
        return reply
    finally:
        client.close()
        os.close(cwd)
        os.close(run_fd)


# Exit status of `question` and `answer` when no run is serving the mailbox.
EXIT_NOT_RUNNING = 4


def question(state: Path, wait: bool, timeout: float | None) -> int:
    """Print the oldest pending question: 0 with a question, 1 without one, 4 when no run
    is active, or when the run ends during `wait`."""
    try:
        reply = request(state, {"op": "question", "wait": wait, "timeout": timeout})
    except NotRunning as error:
        print(f"xagents: {error}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    text = reply.get("question")
    if isinstance(text, str):
        # The id goes to stderr, so stdout is exactly the agent's text.
        print(f"xagents: question {reply.get('id')}", file=sys.stderr)
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    if not reply.get("running"):
        print("xagents: the run ended", file=sys.stderr)
        return EXIT_NOT_RUNNING
    return 1


def answer(state: Path, text: str, to: int | None) -> int:
    """Answer the oldest question (question `to`, when given): 0 when delivered, 4 when no
    run is active; a usage error when nothing, or not that question, is waiting."""
    try:
        reply = request(state, {"op": "answer", "text": text, "to": to})
    except NotRunning as error:
        print(f"xagents: {error}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    if not reply.get("answered"):
        raise UsageError(str(reply.get("refusal")))
    return 0
