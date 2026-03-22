"""Tests for nimble_reviewer.gitops — RepoManager and helpers.

subprocess.run is mocked so no real git binary is required.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from nimble_reviewer.gitops import GitError, RepoManager, _authenticated_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# _authenticated_url (unit-level — duplicated here for gitops-specific view)
# ---------------------------------------------------------------------------


class AuthenticatedUrlTests(unittest.TestCase):
    def test_embeds_credentials(self):
        url = _authenticated_url("https://gitlab.example.com/group/repo.git", "ci-bot", "token123")
        self.assertIn("ci-bot:token123@gitlab.example.com", url)

    def test_encodes_special_chars(self):
        url = _authenticated_url("https://gitlab.example.com/repo.git", "u", "p@ss!")
        self.assertNotIn("p@ss!", url)
        self.assertIn("p%40ss%21", url)

    def test_preserves_port(self):
        url = _authenticated_url("https://gitlab.example.com:9090/repo.git", "u", "p")
        self.assertIn(":9090", url)


# ---------------------------------------------------------------------------
# RepoManager._run_git / _capture_git error handling
# ---------------------------------------------------------------------------


class RunGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = RepoManager(
            cache_dir=Path(self.tmp.name),
            git_username="user",
            token="token",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_git_raises_on_nonzero(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="fatal: not a repo")):
            with self.assertRaises(GitError) as ctx:
                self.manager._run_git(["status"], Path(self.tmp.name))
        self.assertIn("not a repo", str(ctx.exception))

    def test_run_git_succeeds_silently(self):
        with patch("subprocess.run", return_value=_completed(returncode=0)):
            # Should not raise
            self.manager._run_git(["status"], Path(self.tmp.name))

    def test_capture_git_returns_stdout(self):
        with patch("subprocess.run", return_value=_completed(returncode=0, stdout="abc123\n")):
            result = self.manager._capture_git(["rev-parse", "HEAD"], Path(self.tmp.name))
        self.assertEqual(result, "abc123\n")

    def test_capture_git_raises_on_nonzero(self):
        with patch("subprocess.run", return_value=_completed(returncode=128, stderr="ambiguous ref")):
            with self.assertRaises(GitError):
                self.manager._capture_git(["rev-parse", "HEAD"], Path(self.tmp.name))

    def test_run_git_uses_stderr_before_stdout(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="err msg", stdout="out msg")):
            with self.assertRaises(GitError) as ctx:
                self.manager._run_git(["bad"], Path(self.tmp.name))
        self.assertIn("err msg", str(ctx.exception))

    def test_run_git_falls_back_to_stdout_when_stderr_empty(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="", stdout="out msg")):
            with self.assertRaises(GitError) as ctx:
                self.manager._run_git(["bad"], Path(self.tmp.name))
        self.assertIn("out msg", str(ctx.exception))


# ---------------------------------------------------------------------------
# RepoManager._ensure_mirror
# ---------------------------------------------------------------------------


class EnsureMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = RepoManager(
            cache_dir=Path(self.tmp.name),
            git_username="user",
            token="token",
        )
        self.repo_url = "https://gitlab.example.com/group/repo.git"

    def tearDown(self):
        self.tmp.cleanup()

    def _mirror_dir(self) -> Path:
        import hashlib
        return Path(self.tmp.name) / hashlib.sha1(self.repo_url.encode()).hexdigest()

    def test_clones_when_mirror_does_not_exist(self):
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            self.manager._ensure_mirror(self.repo_url)

        commands = [call_args.args[0] for call_args in mock_run.call_args_list]
        self.assertTrue(any("clone" in cmd and "--mirror" in cmd for cmd in commands))

    def test_fetches_when_mirror_already_exists(self):
        mirror = self._mirror_dir()
        mirror.mkdir(parents=True)
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            self.manager._ensure_mirror(self.repo_url)

        commands = [call_args.args[0] for call_args in mock_run.call_args_list]
        self.assertTrue(any("fetch" in cmd for cmd in commands))
        self.assertFalse(any("clone" in cmd and "--mirror" in cmd for cmd in commands))

    def test_updates_remote_url_on_refresh(self):
        mirror = self._mirror_dir()
        mirror.mkdir(parents=True)
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            self.manager._ensure_mirror(self.repo_url)

        commands = [call_args.args[0] for call_args in mock_run.call_args_list]
        self.assertTrue(any("set-url" in cmd for cmd in commands))


# ---------------------------------------------------------------------------
# RepoManager.prepare_checkout
# ---------------------------------------------------------------------------


class PrepareCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = RepoManager(
            cache_dir=Path(self.tmp.name),
            git_username="user",
            token="token",
        )
        self.repo_url = "https://gitlab.example.com/group/repo.git"

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_run(self, mock_run: MagicMock):
        """Configure subprocess.run to return sensible values for git commands."""
        def side_effect(args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if cmd == "merge-base":
                return _completed(stdout="merge-base-sha\n")
            if cmd == "diff" and "--unified=3" in args:
                return _completed(stdout="diff --git a/file.py b/file.py\n+added\n")
            if cmd == "diff" and "--name-only" in args:
                return _completed(stdout="file.py\n")
            return _completed()
        mock_run.side_effect = side_effect

    def test_returns_prepared_checkout(self):
        with patch("subprocess.run") as mock_run:
            self._patch_run(mock_run)
            checkout = self.manager.prepare_checkout(
                repo_http_url=self.repo_url,
                source_sha="abc123",
                target_branch="main",
            )

        self.assertIn("file.py", checkout.changed_files)
        self.assertIn("+added", checkout.diff_text)
        self.assertEqual(checkout.merge_base, "merge-base-sha")
        self.assertIsNone(checkout.review_rules_text)
        # Cleanup should not raise
        checkout.cleanup()

    def test_omits_review_rules_when_file_absent(self):
        with patch("subprocess.run") as mock_run:
            self._patch_run(mock_run)
            checkout = self.manager.prepare_checkout(
                repo_http_url=self.repo_url,
                source_sha="abc123",
                target_branch="main",
            )

        self.assertIsNone(checkout.review_rules_path)

    def test_cleanup_removes_checkout_dir(self):
        with patch("subprocess.run") as mock_run:
            self._patch_run(mock_run)
            checkout = self.manager.prepare_checkout(
                repo_http_url=self.repo_url,
                source_sha="abc123",
                target_branch="main",
            )
        checkout_path = checkout.path
        checkout.cleanup()
        self.assertFalse(checkout_path.exists())

    def test_git_error_propagates(self):
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="Permission denied")):
            with self.assertRaises(GitError):
                self.manager.prepare_checkout(
                    repo_http_url=self.repo_url,
                    source_sha="abc123",
                    target_branch="main",
                )

    def test_trace_is_written_when_provided(self):
        from nimble_reviewer.trace import RunTrace

        trace_path = Path(self.tmp.name) / "trace.jsonl"
        trace = RunTrace(trace_path)

        with patch("subprocess.run") as mock_run:
            self._patch_run(mock_run)
            checkout = self.manager.prepare_checkout(
                repo_http_url=self.repo_url,
                source_sha="abc123",
                target_branch="main",
                trace=trace,
            )
        checkout.cleanup()

        self.assertTrue(trace_path.exists())
        events = [line for line in trace_path.read_text().splitlines() if line.strip()]
        self.assertTrue(any("prepare_checkout.started" in e for e in events))
        self.assertTrue(any("prepare_checkout.completed" in e for e in events))


if __name__ == "__main__":
    unittest.main()
