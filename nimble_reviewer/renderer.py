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

    lines.extend(["## Summary", ""])
    if result.participants:
        lines.extend(_render_summary_with_council(result, findings, comparison))
    else:
        lines.extend([
            f"Overall risk: **{result.overall_risk.upper()}**",
        ])
        if findings:
            lines.append(_render_finding_counts(findings))
        else:
            lines.append("No actionable issues found.")
        lines.append(_render_comparison_counts(comparison))
        lines.extend(["", result.summary.strip(), ""])
        if result.agent_metadata:
            lines.extend([_render_agent_metadata(result.agent_metadata), ""])

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


def _render_agent_metadata(agent_metadata) -> str:
    model = agent_metadata.model or "default"
    reasoning = agent_metadata.reasoning_effort or "default"
    return (
        "Agent: "
        f"`{agent_metadata.provider}` "
        f"Model: `{model}` "
        f"Reasoning: `{reasoning}`"
    )


def _render_summary_with_council(result: ReviewResult, findings, comparison: ReviewComparison) -> list[str]:
    review_participants = [p for p in result.participants if "review" in p.phases]
    synthesis_participants = [p for p in result.participants if "synthesis" in p.phases]

    lines: list[str] = []

    for participant in review_participants:
        label = _provider_label(participant.metadata.provider)
        meta = _participant_meta_inline(participant)
        lines.extend([f"**{label}** · {meta}"])
        if participant.summary:
            lines.extend([f"> {participant.summary}", ""])
        else:
            lines.append("")

    for participant in synthesis_participants:
        label = _provider_label(participant.metadata.provider)
        meta = _participant_meta_inline(participant)
        lines.extend([f"**Overall** · {meta} (synthesis)"])
        summary_text = participant.summary or result.summary
        if summary_text:
            lines.extend([f"> {summary_text}", ""])
        else:
            lines.append("")

    lines.extend([f"Overall risk: **{result.overall_risk.upper()}**"])
    if findings:
        lines.append(_render_finding_counts(findings))
    else:
        lines.append("No actionable issues found.")
    lines.extend([_render_comparison_counts(comparison), ""])

    return lines


def _participant_meta_inline(participant: ReviewParticipant) -> str:
    parts = []
    if participant.metadata.model:
        parts.append(f"`{participant.metadata.model}`")
    if participant.metadata.reasoning_effort:
        parts.append(f"reasoning `{participant.metadata.reasoning_effort}`")
    return " · ".join(parts) if parts else "`default`"


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
        lines.extend([finding.body, ""])
    lines.extend([
        f"Source: `{finding.file}:{finding.line}`",
        "",
        f"Status: `{_render_finding_status(finding_state.status)}`",
    ])
    if tier in ("standard", "detailed"):
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



