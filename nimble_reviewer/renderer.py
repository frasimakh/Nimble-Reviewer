from __future__ import annotations

from datetime import UTC, datetime

from nimble_reviewer.models import ReviewComparison, ReviewFindingState, ReviewParticipant, ReviewResult

STATUS_START = "<!-- nimble-reviewer:status:start -->"
STATUS_END = "<!-- nimble-reviewer:status:end -->"
REVIEW_START = "<!-- nimble-reviewer:review:start -->"
REVIEW_END = "<!-- nimble-reviewer:review:end -->"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def note_marker(project_id: int, mr_iid: int) -> str:
    return f"<!-- nimble-reviewer:{project_id}:{mr_iid} -->"


def render_success_note(
    project_id: int,
    mr_iid: int,
    source_sha: str,
    result: ReviewResult,
    comparison: ReviewComparison | None = None,
) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    findings = sorted(result.findings, key=lambda item: (SEVERITY_ORDER[item.severity], item.file, item.line))
    comparison = comparison or _default_review_comparison(result)
    current_findings = sorted(
        comparison.current_findings,
        key=lambda item: (SEVERITY_ORDER[item.finding.severity], item.finding.file, item.finding.line),
    )
    resolved_findings = sorted(
        comparison.resolved_findings,
        key=lambda item: (SEVERITY_ORDER[item.severity], item.file, item.line),
    )

    lines = [
        "# Nimble Reviewer",
        "",
        f"Reviewed commit `{source_sha[:12]}` at `{timestamp}`.",
        "",
    ]

    lines.extend(["## Summary", "", f"Overall risk: **{result.overall_risk.upper()}**"])
    if findings:
        lines.append(_render_finding_counts(findings))
    else:
        lines.append("No actionable issues found.")
    lines.append(_render_comparison_counts(comparison))
    lines.extend(["", result.summary.strip(), ""])

    if current_findings:
        lines.append("## Current findings")
        lines.append("")
        for index, finding_state in enumerate(current_findings, start=1):
            lines.extend(_render_finding_block(index, finding_state))
    else:
        lines.extend(["## Current findings", "", "- No actionable issues found.", ""])

    lines.extend(["## Resolved since previous review", ""])
    if resolved_findings:
        for finding in resolved_findings:
            lines.append(_render_resolved_finding(finding))
        lines.append("")
    else:
        lines.extend(["- No findings resolved since previous review.", ""])

    if result.participants:
        lines.extend(["## Council", ""])
        lines.extend(_render_council_sections(result.participants))
        lines.append("")
    else:
        if result.agent_metadata:
            lines.extend([_render_agent_metadata(result.agent_metadata), ""])
        if result.token_usage:
            lines.extend([_render_token_usage(result.token_usage), ""])

    return _compose_note(project_id, mr_iid, "", "\n".join(lines).strip())


def render_failure_note(project_id: int, mr_iid: int, source_sha: str, error: str, existing_body: str | None) -> str:
    review_block = extract_review_block(existing_body) or "_No successful review has been posted yet._"
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    status_block = (
        f"> Review for `{source_sha[:12]}` failed at `{timestamp}`.\n"
        f">\n> {error.strip() or 'Unknown error.'}"
    )
    return _compose_note(project_id, mr_iid, status_block, review_block)


def extract_review_block(body: str | None) -> str | None:
    if not body:
        return None
    start = body.find(REVIEW_START)
    end = body.find(REVIEW_END)
    if start == -1 or end == -1 or end < start:
        return None
    content = body[start + len(REVIEW_START) : end].strip()
    return content or None


def _compose_note(project_id: int, mr_iid: int, status_block: str, review_block: str) -> str:
    status = status_block.strip()
    review = review_block.strip()
    return "\n".join(
        [
            note_marker(project_id, mr_iid),
            STATUS_START,
            status,
            STATUS_END,
            "",
            REVIEW_START,
            review,
            REVIEW_END,
        ]
    ).strip() + "\n"


def _render_token_usage(token_usage) -> str:
    # Keep the note compact for now. Detailed usage remains in trace files.
    return f"Tokens: `total={token_usage.total_tokens}`"


def _render_agent_metadata(agent_metadata) -> str:
    model = agent_metadata.model or "default"
    reasoning = agent_metadata.reasoning_effort or "default"
    return (
        "Agent: "
        f"`{agent_metadata.provider}` "
        f"Model: `{model}` "
        f"Reasoning: `{reasoning}`"
    )


def _render_participant(participant: ReviewParticipant) -> list[str]:
    details = [
        f"model `{participant.metadata.model or 'default'}`",
        f"reasoning `{participant.metadata.reasoning_effort or 'default'}`",
    ]
    if participant.token_usage:
        details.append(f"tokens `{participant.token_usage.total_tokens}`")
    return [f"- {_provider_label(participant.metadata.provider)}: {', '.join(details)}"]


