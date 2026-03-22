from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nimble_reviewer.finding_match import findings_match
from nimble_reviewer.models import (
    ReviewAgentMetadata,
    ReviewFinding,
    ReviewOpinion,
    ReviewParticipant,
    ReviewProvider,
    ReviewResult,
    ReviewTokenUsage,
)
from nimble_reviewer.prompts import build_council_synthesis_prompt
from nimble_reviewer.trace import RunTrace

VALID_SEVERITIES = {"high", "medium", "low"}
VALID_PROVIDERS = {"codex", "claude"}
VALID_OPINION_VERDICTS = {"found", "agree", "disagree", "uncertain"}
VALID_REVIEWER_OVERVIEW_KEYS = {"codex", "claude"}
LOGGER = logging.getLogger(__name__)


class ReviewAgentError(RuntimeError):
    pass


class ReviewAgentRunner(Protocol):
    """Minimal interface shared by all review agent implementations.

    A runner receives a plain-text prompt, executes the underlying CLI in
    *cwd*, and returns a parsed ``ReviewResult``.  Implementations may run
    a single provider (``CodexRunner``, ``ClaudeRunner``) or a two-model
    council (``CouncilRunner``).
    """

    provider_name: str

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        ...


class JsonCapableReviewAgent(ReviewAgentRunner, Protocol):
    """Extension of ``ReviewAgentRunner`` that can return raw JSON payloads.

    Used by ``CouncilRunner`` to drive both the individual review phase and
    the synthesis phase with the same runner instances, without re-parsing
    the JSON twice.
    """

    agent_metadata: ReviewAgentMetadata

    def run_json(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[dict, ReviewTokenUsage | None]:
        ...


class CodexRunner:
    """Runs the Codex CLI and parses its JSON event stream into a ``ReviewResult``.

    When *trace* is provided, the runner appends ``--json`` and
    ``--output-last-message`` flags to capture per-event token usage.  It
    retries once on JSON parse failure before raising ``ReviewAgentError``.
    """

    provider_name = "codex"

    def __init__(self, command: tuple[str, ...], timeout_sec: int) -> None:
        self.command = command
        self.timeout_sec = timeout_sec
        self.agent_metadata = _extract_agent_metadata(self.provider_name, command)

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        payload, usage = self.run_json(prompt, cwd, trace=trace)
        return _review_result_from_payload(payload, self.agent_metadata, usage)

    def run_json(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[dict, ReviewTokenUsage | None]:
        errors: list[str] = []
        for attempt in range(2):
            raw, usage = self._run_once(prompt, cwd, trace)
            try:
                return _load_json_object(raw), usage
            except ReviewAgentError as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
        raise ReviewAgentError("; ".join(errors))

    def _run_once(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[str, ReviewTokenUsage | None]:
        if not trace:
            raw = _run_command(self.command, prompt, cwd, self.timeout_sec, self.provider_name)
            return raw, None

        if any(option in self.command for option in ("--json", "--output-last-message", "-o")):
            raise ReviewAgentError(
                "CODEX_CMD must not include --json, --output-last-message, or -o; the service manages those flags internally"
            )

        with tempfile.TemporaryDirectory(prefix="nimble-reviewer-codex-") as tmpdir:
            final_path = Path(tmpdir) / "final-message.txt"
            command = [*self.command, "--json", "--output-last-message", str(final_path)]
            stdout = _run_command(tuple(command), prompt, cwd, self.timeout_sec, self.provider_name)
            usage = _record_codex_events(stdout, trace)
            if final_path.exists():
                return final_path.read_text(encoding="utf-8", errors="replace").strip(), usage
            return _extract_codex_final_message(stdout), usage


class ClaudeRunner:
    """Runs the Claude CLI and parses its output into a ``ReviewResult``.

    Supports three output formats emitted by the CLI: plain text, ``json``
    (single envelope), and ``stream-json`` (newline-delimited events).  When
    *trace* is provided and no explicit ``--output-format`` flag is present,
    ``stream-json`` is injected so that per-message token usage can be
    recorded.
    """

    provider_name = "claude"

    def __init__(self, command: tuple[str, ...], timeout_sec: int) -> None:
        self.command = command
        self.timeout_sec = timeout_sec
        self.agent_metadata = _extract_agent_metadata(self.provider_name, command)

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        payload, usage = self.run_json(prompt, cwd, trace=trace)
        return _review_result_from_payload(payload, self.agent_metadata, usage)

    def run_json(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[dict, ReviewTokenUsage | None]:
        errors: list[str] = []
        for attempt in range(2):
            try:
                raw, usage = self._run_once(prompt, cwd, trace)
                return _load_json_object(raw), usage
            except ReviewAgentError as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
        raise ReviewAgentError("; ".join(errors))

    def _run_once(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[str, ReviewTokenUsage | None]:
        command = list(self.command)
        output_format = _detect_claude_output_format(command)
        if trace and output_format is None:
            command.extend(["--output-format", "stream-json"])
            output_format = "stream-json"
        command = list(_normalize_claude_command(command))

        raw = _run_command(tuple(command), prompt, cwd, self.timeout_sec, self.provider_name)

        usage = _record_claude_events(raw, output_format or "text", trace) if trace else None

        if output_format == "stream-json":
            return _extract_claude_result_from_stream(raw), usage
        return _extract_claude_result(raw), usage


class CouncilRunner:
    """Two-model council: runs Codex and Claude in parallel, then synthesizes.

    Execution flow:
    1. Codex and Claude reviews run concurrently in a ``ThreadPoolExecutor``.
    2. If both succeed, a synthesis prompt is sent to *synthesis_provider*
       (defaults to Codex) with both results as context.
    3. ``_reconcile_council_findings`` ensures no finding from either base
       review is silently dropped by the synthesis step.
    4. If one provider fails, the other's result is used directly (no synthesis).
    5. If the primary synthesizer fails, the fallback provider is tried once.
    """

    provider_name = "council"

    def __init__(
        self,
        codex_runner: JsonCapableReviewAgent,
        claude_runner: JsonCapableReviewAgent,
        synthesis_provider: ReviewProvider = "codex",
    ) -> None:
        self.codex_runner = codex_runner
        self.claude_runner = claude_runner
        self.synthesis_provider = synthesis_provider

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        """Run the full council pipeline and return a merged ``ReviewResult``."""
        provider_runs: list[_ProviderRun] = []
        codex_result, claude_result = self._run_parallel_reviews(prompt, cwd, trace, provider_runs)

        if codex_result is None and claude_result is None:
            raise ReviewAgentError("Both Codex and Claude reviews failed")

        if codex_result is None or claude_result is None:
            successful = codex_result if codex_result is not None else claude_result
            assert successful is not None
            failed_provider = "codex" if codex_result is None else "claude"
            successful_provider = "claude" if codex_result is None else "codex"
            LOGGER.warning(
                "Council skipping synthesis: %s review failed, using %s result only findings=%s overall_risk=%s",
                failed_provider,
                successful_provider,
                len(successful.findings),
                successful.overall_risk,
            )
            if trace:
                trace.write(
                    "council",
                    "synthesis.skipped",
                    reason=f"{failed_provider}_review_failed",
                    fallback_provider=successful_provider,
                    findings=len(successful.findings),
                    overall_risk=successful.overall_risk,
                )
            participants = _summarize_participants(provider_runs, reviewer_overview=None)
            return ReviewResult(
                summary=successful.summary,
                overall_risk=successful.overall_risk,
                findings=successful.findings,
                participants=participants,
            )

        synthesis_prompt = build_council_synthesis_prompt(
            prompt,
            codex_result=codex_result,
            claude_result=claude_result,
        )
        primary_synth = self.codex_runner if self.synthesis_provider == "codex" else self.claude_runner
        fallback_synth = self.claude_runner if self.synthesis_provider == "codex" else self.codex_runner
        synthesizer, synthesis_payload, synthesis_usage = self._run_synthesis(
            synthesis_prompt, cwd, trace, primary_synth, fallback_synth
        )
        reviewer_overview = _parse_reviewer_overview_payload(synthesis_payload.get("reviewer_overview"))
        if trace:
            trace.write_snapshot(f"{synthesizer.provider_name}.synthesis", synthesis_payload)
        provider_runs.append(
            _ProviderRun(
                provider=synthesizer.provider_name,  # type: ignore[arg-type]
                phase="synthesis",
                metadata=synthesizer.agent_metadata,
                token_usage=synthesis_usage,
                summary=str(synthesis_payload.get("summary", "")).strip() or None,
                overall_risk=str(synthesis_payload.get("overall_risk", "")).strip().lower() or None,
            )
        )
        final_result = _review_result_from_payload(
            synthesis_payload,
            metadata=None,
            token_usage=None,
            require_sources=True,
        )
        final_result, preserved_count, attribution_updates = _reconcile_council_findings(
            final_result,
            codex_result=codex_result,
            claude_result=claude_result,
        )
        participants = _summarize_participants(provider_runs, reviewer_overview=reviewer_overview)
        if trace:
            trace.write(
                "council",
                "synthesis.completed",
                provider=synthesizer.provider_name,
                findings=len(final_result.findings),
                overall_risk=final_result.overall_risk,
                preserved_findings=preserved_count,
                attribution_updates=attribution_updates,
                participants=[
                    {
                        "provider": participant.metadata.provider,
                        "model": participant.metadata.model,
                        "reasoning_effort": participant.metadata.reasoning_effort,
                        "phases": list(participant.phases),
                        "token_usage": _token_usage_to_payload(participant.token_usage),
                    }
                    for participant in participants
                ],
            )
        LOGGER.info(
            "Council synthesis finished provider=%s findings=%s overall_risk=%s preserved_findings=%s attribution_updates=%s",
            synthesizer.provider_name,
            len(final_result.findings),
            final_result.overall_risk,
            preserved_count,
            attribution_updates,
        )
        return ReviewResult(
            summary=final_result.summary,
            overall_risk=final_result.overall_risk,
            findings=final_result.findings,
            participants=participants,
        )

    def _run_review_phase(
        self,
        runner: JsonCapableReviewAgent,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None,
    ) -> ReviewResult:
        if trace:
            trace.write("council", "review.started", provider=runner.provider_name)
        LOGGER.info(
            "Council %s review started model=%s reasoning=%s",
            runner.provider_name,
            runner.agent_metadata.model or "default",
            runner.agent_metadata.reasoning_effort or "default",
        )
        started = time.monotonic()
        result = runner.review(prompt, cwd, trace=trace)
        if trace:
            trace.write_snapshot(f"{runner.provider_name}.review", _review_result_to_payload(result))
        if trace:
            trace.write(
                "council",
                "review.completed",
                provider=runner.provider_name,
                findings=len(result.findings),
                overall_risk=result.overall_risk,
            )
        LOGGER.info(
            "Council %s review finished findings=%s overall_risk=%s phase_sec=%.2f",
            runner.provider_name,
            len(result.findings),
            result.overall_risk,
            time.monotonic() - started,
        )
        return result

    def _run_parallel_reviews(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None,
        provider_runs: list["_ProviderRun"],
    ) -> tuple[ReviewResult | None, ReviewResult | None]:
        results: dict[str, ReviewResult] = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="council-review") as executor:
            future_to_provider = {
                executor.submit(self._run_review_phase, self.codex_runner, prompt, cwd, trace): "codex",
                executor.submit(self._run_review_phase, self.claude_runner, prompt, cwd, trace): "claude",
            }
            for future in as_completed(future_to_provider):
                provider = future_to_provider[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Council %s review failed: %s", provider, exc)
                    if trace:
                        trace.write("council", "review.failed", provider=provider, error=str(exc))
                    continue
                runner_ref = self.codex_runner if provider == "codex" else self.claude_runner
                provider_runs.append(
                    _ProviderRun(
                        provider=provider,  # type: ignore[arg-type]
                        phase="review",
                        metadata=result.agent_metadata or runner_ref.agent_metadata,
                        token_usage=result.token_usage,
                        summary=result.summary or None,
                        overall_risk=result.overall_risk,
                    )
                )
                results[provider] = result
        return results.get("codex"), results.get("claude")

    def _run_synthesis(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None,
        primary: JsonCapableReviewAgent,
        fallback: JsonCapableReviewAgent,
    ) -> tuple[JsonCapableReviewAgent, dict, ReviewTokenUsage | None]:
        if trace:
            trace.write("council", "synthesis.started", provider=primary.provider_name)
        LOGGER.info(
            "Council synthesis started provider=%s model=%s reasoning=%s",
            primary.provider_name,
            primary.agent_metadata.model or "default",
            primary.agent_metadata.reasoning_effort or "default",
        )
        started = time.monotonic()
        try:
            payload, usage = primary.run_json(prompt, cwd, trace=trace)
            LOGGER.info(
                "Council synthesis finished provider=%s phase_sec=%.2f",
                primary.provider_name,
                time.monotonic() - started,
            )
            return primary, payload, usage
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Council synthesis failed provider=%s: %s; retrying with %s",
                primary.provider_name,
                exc,
                fallback.provider_name,
            )
            if trace:
                trace.write(
                    "council",
                    "synthesis.failed",
                    provider=primary.provider_name,
                    error=str(exc),
                    fallback_provider=fallback.provider_name,
                )
        if trace:
            trace.write("council", "synthesis.started", provider=fallback.provider_name)
        LOGGER.info(
            "Council synthesis started provider=%s model=%s reasoning=%s (fallback)",
            fallback.provider_name,
            fallback.agent_metadata.model or "default",
            fallback.agent_metadata.reasoning_effort or "default",
        )
        fallback_started = time.monotonic()
        payload, usage = fallback.run_json(prompt, cwd, trace=trace)
        LOGGER.info(
            "Council synthesis finished provider=%s phase_sec=%.2f (fallback)",
            fallback.provider_name,
            time.monotonic() - fallback_started,
        )
        return fallback, payload, usage


@dataclass(frozen=True)
class _ProviderRun:
    provider: ReviewProvider
    phase: str
    metadata: ReviewAgentMetadata
    token_usage: ReviewTokenUsage | None
    summary: str | None = None
    overall_risk: str | None = None


def _review_result_from_payload(
    payload: dict,
    metadata: ReviewAgentMetadata | None,
    token_usage: ReviewTokenUsage | None,
    require_sources: bool = False,
) -> ReviewResult:
    """Parse a raw agent JSON payload into a ``ReviewResult``.

    *metadata* and *token_usage* are attached directly; when *require_sources*
    is ``True`` every finding must include a non-empty ``sources`` array (used
    for synthesis output, which must attribute findings to Codex/Claude).
    """
    result = _parse_review_result_payload(payload, require_sources=require_sources)
    return ReviewResult(
        summary=result.summary,
        overall_risk=result.overall_risk,
        findings=result.findings,
        token_usage=token_usage or result.token_usage,
        agent_metadata=metadata,
    )


def _review_result_to_payload(result: ReviewResult) -> dict:
    return {
        "summary": result.summary,
        "overall_risk": result.overall_risk,
        "findings": [
            {
                "severity": finding.severity,
                "file": finding.file,
                "line": finding.line,
                "title": finding.title,
                "body": finding.body,
                **({"suggestion": finding.suggestion} if finding.suggestion else {}),
                **({"sources": list(finding.sources)} if finding.sources else {}),
                **(
                    {
                        "opinions": [
                            {
                                "provider": opinion.provider,
                                "verdict": opinion.verdict,
                                **({"reason": opinion.reason} if opinion.reason else {}),
                            }
                            for opinion in finding.opinions
                        ]
                    }
                    if finding.opinions
                    else {}
                ),
            }
            for finding in result.findings
        ],
        **({"token_usage": _token_usage_to_payload(result.token_usage)} if result.token_usage else {}),
        **(
            {
                "agent_metadata": {
                    "provider": result.agent_metadata.provider,
                    "model": result.agent_metadata.model,
                    "reasoning_effort": result.agent_metadata.reasoning_effort,
                }
            }
            if result.agent_metadata
            else {}
        ),
    }


def _parse_review_result(raw: str) -> ReviewResult:
    payload = _load_json_object(raw)
    return _parse_review_result_payload(payload)


def _parse_review_result_payload(payload: dict, require_sources: bool = False) -> ReviewResult:
    summary = str(payload.get("summary", "")).strip()
    overall_risk = str(payload.get("overall_risk", "")).strip().lower()
    if not summary:
        raise ReviewAgentError("Missing summary field")
    if overall_risk not in VALID_SEVERITIES:
        raise ReviewAgentError(f"Invalid overall_risk value: {overall_risk!r}")

    findings_payload = payload.get("findings", [])
    if not isinstance(findings_payload, list):
        raise ReviewAgentError("findings must be an array")

    findings: list[ReviewFinding] = []
    for item in findings_payload:
        severity = str(item.get("severity", "")).strip().lower()
        file_path = str(item.get("file", "")).strip()
        title = str(item.get("title", "")).strip()
        body = str(item.get("body", "")).strip()
        suggestion = str(item.get("suggestion", "")).strip()
        line = item.get("line")
        sources = _parse_finding_sources(item.get("sources"), require_sources=require_sources)
        opinions = _parse_finding_opinions(item.get("opinions"))
        if severity not in VALID_SEVERITIES:
            raise ReviewAgentError(f"Invalid finding severity: {severity!r}")
        if not file_path or not title or not body:
            raise ReviewAgentError("Each finding requires file, title, and body")
        if not isinstance(line, int) or line < 1:
            raise ReviewAgentError("Each finding line must be a positive integer")
        findings.append(
            ReviewFinding(
                severity=severity,
                file=file_path,
                line=line,
                title=title,
                body=body,
                suggestion=suggestion or None,
                sources=sources,
                opinions=opinions,
            )
        )

    return ReviewResult(
        summary=summary,
        overall_risk=overall_risk,
        findings=tuple(findings),
    )


def _parse_finding_sources(value, require_sources: bool) -> tuple[ReviewProvider, ...]:
    if value is None:
        if require_sources:
            raise ReviewAgentError("Each synthesized finding requires a non-empty sources array")
        return ()
    if not isinstance(value, list) or not value:
        raise ReviewAgentError("finding sources must be a non-empty array when provided")

    normalized: list[ReviewProvider] = []
    for item in value:
        provider = str(item).strip().lower()
        if provider not in VALID_PROVIDERS:
            raise ReviewAgentError(f"Invalid finding source provider: {provider!r}")
        if provider not in normalized:
            normalized.append(provider)  # type: ignore[arg-type]
    if require_sources and not normalized:
        raise ReviewAgentError("Each synthesized finding requires at least one source")
    return tuple(normalized)


def _parse_finding_opinions(value) -> tuple[ReviewOpinion, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReviewAgentError("finding opinions must be an array when provided")

    opinions: list[ReviewOpinion] = []
    for item in value:
        if not isinstance(item, dict):
            raise ReviewAgentError("each finding opinion must be an object")
        provider = str(item.get("provider", "")).strip().lower()
        verdict = str(item.get("verdict", "")).strip().lower()
        reason = str(item.get("reason", "")).strip()
        if provider not in VALID_PROVIDERS:
            raise ReviewAgentError(f"Invalid finding opinion provider: {provider!r}")
        if verdict not in VALID_OPINION_VERDICTS:
            raise ReviewAgentError(f"Invalid finding opinion verdict: {verdict!r}")
        opinions.append(
            ReviewOpinion(
                provider=provider,  # type: ignore[arg-type]
                verdict=verdict,  # type: ignore[arg-type]
                reason=reason or None,
            )
        )
    return tuple(opinions)


def _parse_reviewer_overview_payload(value) -> dict[ReviewProvider, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReviewAgentError("reviewer_overview must be an object when provided")

    overview: dict[ReviewProvider, str] = {}
    for provider, text_value in value.items():
        normalized_provider = str(provider).strip().lower()
        if normalized_provider not in VALID_REVIEWER_OVERVIEW_KEYS:
            raise ReviewAgentError(f"Invalid reviewer_overview key: {provider!r}")
        summary = str(text_value).strip()
        if not summary:
            raise ReviewAgentError(f"reviewer_overview[{provider!r}] must be a non-empty string")
        overview[normalized_provider] = summary  # type: ignore[assignment]
    return overview


def _reconcile_council_findings(
    final_result: ReviewResult,
    codex_result: ReviewResult,
    claude_result: ReviewResult,
) -> tuple[ReviewResult, int, int]:
    """Ensure findings from base reviews are not silently dropped by synthesis.

    For each finding in *codex_result* and *claude_result*:
    - If a matching finding exists in *final_result*, the provider is added to
      its ``sources`` and ``opinions`` (attribution update).
    - If no match is found, the base finding is preserved as-is (preservation).

    Returns ``(reconciled_result, preserved_count, attribution_updates)``.
    """
    reconciled = list(final_result.findings)
    preserved_count = 0
    attribution_updates = 0

    for provider, base_result in (("codex", codex_result), ("claude", claude_result)):
        for base_finding in base_result.findings:
            match_index = _find_covering_finding_index(reconciled, base_finding)
            if match_index is None:
                reconciled.append(_preserved_base_finding(base_finding, provider))
                preserved_count += 1
                continue

            updated_finding, changed = _merge_provider_attribution(reconciled[match_index], base_finding, provider)
            if changed:
                reconciled[match_index] = updated_finding
                attribution_updates += 1

    overall_risk = _highest_severity(final_result.overall_risk, *(finding.severity for finding in reconciled))
    summary = final_result.summary.strip()
    if preserved_count:
        summary = f"{summary} Preserved {preserved_count} finding(s) from the base reviews.".strip()

    return (
        ReviewResult(
            summary=summary,
            overall_risk=overall_risk,
            findings=tuple(reconciled),
            token_usage=final_result.token_usage,
            agent_metadata=final_result.agent_metadata,
            participants=final_result.participants,
        ),
        preserved_count,
        attribution_updates,
    )


def _find_covering_finding_index(findings: list[ReviewFinding], base_finding: ReviewFinding) -> int | None:
    for index, finding in enumerate(findings):
        if findings_match(finding, base_finding):
            return index
    return None


def _merge_provider_attribution(
    finding: ReviewFinding,
    base_finding: ReviewFinding,
    provider: ReviewProvider,
) -> tuple[ReviewFinding, bool]:
    changed = False

    sources = list(finding.sources)
    if provider not in sources:
        sources.append(provider)
        changed = True

    opinions = list(finding.opinions)
    if not any(opinion.provider == provider for opinion in opinions):
        opinions.append(
            ReviewOpinion(
                provider=provider,
                verdict="found",
                reason=f"Raised in the {provider} base review.",
            )
        )
        changed = True

    if not changed:
        return finding, False

    normalized_sources = tuple(sorted(dict.fromkeys(sources)))
    return (
        ReviewFinding(
            severity=finding.severity,
            file=finding.file,
            line=finding.line,
            title=finding.title,
            body=finding.body,
            suggestion=finding.suggestion or base_finding.suggestion,
            sources=normalized_sources,  # type: ignore[arg-type]
            opinions=tuple(opinions),
            snippet=finding.snippet,
            snippet_start_line=finding.snippet_start_line,
            snippet_language=finding.snippet_language,
        ),
        True,
    )


def _preserved_base_finding(finding: ReviewFinding, provider: ReviewProvider) -> ReviewFinding:
    return ReviewFinding(
        severity=finding.severity,
        file=finding.file,
        line=finding.line,
        title=finding.title,
        body=finding.body,
        suggestion=finding.suggestion,
        sources=(provider,),
        opinions=(
            ReviewOpinion(
                provider=provider,
                verdict="found",
                reason=f"Preserved from the {provider} base review after synthesis omitted it.",
            ),
        ),
        snippet=finding.snippet,
        snippet_start_line=finding.snippet_start_line,
        snippet_language=finding.snippet_language,
    )


def _highest_severity(initial: str, *values: str) -> str:
    ranking = {"high": 0, "medium": 1, "low": 2}
    best = initial
    for value in values:
        if ranking[value] < ranking[best]:
            best = value
    return best


def _summarize_participants(
    runs: list[_ProviderRun],
    reviewer_overview: dict[ReviewProvider, str] | None = None,
) -> tuple[ReviewParticipant, ...]:
    summaries: dict[tuple[str, str | None, str | None], ReviewParticipant] = {}
    reviewer_overview = reviewer_overview or {}
    for run in runs:
        key = (run.provider, run.metadata.model, run.metadata.reasoning_effort)
        current = summaries.get(key)
        phases = tuple(dict.fromkeys((*(current.phases if current else ()), run.phase)))
        usage = _merge_token_usage(current.token_usage if current else None, run.token_usage)
        summary = reviewer_overview.get(run.provider) if run.phase == "review" else None
        if not summary:
            summary = (current.summary if current else None) or run.summary or None
        overall_risk = (current.overall_risk if current else None) or run.overall_risk
        summaries[key] = ReviewParticipant(
            metadata=run.metadata,
            phases=phases,
            token_usage=usage,
            summary=summary,
            overall_risk=overall_risk,
        )
    return tuple(
        participant
        for _, participant in sorted(
            summaries.items(),
            key=lambda item: (
                0 if item[1].metadata.provider == "codex" else 1,
                0 if "review" in item[1].phases else 1,
                item[1].metadata.reasoning_effort or "",
            ),
        )
    )


def _merge_token_usage(
    base: ReviewTokenUsage | None,
    update: ReviewTokenUsage | None,
) -> ReviewTokenUsage | None:
    if base is None:
        return update
    if update is None:
        return base
    return ReviewTokenUsage(
        input_tokens=base.input_tokens + update.input_tokens,
        cached_input_tokens=base.cached_input_tokens + update.cached_input_tokens,
        output_tokens=base.output_tokens + update.output_tokens,
        cache_creation_input_tokens=base.cache_creation_input_tokens + update.cache_creation_input_tokens,
        cost_usd=((base.cost_usd or 0.0) + (update.cost_usd or 0.0)) if (base.cost_usd is not None or update.cost_usd is not None) else None,
        cached_input_included_in_input=base.cached_input_included_in_input,
        cache_creation_included_in_input=base.cache_creation_included_in_input,
    )


def _token_usage_to_payload(token_usage: ReviewTokenUsage | None) -> dict | None:
    if token_usage is None:
        return None
    return {
        "input_tokens": token_usage.input_tokens,
        "cached_input_tokens": token_usage.cached_input_tokens,
        "cache_creation_input_tokens": token_usage.cache_creation_input_tokens,
        "output_tokens": token_usage.output_tokens,
        "total_tokens": token_usage.total_tokens,
        "cost_usd": token_usage.cost_usd,
    }


def _run_command(
    command: tuple[str, ...],
    prompt: str,
    cwd: Path,
    timeout_sec: int,
    provider_name: str,
) -> str:
    try:
        completed = subprocess.run(
            list(command),
            input=prompt,
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewAgentError(f"{provider_name} CLI timed out after {timeout_sec}s") from exc

    if completed.returncode != 0:
        raise ReviewAgentError(
            completed.stderr.strip() or completed.stdout.strip() or f"{provider_name} CLI failed"
        )
    return completed.stdout.strip()


def _extract_claude_result(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise ReviewAgentError("Empty Claude CLI output")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate

    if not isinstance(payload, dict):
        return candidate

    if payload.get("type") != "result" or "result" not in payload:
        return candidate

    result_text = str(payload.get("result", "")).strip()
    if payload.get("is_error"):
        raise ReviewAgentError(result_text or "Claude CLI returned an error result")
    if not result_text:
        raise ReviewAgentError("Claude CLI returned an empty result field")
    return result_text


def _extract_claude_result_from_stream(raw: str) -> str:
    final_payload: dict | None = None
    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            final_payload = payload

    if not final_payload:
        raise ReviewAgentError("Claude stream did not include a final result event")

    result_text = str(final_payload.get("result", "")).strip()
    if final_payload.get("is_error"):
        raise ReviewAgentError(result_text or "Claude CLI returned an error result")
    if not result_text:
        raise ReviewAgentError("Claude CLI returned an empty result field")
    return result_text


def _extract_codex_final_message(raw: str) -> str:
    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item") or {}
        if item.get("type") != "agent_message":
            continue
        text = str(item.get("text", "")).strip()
        if text:
            return text
    raise ReviewAgentError("Codex event stream did not include a final agent message")


def _record_codex_events(raw: str, trace: RunTrace) -> ReviewTokenUsage | None:
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    saw_usage = False

    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            trace.write("provider", "codex.event.invalid_json", raw=event)
            continue

        trace.write("provider", "codex.event", payload=payload)
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input_tokens", 0) or 0)
        cached_input_tokens += int(usage.get("cached_input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
        saw_usage = True

    if not saw_usage:
        return None

    token_usage = ReviewTokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    trace.write(
        "provider",
        "codex.usage",
        input_tokens=token_usage.input_tokens,
        cached_input_tokens=token_usage.cached_input_tokens,
        output_tokens=token_usage.output_tokens,
        total_tokens=token_usage.total_tokens,
    )
    return token_usage


def _record_claude_events(raw: str, output_format: str, trace: RunTrace) -> ReviewTokenUsage | None:
    if output_format == "stream-json":
        usage = _record_claude_stream_events(raw, trace)
        if usage:
            trace.write(
                "provider",
                "claude.usage",
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=usage.cost_usd,
            )
        return usage

    if output_format == "json":
        usage = _record_claude_json_event(raw, trace)
        if usage:
            trace.write(
                "provider",
                "claude.usage",
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=usage.cost_usd,
            )
        return usage

    trace.write("provider", "claude.output.text", output=raw)
    return None


def _record_claude_stream_events(raw: str, trace: RunTrace) -> ReviewTokenUsage | None:
    message_usage_by_id: dict[str, dict[str, int]] = {}
    anonymous_usage: list[dict[str, int]] = []
    final_payload: dict | None = None

    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            trace.write("provider", "claude.event.invalid_json", raw=event)
            continue

        trace.write("provider", "claude.event", payload=payload)
        if not isinstance(payload, dict):
            continue

        usage = _extract_claude_usage_dict(payload)
        if usage:
            message_id = str(payload.get("message_id") or payload.get("id") or "").strip()
            if message_id:
                previous = message_usage_by_id.get(message_id, {})
                message_usage_by_id[message_id] = _merge_usage_counts(previous, usage)
            else:
                anonymous_usage.append(usage)

        if payload.get("type") == "result":
            final_payload = payload

    if final_payload:
        final_usage = _extract_claude_usage_dict(final_payload)
        if final_usage:
            return _build_claude_token_usage(final_usage, final_payload)

    if not message_usage_by_id and not anonymous_usage:
        return None

    aggregate = {"input_tokens": 0, "cached_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
    for usage in message_usage_by_id.values():
        aggregate = _merge_usage_counts(aggregate, usage, additive=True)
    for usage in anonymous_usage:
        aggregate = _merge_usage_counts(aggregate, usage, additive=True)
    return _build_claude_token_usage(aggregate, final_payload)


def _record_claude_json_event(raw: str, trace: RunTrace) -> ReviewTokenUsage | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        trace.write("provider", "claude.event.invalid_json", raw=raw)
        return None
    trace.write("provider", "claude.event", payload=payload)
    if not isinstance(payload, dict):
        return None
    usage = _extract_claude_usage_dict(payload)
    if not usage:
        return None
    return _build_claude_token_usage(usage, payload)


def _extract_claude_usage_dict(payload: dict) -> dict[str, int] | None:
    candidates: list[dict] = []
    usage = payload.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)
    message = payload.get("message")
    if isinstance(message, dict):
        message_usage = message.get("usage")
        if isinstance(message_usage, dict):
            candidates.append(message_usage)

    for candidate in candidates:
        values = {
            "input_tokens": int(candidate.get("input_tokens", 0) or 0),
            "cached_input_tokens": int(candidate.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(candidate.get("cache_creation_input_tokens", 0) or 0),
            "output_tokens": int(candidate.get("output_tokens", 0) or 0),
        }
        if any(values.values()):
            return values
    return None


def _merge_usage_counts(
    base: dict[str, int],
    update: dict[str, int],
    additive: bool = False,
) -> dict[str, int]:
    merged = dict(base)
    for key in ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"):
        if additive:
            merged[key] = int(merged.get(key, 0) or 0) + int(update.get(key, 0) or 0)
        else:
            merged[key] = max(int(merged.get(key, 0) or 0), int(update.get(key, 0) or 0))
    return merged


def _build_claude_token_usage(
    usage: dict[str, int],
    payload: dict | None,
) -> ReviewTokenUsage:
    cost_usd: float | None = None
    if isinstance(payload, dict) and payload.get("total_cost_usd") is not None:
        try:
            cost_usd = float(payload["total_cost_usd"])
        except (TypeError, ValueError):
            cost_usd = None
    return ReviewTokenUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        cost_usd=cost_usd,
        cached_input_included_in_input=False,
        cache_creation_included_in_input=False,
    )


def _load_json_object(raw: str) -> dict:
    """Extract the first top-level JSON object from *raw*.

    First attempts a direct ``json.loads``; on failure scans for the outermost
    ``{…}`` block and retries.  Raises ``ReviewAgentError`` if no valid JSON
    object is found or the top-level type is not a dict.
    """
    candidate = raw.strip()
    if not candidate:
        raise ReviewAgentError("Empty review agent output")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ReviewAgentError("Output was not valid JSON")
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ReviewAgentError("Output was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise ReviewAgentError("Top-level JSON must be an object")
    return payload


def _extract_agent_metadata(provider_name: str, command: tuple[str, ...]) -> ReviewAgentMetadata:
    model: str | None = None
    reasoning_effort: str | None = None

    index = 0
    while index < len(command):
        part = command[index]
        if part in {"-m", "--model"} and index + 1 < len(command):
            model = command[index + 1]
            index += 2
            continue
        if part.startswith("--model="):
            model = part.split("=", 1)[1]
            index += 1
            continue
        if part in {"--effort"} and index + 1 < len(command):
            reasoning_effort = command[index + 1]
            index += 2
            continue
        if part.startswith("--effort="):
            reasoning_effort = part.split("=", 1)[1]
            index += 1
            continue
        if part in {"-c", "--config"} and index + 1 < len(command):
            reasoning_effort = _extract_reasoning_effort(command[index + 1]) or reasoning_effort
            index += 2
            continue
        if part.startswith("--config="):
            reasoning_effort = _extract_reasoning_effort(part.split("=", 1)[1]) or reasoning_effort
            index += 1
            continue
        index += 1

    return ReviewAgentMetadata(
        provider=provider_name,  # type: ignore[arg-type]
        model=_clean_value(model),
        reasoning_effort=_clean_value(reasoning_effort),
    )


def _detect_claude_output_format(command: list[str]) -> str | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "--output-format" and index + 1 < len(command):
            return command[index + 1].strip()
        if part.startswith("--output-format="):
            return part.split("=", 1)[1].strip()
        index += 1
    return None


def _normalize_claude_command(command: list[str]) -> tuple[str, ...]:
    output_format = _detect_claude_output_format(command)
    normalized = list(command)
    if output_format == "stream-json" and "--verbose" not in normalized:
        normalized.append("--verbose")
    return tuple(normalized)


def _extract_reasoning_effort(value: str) -> str | None:
    if "=" not in value:
        return None
    key, raw_value = value.split("=", 1)
    if key.strip() not in {"model_reasoning_effort", "reasoning_effort"}:
        return None
    return raw_value


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\"'")
    return cleaned or None
