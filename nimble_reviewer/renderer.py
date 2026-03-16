from __future__ import annotations

from datetime import UTC, datetime

from nimble_reviewer.models import ReviewResult

STATUS_START = "<!-- nimble-reviewer:status:start -->"
STATUS_END = "<!-- nimble-reviewer:status:end -->"
REVIEW_START = "<!-- nimble-reviewer:review:start -->"
REVIEW_END = "<!-- nimble-reviewer:review:end -->"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def note_marker(project_id: int, mr_iid: int) -> str:
    return f"<!-- nimble-reviewer:{project_id}:{mr_iid} -->"


def render_success_note(project_id: int, mr_iid: int, source_sha: str, result: ReviewResult) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    findings = sorted(result.findings, key=lambda item: (SEVERITY_ORDER[item.severity], item.file, item.line))

    lines = [
        "# Nimble Reviewer",
        "",
        f"Reviewed commit `{source_sha[:12]}` at `{timestamp}`.",
        "",
    ]

    if result.agent_metadata:
        lines.extend([_render_agent_metadata(result.agent_metadata), ""])

    if result.token_usage:
        lines.extend([_render_token_usage(result.token_usage), ""])

    lines.extend(["## Summary", "", f"Overall risk: **{result.overall_risk.upper()}**"])
    if findings:
        lines.append(_render_finding_counts(findings))
    else:
        lines.append("No actionable issues found.")
    lines.extend(["", result.summary.strip(), ""])

    if findings:
        lines.append("## Findings")
        lines.append("")
        for index, finding in enumerate(findings, start=1):
            lines.extend(_render_finding_block(index, finding))
    else:
        lines.extend(["## Findings", "", "- No actionable issues found.", ""])

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
    parts = [
        "Tokens:",
        f"`input={token_usage.input_tokens}`",
        f"`cached_input={token_usage.cached_input_tokens}`",
    ]
    if getattr(token_usage, "cache_creation_input_tokens", 0):
        parts.append(f"`cache_write={token_usage.cache_creation_input_tokens}`")
    parts.extend(
        [
            f"`output={token_usage.output_tokens}`",
            f"`total={token_usage.total_tokens}`",
        ]
    )
    if getattr(token_usage, "cost_usd", None) is not None:
        parts.append(f"`cost_usd={token_usage.cost_usd:.6f}`")
    return " ".join(parts)


def _render_agent_metadata(agent_metadata) -> str:
    model = agent_metadata.model or "default"
    reasoning = agent_metadata.reasoning_effort or "default"
    return (
        "Agent: "
        f"`{agent_metadata.provider}` "
        f"Model: `{model}` "
        f"Reasoning: `{reasoning}`"
    )


def _render_finding_counts(findings) -> str:
    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] += 1
    parts = [f"`{counts['high']} high`", f"`{counts['medium']} medium`", f"`{counts['low']} low`"]
    return f"Findings: {' '.join(parts)}"


def _render_finding_block(index: int, finding) -> list[str]:
    lines = [
        f"### {index}. {_severity_label(finding.severity)}: {finding.title}",
        "",
        finding.body,
        "",
        f"Source: `{finding.file}:{finding.line}`",
    ]
    if finding.snippet:
        lines.extend(
            [
                "",
                "```" + (finding.snippet_language or ""),
                finding.snippet,
                "```",
            ]
        )
    if finding.suggestion:
        lines.extend(["", f"Fix: {finding.suggestion}"])
    lines.extend(["", "---", ""])
    return lines


def _severity_label(severity: str) -> str:
    return {
        "high": "High",
        "medium": "Warning",
        "low": "Low",
    }.get(severity, severity.capitalize())
