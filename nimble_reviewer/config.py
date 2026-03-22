from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from nimble_reviewer.models import ReviewProvider


@dataclass(frozen=True)
class Settings:
    gitlab_url: str
    gitlab_token: str
    gitlab_webhook_secret: str
    codex_cmd: tuple[str, ...]
    claude_cmd: tuple[str, ...]
    council_synthesis_provider: ReviewProvider
    discussion_reconcile_provider: ReviewProvider
    sqlite_path: Path
    repo_cache_dir: Path
    review_trace_dir: Path
    review_timeout_sec: int
    max_concurrent_reviews: int
    poll_interval_sec: float
    gitlab_git_username: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gitlab_url=_read_required("GITLAB_URL").rstrip("/"),
            gitlab_token=_read_required("GITLAB_TOKEN"),
            gitlab_webhook_secret=_read_required("GITLAB_WEBHOOK_SECRET"),
            codex_cmd=_read_command(
                "CODEX_CMD",
                default='codex exec -m gpt-5.4 -c model_reasoning_effort="high" -',
            ),
            claude_cmd=_read_command(
                "CLAUDE_CMD",
                default="claude -p --output-format stream-json --model sonnet --effort high --permission-mode bypassPermissions",
            ),
            council_synthesis_provider=_read_review_provider("COUNCIL_SYNTHESIS_PROVIDER", default="codex"),
            discussion_reconcile_provider=_read_review_provider(
                "DISCUSSION_RECONCILE_PROVIDER",
                default=_read_review_provider("COUNCIL_SYNTHESIS_PROVIDER", default="codex"),
            ),
            sqlite_path=Path(_read_required("SQLITE_PATH")),
            repo_cache_dir=Path(_read_required("REPO_CACHE_DIR")),
            review_trace_dir=Path(os.getenv("REVIEW_TRACE_DIR", "/data/review-traces")),
            review_timeout_sec=int(os.getenv("REVIEW_TIMEOUT_SEC", "600")),
            max_concurrent_reviews=max(1, int(os.getenv("MAX_CONCURRENT_REVIEWS", "1"))),
            poll_interval_sec=float(os.getenv("POLL_INTERVAL_SEC", "1.0")),
            gitlab_git_username=os.getenv("GITLAB_GIT_USERNAME", "oauth2"),
            port=int(os.getenv("PORT", "8080")),
        )


def _read_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _read_review_provider(name: str, default: ReviewProvider) -> ReviewProvider:
    raw = os.getenv(name, default).strip().lower()
    if raw not in {"codex", "claude"}:
        raise RuntimeError(f"Invalid {name}. Expected one of: codex, claude")
    return raw  # type: ignore[return-value]


def _read_command(env_name: str, default: str | None = None) -> tuple[str, ...]:
    value = os.getenv(env_name, default or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return tuple(shlex.split(value))

