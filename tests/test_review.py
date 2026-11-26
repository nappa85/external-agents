from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import SCRIPT, common, review, snapshot
from xagents.backends import INSTALLERS


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class Sandbox(unittest.TestCase):
    """A Git checkout plus a PATH holding only fake reviewer CLIs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Only the fakes and the tools they and the script need, so an installed reviewer
        # CLI is never found.
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for tool in ("sh", "git", "grep", "cat", "ls", "sleep", "mkdir", "rm"):
            found = shutil.which(tool)
            assert found is not None, tool
            (self.tools / tool).symlink_to(found)
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.write("AGENTS.md", "# AGENTS.md\n")
        self.write(".gitignore", ".agents/plans/\n")
        self.write("src/lib.rs", "fn old() {}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("Review the change.\n")
        self.output = self.root / "review.md"

    def write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_script(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        environment = {"PATH": f"{self.bin}:{self.tools}", "XDG_STATE_HOME": str(self.root / "state"), **(env or {})}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
        )

    def review(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Review with a `claude` panel unless the test configures another."""
        return self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output", str(self.output), "--no-preflight",
            *args, env={"EXTERNAL_REVIEWERS": "claude", **(env or {})},
        )  # fmt: skip


class SnapshotTests(Sandbox):
    def test_snapshot_holds_reviewable_files_and_excludes_secrets(self) -> None:
        self.write("src/new.rs", "fn new() {}\n")
        self.write(".env", "TOKEN=secret\n")
        self.write("config/.env.example", "TOKEN=\n")
        self.write("certs/server.key", "secret\n")
        self.write("docs/.npmrc", "//registry.example/:_authToken=secret\n")
        self.write("logs/audit.jsonl", '{"tool": "bash", "input": "export TOKEN=secret"}\n')
        self.write("examples/.tool/config.toml", "key = 1\n")
        self.write(".agents/plans/change.md", "plan\n")
        dest = self.root / "snapshot"

        snapshot.snapshot(self.repo, dest, [Path(".agents/plans/change.md")])

        files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
        self.assertEqual(
            files,
            [
                ".agents/plans/change.md",
                ".gitignore",
                "AGENTS.md",
                "config/.env.example",
                "examples/.tool/config.toml",
                "src/lib.rs",
                "src/new.rs",
            ],
        )

    def test_including_a_secret_is_refused(self) -> None:
        self.write(".env", "TOKEN=secret\n")

        with self.assertRaisesRegex(common.UsageError, "secret-like"):
            snapshot.snapshot(self.repo, self.root / "snapshot", [Path(".env")])

    def test_base_diff_covers_tracked_edits_and_untracked_files(self) -> None:
        self.write("src/lib.rs", "fn changed() {}\n")
        self.write("src/new.rs", "fn new() {}\n")

        diff, names = snapshot.filter_patch(snapshot.base_diff(self.repo, "HEAD"))

        self.assertIn("+fn changed() {}", diff)
        self.assertIn("+fn new() {}", diff)
        self.assertEqual(names, "M\tsrc/lib.rs\nA\tsrc/new.rs\n")

    def test_base_diff_does_not_depend_on_the_users_diff_configuration(self) -> None:
        self.write("src/lib.rs", "fn changed() {}\n")
        self.write("src/new.rs", "fn new() {}\n")

        for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("diff.external", "false")):
            with self.subTest(key):
                git(self.repo, "config", key, value)
                try:
                    diff, names = snapshot.filter_patch(snapshot.base_diff(self.repo, "HEAD"))
                finally:
                    git(self.repo, "config", "--unset", key)

                self.assertIn("+fn changed() {}", diff)
                self.assertEqual(names, "M\tsrc/lib.rs\nA\tsrc/new.rs\n")

    def test_base_diff_of_a_subdirectory_uses_the_snapshots_paths(self) -> None:
        # `--source` may name a subdirectory; the snapshot holds its files relative to it,
        # so every diff header must too, tracked and untracked alike.
        source = self.repo / "src"
        self.write("src/lib.rs", "fn changed() {}\n")
        self.write("src/new.rs", "fn new() {}\n")

        diff = snapshot.base_diff(source, "HEAD")

        headers = sorted(line for line in diff.splitlines() if line.startswith("diff --git"))
        self.assertEqual(headers, ["diff --git a/lib.rs b/lib.rs", "diff --git a/new.rs b/new.rs"])
        self.assertEqual(snapshot.source_files(source), [Path("lib.rs"), Path("new.rs")])

    def test_changes_ignore_configuration_written_into_the_recording_repository(self) -> None:
        # The recording repository sits next to the snapshot, where a codex reviewer can
        # write (its workspace-write sandbox allows /tmp). Configuration planted there must
        # not make the wrapper's own git run commands on the host.
        snap = self.root / "snap"
        snap.mkdir()
        (snap / "a.txt").write_text("a\n")
        git_dir = self.root / "changes.git"
        changes = snapshot.Changes(git_dir, snap, ())
        before = changes.tree()
        marker = self.root / "ran"
        with (git_dir / "config").open("a") as config:
            config.write(f'[core]\n\tfsmonitor = "touch {marker}-fsmonitor; false"\n')
            config.write(f'[filter "x"]\n\tclean = "touch {marker}-filter; cat"\n')
        with (git_dir / "info" / "attributes").open("a") as attributes:
            attributes.write("* filter=x\n")
        (snap / "a.txt").write_text("b\n")

        diff = changes.diff(before, changes.tree())

        self.assertIn(b"+b", diff)
        self.assertEqual(sorted(p.name for p in self.root.glob("ran-*")), [])

    def test_non_utf8_content_and_names_survive_snapshot_and_diff(self) -> None:
        # Latin-1 fixtures and file names are ordinary in real repositories.
        (self.repo / "src/lib.rs").write_bytes(b"caf\xe9\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "latin-1")
        (self.repo / "src/lib.rs").write_bytes(b"caf\xe9 au lait\n")
        name = os.fsdecode(b"b\xe9.txt")
        (self.repo / name).write_text("new\n")

        diff = snapshot.base_diff(self.repo, "HEAD")
        snapshot.snapshot(self.repo, self.root / "snap", [])

        self.assertIn("diff --git a/src/lib.rs b/src/lib.rs", diff)
        self.assertTrue((self.root / "snap" / name).is_file())

    def test_diff_omits_tracked_and_untracked_secret_files(self) -> None:
        self.write("config/.env", "TOKEN=old\n")
        self.write("certs/server.key", "old-key\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "secrets")
        self.write("config/.env", "TOKEN=new\n")
        (self.repo / "certs/server.key").unlink()
        self.write("local.pem", "untracked-key\n")
        self.write("src/lib.rs", "fn changed() {}\n")

        diff, names = snapshot.filter_patch(snapshot.base_diff(self.repo, "HEAD"))

        self.assertNotIn("TOKEN", diff)
        self.assertNotIn("key", diff)
        self.assertEqual(names, "M\tsrc/lib.rs\n")

    def test_symlinks_are_kept_only_when_relative_and_inside_the_tree(self) -> None:
        self.write(".env", "TOKEN=secret\n")
        (self.repo / "relative").symlink_to("src/lib.rs")
        (self.repo / "absolute").symlink_to(self.repo / "src/lib.rs")
        (self.repo / "alias").symlink_to(".env")
        dest = self.root / "snapshot"

        snapshot.snapshot(self.repo, dest, [])

        links = sorted(p.name for p in dest.iterdir() if p.is_symlink())
        self.assertEqual(links, ["relative"])

    def test_a_directory_replaced_by_a_link_out_of_the_tree_is_not_followed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "lib.rs").write_text("private, outside the checkout\n")
        shutil.rmtree(self.repo / "src")
        (self.repo / "src").symlink_to(outside, target_is_directory=True)
        dest = self.root / "snapshot"

        snapshot.snapshot(self.repo, dest, [])

        self.assertFalse((dest / "src").exists())
        self.assertTrue((dest / "AGENTS.md").is_file())

    def test_include_outside_the_source_is_refused(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("x\n")

        with self.assertRaisesRegex(common.UsageError, "outside the source tree"):
            snapshot.snapshot(self.repo, self.root / "snapshot", [Path("../outside.md")])


class ReviewerSelectionTests(Sandbox):
    def test_falls_back_to_the_next_reviewer_when_one_is_unavailable(self) -> None:
        self.fake("codex", 'echo "error: usage quota exceeded" >&2; exit 1')
        self.fake("claude", 'cat REVIEW_PROMPT.md; test -f REVIEW_DIFF.patch && echo "diff present"')

        result = self.review("--base", "HEAD", env={"EXTERNAL_REVIEWERS": "codex, claude"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: claude\nReview the change.\ndiff present\n")
        self.assertIn("codex: unavailable (exit 1)\n  error: usage quota exceeded\n", result.stderr)

    def test_reports_no_reviewer_when_every_backend_fails(self) -> None:
        self.fake("claude", 'echo "Error: model not found: sonnet-9"; exit 1')

        result = self.review()

        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            self.output.read_text(),
            "no reviewer available:\nclaude: unavailable (exit 1)\n  Error: model not found: sonnet-9\n",
        )

    def test_configured_fallback_order_is_followed(self) -> None:
        self.write(".opencode/plugin/repo-plugin.ts", "export default async () => ({})\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "opencode config")
        self.fake("claude", "echo claude review")
        self.fake("opencode", 'test -f opencode.json && test ! -e .opencode && echo "opencode review with $*"')

        result = self.review(env={"EXTERNAL_REVIEWERS": "opencode:provider/model,claude"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.read_text().startswith("reviewer: opencode:provider/model\n"))
        self.assertIn("-m provider/model --agent reviewer", self.output.read_text())

    def test_panel_slots_run_in_parallel_on_separate_snapshots(self) -> None:
        sync = self.root / "sync"
        sync.mkdir()
        # Each run waits for the other to start, then reports its generated model.
        self.fake(
            "amp",
            ': > "$SYNC/$$"; i=0\n'
            'while [ "$(ls "$SYNC" | grep -c .)" -lt 2 ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i + 1)); done\n'
            "[ $i -lt 100 ] || echo alone\n"
            'grep -o \'model: "[^"]*"\' .amp/plugins/external-reviewer.ts',
        )
        env = {
            "EXTERNAL_REVIEWERS": "amp:p/one; amp:p/two",
            "SYNC": str(sync),
            "EXTERNAL_AGENTS_ENV_PASSTHROUGH": "SYNC",
        }

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output-dir", str(self.root / "out"), "--no-preflight",
            env=env,
        )  # fmt: skip

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "out/review-1.md").read_text(), 'reviewer: amp:p/one\nmodel: "p/one"\n')
        self.assertEqual((self.root / "out/review-2.md").read_text(), 'reviewer: amp:p/two\nmodel: "p/two"\n')

    def test_a_slot_that_crashes_does_not_discard_the_other_slots_reviews(self) -> None:
        # A tracked file named `.amp` makes the amp slot fail while it writes its
        # configuration; the claude slot's finished review must still be written, and the
        # amp slot must report its own outcome.
        self.write(".amp", "not a directory\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "dot amp file")
        self.fake("claude", "echo claude review")
        self.fake("amp", "echo amp review")

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output-dir", str(self.root / "out"), "--no-preflight",
            env={"EXTERNAL_REVIEWERS": "claude; amp:p/model"},
        )  # fmt: skip

        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((self.root / "out/review-1.md").read_text(), "reviewer: claude\nclaude review\n")
        self.assertTrue((self.root / "out/review-2.md").is_file(), result.stderr)
        # 0 once the amp slot copes with the file, 3 while it still fails on its own.
        self.assertIn(result.returncode, (0, 3), result.stderr)

    def fake_amp(self, report: str, exit_code: int = 0) -> Path:
        """An `amp` whose `check-access` prints `report`, exits `exit_code`, and records its arguments."""
        calls = self.root / "amp-calls"
        self.fake(
            "amp",
            f'if [ "$1" = config ]; then echo "$*" >> {calls}; echo \'{report}\'; exit {exit_code}; fi\necho amp review',
        )
        return calls

    def no_credits_review(self, reviewers: str) -> subprocess.CompletedProcess[str]:
        return self.review(env={"EXTERNAL_REVIEWERS": reviewers, "EXTERNAL_AGENTS_AMP_NO_CREDITS": "1"})

    def test_amp_on_credits_is_unavailable_when_credits_are_forbidden(self) -> None:
        routing = {"kind": "upstream-model-provider", "modelProviderID": "openai", "modelProviderName": "OpenAI"}
        calls = self.fake_amp(json.dumps({"ok": True, "modelRouting": routing}))
        self.fake("claude", "echo claude review")

        result = self.no_credits_review("amp:openai/gpt | claude")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: claude\nclaude review\n")
        reason = (
            "Amp credits would pay for openai/gpt (served by OpenAI), and EXTERNAL_AGENTS_AMP_NO_CREDITS forbids them"
        )
        self.assertIn(f"amp:openai/gpt: unavailable ({reason})", result.stderr)
        self.assertIn(reason, self.output.with_suffix(".log").read_text())
        self.assertEqual(calls.read_text(), "config model-providers check-access --provider-model openai/gpt\n")

    def test_amp_is_unavailable_when_its_routing_check_fails(self) -> None:
        self.fake("claude", "echo claude review")
        cases = {
            "failed inference": ('{"ok": false, "modelRouting": {"kind": "model-provider"}}', 1),
            "routing not an object": ('{"ok": true, "modelRouting": "model-provider"}', 0),
            "not JSON": ("Error: not logged in", 1),
            "unknown routing": ('{"ok": true, "modelRouting": {"kind": "something-new"}}', 0),
        }
        for name, (report, exit_code) in cases.items():
            with self.subTest(name):
                self.fake_amp(report, exit_code)

                result = self.no_credits_review("amp:openai/gpt | claude")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.output.read_text(), "reviewer: claude\nclaude review\n")
                self.assertIn("amp:openai/gpt: unavailable (its model routing check failed", result.stderr)

    def test_amp_on_a_linked_provider_runs_when_credits_are_forbidden(self) -> None:
        routing = {"kind": "model-provider", "modelProviderName": "my ChatGPT subscription"}
        calls = self.fake_amp(json.dumps({"ok": True, "modelRouting": routing}))

        result = self.no_credits_review("amp:openai/gpt@high")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: amp:openai/gpt@high\namp review\n")
        self.assertIn("--provider-model openai/gpt --reasoning-effort high\n", calls.read_text())

    def test_amp_routing_is_not_checked_unless_credits_are_forbidden(self) -> None:
        calls = self.fake_amp(json.dumps({"ok": True, "modelRouting": {"kind": "upstream-model-provider"}}))

        result = self.review(env={"EXTERNAL_REVIEWERS": "amp:openai/gpt"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: amp:openai/gpt\namp review\n")
        self.assertFalse(calls.exists())

    def test_optional_slot_falls_back_and_may_end_without_a_review(self) -> None:
        self.fake("claude", "echo claude review")
        self.fake("opencode", 'echo "Error: model not found"; exit 1')

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output-dir", str(self.root / "out"), "--no-preflight",
            env={"EXTERNAL_REVIEWERS": "claude:sonnet; ? codex:gpt | opencode:p/m"},
        )  # fmt: skip

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "out/review-1.md").read_text(), "reviewer: claude:sonnet\nclaude review\n")
        self.assertEqual(
            (self.root / "out/review-2.md").read_text(),
            "no reviewer available:\nopencode:p/m: unavailable (exit 1)\n  Error: model not found\n",
        )
        self.assertIn("optional slot 2: no reviewer available", result.stderr)

    def test_missing_required_slot_fails_the_panel(self) -> None:
        self.fake("claude", "echo claude review")
        self.fake("opencode", "exit 1")

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output-dir", str(self.root / "out"), "--no-preflight",
            "--reviewers", "claude:sonnet; opencode:p/m",
        )  # fmt: skip

        self.assertEqual(result.returncode, 3)
        self.assertEqual((self.root / "out/review-1.md").read_text(), "reviewer: claude:sonnet\nclaude review\n")
        self.assertIn("slot 2: no reviewer available", result.stderr)

    def test_reviewer_changes_are_returned_as_a_patch_that_applies_to_the_checkout(self) -> None:
        self.write("src/old.rs", "fn old() {}\n")
        self.write("snapshot/nested.rs", "fn nested() {}\n")
        self.write("crlf.txt", "a\r\nb\r\n")
        self.write(".gitattributes", "crlf.txt -text\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "old")
        # The second reviewer must start from the reviewed tree, without the failed first one's change.
        self.fake("codex", "echo 'fn stale() {}' > src/stale.rs; exit 1")
        self.fake(
            "opencode",
            "mkdir -p tests .opencode; echo 'fn regression() {}' > tests/regression.rs; "
            "echo 'fn fixed() {}' > src/lib.rs; rm src/old.rs; echo 'fn moved() {}' > snapshot/nested.rs; "
            "printf 'a\\r\\nc\\r\\n' > crlf.txt; rm REVIEW_PROMPT.md; echo '{}' > opencode.json; "
            "echo 'run()' > .opencode/plugin.ts; echo TOKEN=x > .env; test -e src/stale.rs && echo stale; "
            "echo 'crlf.txt text' > .gitattributes; git init -q fixture; echo data > fixture/data.txt; echo review",
        )

        result = self.review(env={"EXTERNAL_REVIEWERS": "codex | opencode:p/m"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: opencode:p/m\nreview\n\nproposed changes: review.patch\n")
        git(self.repo, "apply", str(self.root / "review.patch"))
        self.assertEqual(
            git(self.repo, "status", "--porcelain", "--untracked-files=all").splitlines(),
            [
                " M .gitattributes",
                " M crlf.txt",
                " M snapshot/nested.rs",
                " M src/lib.rs",
                " D src/old.rs",
                "?? fixture/data.txt",
                "?? tests/regression.rs",
            ],
        )
        self.assertEqual((self.repo / "tests/regression.rs").read_text(), "fn regression() {}\n")
        self.assertEqual((self.repo / "snapshot/nested.rs").read_text(), "fn moved() {}\n")
        self.assertEqual((self.repo / "crlf.txt").read_bytes(), b"a\r\nc\r\n")

    def test_unconfigured_panel_runs_no_external_reviewer(self) -> None:
        self.fake("claude", "echo claude review")

        detected = self.run_script("detect")
        result = self.run_script("review", "--prompt-file", str(self.prompt), "--output", str(self.output))

        self.assertEqual(json.loads(detected.stdout)["reviewers"]["panel"], [])
        self.assertEqual(result.returncode, 2)
        self.assertIn("no external reviewer is configured", result.stderr)
        self.assertFalse(self.output.exists())

    def test_detect_names_configured_backends_that_are_not_installed(self) -> None:
        self.fake("claude", "echo claude review")

        result = self.run_script("detect", env={"EXTERNAL_REVIEWERS": "claude; ? codex | opencode:p/m"})

        detected = json.loads(result.stdout)["reviewers"]
        self.assertEqual(detected["panel"], [{"optional": False, "chain": ["claude"]}, {"optional": True, "chain": []}])
        self.assertEqual(
            detected["missing"], {"codex": str(INSTALLERS["codex"]), "opencode": str(INSTALLERS["opencode"])}
        )

    def test_opencode_requires_an_explicit_model(self) -> None:
        self.fake("opencode", "echo review")

        result = self.run_script("detect", env={"EXTERNAL_REVIEWERS": "opencode"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit model", result.stderr)

    def test_only_a_known_effort_suffix_is_split_from_the_model(self) -> None:
        self.fake("amp", "echo review")
        self.fake("opencode", "echo review")
        panel = "amp:deepseek/deepseek-v4.1-flash@high; opencode:google-vertex/claude-sonnet-4@20250514"

        result = self.run_script("detect", env={"EXTERNAL_REVIEWERS": panel})

        self.assertEqual(result.returncode, 0, result.stderr)
        chains = [slot["chain"] for slot in json.loads(result.stdout)["reviewers"]["panel"]]
        self.assertEqual(
            chains, [["amp:deepseek/deepseek-v4.1-flash@high"], ["opencode:google-vertex/claude-sonnet-4@20250514"]]
        )

    def test_an_effort_claude_does_not_accept_is_a_usage_error(self) -> None:
        self.fake("claude", "echo review")

        result = self.run_script("detect", env={"EXTERNAL_REVIEWERS": "claude:sonnet@minimal"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("claude takes no reasoning effort 'minimal'", result.stderr)

    def test_each_cli_backend_receives_the_effort_in_its_own_flag(self) -> None:
        for spec, expected in (
            ("codex:gpt-6.1-sol@xhigh", '-c model_reasoning_effort="xhigh"'),
            ("claude:sonnet@max", "--effort max --"),
            ("opencode:opencode/deepseek-v4.1-flash-free@high", "--agent reviewer --variant high"),
            ("vibe:mistral-medium-3.5@high", "--model mistral-medium-3.5 --thinking high"),
            ("vibe@none", "--output streaming --thinking off"),
        ):
            backend = spec.partition(":")[0].partition("@")[0]
            with self.subTest(backend=backend):
                self.fake(backend, 'echo "$@"')

                result = self.review("--reviewer", spec)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, self.output.read_text())

    def test_short_provider_error_on_success_counts_as_unavailable(self) -> None:
        self.assertEqual(review.classify(0, "Error: rate limit reached, try later."), "unavailable")
        self.assertEqual(review.classify(0, "> reviewer\n\u2717 API Error: 429 quota exceeded"), "unavailable")

    def test_short_review_mentioning_a_provider_failure_word_is_kept(self) -> None:
        self.assertEqual(review.classify(0, "High: unauthorized users can read billing records."), "ok")
        self.assertEqual(review.classify(0, "No findings."), "ok")

    def test_review_discussing_authorization_is_kept(self) -> None:
        text = "1. High: unauthorized users can read another tenant's billing records. " * 10

        self.assertEqual(review.classify(0, text), "ok")

    def test_supplied_diff_is_filtered_and_gets_a_manifest(self) -> None:
        self.write("src/lib.rs", "fn changed() {}\n")
        self.write(".env", "TOKEN=secret\n")
        patch = self.root / "change.patch"
        patch.write_text(snapshot.base_diff(self.repo, "HEAD"))
        self.fake("claude", "cat REVIEW_FILES.txt; grep -c TOKEN REVIEW_DIFF.patch || true")

        result = self.review("--diff-file", str(patch))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: claude\nM\tsrc/lib.rs\n0\n")

    def test_output_in_a_missing_directory_is_a_usage_error(self) -> None:
        self.fake("claude", "echo review")

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output", str(self.root / "missing/review.md"),
            "--no-preflight", env={"EXTERNAL_REVIEWERS": "claude"},
        )  # fmt: skip

        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_missing_diff_file_is_a_usage_error(self) -> None:
        self.fake("claude", "echo review")

        result = self.review("--diff-file", str(self.root / "missing.patch"))

        self.assertEqual(result.returncode, 2)
        self.assertIn("diff file", result.stderr)

    def test_output_dir_that_is_a_file_is_a_usage_error(self) -> None:
        self.fake("claude", "echo review")

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output-dir", str(self.prompt), "--no-preflight",
            env={"EXTERNAL_REVIEWERS": "claude"},
        )  # fmt: skip

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot use --output-dir", result.stderr)

    def test_reviewer_environment_drops_unrelated_credentials(self) -> None:
        self.fake(
            "claude",
            'echo "github=${GITHUB_TOKEN:-unset} ssh=${SSH_AUTH_SOCK:-unset} openai=${OPENAI_API_KEY:-unset}'
            ' amp=${AMP_API_KEY:-unset} lang=${LANG:-unset}"',
        )
        env = {"GITHUB_TOKEN": "gh", "SSH_AUTH_SOCK": "/agent", "OPENAI_API_KEY": "sk", "AMP_API_KEY": "amp"}

        result = self.review(env={**env, "LANG": "C.UTF-8"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.output.read_text(), "reviewer: claude\ngithub=unset ssh=unset openai=sk amp=unset lang=C.UTF-8\n"
        )

    def test_amp_reviewer_runs_only_the_generated_confined_mode(self) -> None:
        self.write(".amp/plugins/repo-plugin.ts", "export default function () {}\n")
        self.write(".amp/settings.json", '{"amp.permissions": [{"tool": "*", "action": "allow"}]}\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "amp config")
        self.fake(
            "amp",
            'echo "amp=${AMP_API_KEY:-unset} github=${GITHUB_TOKEN:-unset}"; ls .amp/plugins; '
            'grep -o "model: .*" .amp/plugins/external-reviewer.ts; '
            'grep -c \'"action": "reject"\' .amp/settings.json; echo "$@"',
        )

        result = self.review("--reviewer", "amp:openai/gpt-6.1-sol", env={"AMP_API_KEY": "amp", "GITHUB_TOKEN": "gh"})

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.output.read_text().splitlines()
        self.assertEqual(
            lines[:5],
            [
                "reviewer: amp:openai/gpt-6.1-sol",
                "amp=amp github=unset",
                "external-reviewer.ts",
                'model: "openai/gpt-6.1-sol",',
                "4",
            ],
        )
        self.assertRegex(lines[5], r"--settings-file \S+/snapshot/\.amp/settings\.json --mode external-reviewer ")

    def test_amp_reviewer_effort_reaches_the_generated_agent(self) -> None:
        self.fake("amp", "grep -E 'model|reasoningEffort' .amp/plugins/external-reviewer.ts")

        result = self.review("--reviewer", "amp:deepseek/deepseek-v4.1-flash@high", env={"AMP_API_KEY": "amp"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.output.read_text().splitlines()[:3],
            [
                "reviewer: amp:deepseek/deepseek-v4.1-flash@high",
                '\t\tmodel: "deepseek/deepseek-v4.1-flash",',
                '\t\treasoningEffort: "high",',
            ],
        )

    def test_vibe_reviewer_runs_confined_without_the_repository_config(self) -> None:
        self.write(".vibe/tools/repo_tool.py", "import os\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "vibe config")
        seen = self.root / "seen"
        answer = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "No findings."}]}
        self.fake(
            "vibe",
            f'{{ echo "key=${{MISTRAL_API_KEY:-unset}} github=${{GITHUB_TOKEN:-unset}}"; '
            f'echo "outside=$VIBE_OUTSIDE_WORKDIR bypass=$VIBE_BYPASS_TOOL_PERMISSIONS"; ls -A; '
            f'echo "$VIBE_TOOLS"; echo "$@"; }} > {seen}\n'
            f"echo '{json.dumps(answer)}'",
        )

        result = self.review("--reviewer", "vibe", env={"MISTRAL_API_KEY": "m", "GITHUB_TOKEN": "gh"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: vibe\nNo findings.")
        lines = seen.read_text().splitlines()
        self.assertEqual(lines[:2], ["key=m github=unset", "outside=deny bypass=false"])
        self.assertNotIn(".vibe", lines)
        tools = json.loads(next(line for line in lines if line.startswith("{")))
        self.assertEqual(tools["bash"], {"permission": "never"})
        self.assertRegex(lines[-1], r"^--prompt=You are a reviewer.* --workdir \S+/snapshot --agent default ")
        self.assertTrue(lines[-1].endswith("--approval-policy accept-edits --output streaming"), lines[-1])

    def test_vibe_output_keeps_the_last_answer_or_a_trace(self) -> None:
        def entry(kind: str, **fields: object) -> str:
            return json.dumps({"type": kind, **fields})

        texts = [{"type": "text", "text": "Findings."}, {"type": "text", "text": "VERDICT: approve"}]
        events = [
            entry("message", role="user", content=[{"type": "text", "text": "Read the prompt"}]),
            entry("effect", title="file_system.read_file", detail={"display": {"summary": "Reading a.py"}}),
            "Tool call denied: file_system.bash (Tool execution denied by approval policy)",
        ]

        self.assertEqual(
            review.vibe_output(0, "\n".join([*events, entry("message", role="assistant", content=texts)])),
            "Findings.\n\nVERDICT: approve",
        )
        self.assertEqual(
            review.vibe_output(124, "\n".join([*events, "[killed: idle timeout]"])),
            "tool file_system.read_file: Reading a.py\n"
            "Tool call denied: file_system.bash (Tool execution denied by approval policy)\n[killed: idle timeout]",
        )
        self.assertEqual(review.vibe_output(1, "Error: Invalid API key"), "Error: Invalid API key")

    def test_passthrough_keeps_a_named_credential(self) -> None:
        self.fake("claude", 'echo "mistral=${MISTRAL_API_KEY:-unset}"')

        result = self.review(env={"MISTRAL_API_KEY": "m", "EXTERNAL_AGENTS_ENV_PASSTHROUGH": "MISTRAL_API_KEY"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: claude\nmistral=m\n")


class FreeTierAndCompletenessTests(Sandbox):
    def panel(self, spec: str, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output", str(self.output), "--no-preflight",
            "--reviewers", spec, "--idle-timeout", "1", *args,
        )  # fmt: skip

    def test_a_stalled_free_model_is_skipped_for_the_rest_of_the_day(self) -> None:
        self.fake("opencode", 'case "$*" in *a-free*) sleep 30;; *) echo "review by $*";; esac')

        first = self.panel("opencode:p/a-free | opencode:p/b")
        second = self.panel("opencode:p/a-free | opencode:p/b")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("opencode:p/a-free: stalled, likely a free-tier limit", first.stderr)
        self.assertIn("opencode:p/a-free: limited earlier today", second.stderr)
        ledger = json.loads((self.root / "state/external-agents/limits.json").read_text())
        self.assertEqual(list(ledger), ["opencode:p/a-free"])

    def test_a_slot_waiting_for_a_free_model_sees_the_limit_the_other_slot_recorded(self) -> None:
        self.fake("opencode", 'case "$*" in *a-free*) sleep 30;; *) echo "review by $*";; esac')
        spec = "opencode:p/a-free | opencode:p/b"

        result = self.run_script(
            "review", "--prompt-file", str(self.prompt), "--output-dir", str(self.root / "reviews"),
            "--no-preflight", "--reviewers", f"{spec}; {spec}", "--idle-timeout", "1",
        )  # fmt: skip

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("opencode:p/a-free: stalled"), 1, result.stderr)
        self.assertEqual(result.stderr.count("opencode:p/a-free: limited earlier today"), 1, result.stderr)

    def test_an_output_without_the_expected_text_is_kept_but_not_accepted(self) -> None:
        self.fake("claude", "echo 'I looked at things.'")

        result = self.panel("claude", "--expect", "(?i)verdict")

        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            self.output.read_text(),
            "no reviewer available:\nclaude: incomplete (exit 0)\n--- incomplete output ---\nI looked at things.\n",
        )

    def test_an_invalid_expected_pattern_is_a_usage_error(self) -> None:
        self.fake("claude", "echo 'Verdict: fine.'")

        result = self.panel("claude", "--expect", "verdict[")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--expect: not a valid regex", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_silent_run_killed_for_idleness_is_stalled(self) -> None:
        self.assertEqual(review.classify(124, "partial\n[killed: idle timeout]"), "stalled")
        self.assertEqual(review.classify(124, "partial\n[killed: total timeout]"), "timed out")

    def test_a_stalled_amp_reviewer_is_retried_once_and_its_trace_kept(self) -> None:
        # `amp --stream-json` events; the run stalls after its first tool call while the
        # counter file holds a positive number.
        events = {
            "start": [
                {"type": "system", "subtype": "init", "session_id": "T-1"},
                {"type": "assistant", "session_id": "T-1", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "REVIEW_DIFF.patch"}}
                ]}},
            ],
            "end": [{"type": "result", "is_error": False, "result": "Findings.\nVERDICT: approve", "session_id": "T-1"}],
        }  # fmt: skip
        for name, lines in events.items():
            (self.root / name).write_text("".join(json.dumps(line) + "\n" for line in lines))
        counter = self.root / "stalls"
        self.fake(
            "amp",
            f"echo 'Warning: deprecated' >&2\ncat {self.root / 'start'}\nn=$(cat {counter})\n"
            f'if [ "$n" -gt 0 ]; then echo $((n - 1)) > {counter}; sleep 30; fi\ncat {self.root / "end"}',
        )

        counter.write_text("1\n")
        retried = self.panel("amp:p/model")

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn("amp:p/model: stalled (exit 124); retrying once.", retried.stderr)
        self.assertEqual(self.output.read_text(), "reviewer: amp:p/model\nFindings.\nVERDICT: approve")

        counter.write_text("2\n")
        failed = self.panel("amp:p/model")

        self.assertEqual(failed.returncode, 3, failed.stderr)
        self.assertEqual(
            self.output.read_text(),
            "no reviewer available:\namp:p/model: stalled (exit 124)\n  Warning: deprecated\n"
            '  tool Read {"path": "REVIEW_DIFF.patch"}\n  amp thread: T-1\n  [killed: idle timeout]\n',
        )
        log = self.output.with_suffix(".log").read_text()
        self.assertEqual(log.count("[killed: idle timeout]"), 2, log)


class PatchFilterTests(unittest.TestCase):
    def test_quoted_secret_headers_are_filtered(self) -> None:
        patch = (
            "diff --git a/src/lib.rs b/src/lib.rs\n+fn changed() {}\n"
            'diff --git "a/d\\303\\251/.env" "b/d\\303\\251/.env"\n+TOKEN=secret\n'
            'diff --git "a/d\\303\\251/ok.rs" "b/d\\303\\251/ok.rs"\n+fn ok() {}\n'
        )

        diff, names = snapshot.filter_patch(patch)

        self.assertNotIn("TOKEN", diff)
        self.assertEqual(names, "M\tsrc/lib.rs\nM\td\u00e9/ok.rs\n")

    def test_unparsable_header_drops_its_file(self) -> None:
        patch = 'diff --git "a/x.rs" b/.env\n+TOKEN=secret\n'

        self.assertEqual(snapshot.filter_patch(patch), ("", ""))

    def test_a_secret_in_a_plain_unified_diff_is_filtered(self) -> None:
        # `--diff-file` accepts any diff, such as one from `diff -ruN` or another VCS,
        # whose file headers are not `diff --git` lines.
        patch = "--- a/.env\n+++ b/.env\n@@ -0,0 +1 @@\n+TOKEN=secret\n"

        diff, _ = snapshot.filter_patch(patch)

        self.assertNotIn("TOKEN", diff)


class TimeoutTests(unittest.TestCase):
    def test_output_without_a_newline_does_not_defeat_the_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = ["sh", "-c", "printf partial; sleep 30 & wait"]

            code, output = common.run_with_timeouts(command, Path(tmp), total=2, idle=None, env=dict(os.environ))

        self.assertEqual(code, 124)
        self.assertEqual(output, "partial\n[killed: total timeout]")

    def test_closing_output_does_not_defeat_the_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = ["sh", "-c", "echo partial; exec >&- 2>&-; sleep 30"]

            code, output = common.run_with_timeouts(command, Path(tmp), total=2, idle=None, env=dict(os.environ))

        self.assertEqual((code, output), (124, "partial\n\n[killed: total timeout]"))

    def test_lingering_descendant_does_not_discard_a_finished_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = ["sh", "-c", "(sleep 30 &); echo review"]

            code, output = common.run_with_timeouts(command, Path(tmp), total=10, idle=None, env=dict(os.environ))

        self.assertEqual((code, output), (0, "review\n"))

    def test_a_chatty_descendant_does_not_turn_a_finished_run_into_a_timeout(self) -> None:
        # A CLI may leave a child that keeps writing to the inherited stdout after the CLI
        # itself exited with its review.
        with tempfile.TemporaryDirectory() as tmp:
            command = ["sh", "-c", "echo review; (while :; do echo tick; sleep 0.2; done) & exit 0"]

            code, output = common.run_with_timeouts(command, Path(tmp), total=5, idle=None, env=dict(os.environ))

        self.assertEqual(code, 0, output[-200:])
        self.assertTrue(output.startswith("review\n"), output[:200])


if __name__ == "__main__":
    unittest.main()
