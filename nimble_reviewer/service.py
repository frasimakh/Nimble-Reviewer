from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace as dc_replace
from pathlib import Path

from nimble_reviewer.diff_mapping import DiffMapping, build_diff_mapping
from nimble_reviewer.finding_match import finding_fingerprint, findings_match
from nimble_reviewer.gitlab import GitLabClient, GitLabDiscussion, GitLabError
from nimble_reviewer.gitops import RepoManager
from nimble_reviewer.models import (
    DiscussionReconcileResult,
    ReviewAgentMetadata,
    ReviewComparison,
    ReviewFinding,
    ReviewFindingState,
    ReviewOpinion,
    ReviewParticipant,
    ReviewResult,
    ReviewRun,
    ReviewTokenUsage,
    ThreadOwner,
    TrackedFinding,
)
from nimble_reviewer.prompts import (
    HUNK_CONTEXT_LINES,
    MAX_HUNK_CONTEXT_CHARS_PER_FILE,
    MAX_HUNK_CONTEXT_FILES,
    build_discussion_reconcile_prompt,
    build_review_prompt,
)
from nimble_reviewer.review_agent import JsonCapableReviewAgent, ReviewAgentRunner, _review_result_from_payload
from nimble_reviewer.store import Store
from nimble_reviewer.trace import RunTrace, TraceSettings

LOGGER = logging.getLogger(__name__)

FINDING_MARKER_PREFIX = "<!-- nimble-reviewer:finding:"
STILL_PRESENT_MARKER_PREFIX = "<!-- nimble-reviewer:still-present:"


@dataclass(frozen=True)
class ServiceDependencies:
    store: Store
    gitlab: GitLabClient
    repo_manager: RepoManager
    review_agent: ReviewAgentRunner
    discussion_reconcile_agent: JsonCapableReviewAgent
    trace_settings: TraceSettings


@dataclass(frozen=True)
class _PublishedFinding:
    finding: ReviewFinding
    placement: str
    discussion_id: str | None
    thread_owner: ThreadOwner


@dataclass(frozen=True)
class _PublishedReviewState:
    current_states: tuple[ReviewFindingState, ...]


class _StaleRunDuringPublish(RuntimeError):
    pass


