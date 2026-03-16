from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ReviewAgentProvider = Literal["codex", "claude"]


@dataclass(frozen=True)
class Settings:
    gitlab_url: str
    gitlab_token: str
    gitlab_webhook_secret: str
    review_agent_provider: ReviewAgentProvider
    codex_cmd: tuple[str, ...] | None
    claude_cmd: tuple[str, ...] | None
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
        provider = _read_review_agent_provider()
        return cls(
            gitlab_url=_read_required("GITLAB_URL").rstrip("/"),
            gitlab_token=_read_required("GITLAB_TOKEN"),
            gitlab_webhook_secret=_read_required("GITLAB_WEBHOOK_SECRET"),
            review_agent_provider=provider,
            codex_cmd=_read_command_if_selected("CODEX_CMD", provider, "codex"),
            claude_cmd=_read_command_if_selected("CLAUDE_CMD", provider, "claude"),
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


def _read_review_agent_provider() -> ReviewAgentProvider:
    raw = os.getenv("REVIEW_AGENT_PROVIDER", "codex").strip().lower()
    if raw not in {"codex", "claude"}:
        raise RuntimeError(
            "Invalid REVIEW_AGENT_PROVIDER. Expected one of: codex, claude"
        )
    return raw  # type: ignore[return-value]


def _read_command_if_selected(
    env_name: str,
    selected_provider: ReviewAgentProvider,
    expected_provider: ReviewAgentProvider,
) -> tuple[str, ...] | None:
    value = os.getenv(env_name)
    if selected_provider != expected_provider:
        return tuple(shlex.split(value)) if value else None
    if not value:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return tuple(shlex.split(value))
