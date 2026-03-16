from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

RULE_FILE_PATH = Path("NIMBLE-REVIEWER.MD")
MAX_RULE_CHARS = 20_000


@dataclass(frozen=True)
class RepoReviewRules:
    path: str
    text: str
    truncated: bool


def load_repo_review_rules(repo_root: Path) -> RepoReviewRules | None:
    candidate = repo_root / RULE_FILE_PATH
    if not candidate.is_file():
        return None

    text = candidate.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None

    truncated = False
    if len(text) > MAX_RULE_CHARS:
        text = text[:MAX_RULE_CHARS]
        truncated = True

    return RepoReviewRules(
        path=RULE_FILE_PATH.as_posix(),
        text=text,
        truncated=truncated,
    )
