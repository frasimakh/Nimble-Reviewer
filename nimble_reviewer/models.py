from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["high", "medium", "low"]
RunStatus = Literal["queued", "running", "done", "failed", "superseded"]
ReviewProvider = Literal["codex", "claude"]
ReviewOpinionVerdict = Literal["found", "agree", "disagree", "uncertain"]
FindingStatus = Literal["new", "still_present"]


@dataclass(frozen=True)
class ReviewOpinion:
    provider: ReviewProvider
    verdict: ReviewOpinionVerdict
    reason: str | None = None


@dataclass(frozen=True)
class ReviewFinding:
    severity: Severity
    file: str
    line: int
    title: str
    body: str
    suggestion: str | None = None
    sources: tuple[ReviewProvider, ...] = field(default_factory=tuple)
    opinions: tuple[ReviewOpinion, ...] = field(default_factory=tuple)
    snippet: str | None = None
    snippet_start_line: int | None = None
    snippet_language: str | None = None


@dataclass(frozen=True)
class ReviewTokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cost_usd: float | None = None
    cached_input_included_in_input: bool = True
    cache_creation_included_in_input: bool = True

    @property
    def total_tokens(self) -> int:
        total = self.input_tokens + self.output_tokens
        if not self.cached_input_included_in_input:
            total += self.cached_input_tokens
        if not self.cache_creation_included_in_input:
            total += self.cache_creation_input_tokens
        return total


@dataclass(frozen=True)
class ReviewAgentMetadata:
    provider: ReviewProvider
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ReviewParticipant:
    metadata: ReviewAgentMetadata
    phases: tuple[str, ...] = field(default_factory=tuple)
    token_usage: ReviewTokenUsage | None = None
    summary: str | None = None
    overall_risk: Severity | None = None


@dataclass(frozen=True)
class ReviewResult:
    summary: str
    overall_risk: Severity
    findings: tuple[ReviewFinding, ...] = field(default_factory=tuple)
    token_usage: ReviewTokenUsage | None = None
    agent_metadata: ReviewAgentMetadata | None = None
    participants: tuple[ReviewParticipant, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReviewFindingState:
    finding: ReviewFinding
    status: FindingStatus


@dataclass(frozen=True)
class ReviewComparison:
    current_findings: tuple[ReviewFindingState, ...] = field(default_factory=tuple)
    resolved_findings: tuple[ReviewFinding, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MergeRequestInfo:
    project_id: int
    mr_iid: int
    title: str
    description: str
    source_branch: str
    target_branch: str
    source_sha: str
    web_url: str
    repo_http_url: str


@dataclass(frozen=True)
class ReviewRun:
    id: int
    project_id: int
    mr_iid: int
    source_sha: str
    target_sha: str | None
    status: RunStatus
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    superseded_by: int | None


@dataclass(frozen=True)
class MergeRequestState:
    project_id: int
    mr_iid: int
    note_id: int | None
    last_seen_sha: str | None
    last_reviewed_sha: str | None
    status: str
    updated_at: str


@dataclass(frozen=True)
class EnqueueDecision:
    enqueued: bool
    reason: str
    run_id: int | None = None


@dataclass(frozen=True)
class MergeRequestEvent:
    project_id: int
    mr_iid: int
    source_sha: str
    target_sha: str | None
    action: str


@dataclass
class PreparedCheckout:
    path: Path
    merge_base: str
    diff_text: str
    changed_files: list[str]
    cleanup: callable
    review_rules_path: str | None = None
    review_rules_text: str | None = None
    review_rules_truncated: bool = False

    def close(self) -> None:
        self.cleanup()
