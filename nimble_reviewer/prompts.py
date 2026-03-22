from __future__ import annotations

import json

from nimble_reviewer.models import MergeRequestInfo, ReviewResult


MAX_DIFF_CHARS = 200_000
MAX_DISCUSSION_CHARS = 12_000


def build_review_prompt(
    mr: MergeRequestInfo,
    diff_text: str,
    changed_files: list[str],
    discussion_digest: str | None = None,
    repo_rules_text: str | None = None,
    repo_rules_path: str | None = None,
    repo_rules_truncated: bool = False,
) -> str:
    truncated = False
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS]
        truncated = True

    changed_files_text = "\n".join(f"- {path}" for path in changed_files) or "- No changed files reported"
    repo_rules_section = ""
    if repo_rules_text:
        repo_rules_notice = (
            "\nThe repository-specific rules file was truncated to fit the prompt budget.\n"
            if repo_rules_truncated
            else ""
        )
        repo_rules_section = f"""
Repository-specific review rules from `{repo_rules_path or "repository rules file"}`:
{repo_rules_notice}
```md
{repo_rules_text}
```
"""
    discussion_section = ""
    if discussion_digest:
        discussion_section = f"""
Open and recent merge request discussion context:
```md
{discussion_digest[:MAX_DISCUSSION_CHARS]}
```
"""
    truncation_notice = (
        "\nThe diff was truncated to fit the review budget. Focus on the visible diff and use repository context as needed.\n"
        if truncated
        else ""
    )
    return f"""You are reviewing a GitLab merge request.

Return only strict JSON with this shape:
{{
  "summary": "short review summary",
  "overall_risk": "high|medium|low",
  "findings": [
    {{
      "severity": "high|medium|low",
      "file": "path/to/file.py",
      "line": 123,
      "title": "short title",
      "body": "actionable explanation",
      "suggestion": "optional short fix direction"
    }}
  ]
}}

Rules:
- Focus on correctness, regressions, security, and maintainability issues that matter before merge.
- Omit praise and style nits unless they represent a real risk.
- If there are no actionable issues, return an empty findings array.
- Keep summary concise. Use it to say what this reviewer actually found and how risky it is; do not restate the whole MR.
- Keep each finding title short and specific.
- Include `suggestion` only when you have a concrete, short remediation direction.
- Use repository context when needed, but keep the final output concise and structured.
- Consider existing MR discussion context. Do not restate concerns that have already been convincingly dismissed by discussion unless the visible diff reintroduces the risk.
- Follow repository-specific review rules when provided, unless they conflict with the JSON output contract above.

Merge request metadata:
- Project ID: {mr.project_id}
- MR IID: {mr.mr_iid}
- Title: {mr.title}
- Source branch: {mr.source_branch}
- Target branch: {mr.target_branch}
- Source SHA: {mr.source_sha}
- Web URL: {mr.web_url}

Description:
{mr.description or "(empty)"}
{repo_rules_section}
{discussion_section}

Changed files:
{changed_files_text}
{truncation_notice}
Unified diff:
```diff
{diff_text}
```"""


def build_discussion_reconcile_prompt(
    mr: MergeRequestInfo,
    *,
    discussion_id: str,
    discussion_text: str,
    trigger_note_body: str,
    linked_finding_payload: dict | None,
) -> str:
    finding_json = json.dumps(linked_finding_payload or {}, indent=2, sort_keys=True)
    return f"""You are reconciling a GitLab merge request discussion against an existing AI review finding.

Return only strict JSON with this shape:
{{
  "decision": "keep_open|dismissed_by_discussion|reply_only|no_action",
  "reason": "short rationale",
  "reply_body": "optional short markdown reply"
}}

Rules:
- `dismissed_by_discussion` means the human explanation convincingly resolves the bot concern.
- Use `dismissed_by_discussion` only when the discussion contains a concrete technical explanation that removes the risk.
- Use `reply_only` when the bot should respond but the concern should remain open or unresolved.
- Use `keep_open` when the thread should stay open without any bot reply.
- Use `no_action` when the note is irrelevant, too weak, or too ambiguous to act on.
- Keep `reason` short and specific.
- Include `reply_body` only when the bot should actually post a reply.
- Never ask for more information. Decide from the discussion context.

Merge request:
- Project ID: {mr.project_id}
- MR IID: {mr.mr_iid}
- Title: {mr.title}
- Source SHA: {mr.source_sha}
- Web URL: {mr.web_url}

Tracked finding:
```json
{finding_json}
```

Discussion {discussion_id}:
```md
{discussion_text[:MAX_DISCUSSION_CHARS]}
```

Latest human note:
```md
{trigger_note_body[:4000]}
```"""


def build_council_synthesis_prompt(
    base_review_prompt: str,
    codex_result: ReviewResult,
    claude_result: ReviewResult,
) -> str:
    return f"""You are the final synthesis reviewer for a merge request review council.

Return only strict JSON with this shape:
{{
  "summary": "short final review summary",
  "overall_risk": "high|medium|low",
  "reviewer_overview": {{
    "codex": "short note on what Codex saw",
    "claude": "short note on what Claude saw"
  }},
  "findings": [
    {{
      "severity": "high|medium|low",
      "file": "path/to/file.py",
      "line": 123,
      "title": "short title",
      "body": "actionable explanation",
      "suggestion": "optional short fix direction",
      "sources": ["codex"] | ["claude"] | ["codex", "claude"],
      "opinions": [
        {{
          "provider": "codex|claude",
          "verdict": "found|agree|disagree|uncertain",
          "reason": "optional short rationale"
        }}
      ]
    }}
  ]
}}

Rules:
- Produce the final actionable review output for the merge request.
- Use the two base reviews as inputs.
- Do not drop findings from either base review.
- Every base-review finding must appear in the final output either as a merged issue or as its own standalone issue.
- You may merge findings when they clearly refer to the same underlying problem.
- Use `sources` only for the models that independently found the issue in their base reviews.
- Use `sources=["codex","claude"]` only when both base reviews independently surfaced the same underlying issue.
- `opinions` is optional, but include it when it improves transparency about how each model viewed the final finding.
- Use `found` when the model raised the issue in its own base review.
- Use `agree` when the model did not raise it independently but would keep it in the final review.
- Use `disagree` when the model's base review conflicts with keeping that issue.
- Use `uncertain` when the support is mixed or weak.
- Prefer keeping unique but credible findings from one model instead of dropping them.
- Your job is synthesis and de-duplication, not suppression.
- Omit praise and style nits unless they represent a real risk.
- Write `summary` as the final council verdict only. It will be shown directly to developers in the MR comment, so be direct and opinionated: state whether the MR is safe to merge, needs fixes, or has blockers. If concerns are minor or raised by only one model, say so (e.g. "Safe to merge — concerns are minimal and low-confidence"). Do not just list findings; give a clear recommendation.
- Fill `reviewer_overview.codex` and `reviewer_overview.claude` with short reviewer-specific notes describing what each model independently surfaced, emphasized, or agreed with.
- Do not repeat or paraphrase the full base-review summaries inside `summary`; use `reviewer_overview` for reviewer-specific attribution.
- Prefer highlighting overlap and unique findings explicitly, e.g. one reviewer confirmed a shared issue while the other added an extra concern.

Original review brief:
{base_review_prompt}

Codex base review:
```json
{_render_review_result_json(codex_result)}
```

Claude base review:
```json
{_render_review_result_json(claude_result)}
```
"""


def _render_review_result_json(result: ReviewResult) -> str:
    payload = {
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
            }
            for finding in result.findings
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
