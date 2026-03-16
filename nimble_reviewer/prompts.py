from __future__ import annotations

from nimble_reviewer.models import MergeRequestInfo

MAX_DIFF_CHARS = 200_000


def build_review_prompt(
    mr: MergeRequestInfo,
    diff_text: str,
    changed_files: list[str],
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
- Keep summary concise.
- Keep each finding title short and specific.
- Include `suggestion` only when you have a concrete, short remediation direction.
- Use repository context when needed, but keep the final output concise and structured.
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

Changed files:
{changed_files_text}
{truncation_notice}
Unified diff:
```diff
{diff_text}
```"""