class ReviewService:
    """Orchestrates the full review lifecycle for a single queued run.

    Handles two run kinds:
    - ``full_review``: checkout → LLM review → compare vs previous → publish
      inline discussions + summary note.
    - ``discussion_reconcile``: re-evaluate an open finding thread after a
      human reply and decide whether to dismiss, reply, or keep open.
    """

    def __init__(self, deps: ServiceDependencies) -> None:
        self.store = deps.store
        self.gitlab = deps.gitlab
        self.repo_manager = deps.repo_manager
        self.review_agent = deps.review_agent
        self.discussion_reconcile_agent = deps.discussion_reconcile_agent
        self.trace_settings = deps.trace_settings

    def process_run(self, run: ReviewRun) -> None:
        """Dispatch *run* to the appropriate handler based on ``run.kind``.

        Exits immediately if the run is no longer in ``running`` state (e.g.
        superseded by a newer push before the worker claimed it).
        """
        if self.store.get_run_status(run.id) != "running":
            LOGGER.info("Skipping run %s because it is no longer running", run.id)
            return
        if run.kind == "discussion_reconcile":
            self._process_discussion_reconcile_run(run)
            return
        self._process_full_review_run(run)

    def _process_full_review_run(self, run: ReviewRun) -> None:
        """Execute a full MR review: checkout → review → publish.

        Checks staleness twice — once before the expensive LLM call and once
        immediately before publishing — and aborts with ``superseded`` if the
        MR head has moved on either check.  On any unhandled error the run is
        marked ``failed``.
        """
        LOGGER.info(
            "Starting full review run_id=%s project=%s mr=%s sha=%s",
            run.id,
            run.project_id,
            run.mr_iid,
            _short_sha(run.source_sha),
        )
        trace = self._create_trace(run)
        if trace:
            trace.write(
                "app",
                "review.started",
                run_id=run.id,
                run_kind=run.kind,
                project_id=run.project_id,
                mr_iid=run.mr_iid,
                source_sha=_short_sha(run.source_sha),
            )
        try:
            started = time.monotonic()
            mr_info = self.gitlab.get_merge_request_info(run.project_id, run.mr_iid)
            if run.source_sha and mr_info.source_sha != run.source_sha:
                self.store.mark_superseded_if_running(run.id)
                LOGGER.info("Run %s is stale; GitLab head is now %s", run.id, mr_info.source_sha)
                return

            previous_full_review = self.store.get_latest_done_run(run.project_id, run.mr_iid, kind="full_review")
            previous_reviewed_sha = previous_full_review.source_sha if previous_full_review else None

            checkout = self.repo_manager.prepare_checkout(
                mr_info.repo_http_url,
                mr_info.source_sha,
                mr_info.target_branch,
                trace=trace,
                previous_reviewed_sha=previous_reviewed_sha,
            )
            try:
                discussions = self.gitlab.list_merge_request_discussions(run.project_id, run.mr_iid)
                tracked_findings = self.store.list_tracked_findings(run.project_id, run.mr_iid)
                discussion_digest = _build_discussion_digest(discussions, checkout.changed_files, tracked_findings)
                discussion_inventory = _build_discussion_inventory(discussions)
                hunk_context = _build_hunk_context(checkout.diff_text, checkout.path)
                prompt = build_review_prompt(
                    mr_info,
                    checkout.diff_text,
                    checkout.changed_files,
                    discussion_digest=discussion_digest,
                    discussion_inventory=discussion_inventory or None,
                    repo_rules_text=checkout.review_rules_text,
                    repo_rules_path=checkout.review_rules_path,
                    repo_rules_truncated=checkout.review_rules_truncated,
                    incremental_diff_text=checkout.incremental_diff_text,
                    previous_reviewed_sha=checkout.previous_reviewed_sha,
                    hunk_context=hunk_context,
                )
                result = self.review_agent.review(prompt, checkout.path, trace=trace)
                result = _enrich_result_for_rendering(result, checkout.path)
                diff_mapping = build_diff_mapping(checkout.diff_text)
                result = _suppress_dismissed_findings(result, tracked_findings, diff_mapping)
                previous_result = self._load_previous_success_result(
                    run,
                    kind="full_review",
                    previous_run=previous_full_review,
                )
                comparison = _build_review_comparison(previous_result, result)
                if trace:
                    trace.write_snapshot("review.final", _review_result_snapshot_payload(result))
            finally:
                checkout.close()

            if self.store.get_run_status(run.id) != "running":
                LOGGER.info("Run %s was superseded before publish", run.id)
                return
            current_head_sha = self.gitlab.get_merge_request_head_sha(run.project_id, run.mr_iid)
            stale_before_publish = current_head_sha != mr_info.source_sha
            if stale_before_publish:
                LOGGER.info(
                    "Run %s became stale before publish (head now %s); publishing summary-only",
                    run.id,
                    _short_sha(current_head_sha),
                )

            current_discussions = self.gitlab.list_merge_request_discussions(run.project_id, run.mr_iid)
            latest_version = None if stale_before_publish else self.gitlab.get_latest_merge_request_version(run.project_id, run.mr_iid)
            try:
                publication = self._publish_review_findings(
                    run_id=run.id,
                    project_id=run.project_id,
                    mr_iid=run.mr_iid,
                    source_sha=mr_info.source_sha,
                    result=result,
                    comparison=comparison,
                    tracked_findings=tracked_findings,
                    discussions=current_discussions,
                    diff_mapping=diff_mapping,
                    latest_version=latest_version,
                    force_summary_only=stale_before_publish,
                )
            except _StaleRunDuringPublish:
                self.store.mark_superseded_if_running(run.id)
                LOGGER.info("Run %s became stale during inline publish", run.id)
                return

            self.store.mark_done(run.id, run.project_id, run.mr_iid, mr_info.source_sha)
            LOGGER.info(
                "Completed full review run_id=%s total_sec=%.2f",
                run.id,
                time.monotonic() - started,
            )
            if trace:
                trace.write(
                    "app",
                    "review.completed",
                    run_id=run.id,
                    run_kind=run.kind,
                    total_sec=round(time.monotonic() - started, 3),
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Full review run %s failed", run.id)
            if trace:
                trace.write("app", "review.failed", run_id=run.id, run_kind=run.kind, error=str(exc))
            if not self.store.mark_failed(run.id, run.project_id, run.mr_iid, run.source_sha, str(exc)):
                return
            pass

    def _process_discussion_reconcile_run(self, run: ReviewRun) -> None:
        """Handle a follow-up review triggered by a human reply in a thread.

        Looks up the tracked finding linked to *run.trigger_discussion_id*,
        asks the reconcile agent for a decision, then either dismisses the
        finding, adds a bot reply, or leaves it open.
        """
        LOGGER.info(
            "Starting discussion reconcile run_id=%s project=%s mr=%s discussion=%s note=%s provider=%s",
            run.id,
            run.project_id,
            run.mr_iid,
            run.trigger_discussion_id or "-",
            run.trigger_note_id or "-",
            self.discussion_reconcile_agent.provider_name,
        )
        trace = self._create_trace(run)
        try:
            mr_info = self.gitlab.get_merge_request_info(run.project_id, run.mr_iid)
            discussions = self.gitlab.list_merge_request_discussions(run.project_id, run.mr_iid)
            discussion = _find_discussion(discussions, run.trigger_discussion_id, run.trigger_note_id)
            if discussion is None:
                self.store.mark_done(run.id, run.project_id, run.mr_iid, mr_info.source_sha)
                return

            tracked_findings = self.store.list_tracked_findings(run.project_id, run.mr_iid)
            tracked = self.store.get_tracked_finding_by_discussion(run.project_id, run.mr_iid, discussion.id)
            if tracked is None:
                tracked = _match_discussion_to_tracked_finding(discussion, tracked_findings)
            if tracked is None or tracked.status != "open":
                self.store.mark_done(run.id, run.project_id, run.mr_iid, mr_info.source_sha)
                return

            checkout = self.repo_manager.prepare_checkout(
                mr_info.repo_http_url,
                mr_info.source_sha,
                mr_info.target_branch,
                trace=trace,
            )
            try:
                trigger_note = _find_note_in_discussion(discussion, run.trigger_note_id)
                prompt = build_discussion_reconcile_prompt(
                    mr_info,
                    discussion_id=discussion.id,
                    discussion_text=_render_discussion_context(discussion),
                    trigger_note_body=(trigger_note.body if trigger_note else discussion.root_note.body if discussion.root_note else ""),
                    linked_finding_payload=_tracked_finding_payload(tracked),
                    diff_text=checkout.diff_text,
                    finding_file=tracked.file,
                    repo_rules_text=checkout.review_rules_text,
                    repo_rules_path=checkout.review_rules_path,
                )
                payload, _ = self.discussion_reconcile_agent.run_json(prompt, checkout.path, trace=trace)
            finally:
                checkout.close()
            decision = _parse_discussion_reconcile_result(payload)
            if trace:
                trace.write_snapshot("discussion.reconcile", payload)

            if decision.reply_body:
                self.gitlab.add_discussion_note(run.project_id, run.mr_iid, discussion.id, _reply_with_marker(decision.reply_body, tracked.fingerprint, self.discussion_reconcile_agent.provider_name))
            if decision.decision == "dismissed_by_discussion":
                tracked = dc_replace(tracked, status="dismissed_by_discussion", dismissed_sha=mr_info.source_sha, updated_at=None)
                self.store.upsert_tracked_finding(tracked)
                if tracked.thread_owner == "bot" and not discussion.resolved:
                    self.gitlab.set_discussion_resolved(run.project_id, run.mr_iid, discussion.id, resolved=True)
            elif decision.decision in {"reply_only", "keep_open"}:
                tracked = dc_replace(tracked, last_seen_sha=mr_info.source_sha, updated_at=None)
                self.store.upsert_tracked_finding(tracked)

            self.store.mark_done(run.id, run.project_id, run.mr_iid, mr_info.source_sha)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Discussion reconcile run %s failed", run.id)
            if not self.store.mark_failed(run.id, run.project_id, run.mr_iid, run.source_sha, str(exc)):
                return

    def _publish_review_findings(
        self,
        *,
        run_id: int,
        project_id: int,
        mr_iid: int,
        source_sha: str,
        result: ReviewResult,
        comparison: ReviewComparison,
        tracked_findings: list[TrackedFinding],
        discussions: list[GitLabDiscussion],
        diff_mapping: DiffMapping,
        latest_version,
        force_summary_only: bool = False,
    ) -> _PublishedReviewState:
        """Place each finding into a GitLab discussion thread and update the store.

        For each finding in *result*:
        1. Re-uses an existing tracked thread when the fingerprint matches.
        2. Appends to an existing human thread when the file/line is close.
        3. Creates a new inline diff discussion via the GitLab API.
        4. Falls back to ``summary-only`` placement when no diff position is
           available or inline creation fails because the MR head is stable.

        When *force_summary_only* is ``True`` all new findings are placed as
        ``summary-only`` — no diff position lookup and no inline discussion
        creation.  Use this when the MR head advanced after the review
        completed (diff positions are no longer valid) but the findings
        themselves are still worth publishing.

        Raises ``_StaleRunDuringPublish`` if the MR head changes mid-publish
        (caller marks the run superseded and aborts without posting a note).
        Also resolves any bot-owned threads whose findings no longer appear in
        *result*.
        """
        bot_user_id = self.gitlab.get_current_user().id
        discussions_by_id = {discussion.id: discussion for discussion in discussions}
        used_tracked: set[str] = set()
        current_states: list[ReviewFindingState] = []
        current_open_fingerprints: set[str] = set()

        for finding_state in comparison.current_findings:
            finding = finding_state.finding
            fp = finding_fingerprint(finding)
            tracked = _match_tracked_finding(
                finding,
                tracked_findings,
                used_fingerprints=used_tracked,
                diff_mapping=diff_mapping,
            )
            placement = "summary"
            discussion_id = None
            thread_owner: ThreadOwner = "summary-only"

            if tracked and tracked.discussion_id:
                discussion = discussions_by_id.get(tracked.discussion_id)
                if discussion and tracked.thread_owner != "summary-only":
                    placement = "inline"
                    discussion_id = tracked.discussion_id
                    thread_owner = tracked.thread_owner
                    if tracked.thread_owner == "bot" and discussion.resolved:
                        self.gitlab.set_discussion_resolved(project_id, mr_iid, discussion.id, resolved=False)
            if tracked and tracked.thread_owner == "summary-only":
                tracked = None

            if tracked is None:
                position = None if force_summary_only else diff_mapping.to_position(finding.file, finding.line, latest_version)
                created_discussion = None
                if position is not None:
                    created_discussion = self._create_inline_discussion_or_none(
                        run_id=run_id,
                        project_id=project_id,
                        mr_iid=mr_iid,
                        source_sha=source_sha,
                        finding=finding,
                        fingerprint=fp,
                        position=position,
                    )
                if created_discussion is not None:
                    discussion_id = created_discussion.id
                    thread_owner = "bot"
                    placement = "inline"
                    tracked = TrackedFinding(
                        project_id=project_id,
                        mr_iid=mr_iid,
                        fingerprint=fp,
                        status="open",
                        severity=finding.severity,
                        file=finding.file,
                        line=finding.line,
                        title=finding.title,
                        body=finding.body,
                        suggestion=finding.suggestion,
                        sources=finding.sources,
                        discussion_id=created_discussion.id,
                        root_note_id=created_discussion.root_note.id if created_discussion.root_note else None,
                        thread_owner="bot",
                        opened_sha=source_sha,
                        last_seen_sha=source_sha,
                        context_snippet=finding.snippet,
                    )
                else:
                    created_plain = self.gitlab.create_plain_discussion(
                        project_id, mr_iid, _render_finding_thread_body(finding, fp)
                    )
                    discussion_id = created_plain.id
                    thread_owner = "bot"
                    placement = "inline"
                    tracked = TrackedFinding(
                        project_id=project_id,
                        mr_iid=mr_iid,
                        fingerprint=fp,
                        status="open",
                        severity=finding.severity,
                        file=finding.file,
                        line=finding.line,
                        title=finding.title,
                        body=finding.body,
                        suggestion=finding.suggestion,
                        sources=finding.sources,
                        discussion_id=created_plain.id,
                        root_note_id=created_plain.root_note.id if created_plain.root_note else None,
                        thread_owner="bot",
                        opened_sha=source_sha,
                        last_seen_sha=source_sha,
                        context_snippet=finding.snippet,
                    )

            tracked = dc_replace(
                tracked,
                status="open",
                severity=finding.severity,
                file=finding.file,
                line=finding.line,
                title=finding.title,
                body=finding.body,
                suggestion=finding.suggestion,
                sources=finding.sources,
                last_seen_sha=source_sha,
                context_snippet=finding.snippet,
                updated_at=None,
            )
            self.store.upsert_tracked_finding(tracked)
            used_tracked.add(tracked.fingerprint)
            current_open_fingerprints.add(tracked.fingerprint)
            current_states.append(
                ReviewFindingState(
                    finding=finding,
                    status=finding_state.status,
                    placement=placement,  # type: ignore[arg-type]
                    discussion_id=discussion_id,
                    thread_owner=thread_owner,
                )
            )

            existing_discussion = discussions_by_id.get(discussion_id) if discussion_id else None
            if (
                finding_state.status == "still_present"
                and thread_owner == "bot"
                and discussion_id
                and existing_discussion is not None
                and not diff_mapping.has_changes_near(finding.file, finding.line)
                and _should_add_still_present_reply(
                    existing_discussion,
                    source_sha=source_sha,
                    fingerprint=tracked.fingerprint,
                    bot_user_id=bot_user_id,
                )
            ):
                self.gitlab.add_discussion_note(
                    project_id,
                    mr_iid,
                    discussion_id,
                    _render_still_present_reply(source_sha, tracked.fingerprint),
                )

        resolved_count = 0
        for tracked in tracked_findings:
            if tracked.status != "open":
                continue
            if tracked.fingerprint in current_open_fingerprints:
                continue
            if tracked.thread_owner == "bot" and tracked.discussion_id:
                discussion = discussions_by_id.get(tracked.discussion_id)
                if discussion and not discussion.resolved:
                    self.gitlab.set_discussion_resolved(project_id, mr_iid, tracked.discussion_id, resolved=True)
            resolved = dc_replace(tracked, status="resolved", resolved_sha=source_sha, updated_at=None)
            self.store.upsert_tracked_finding(resolved)
            resolved_count += 1

        return _PublishedReviewState(
            current_states=tuple(current_states),
        )

    def _create_inline_discussion_or_none(
        self,
        *,
        run_id: int,
        project_id: int,
        mr_iid: int,
        source_sha: str,
        finding: ReviewFinding,
        fingerprint: str,
        position,
    ) -> GitLabDiscussion | None:
        """Attempt to post an inline diff discussion, with stale-check on failure.

        Returns the created ``GitLabDiscussion`` on success, or ``None`` when
        the GitLab API rejects the position but the MR head has not changed
        (safe to degrade to summary-only).  Raises ``_StaleRunDuringPublish``
        if the API error coincides with the MR head advancing, so the caller
        can abort the entire publish without posting a partial note.
        """
        try:
            return self.gitlab.create_diff_discussion(
                project_id,
                mr_iid,
                _render_finding_thread_body(finding, fingerprint),
                position,
            )
        except GitLabError as exc:
            current_head_sha = self.gitlab.get_merge_request_head_sha(project_id, mr_iid)
            if current_head_sha != source_sha:
                LOGGER.warning(
                    "Inline publish failed for run_id=%s finding=%r at %s:%s because MR head changed from %s to %s",
                    run_id,
                    finding.title,
                    finding.file,
                    finding.line,
                    _short_sha(source_sha),
                    _short_sha(current_head_sha),
                )
                raise _StaleRunDuringPublish from exc

            LOGGER.warning(
                "Inline publish failed for run_id=%s finding=%r at %s:%s; degrading to summary-only. "
                "position old=%s:%s new=%s:%s diff_refs=%s/%s/%s error=%s",
                run_id,
                finding.title,
                finding.file,
                finding.line,
                position.old_path,
                position.old_line,
                position.new_path,
                position.new_line,
                _short_sha(position.base_sha),
                _short_sha(position.start_sha),
                _short_sha(position.head_sha),
                exc,
            )
            return None

    def _create_trace(self, run: ReviewRun) -> RunTrace | None:
        path = self.trace_settings.directory / f"run-{run.id}.jsonl"
        LOGGER.info("Trace enabled for run_id=%s path=%s", run.id, path)
        return RunTrace(path)

    def _load_previous_success_result(
        self,
        run: ReviewRun,
        *,
        kind: str,
        previous_run: ReviewRun | None = None,
    ) -> ReviewResult | None:
        if previous_run is None:
            previous_run = self.store.get_latest_done_run(run.project_id, run.mr_iid, kind=kind)  # type: ignore[arg-type]
        if not previous_run:
            return None
        snapshot_path = self.trace_settings.directory / f"run-{previous_run.id}.review.final.json"
        if not snapshot_path.is_file():
            return None
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load previous review snapshot run_id=%s path=%s", previous_run.id, snapshot_path)
            return None
        return _review_result_from_snapshot_payload(payload)


def _short_sha(value: str | None) -> str:
    if not value:
        return "-"
    return value[:12]


def _build_discussion_digest(
    discussions: list[GitLabDiscussion],
    changed_files: list[str],
    tracked_findings: list[TrackedFinding],
) -> str:
    linked_discussions = {item.discussion_id for item in tracked_findings if item.discussion_id}
    changed_file_set = set(changed_files)

    def _priority(d: GitLabDiscussion) -> int:
        # 0 = unresolved on changed file, 1 = unresolved elsewhere, 2 = resolved+tracked, 3 = skip
        if d.individual_note:
            return 3
        file_path = d.position.new_path if d.position else None
        if not d.resolved:
            return 0 if file_path in changed_file_set else 1
        if d.id in linked_discussions:
            return 2
        return 3

    sorted_discussions = sorted(discussions, key=_priority)
    blocks: list[str] = []
    total_chars = 0
    for discussion in sorted_discussions:
        if _priority(discussion) == 3:
            break
        block = _render_discussion_context(discussion)
        blocks.append(block)
        total_chars += len(block)
        if total_chars > 35_000:
            break
    return "\n\n".join(blocks)


def _render_discussion_context(discussion: GitLabDiscussion) -> str:
    location = ""
    if discussion.position:
        line = discussion.position.new_line or discussion.position.old_line or "?"
        location = f" at {discussion.position.new_path}:{line}"
    lines = [f"Discussion {discussion.id}{location} ({'resolved' if discussion.resolved else 'open'})"]
    for note in discussion.notes:
        author = f"user:{note.author_id}" if note.author_id is not None else "user:unknown"
        lines.append(f"- {author}: {note.body.strip()}")
    return "\n".join(lines)


def _build_discussion_inventory(discussions: list[GitLabDiscussion]) -> str:
    """One-liner per discussion so the model knows every thread exists, even if not in the digest."""
    lines: list[str] = []
    for d in discussions:
        if d.individual_note:
            continue
        location = ""
        if d.position:
            line = d.position.new_line or d.position.old_line or "?"
            location = f" @ {d.position.new_path}:{line}"
        status = "resolved" if d.resolved else "open"
        n = len(d.notes)
        lines.append(f"- {d.id}{location} [{status}, {n} note{'s' if n != 1 else ''}]")
    return "\n".join(lines)


def _match_tracked_finding(
    finding: ReviewFinding,
    tracked_findings: list[TrackedFinding],
    *,
    used_fingerprints: set[str],
    diff_mapping: DiffMapping,
) -> TrackedFinding | None:
    exact_fingerprint = finding_fingerprint(finding)
    candidates = [
        item
        for item in tracked_findings
        if item.fingerprint not in used_fingerprints
    ]
    for item in candidates:
        if item.fingerprint == exact_fingerprint:
            return item

    prioritized = sorted(
        candidates,
        key=lambda item: (
            0 if item.status == "open" else 1 if item.status == "dismissed_by_discussion" else 2,
            0 if item.thread_owner != "summary-only" else 1,
        ),
    )
    for item in prioritized:
        if item.status == "dismissed_by_discussion" and not diff_mapping.has_changes_near(item.file, item.line):
            continue
        if findings_match(finding, _tracked_finding_to_review_finding(item)):
            return item
    return None


def _tracked_finding_to_review_finding(item: TrackedFinding) -> ReviewFinding:
    return ReviewFinding(
        severity=item.severity,
        file=item.file,
        line=item.line,
        title=item.title,
        body=item.body,
        suggestion=item.suggestion,
        sources=item.sources,
        snippet=item.context_snippet,
    )


def _find_discussion(
    discussions: list[GitLabDiscussion],
    discussion_id: str | None,
    note_id: int | None,
) -> GitLabDiscussion | None:
    if discussion_id:
        for discussion in discussions:
            if discussion.id == discussion_id:
                return discussion
    if note_id is not None:
        for discussion in discussions:
            if any(note.id == note_id for note in discussion.notes):
                return discussion
    return None


def _find_note_in_discussion(discussion: GitLabDiscussion, note_id: int | None):
    if note_id is None:
        return discussion.notes[-1] if discussion.notes else None
    for note in discussion.notes:
        if note.id == note_id:
            return note
    return discussion.notes[-1] if discussion.notes else None


def _match_discussion_to_tracked_finding(
    discussion: GitLabDiscussion,
    tracked_findings: list[TrackedFinding],
) -> TrackedFinding | None:
    for tracked in tracked_findings:
        if tracked.discussion_id == discussion.id:
            return tracked
    synthetic = ReviewFinding(
        severity="low",
        file=discussion.position.new_path if discussion.position else "",
        line=discussion.position.new_line or discussion.position.old_line or 1 if discussion.position else 1,
        title=discussion.root_note.body[:80] if discussion.root_note else "discussion",
        body=" ".join(note.body for note in discussion.notes),
    )
    for tracked in tracked_findings:
        if tracked.status != "open":
            continue
        if tracked.file and synthetic.file and tracked.file != synthetic.file:
            continue
        if findings_match(_tracked_finding_to_review_finding(tracked), synthetic):
            return tracked
    return None


def _tracked_finding_payload(tracked: TrackedFinding) -> dict:
    return {
        "fingerprint": tracked.fingerprint,
        "status": tracked.status,
        "severity": tracked.severity,
        "file": tracked.file,
        "line": tracked.line,
        "title": tracked.title,
        "body": tracked.body,
        "suggestion": tracked.suggestion,
        "sources": list(tracked.sources),
        "thread_owner": tracked.thread_owner,
        "context_snippet": tracked.context_snippet,
    }


def _parse_discussion_reconcile_result(payload: dict) -> DiscussionReconcileResult:
    decision = str(payload.get("decision", "")).strip().lower()
    if decision not in {"keep_open", "dismissed_by_discussion", "reply_only", "no_action"}:
        raise RuntimeError(f"Invalid discussion reconcile decision: {decision!r}")
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise RuntimeError("Discussion reconcile result requires a reason")
    reply_body = str(payload.get("reply_body", "")).strip() or None
    return DiscussionReconcileResult(decision=decision, reason=reason, reply_body=reply_body)  # type: ignore[arg-type]


def _format_provider_label(sources: tuple) -> tuple[str, bool]:
    """Return (label_markdown, both_found) for the given sources."""
    names = {"codex": "Codex ֎", "claude": "Claude ✴️"}
    labels = [names.get(s, s.capitalize()) for s in sources if s in names]
    both = len(labels) >= 2
    if both:
        return "**Codex ֎ + Claude ✴️**", True
    return labels[0] if labels else "", False


def _reply_with_marker(body: str, fingerprint: str, provider: str = "") -> str:
    label = {"codex": "Codex ֎", "claude": "Claude ✴️"}.get(provider, "")
    prefix = f"{label}\n\n" if label else ""
    return f"{prefix}{body.strip()}\n\n{_finding_marker(fingerprint)}"


def _render_still_present_reply(source_sha: str, fingerprint: str) -> str:
    return (
        f"Reviewed `{source_sha[:8]}` - no changes in this area since the last review. "
        f"Concern still applies.\n\n{_still_present_marker(fingerprint, source_sha)}"
    )


_SEVERITY_LABEL = {"high": "🚨 High", "medium": "⚠️ Medium", "low": "💡 Low"}


def _render_finding_thread_body(finding: ReviewFinding, fingerprint: str) -> str:
    provider_label, both_found = _format_provider_label(finding.sources)
    is_low_single = not both_found and finding.severity == "low"

    severity = _SEVERITY_LABEL.get(finding.severity, finding.severity.capitalize())
    header = severity
    if is_low_single:
        header += " · *minor note*"

    title = f"_**{finding.title}**_" if is_low_single else f"**{finding.title}**"

    lines = [header, "", title, "", finding.body]
    if finding.suggestion:
        lines.extend(["", f"Suggested fix: {finding.suggestion}"])
    if provider_label:
        lines.extend(["", f"By {provider_label}"])
    lines.extend(["", _finding_marker(fingerprint)])
    return "\n".join(lines).strip()


def _render_thread_reply(finding: ReviewFinding) -> str:
    lines = [f"**{finding.title}**", "", finding.body]
    if finding.suggestion:
        lines.extend(["", f"Suggested fix: {finding.suggestion}"])
    return "\n".join(lines).strip()


def _finding_marker(fingerprint: str) -> str:
    return f"{FINDING_MARKER_PREFIX}{fingerprint} -->"


def _still_present_marker(fingerprint: str, source_sha: str) -> str:
    return f"{STILL_PRESENT_MARKER_PREFIX}{fingerprint}:{source_sha[:8]} -->"


def _should_add_still_present_reply(
    discussion: GitLabDiscussion,
    *,
    source_sha: str,
    fingerprint: str,
    bot_user_id: int,
) -> bool:
    marker = _still_present_marker(fingerprint, source_sha)
    if any(marker in note.body for note in discussion.notes):
        return False

    for note in reversed(discussion.notes[1:]):
        if note.system:
            continue
        return note.author_id != bot_user_id
    return True


def _build_review_comparison(previous: ReviewResult | None, current: ReviewResult) -> ReviewComparison:
    if previous is None:
        return ReviewComparison(
            current_findings=tuple(ReviewFindingState(finding=finding, status="new") for finding in current.findings),
            resolved_findings=(),
        )

    matched_previous: set[int] = set()
    current_findings: list[ReviewFindingState] = []
    for finding in current.findings:
        previous_index = next(
            (
                index
                for index, previous_finding in enumerate(previous.findings)
                if index not in matched_previous and findings_match(finding, previous_finding)
            ),
            None,
        )
        if previous_index is None:
            current_findings.append(ReviewFindingState(finding=finding, status="new"))
            continue
        matched_previous.add(previous_index)
        current_findings.append(ReviewFindingState(finding=finding, status="still_present"))

    resolved = tuple(finding for index, finding in enumerate(previous.findings) if index not in matched_previous)
    return ReviewComparison(current_findings=tuple(current_findings), resolved_findings=resolved)


def _suppress_dismissed_findings(
    result: ReviewResult,
    tracked_findings: list[TrackedFinding],
    diff_mapping: DiffMapping,
) -> ReviewResult:
    dismissed = [item for item in tracked_findings if item.status == "dismissed_by_discussion"]
    if not dismissed:
        return result

    dismissed_as_findings = [_tracked_finding_to_review_finding(t) for t in dismissed]
    kept_findings: list[ReviewFinding] = []
    for finding in result.findings:
        should_suppress = False
        for tracked, dismissed_finding in zip(dismissed, dismissed_as_findings):
            if not findings_match(finding, dismissed_finding):
                continue
            if diff_mapping.has_changes_near(tracked.file, tracked.line):
                break
            should_suppress = True
            break
        if not should_suppress:
            kept_findings.append(finding)

    return ReviewResult(
        summary=result.summary,
        overall_risk=result.overall_risk,
        findings=tuple(kept_findings),
        token_usage=result.token_usage,
        agent_metadata=result.agent_metadata,
        participants=result.participants,
    )


def _enrich_result_for_rendering(result: ReviewResult, checkout_path: Path) -> ReviewResult:
    if not result.findings:
        return result
    enriched = tuple(_enrich_finding(finding, checkout_path) for finding in result.findings)
    return ReviewResult(
        summary=result.summary,
        overall_risk=result.overall_risk,
        findings=enriched,
        token_usage=result.token_usage,
        agent_metadata=result.agent_metadata,
        participants=result.participants,
    )


def _enrich_finding(finding: ReviewFinding, checkout_path: Path) -> ReviewFinding:
    snippet = finding.snippet
    if snippet is None:
        path = checkout_path / finding.file
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        if lines:
            start = max(0, finding.line - 3)
            end = min(len(lines), finding.line + 2)
            snippet = "\n".join(lines[start:end])
    return ReviewFinding(
        severity=finding.severity,
        file=finding.file,
        line=finding.line,
        title=finding.title,
        body=finding.body,
        suggestion=finding.suggestion,
        sources=finding.sources,
        opinions=finding.opinions,
        snippet=snippet,
        snippet_start_line=max(1, finding.line - 2) if snippet else None,
        snippet_language=_guess_language(finding.file),
    )


def _build_hunk_context(diff_text: str, checkout_path: Path) -> dict[str, str]:
    """Read surrounding file context for each changed hunk.

    Returns {file_path: rendered snippet} for files found in the checkout.
    Snippets show HUNK_CONTEXT_LINES lines above and below each hunk start,
    with ``...`` separators between non-adjacent ranges.
    """
    file_lines: dict[str, list[int]] = {}
    current_file: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            file_lines.setdefault(current_file, [])
        elif line.startswith("@@ ") and current_file is not None:
            m = re.search(r"\+(\d+)", line)
            if m:
                file_lines[current_file].append(int(m.group(1)))

    result: dict[str, str] = {}
    for file_path, hunk_starts in list(file_lines.items())[:MAX_HUNK_CONTEXT_FILES]:
        full_path = checkout_path / file_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not content:
            continue

        ranges: set[int] = set()
        for ln in hunk_starts:
            lo = max(0, ln - HUNK_CONTEXT_LINES - 1)
            hi = min(len(content), ln + HUNK_CONTEXT_LINES)
            ranges.update(range(lo, hi))

        snippet_lines: list[str] = []
        prev: int | None = None
        for i in sorted(ranges):
            if prev is not None and i > prev + 1:
                snippet_lines.append("...")
            snippet_lines.append(f"{i + 1:5d} | {content[i]}")
            prev = i

        result[file_path] = "\n".join(snippet_lines)[:MAX_HUNK_CONTEXT_CHARS_PER_FILE]

    return result


def _guess_language(file_path: str) -> str | None:
    suffix = Path(file_path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".rb": "ruby",
        ".go": "go",
    }.get(suffix)


def _review_result_snapshot_payload(result: ReviewResult) -> dict:
    return {
        "summary": result.summary,
        "overall_risk": result.overall_risk,
        "findings": [_finding_snapshot_payload(finding) for finding in result.findings],
        "token_usage": _token_usage_snapshot_payload(result.token_usage),
        "agent_metadata": _agent_metadata_payload(result),
        "participants": _participants_payload(result),
    }


def _finding_snapshot_payload(finding: ReviewFinding) -> dict:
    payload = {
        "severity": finding.severity,
        "file": finding.file,
        "line": finding.line,
        "title": finding.title,
        "body": finding.body,
        "suggestion": finding.suggestion,
        "opinions": [
            {
                "provider": opinion.provider,
                "verdict": opinion.verdict,
                "reason": opinion.reason,
            }
            for opinion in finding.opinions
        ],
        "snippet": finding.snippet,
        "snippet_start_line": finding.snippet_start_line,
        "snippet_language": finding.snippet_language,
    }
    if finding.sources:
        payload["sources"] = list(finding.sources)
    return payload


def _token_usage_snapshot_payload(token_usage: ReviewTokenUsage | None) -> dict | None:
    if not token_usage:
        return None
    return {
        "input_tokens": token_usage.input_tokens,
        "cached_input_tokens": token_usage.cached_input_tokens,
        "cache_creation_input_tokens": token_usage.cache_creation_input_tokens,
        "output_tokens": token_usage.output_tokens,
        "cost_usd": token_usage.cost_usd,
        "cached_input_included_in_input": token_usage.cached_input_included_in_input,
        "cache_creation_included_in_input": token_usage.cache_creation_included_in_input,
    }


def _agent_metadata_payload(result: ReviewResult) -> dict | None:
    if not result.agent_metadata:
        return None
    return {
        "provider": result.agent_metadata.provider,
        "model": result.agent_metadata.model,
        "reasoning_effort": result.agent_metadata.reasoning_effort,
    }


def _participants_payload(result: ReviewResult) -> list[dict] | None:
    if not result.participants:
        return None
    return [
        {
            "provider": participant.metadata.provider,
            "model": participant.metadata.model,
            "reasoning_effort": participant.metadata.reasoning_effort,
            "phases": list(participant.phases),
            "token_usage": _token_usage_snapshot_payload(participant.token_usage),
            "summary": participant.summary,
            "overall_risk": participant.overall_risk,
        }
        for participant in result.participants
    ]


def _review_result_from_snapshot_payload(payload: dict) -> ReviewResult:
    result = _review_result_from_payload(payload, metadata=None, token_usage=None)
    token_usage_payload = payload.get("token_usage")
    token_usage = None
    if isinstance(token_usage_payload, dict):
        token_usage = ReviewTokenUsage(
            input_tokens=int(token_usage_payload.get("input_tokens", 0)),
            cached_input_tokens=int(token_usage_payload.get("cached_input_tokens", 0)),
            output_tokens=int(token_usage_payload.get("output_tokens", 0)),
            cache_creation_input_tokens=int(token_usage_payload.get("cache_creation_input_tokens", 0)),
            cost_usd=token_usage_payload.get("cost_usd"),
            cached_input_included_in_input=bool(token_usage_payload.get("cached_input_included_in_input", True)),
            cache_creation_included_in_input=bool(token_usage_payload.get("cache_creation_included_in_input", True)),
        )
    participants = _participants_from_snapshot_payload(payload.get("participants"))
    metadata = _agent_metadata_from_snapshot_payload(payload.get("agent_metadata"))
    return ReviewResult(
        summary=result.summary,
        overall_risk=result.overall_risk,
        findings=result.findings,
        token_usage=token_usage,
        agent_metadata=metadata,
        participants=participants,
    )


def _agent_metadata_from_snapshot_payload(payload: dict | None) -> ReviewAgentMetadata | None:
    if not isinstance(payload, dict):
        return None
    provider = str(payload.get("provider", "")).strip().lower()
    if provider not in {"codex", "claude"}:
        return None
    return ReviewAgentMetadata(
        provider=provider,  # type: ignore[arg-type]
        model=payload.get("model"),
        reasoning_effort=payload.get("reasoning_effort"),
    )


def _participants_from_snapshot_payload(payload) -> tuple[ReviewParticipant, ...]:
    if not isinstance(payload, list):
        return ()
    participants: list[ReviewParticipant] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        metadata = _agent_metadata_from_snapshot_payload(
            {
                "provider": item.get("provider"),
                "model": item.get("model"),
                "reasoning_effort": item.get("reasoning_effort"),
            }
        )
        if metadata is None:
            continue
        token_usage = None
        if isinstance(item.get("token_usage"), dict):
            usage = item["token_usage"]
            token_usage = ReviewTokenUsage(
                input_tokens=int(usage.get("input_tokens", 0)),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
                cost_usd=usage.get("cost_usd"),
            )
        participants.append(
            ReviewParticipant(
                metadata=metadata,
                phases=tuple(item.get("phases") or ()),
                token_usage=token_usage,
                summary=item.get("summary"),
                overall_risk=item.get("overall_risk"),
            )
        )
    return tuple(participants)
