from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["high", "medium", "low"]
RunStatus = Literal["queued", "running", "done", "failed", "superseded"]
RunKind = Literal["full_review", "discussion_reconcile"]
ReviewProvider = Literal["codex", "claude"]
ReviewOpinionVerdict = Literal["found", "agree", "disagree", "uncertain"]
FindingStatus = Literal["new", "still_present"]
FindingPlacement = Literal["inline", "summary"]
TrackedFindingStatus = Literal["open", "resolved", "dismissed_by_discussion"]
ThreadOwner = Literal["bot", "human", "summary-only"]
ReconcileDecision = Literal["keep_open", "dismissed_by_discussion", "reply_only", "no_action"]


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
    placement: FindingPlacement = "inline"
    discussion_id: str | None = None
    thread_owner: ThreadOwner | None = None


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
    kind: RunKind
    source_sha: str | None
    target_sha: str | None
    status: RunStatus
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    superseded_by: int | None
    trigger_discussion_id: str | None
    trigger_note_id: int | None
    trigger_author_id: int | None


@dataclass(frozen=True)
class MergeRequestState:
    project_id: int
    mr_iid: int
    summary_note_id: int | None
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
class ReviewRequestEvent:
    project_id: int
    mr_iid: int
    kind: RunKind
    source_sha: str | None
    target_sha: str | None
    action: str
    trigger_discussion_id: str | None = None
    trigger_note_id: int | None = None
    trigger_author_id: int | None = None


@dataclass(frozen=True)
class TrackedFinding:
    project_id: int
    mr_iid: int
    fingerprint: str
    status: TrackedFindingStatus
    severity: Severity
    file: str
    line: int
    title: str
    body: str
    suggestion: str | None = None
    sources: tuple[ReviewProvider, ...] = field(default_factory=tuple)
    discussion_id: str | None = None
    root_note_id: int | None = None
    thread_owner: ThreadOwner = "summary-only"
    opened_sha: str | None = None
    last_seen_sha: str | None = None
    resolved_sha: str | None = None
    dismissed_sha: str | None = None
    context_snippet: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ReviewSummaryMetrics:
    open_count: int
    new_count: int
    still_present_count: int
    resolved_count: int
    dismissed_count: int
    unplaced_count: int


@dataclass(frozen=True)
class DiscussionReconcileResult:
    decision: ReconcileDecision
    reason: str
    reply_body: str | None = None


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
    incremental_diff_text: str | None = None
    previous_reviewed_sha: str | None = None

    def close(self) -> None:
        self.cleanup()
