from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass
from pathlib import Path

from nimble_reviewer.finding_match import findings_match
from nimble_reviewer.gitlab import GitLabClient
from nimble_reviewer.gitops import RepoManager
from nimble_reviewer.models import ReviewAgentMetadata, ReviewComparison, ReviewFinding, ReviewFindingState, ReviewOpinion, ReviewParticipant, ReviewResult, ReviewRun, ReviewTokenUsage
from nimble_reviewer.prompts import build_review_prompt
from nimble_reviewer.review_agent import ReviewAgentRunner
from nimble_reviewer.renderer import note_marker, render_failure_note, render_success_note
from nimble_reviewer.store import Store
from nimble_reviewer.trace import RunTrace, TraceSettings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceDependencies:
    store: Store
    gitlab: GitLabClient
    repo_manager: RepoManager
    review_agent: ReviewAgentRunner
    trace_settings: TraceSettings


class ReviewService:
    def __init__(self, deps: ServiceDependencies) -> None:
        self.store = deps.store
        self.gitlab = deps.gitlab
        self.repo_manager = deps.repo_manager
        self.review_agent = deps.review_agent
        self.trace_settings = deps.trace_settings

    def process_run(self, run: ReviewRun) -> None:
        if self.store.get_run_status(run.id) != "running":
            LOGGER.info("Skipping run %s because it is no longer running", run.id)
            return

        LOGGER.info(
            "Starting review run_id=%s project=%s mr=%s sha=%s",
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
                project_id=run.project_id,
                mr_iid=run.mr_iid,
                source_sha=_short_sha(run.source_sha),
            )
        try:
            started = time.monotonic()
            mr_info = self.gitlab.get_merge_request_info(run.project_id, run.mr_iid)
            LOGGER.info(
                "Fetched MR metadata run_id=%s title=%r source_branch=%s target_branch=%s sha=%s",
                run.id,
                mr_info.title,
                mr_info.source_branch,
                mr_info.target_branch,
                _short_sha(mr_info.source_sha),
            )
            if trace:
                trace.write(
                    "app",
                    "mr.metadata.loaded",
                    run_id=run.id,
                    title=mr_info.title,
                    source_branch=mr_info.source_branch,
                    target_branch=mr_info.target_branch,
                    source_sha=_short_sha(mr_info.source_sha),
                )
            if mr_info.source_sha != run.source_sha:
                self.store.mark_superseded_if_running(run.id)
                LOGGER.info("Run %s is stale; GitLab head is now %s", run.id, mr_info.source_sha)
                if trace:
                    trace.write(
                        "app",
                        "review.stale_before_checkout",
                        run_id=run.id,
                        current_head_sha=_short_sha(mr_info.source_sha),
                    )
                return

            checkout_started = time.monotonic()
            checkout = self.repo_manager.prepare_checkout(
                mr_info.repo_http_url,
                run.source_sha,
                mr_info.target_branch,
                trace=trace,
            )
            try:
                LOGGER.info(
                    "Checkout ready run_id=%s dir=%s merge_base=%s changed_files=%s review_rules=%s checkout_sec=%.2f",
                    run.id,
                    checkout.path,
                    _short_sha(checkout.merge_base),
                    len(checkout.changed_files),
                    checkout.review_rules_path or "-",
                    time.monotonic() - checkout_started,
                )
                if trace:
                    trace.write(
                        "app",
                        "checkout.ready",
                        run_id=run.id,
                        checkout_dir=str(checkout.path),
                        merge_base=_short_sha(checkout.merge_base),
                        changed_files=checkout.changed_files,
                        review_rules_path=checkout.review_rules_path,
                    )
                prompt = build_review_prompt(
                    mr_info,
                    checkout.diff_text,
                    checkout.changed_files,
                    repo_rules_text=checkout.review_rules_text,
                    repo_rules_path=checkout.review_rules_path,
                    repo_rules_truncated=checkout.review_rules_truncated,
                )
                LOGGER.info(
                    "Starting %s review run_id=%s prompt_bytes=%s diff_bytes=%s review_rules=%s",
                    self.review_agent.provider_name,
                    run.id,
                    len(prompt.encode("utf-8")),
                    len(checkout.diff_text.encode("utf-8")),
                    checkout.review_rules_path or "-",
                )
                if trace:
                    trace.write(
                        "app",
                        "provider.started",
                        run_id=run.id,
                        provider=self.review_agent.provider_name,
                        prompt_bytes=len(prompt.encode("utf-8")),
                        diff_bytes=len(checkout.diff_text.encode("utf-8")),
                    )
                agent_started = time.monotonic()
                result = self.review_agent.review(prompt, checkout.path, trace=trace)
                result = _enrich_result_for_rendering(result, checkout.path)
                previous_result = self._load_previous_success_result(run)
                comparison = _build_review_comparison(previous_result, result)
                if trace:
                    trace.write_snapshot("review.final", _review_result_snapshot_payload(result))
                LOGGER.info(
                    "%s finished run_id=%s findings=%s overall_risk=%s agent_sec=%.2f",
                    self.review_agent.provider_name,
                    run.id,
                    len(result.findings),
                    result.overall_risk,
                    time.monotonic() - agent_started,
                )
                if trace:
                    trace.write(
                        "app",
                        "provider.completed",
                        run_id=run.id,
                        provider=self.review_agent.provider_name,
                        findings=len(result.findings),
                        overall_risk=result.overall_risk,
                        token_usage=_token_usage_payload(result),
                        agent_metadata=_agent_metadata_payload(result),
                        participants=_participants_payload(result),
                    )
            finally:
                checkout.close()
                LOGGER.info("Cleaned checkout for run_id=%s", run.id)
                if trace:
                    trace.write("app", "checkout.cleaned", run_id=run.id)

            if self.store.get_run_status(run.id) != "running":
                LOGGER.info("Run %s was superseded before publish", run.id)
                if trace:
                    trace.write("app", "review.superseded_before_publish", run_id=run.id)
                return
            if self.gitlab.get_merge_request_head_sha(run.project_id, run.mr_iid) != run.source_sha:
                self.store.mark_superseded_if_running(run.id)
                LOGGER.info("Run %s became stale before publish", run.id)
                if trace:
                    trace.write("app", "review.stale_before_publish", run_id=run.id)
                return

            note = self._upsert_success_note(run.project_id, run.mr_iid, run.source_sha, result, comparison)
            self.store.update_note_id(run.project_id, run.mr_iid, note.id)
            self.store.mark_done(run.id, run.project_id, run.mr_iid, run.source_sha)
            LOGGER.info(
                "Completed review run_id=%s note_id=%s total_sec=%.2f",
                run.id,
                note.id,
                time.monotonic() - started,
            )
            if trace:
                trace.write(
                    "app",
                    "review.completed",
                    run_id=run.id,
                    note_id=note.id,
                    total_sec=round(time.monotonic() - started, 3),
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Review run %s failed", run.id)
            if trace:
                trace.write("app", "review.failed", run_id=run.id, error=str(exc))
            if not self.store.mark_failed(run.id, run.project_id, run.mr_iid, run.source_sha, str(exc)):
                return
            try:
                note = self._upsert_failure_note(run.project_id, run.mr_iid, run.source_sha, str(exc))
                self.store.update_note_id(run.project_id, run.mr_iid, note.id)
                LOGGER.info("Published failure note run_id=%s note_id=%s", run.id, note.id)
                if trace:
                    trace.write("app", "failure_note.published", run_id=run.id, note_id=note.id)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to publish failure note for run %s", run.id)
                if trace:
                    trace.write("app", "failure_note.publish_failed", run_id=run.id)

    def _upsert_success_note(self, project_id: int, mr_iid: int, source_sha: str, result, comparison: ReviewComparison):
        state = self.store.get_merge_request_state(project_id, mr_iid)
        marker = note_marker(project_id, mr_iid)
        existing = self.gitlab.find_bot_note(project_id, mr_iid, marker, state.note_id if state else None)
        body = render_success_note(project_id, mr_iid, source_sha, result, comparison=comparison)
        if existing:
            LOGGER.info(
                "Updating success note project=%s mr=%s note_id=%s sha=%s findings=%s",
                project_id,
                mr_iid,
                existing.id,
                _short_sha(source_sha),
                len(result.findings),
            )
            return self.gitlab.update_note(project_id, mr_iid, existing.id, body)
        LOGGER.info(
            "Creating success note project=%s mr=%s sha=%s findings=%s",
            project_id,
            mr_iid,
            _short_sha(source_sha),
            len(result.findings),
        )
        return self.gitlab.create_note(project_id, mr_iid, body)

    def _create_trace(self, run: ReviewRun) -> RunTrace | None:
        path = self.trace_settings.directory / f"run-{run.id}.jsonl"
        LOGGER.info("Trace enabled for run_id=%s path=%s", run.id, path)
        return RunTrace(path)

    def _upsert_failure_note(self, project_id: int, mr_iid: int, source_sha: str, error: str):
        state = self.store.get_merge_request_state(project_id, mr_iid)
        marker = note_marker(project_id, mr_iid)
        existing = self.gitlab.find_bot_note(project_id, mr_iid, marker, state.note_id if state else None)
        body = render_failure_note(project_id, mr_iid, source_sha, error, existing.body if existing else None)
        if existing:
            LOGGER.info(
                "Updating failure note project=%s mr=%s note_id=%s sha=%s",
                project_id,
                mr_iid,
                existing.id,
                _short_sha(source_sha),
            )
            return self.gitlab.update_note(project_id, mr_iid, existing.id, body)
        LOGGER.info(
            "Creating failure note project=%s mr=%s sha=%s",
            project_id,
            mr_iid,
            _short_sha(source_sha),
        )
        return self.gitlab.create_note(project_id, mr_iid, body)

    def _load_previous_success_result(self, run: ReviewRun) -> ReviewResult | None:
        previous_run = self.store.get_latest_done_run(run.project_id, run.mr_iid)
        if not previous_run:
            return None
        snapshot_path = self.trace_settings.directory / f"run-{previous_run.id}.review.final.json"
        if not snapshot_path.is_file():
            LOGGER.info("Previous review snapshot missing run_id=%s path=%s", previous_run.id, snapshot_path)
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


def _token_usage_payload(result_or_usage) -> dict | None:
    token_usage = getattr(result_or_usage, "token_usage", result_or_usage)
    if not token_usage:
        return None
    return {
        "input_tokens": token_usage.input_tokens,
        "cached_input_tokens": token_usage.cached_input_tokens,
        "cache_creation_input_tokens": token_usage.cache_creation_input_tokens,
        "output_tokens": token_usage.output_tokens,
        "total_tokens": token_usage.total_tokens,
        "cost_usd": token_usage.cost_usd,
    }


def _agent_metadata_payload(result) -> dict | None:
    if not result.agent_metadata:
        return None
    return {
        "provider": result.agent_metadata.provider,
        "model": result.agent_metadata.model,
        "reasoning_effort": result.agent_metadata.reasoning_effort,
    }


def _participants_payload(result) -> list[dict] | None:
    if not getattr(result, "participants", None):
        return None
    payload: list[dict] = []
    for participant in result.participants:
        payload.append(
            {
                "provider": participant.metadata.provider,
                "model": participant.metadata.model,
                "reasoning_effort": participant.metadata.reasoning_effort,
                "phases": list(participant.phases),
                "token_usage": _token_usage_payload(participant.token_usage),
            }
        )
    return payload


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

    resolved = tuple(
        finding
        for index, finding in enumerate(previous.findings)
        if index not in matched_previous
    )
    return ReviewComparison(
        current_findings=tuple(current_findings),
        resolved_findings=resolved,
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
    return {
        "severity": finding.severity,
        "file": finding.file,
        "line": finding.line,
        "title": finding.title,
        "body": finding.body,
        "suggestion": finding.suggestion,
        "sources": list(finding.sources),
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


def _token_usage_snapshot_payload(token_usage: ReviewTokenUsage | None) -> dict | None:
    if token_usage is None:
        return None
    return {
        "input_tokens": token_usage.input_tokens,
        "cached_input_tokens": token_usage.cached_input_tokens,
        "output_tokens": token_usage.output_tokens,
        "cache_creation_input_tokens": token_usage.cache_creation_input_tokens,
        "cost_usd": token_usage.cost_usd,
        "cached_input_included_in_input": token_usage.cached_input_included_in_input,
        "cache_creation_included_in_input": token_usage.cache_creation_included_in_input,
    }


def _review_result_from_snapshot_payload(payload: dict) -> ReviewResult:
    findings = tuple(_finding_from_snapshot_payload(item) for item in payload.get("findings", []))
    agent_metadata = payload.get("agent_metadata") or {}
    participants_payload = payload.get("participants") or []
    return ReviewResult(
        summary=str(payload.get("summary", "")),
        overall_risk=str(payload.get("overall_risk", "low")),
        findings=findings,
        token_usage=_token_usage_from_snapshot_payload(payload.get("token_usage")),
        agent_metadata=(
            None
            if not agent_metadata
            else ReviewAgentMetadata(
                provider=str(agent_metadata.get("provider", "")),
                model=agent_metadata.get("model"),
                reasoning_effort=agent_metadata.get("reasoning_effort"),
            )
        ),
        participants=tuple(_participant_from_snapshot_payload(item) for item in participants_payload),
    )


def _finding_from_snapshot_payload(payload: dict) -> ReviewFinding:
    return ReviewFinding(
        severity=str(payload.get("severity", "low")),
        file=str(payload.get("file", "")),
        line=int(payload.get("line", 1)),
        title=str(payload.get("title", "")),
        body=str(payload.get("body", "")),
        suggestion=payload.get("suggestion"),
        sources=tuple(payload.get("sources", ())),
        opinions=tuple(
            ReviewOpinion(
                provider=str(item.get("provider", "")),
                verdict=str(item.get("verdict", "")),
                reason=item.get("reason"),
            )
            for item in payload.get("opinions", [])
        ),
        snippet=payload.get("snippet"),
        snippet_start_line=payload.get("snippet_start_line"),
        snippet_language=payload.get("snippet_language"),
    )


def _token_usage_from_snapshot_payload(payload: dict | None) -> ReviewTokenUsage | None:
    if not payload:
        return None
    return ReviewTokenUsage(
        input_tokens=int(payload.get("input_tokens", 0)),
        cached_input_tokens=int(payload.get("cached_input_tokens", 0)),
        output_tokens=int(payload.get("output_tokens", 0)),
        cache_creation_input_tokens=int(payload.get("cache_creation_input_tokens", 0)),
        cost_usd=payload.get("cost_usd"),
        cached_input_included_in_input=bool(payload.get("cached_input_included_in_input", True)),
        cache_creation_included_in_input=bool(payload.get("cache_creation_included_in_input", True)),
    )


def _participant_from_snapshot_payload(payload: dict) -> ReviewParticipant:
    metadata_payload = payload.get("metadata") or payload

    return ReviewParticipant(
        metadata=ReviewAgentMetadata(
            provider=str(metadata_payload.get("provider", "")),
            model=metadata_payload.get("model"),
            reasoning_effort=metadata_payload.get("reasoning_effort"),
        ),
        phases=tuple(payload.get("phases", ())),
        token_usage=_token_usage_from_snapshot_payload(payload.get("token_usage")),
    )


def _enrich_finding(finding: ReviewFinding, checkout_path: Path) -> ReviewFinding:
    snippet, snippet_start_line, snippet_language = _build_snippet(checkout_path, finding.file, finding.line)
    if snippet is None:
        return finding
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
        snippet_start_line=snippet_start_line,
        snippet_language=snippet_language,
    )


def _build_snippet(checkout_path: Path, relative_file: str, line_number: int) -> tuple[str | None, int | None, str | None]:
    try:
        root = checkout_path.resolve()
        candidate = (checkout_path / relative_file).resolve()
    except OSError:
        return None, None, None

    if not _is_within_root(candidate, root) or not candidate.is_file():
        return None, None, None

    try:
        content = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None, None

    if not content:
        return None, None, None

    start = max(1, line_number - 2)
    end = min(len(content), line_number + 2)
    width = len(str(end))
    snippet_lines: list[str] = []
    for current_line in range(start, end + 1):
        prefix = ">>" if current_line == line_number else "  "
        snippet_lines.append(f"{prefix}{current_line:>{width}} | {content[current_line - 1]}")

    return "\n".join(snippet_lines), start, _language_for_path(candidate)


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _language_for_path(path: Path) -> str | None:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".java": "java",
        ".go": "go",
        ".rb": "ruby",
        ".rs": "rust",
        ".sql": "sql",
        ".sh": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".json": "json",
        ".md": "markdown",
    }.get(suffix, suffix.lstrip(".") or None)
