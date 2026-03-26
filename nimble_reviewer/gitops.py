from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from urllib.parse import quote, urlsplit, urlunsplit

from nimble_reviewer.models import PreparedCheckout
from nimble_reviewer.review_rules import load_repo_review_rules
from nimble_reviewer.trace import RunTrace

LOGGER = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


class RepoManager:
    def __init__(self, cache_dir: Path, git_username: str, token: str) -> None:
        self.cache_dir = cache_dir
        self.git_username = git_username
        self.token = token

    def prepare_checkout(
        self,
        repo_http_url: str,
        source_sha: str,
        target_branch: str,
        trace: RunTrace | None = None,
        previous_reviewed_sha: str | None = None,
    ) -> PreparedCheckout:
        LOGGER.info(
            "Preparing checkout repo=%s sha=%s target_branch=%s",
            repo_http_url,
            source_sha[:12],
            target_branch,
        )
        if trace:
            trace.write(
                "git",
                "prepare_checkout.started",
                repo=repo_http_url,
                source_sha=source_sha[:12],
                target_branch=target_branch,
            )
        mirror_dir = self._ensure_mirror(repo_http_url)
        checkout_dir = Path(mkdtemp(prefix="review-", dir=self.cache_dir))
        self._run_git(["clone", str(mirror_dir), str(checkout_dir)], cwd=self.cache_dir)
        self._run_git(["checkout", "--detach", source_sha], cwd=checkout_dir)
        self._run_git(["fetch", "origin", target_branch], cwd=checkout_dir)

        merge_base = self._capture_git(["merge-base", "HEAD", f"origin/{target_branch}"], cwd=checkout_dir).strip()
        diff_text = self._capture_git(["diff", "--unified=3", f"{merge_base}..HEAD"], cwd=checkout_dir)
        changed_files = [
            line.strip()
            for line in self._capture_git(["diff", "--name-only", f"{merge_base}..HEAD"], cwd=checkout_dir).splitlines()
            if line.strip()
        ]
        incremental_diff_text: str | None = None
        if previous_reviewed_sha and previous_reviewed_sha != source_sha:
            try:
                incremental_diff_text = self._capture_git(
                    ["diff", "--unified=3", f"{previous_reviewed_sha}..HEAD"],
                    cwd=checkout_dir,
                )
            except GitError:
                LOGGER.warning(
                    "Could not compute incremental diff from %s to %s; skipping",
                    previous_reviewed_sha[:12],
                    source_sha[:12],
                )

        review_rules = load_repo_review_rules(checkout_dir)
        LOGGER.info(
            "Prepared checkout dir=%s merge_base=%s changed_files=%s diff_bytes=%s review_rules=%s",
            checkout_dir,
            merge_base[:12],
            len(changed_files),
            len(diff_text.encode("utf-8")),
            review_rules.path if review_rules else "-",
        )
        if trace:
            trace.write(
                "git",
                "prepare_checkout.completed",
                checkout_dir=str(checkout_dir),
                merge_base=merge_base[:12],
                changed_files=changed_files,
                diff_bytes=len(diff_text.encode("utf-8")),
                review_rules_path=review_rules.path if review_rules else None,
            )

        return PreparedCheckout(
            path=checkout_dir,
            merge_base=merge_base,
            diff_text=diff_text,
            changed_files=changed_files,
            review_rules_path=review_rules.path if review_rules else None,
            review_rules_text=review_rules.text if review_rules else None,
            review_rules_truncated=review_rules.truncated if review_rules else False,
            cleanup=lambda: shutil.rmtree(checkout_dir, ignore_errors=True),
            incremental_diff_text=incremental_diff_text or None,
            previous_reviewed_sha=previous_reviewed_sha,
        )

    def _ensure_mirror(self, repo_http_url: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        mirror_dir = self.cache_dir / hashlib.sha1(repo_http_url.encode("utf-8")).hexdigest()
        auth_url = _authenticated_url(repo_http_url, self.git_username, self.token)

        if not mirror_dir.exists():
            LOGGER.info("Creating repository mirror repo=%s mirror_dir=%s", repo_http_url, mirror_dir)
            self._run_git(["clone", "--mirror", auth_url, str(mirror_dir)], cwd=self.cache_dir)
            return mirror_dir

        LOGGER.info("Refreshing repository mirror repo=%s mirror_dir=%s", repo_http_url, mirror_dir)
        self._run_git(["remote", "set-url", "origin", auth_url], cwd=mirror_dir)
        self._run_git(["fetch", "--prune", "origin"], cwd=mirror_dir)
        return mirror_dir

    @staticmethod
    def _run_git(args: list[str], cwd: Path) -> None:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env={"GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")

    @staticmethod
    def _capture_git(args: list[str], cwd: Path) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env={"GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")
        return completed.stdout


def _authenticated_url(repo_http_url: str, username: str, token: str) -> str:
    parsed = urlsplit(repo_http_url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    auth = f"{quote(username, safe='')}:{quote(token, safe='')}"
    return urlunsplit((parsed.scheme, f"{auth}@{netloc}", parsed.path, parsed.query, parsed.fragment))
