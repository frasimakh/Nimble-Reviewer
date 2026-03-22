from __future__ import annotations

from datetime import UTC, datetime

from nimble_reviewer.models import ReviewComparison, ReviewFinding, ReviewParticipant, ReviewResult, ReviewSummaryMetrics

STATUS_START = "<!-- nimble-reviewer:status:start -->"
STATUS_END = "<!-- nimble-reviewer:status:end -->"
REVIEW_START = "<!-- nimble-reviewer:review:start -->"
REVIEW_END = "<!-- nimble-reviewer:review:end -->"


def note_marker(project_id: int, mr_iid: int) -> str:
    return f"<!-- nimble-reviewer:{project_id}:{mr_iid} -->"


def render_success_note(
    project_id: int,
    mr_iid: int,
    source_sha: str,
    result: ReviewResult,
    *,
    metrics: ReviewSummaryMetrics,
    comparison: ReviewComparison | None = None,
    unplaced_findings: tuple[ReviewFinding, ...] = (),
) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "# Nimble Reviewer",
        "",
        f"Reviewed commit `{source_sha[:12] or '-'}` at `{timestamp}`.",
        "",
        "## Summary",
        "",
    ]

    if result.participants:
        lines.extend(_render_summary_with_council(result))
    else:
        lines.extend([f"Overall risk: **{result.overall_risk.upper()}**", "", result.summary.strip(), ""])

    lines.extend(
        [
            f"Open findings: `{metrics.open_count}`",
            (
                "Changes: "
                f"`{metrics.new_count} new` "
                f"`{metrics.still_present_count} still present` "
                f"`{metrics.resolved_count} resolved` "
                f"`{metrics.dismissed_count} dismissed by discussion` "
                f"`{metrics.unplaced_count} unplaced`"
            ),
            "",
        ]
    )

    if unplaced_findings:
        lines.extend(["## Unplaced Findings", ""])
        for finding in unplaced_findings:
            lines.append(f"- {_severity_label(finding.severity)}: {finding.title} at `{finding.file}:{finding.line}`")
        lines.append("")
    else:
        lines.extend(["## Unplaced Findings", "", "- No unplaced findings.", ""])

    if comparison and comparison.resolved_findings:
        lines.extend(["## Resolved In Code", ""])
        for finding in comparison.resolved_findings:
            lines.append(f"- {_severity_label(finding.severity)}: {finding.title} at `{finding.file}:{finding.line}`")
        lines.append("")

    return _compose_note(project_id, mr_iid, "", "\n".join(lines).strip())


def render_failure_note(project_id: int, mr_iid: int, source_sha: str, error: str, existing_body: str | None) -> str:
    review_block = extract_review_block(existing_body) or "_No successful review has been posted yet._"
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    status_block = (
        f"> Review for `{source_sha[:12] or '-'}` failed at `{timestamp}`.\n"
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


def _render_summary_with_council(result: ReviewResult) -> list[str]:
    review_participants = [p for p in result.participants if "review" in p.phases]
    synthesis_participants = [p for p in result.participants if "synthesis" in p.phases]
    lines: list[str] = []

    for participant in review_participants:
        label = _provider_label(participant.metadata.provider)
        meta = _participant_meta_inline(participant)
        risk = _participant_risk_inline(participant)
        lines.extend([f"**{label}** · {risk} · {meta}"])
        if participant.summary:
            lines.extend([f"> {participant.summary}", ""])
        else:
            lines.append("")

    for participant in synthesis_participants:
        meta = _participant_meta_inline(participant)
        lines.extend([f"**Council** · overall risk **{result.overall_risk.upper()}** · {meta} (synthesis)"])
        summary_text = _synthesis_meta_summary(review_participants, result)
        if summary_text:
            lines.extend([f"> {summary_text}", ""])
        else:
            lines.append("")

    if not synthesis_participants:
        lines.extend([f"**Council** · overall risk **{result.overall_risk.upper()}**", f"> {result.summary.strip()}", ""])
    return lines


def _synthesis_meta_summary(review_participants: list[ReviewParticipant], result: ReviewResult) -> str:
    if not review_participants:
        return result.summary or ""
    labels = [_provider_label(p.metadata.provider) for p in review_participants]
    risks = [p.overall_risk or "unknown" for p in review_participants]
    all_agree = len(set(r.lower() for r in risks)) == 1
    reviewer_str = " і ".join(labels)
    if all_agree:
        verdict = f"{reviewer_str} незалежно підтвердили ризик **{risks[0].upper()}**"
    else:
        risk_parts = ", ".join(f"{l} ({r.upper()})" for l, r in zip(labels, risks))
        verdict = f"Оцінки розійшлись: {risk_parts}"
    n = len(result.findings)
    finding_str = f"{n} {'знахідка' if n == 1 else 'знахідок'}" if n else "знахідок немає"
    return f"{verdict} — {finding_str}."


def _participant_meta_inline(participant: ReviewParticipant) -> str:
    parts = []
    if participant.metadata.model:
        parts.append(f"`{participant.metadata.model}`")
    if participant.metadata.reasoning_effort:
        parts.append(f"reasoning `{participant.metadata.reasoning_effort}`")
    return " · ".join(parts) if parts else "`default`"


def _participant_risk_inline(participant: ReviewParticipant) -> str:
    risk = (participant.overall_risk or "unknown").upper()
    return f"risk **{risk}**"


def _severity_label(severity: str) -> str:
    emoji = {"high": "🚨 High", "medium": "⚠️ Medium", "low": "💡 Low"}.get(severity, severity.capitalize())
    return emoji


def _provider_label(provider: str) -> str:
    return {"codex": "Codex", "claude": "Claude"}.get(provider, provider.capitalize())