def _render_council_sections(participants: tuple[ReviewParticipant, ...]) -> list[str]:
    review_participants = [participant for participant in participants if "review" in participant.phases]
    synthesis_participants = [participant for participant in participants if "synthesis" in participant.phases]

    lines: list[str] = []
    for title, group in (("Review", review_participants), ("Synthesis", synthesis_participants)):
        if not group:
            continue
        lines.extend([f"**{title}**", ""])
        for participant in group:
            lines.extend(_render_participant(participant))
        lines.append("")
    if lines and lines[-1] == "":
        return lines[:-1]
    return lines


def _default_review_comparison(result: ReviewResult) -> ReviewComparison:
    return ReviewComparison(
        current_findings=tuple(ReviewFindingState(finding=finding, status="new") for finding in result.findings),
        resolved_findings=(),
    )


def _render_finding_counts(findings) -> str:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] += 1
    parts = [f"`{counts['high']} high`", f"`{counts['medium']} medium`", f"`{counts['low']} low`"]
    return f"Findings: {' '.join(parts)}"


def _render_comparison_counts(comparison: ReviewComparison) -> str:
    new_count = sum(1 for item in comparison.current_findings if item.status == "new")
    still_present_count = sum(1 for item in comparison.current_findings if item.status == "still_present")
    resolved_count = len(comparison.resolved_findings)
    return f"Changes: `{new_count} new` `{still_present_count} still present` `{resolved_count} resolved`"


def _render_finding_block(index: int, finding_state: ReviewFindingState) -> list[str]:
    finding = finding_state.finding
    tier = _finding_tier(finding)
    label = _severity_label(finding.severity, finding.sources)
    lines = [
        f"### {index}. {label}: {finding.title}",
        "",
    ]
    if tier in ("standard", "detailed"):
        if finding.snippet:
            lang = finding.snippet_language or ""
            lines.extend([f"```{lang}", finding.snippet, "```", ""])
        lines.extend([finding.body, ""])
    lines.extend([
        f"Source: `{finding.file}:{finding.line}`",
        "",
        f"Status: `{_render_finding_status(finding_state.status)}`",
    ])
    if tier in ("standard", "detailed"):
        if finding.opinions:
            lines.extend(["", "Council:"])
            for opinion in finding.opinions:
                lines.append(f"`{_provider_label(opinion.provider)}`: {_render_opinion(opinion)}")
        if finding.suggestion:
            lines.extend(["", f"Fix: {finding.suggestion}"])
    lines.extend(["", "---", ""])
    return lines


def _render_resolved_finding(finding) -> str:
    return f"- {_severity_label(finding.severity, finding.sources)}: {finding.title} at `{finding.file}:{finding.line}`"


def _severity_label(severity: str, sources: tuple[str, ...] = ()) -> str:
    emoji = {"high": "🚨", "medium": "⚠️", "low": "💡"}.get(severity, "")
    name = {"high": "High", "medium": "Medium", "low": "Low"}.get(severity, severity.capitalize())
    sources_str = _render_sources_inline(sources)
    if sources_str:
        return f"{emoji} {name} ({sources_str})"
    return f"{emoji} {name}"


def _render_sources_inline(sources: tuple[str, ...]) -> str:
    normalized = tuple(sorted(dict.fromkeys(sources)))
    if normalized == ("claude", "codex"):
        return "Claude + Codex"
    labels = {"claude": "Claude", "codex": "Codex"}
    return " + ".join(labels.get(s, s.capitalize()) for s in normalized)


def _finding_tier(finding) -> str:
    source_count = len(set(finding.sources))
    if finding.severity == "low":
        return "short"
    if finding.severity == "medium" and source_count < 2:
        return "standard"
    return "detailed"


def _render_finding_status(status: str) -> str:
    return {
        "new": "New",
        "still_present": "Still present",
    }.get(status, status.replace("_", " ").title())


def _provider_label(provider: str) -> str:
    return {
        "codex": "Codex",
        "claude": "Claude",
    }.get(provider, provider.capitalize())


def _render_opinion(opinion) -> str:
    verdict = {
        "found": "found independently",
        "agree": "supports inclusion",
        "disagree": "questions inclusion",
        "uncertain": "is uncertain",
    }.get(opinion.verdict, opinion.verdict)
    if opinion.reason:
        return f"{verdict} - {opinion.reason}"
    return verdict


def _render_participant_role(phases: tuple[str, ...]) -> str:
    phase_set = set(phases)
    if phase_set == {"synthesis"}:
        return "final synthesis"
    if phase_set == {"review"}:
        return "independent review"
    return ", ".join(phases) or "review"


