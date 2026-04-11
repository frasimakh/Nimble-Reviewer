from __future__ import annotations

import json
import re

from nimble_reviewer.models import MergeRequestInfo, ReviewResult


MAX_DIFF_CHARS = 200_000
MAX_DISCUSSION_CHARS = 40_000
MAX_HUNK_CONTEXT_CHARS = 40_000
MAX_HUNK_CONTEXT_CHARS_PER_FILE = 3_000
MAX_HUNK_CONTEXT_FILES = 15
HUNK_CONTEXT_LINES = 25


def build_review_prompt(
    mr: MergeRequestInfo,
    diff_text: str,
    changed_files: list[str],
    discussion_digest: str | None = None,
    discussion_inventory: str | None = None,
    repo_rules_text: str | None = None,
    repo_rules_path: str | None = None,
    repo_rules_truncated: bool = False,
    incremental_diff_text: str | None = None,
    previous_reviewed_sha: str | None = None,
    hunk_context: dict[str, str] | None = None,
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
    inventory_section = ""
    if discussion_inventory:
        inventory_section = f"""
All MR discussions (full list — open and resolved):
{discussion_inventory}
"""

    discussion_section = ""
    if discussion_digest:
        discussion_section = f"""
Open and recent discussion details (excerpts from the above list):
```md
{discussion_digest[:MAX_DISCUSSION_CHARS]}
```
"""

    incremental_diff_section = ""
    if incremental_diff_text and previous_reviewed_sha:
        short_sha = previous_reviewed_sha[:12]
        incremental_diff_section = f"""
Changes since the previous full review (since {short_sha}):
```diff
{incremental_diff_text[:MAX_DIFF_CHARS]}
```
Focus your review primarily on these incremental changes. Use the full diff below as context, but avoid re-reporting concerns that were already visible in the previous full review and have not changed.
"""
    hunk_context_section = ""
    if hunk_context:
        parts: list[str] = []
        total = 0
        for file_path, snippet in hunk_context.items():
            if total + len(snippet) > MAX_HUNK_CONTEXT_CHARS:
                break
            parts.append(f"**{file_path}**\n```\n{snippet}\n```")
            total += len(snippet)
        if parts:
            hunk_context_section = "\nSurrounding file context (lines around each changed area):\n" + "\n\n".join(parts) + "\n"

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
- Write `body` in plain, direct language: 1–2 short sentences stating the problem and its consequence. Avoid long complex clauses and technical jargon where plain words work.
- Include `suggestion` only when you have a concrete, short remediation direction.
- Use repository context when needed, but keep the final output concise and structured.
- Consider existing MR discussion context. Do not restate concerns that have already been convincingly dismissed by discussion unless the visible diff reintroduces the risk.
- Follow repository-specific review rules when provided, unless they conflict with the JSON output contract above.
- If repository rules specify a language for output, write all text fields (title, body, summary, suggestion) in that language.

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
{inventory_section}
{discussion_section}

Changed files:
{changed_files_text}
{incremental_diff_section}{hunk_context_section}{truncation_notice}
Unified diff (full, base → HEAD):
```diff
{diff_text}
```"""


MAX_RECONCILE_FILE_DIFF_CHARS = 20_000


def _extract_file_diff(diff_text: str, file_path: str) -> str:
    """Extract the diff section for a specific file from a unified diff."""
    lines = diff_text.splitlines(keepends=True)
    result: list[str] = []
    in_file = False
    for line in lines:
        if line.startswith("diff --git"):
            in_file = file_path in line
        if in_file:
            result.append(line)
    return "".join(result)


def build_discussion_reconcile_prompt(
    mr: MergeRequestInfo,
    *,
    discussion_id: str,
    discussion_text: str,
    trigger_note_body: str,
    linked_finding_payload: dict | None,
    diff_text: str | None = None,
    finding_file: str | None = None,
    repo_rules_text: str | None = None,
    repo_rules_path: str | None = None,
) -> str:
    finding_json = json.dumps(linked_finding_payload or {}, indent=2, sort_keys=True)

    repo_rules_section = ""
    if repo_rules_text:
        repo_rules_section = f"""
Repository-specific review rules from `{repo_rules_path or "repository rules file"}`:
```md
{repo_rules_text}
```
Use these rules when judging whether a human's dismissal is acceptable or whether the concern should stay open.
"""

    diff_section = ""
    if diff_text:
        relevant_diff = _extract_file_diff(diff_text, finding_file) if finding_file else ""
        if not relevant_diff:
            relevant_diff = diff_text
        relevant_diff = relevant_diff[:MAX_RECONCILE_FILE_DIFF_CHARS]
        diff_section = f"""
MR diff (use this to verify claims about what changed):
```diff
{relevant_diff}
```
"""

    return f"""You are reconciling a GitLab merge request discussion against an existing AI review finding.

Return only strict JSON with this shape:
{{
  "decision": "keep_open|dismissed_by_discussion|reply_only|no_action",
  "reason": "short rationale",
  "reply_body": "optional short markdown reply"
}}

Rules:
- `dismissed_by_discussion`: use in two cases: (a) the human provides a concrete technical explanation that removes the risk, or (b) the human explicitly and clearly states the concern is not important, is acceptable risk, or they take responsibility for it. Always include `reply_body`. For case (a), acknowledge the fix naturally, e.g. "Makes sense, thanks." For case (b), acknowledge the decision gracefully, e.g. "Understood — closing this, it's your call." or "Fair enough, I won't push on this."
- `reply_only`: concern stays open but the bot should respond. Always include `reply_body` explaining what still needs attention.
- `keep_open`: the human replied but appears to have misunderstood or not fully engaged with the concern. Always include `reply_body` — re-explain briefly and specifically why the concern still stands, as a colleague would in a code review.
- `no_action`: the note is clearly irrelevant, off-topic, or bot-authored noise — skip entirely, no `reply_body`.
- Do not dismiss on vague or ambiguous reassurances. Distinguish between "this is not important to us" (explicit acceptance — use `dismissed_by_discussion`) and an incomplete or tangential reply (use `keep_open`).
- When the diff is available, verify the human's claim against it before deciding. If the diff confirms the fix, use `dismissed_by_discussion`. If the diff contradicts the claim, use `keep_open`.
- Write `reply_body` in plain, direct markdown. Be concise and natural — like a colleague in a code review, not a formal system message.
- Write `reply_body` in the same language as the human's latest note.
- Keep `reason` short and specific.
- Never ask for more information. Decide from the discussion context.

Merge request:
- Project ID: {mr.project_id}
- MR IID: {mr.mr_iid}
- Title: {mr.title}
- Source SHA: {mr.source_sha}
- Web URL: {mr.web_url}
{repo_rules_section}
Tracked finding:
```json
{finding_json}
```
{diff_section}
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
- Merge findings that describe the same root cause even if they point to different files, lines, or use different wording. Prefer one clear merged finding over two overlapping ones.
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
- Write each `body` in plain, direct language: 1–2 short sentences stating the problem and its consequence. Avoid long complex clauses.
- Write `summary` as the final council verdict only. It will be shown directly to developers in the MR comment, so be direct and opinionated: state whether the MR is safe to merge, needs fixes, or has blockers. If concerns are minor or raised by only one model, say so (e.g. "Safe to merge — concerns are minimal and low-confidence"). Do not just list findings; give a clear recommendation.
- Fill `reviewer_overview.codex` and `reviewer_overview.claude` with short reviewer-specific notes describing what each model independently surfaced, emphasized, or agreed with.
- Do not repeat or paraphrase the full base-review summaries inside `summary`; use `reviewer_overview` for reviewer-specific attribution.
- Prefer highlighting overlap and unique findings explicitly, e.g. one reviewer confirmed a shared issue while the other added an extra concern.
- Match the language of the base reviews for all text fields (title, body, summary, suggestion). If the base reviews are in a non-English language, preserve that language in the final output.

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
