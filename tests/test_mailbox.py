from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import SCRIPT, common, mailbox


class Mailbox(unittest.TestCase):
    """A served run directory, with the agent's `ask` run as a real subprocess."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.run_dir = self.state / "run-1"
        (self.run_dir / "ask").mkdir(parents=True)
        (self.state / "latest").symlink_to("run-1")
        self.ask_socket = self.run_dir / "ask" / "sock"
        self.box = mailbox.Mailbox(self.run_dir / "questions.log")
        self.server = mailbox.Server(self.box, self.ask_socket, self.run_dir / "control.sock")
        self.server.start()
        self.addCleanup(self.server.close)
        self.script = self.state / "ask.py"
        self.script.write_text(mailbox.ASK)

    def ask(self, text: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-I", str(self.script), text],
            env={"XAGENTS_ASK_SOCKET": str(self.ask_socket)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args, "--state", str(self.state)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def wait_for_question(self) -> mailbox.Pending:
        item = self.box.oldest(wait=True, timeout=10)
        assert item is not None
        return item

    def test_a_question_round_trips_to_the_asker(self) -> None:
        asker = self.ask("which parser?")
        self.wait_for_question()

        shown = self.cli("question")
        answered = self.cli("answer", "--to", "1", "the existing one")

        self.assertEqual((shown.returncode, shown.stdout), (0, "which parser?\n"))
        self.assertIn("question 1", shown.stderr)
        self.assertEqual(answered.returncode, 0, answered.stderr)
        self.assertEqual(asker.communicate(timeout=10), ("the existing one\n", ""))
        log = [json.loads(line) for line in (self.run_dir / "questions.log").read_text().splitlines()]
        self.assertEqual(
            [entry.get("question", entry.get("answer")) for entry in log], ["which parser?", "the existing one"]
        )

    def test_without_a_question_question_exits_1_and_answer_is_refused(self) -> None:
        self.assertEqual(self.cli("question").returncode, 1)
        refused = self.cli("answer", "too early")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no question is waiting", refused.stderr)

    def test_wait_returns_as_soon_as_a_question_arrives(self) -> None:
        threading.Timer(0.5, lambda: self.box.post("late question")).start()
        started = time.monotonic()

        shown = self.cli("question", "--wait", "--timeout", "20")

        self.assertEqual((shown.returncode, shown.stdout), (0, "late question\n"))
        self.assertLess(time.monotonic() - started, 10)

    def test_wait_gives_up_after_its_timeout(self) -> None:
        self.assertEqual(self.cli("question", "--wait", "--timeout", "0.5").returncode, 1)

    def test_wait_ends_with_4_when_the_run_ends(self) -> None:
        threading.Timer(0.5, self.server.close).start()

        self.assertEqual(self.cli("question", "--wait").returncode, mailbox.EXIT_NOT_RUNNING)
        self.assertEqual(self.cli("question").returncode, mailbox.EXIT_NOT_RUNNING)

    def test_questions_are_answered_oldest_first_and_a_stale_id_is_refused(self) -> None:
        first = self.ask("first")
        self.wait_for_question()
        second = self.ask("second")
        while len(self.box.pending) < 2:
            time.sleep(0.05)

        stale = self.cli("answer", "--to", "2", "for the second")
        self.assertEqual(self.cli("answer", "one").returncode, 0)
        self.assertEqual(self.cli("answer", "--to", "2", "two").returncode, 0)

        self.assertIn("question 2 is not the oldest", stale.stderr)
        self.assertEqual(first.communicate(timeout=10)[0], "one\n")
        self.assertEqual(second.communicate(timeout=10)[0], "two\n")

    def test_an_asker_that_hangs_up_withdraws_its_question(self) -> None:
        asker = self.ask("never mind")
        self.wait_for_question()
        asker.kill()
        asker.wait()
        deadline = time.monotonic() + 5
        while self.box.pending and time.monotonic() < deadline:
            time.sleep(0.05)

        self.assertEqual(self.box.pending, [])
        self.assertEqual(self.cli("question").returncode, 1)

    def test_an_oversized_question_is_refused_by_ask_and_by_the_server(self) -> None:
        asker = self.ask("x" * (mailbox.MAX_QUESTION + 1))
        _, err = asker.communicate(timeout=10)
        client = socket.socket(socket.AF_UNIX)
        self.addCleanup(client.close)
        client.connect(str(self.ask_socket))
        client.sendall(struct.pack(">I", mailbox.MAX_QUESTION + 1))
        client.settimeout(10)

        self.assertEqual(asker.returncode, 1)
        self.assertIn("longer than", err)
        self.assertTrue(client.recv(4096).startswith(b"Ethe question is longer than"))
        self.assertEqual(self.box.pending, [])

    def test_a_silent_asker_is_dropped_at_the_request_deadline(self) -> None:
        client = socket.socket(socket.AF_UNIX)
        self.addCleanup(client.close)
        client.connect(str(self.ask_socket))
        client.sendall(struct.pack(">I", 10) + b"abc")
        client.settimeout(mailbox.REQUEST_SECONDS + 5)

        self.assertEqual(client.recv(1), b"")
        self.assertEqual(self.box.pending, [])

    def test_the_run_ending_releases_a_waiting_asker(self) -> None:
        asker = self.ask("anyone there?")
        self.wait_for_question()

        self.server.close()

        out, err = asker.communicate(timeout=10)
        self.assertEqual(asker.returncode, 1)
        self.assertIn("the run is ending", err)

    def test_a_malformed_control_request_gets_an_error(self) -> None:
        client = socket.socket(socket.AF_UNIX)
        self.addCleanup(client.close)
        client.connect(str(self.run_dir / "control.sock"))
        client.sendall(b"not json\n")

        self.assertEqual(json.loads(client.recv(4096)), {"error": "malformed request"})

    def test_a_swapped_latest_link_or_planted_control_file_is_refused(self) -> None:
        self.server.close()
        (self.run_dir / "control.sock").write_text("not a socket")
        with self.assertRaisesRegex(common.UsageError, "not a socket"):
            mailbox.request(self.state, {"op": "question"})
        (self.state / "latest").unlink()
        (self.state / "latest").symlink_to(str(self.run_dir))
        with self.assertRaisesRegex(common.UsageError, "not a run directory"):
            mailbox.request(self.state, {"op": "question"})


class LongPaths(unittest.TestCase):
    def test_sockets_work_below_paths_longer_than_the_socket_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp, *(["d" * 40] * 4), "run-1")
            (run / "ask").mkdir(parents=True)
            Path(tmp, *(["d" * 40] * 4), "latest").symlink_to("run-1")
            self.assertGreater(len(os.fsencode(run / "control.sock")), 108)
            server = mailbox.Server(mailbox.Mailbox(run / "questions.log"), run / "ask/sock", run / "control.sock")
            server.start()
            try:
                self.assertEqual(mailbox.question(run.parent, wait=False, timeout=None), 1)
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
