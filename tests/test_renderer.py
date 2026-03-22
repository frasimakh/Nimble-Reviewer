import unittest

from nimble_reviewer.models import (
    ReviewComparison,
    ReviewAgentMetadata,
    ReviewFinding,
    ReviewFindingState,
    ReviewOpinion,
    ReviewParticipant,
    ReviewQuotaStatus,
    ReviewResult,
    ReviewTokenUsage,
)
from nimble_reviewer.renderer import extract_review_block, render_failure_note, render_success_note


class RendererTests(unittest.TestCase):
    def test_success_note_contains_markers_and_findings(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="Two real risks found.",
                overall_risk="high",
                findings=(
                    ReviewFinding("medium", "b.py", 9, "Second", "Body two"),
                    ReviewFinding("high", "a.py", 3, "First", "Body one"),
                ),
            ),
        )
        self.assertIn("<!-- nimble-reviewer:1:2 -->", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Current findings", body)
        self.assertIn("## Resolved since previous review", body)
        self.assertIn("### 1. 🚨 High: First", body)
        self.assertIn("### 2. ⚠️ Medium: Second", body)
        self.assertIn("Status: `New`", body)
        self.assertLess(body.find("### 1. 🚨 High: First"), body.find("### 2. ⚠️ Medium: Second"))

    def test_failure_note_keeps_previous_review(self):
        success = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(summary="Looks risky.", overall_risk="medium", findings=()),
        )
        failed = render_failure_note(1, 2, "fedcba654321", "timeout", success)
        self.assertIn("failed", failed.lower())
        self.assertIn("Looks risky.", failed)
        self.assertEqual(extract_review_block(success), extract_review_block(failed))

    def test_success_note_includes_tokens_and_agent_metadata(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="Looks safe enough.",
                overall_risk="low",
                findings=(),
                token_usage=ReviewTokenUsage(
                    input_tokens=1200,
                    cached_input_tokens=300,
                    output_tokens=80,
                ),
                agent_metadata=ReviewAgentMetadata(
                    provider="codex",
                    model="gpt-5.4",
                    reasoning_effort="xhigh",
                ),
            ),
        )
        self.assertIn("Agent:", body)
        self.assertIn("gpt-5.4", body)
        self.assertIn("xhigh", body)
        self.assertNotIn("input=", body)
        self.assertNotIn("Review Trace", body)

    def test_success_note_renders_snippet_and_fix(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="One actionable issue.",
                overall_risk="medium",
                findings=(
                    ReviewFinding(
                        "medium",
                        "src/app.py",
                        42,
                        "Potential null access",
                        "This can read from an empty collection.",
                        suggestion="Guard the collection before indexing.",
                        opinions=(
                            ReviewOpinion("codex", "found", "Flagged the empty collection access."),
                            ReviewOpinion("claude", "agree", "The same path can index an empty list."),
                        ),
                        snippet=" 40 | items = []\n 41 | if not ready:\n>>42 | value = items[0]",
                        snippet_language="python",
                    ),
                ),
            ),
        )
        self.assertIn("Source: `src/app.py:42`", body)
        self.assertIn("Status: `New`", body)
        self.assertNotIn("Council:", body)
        self.assertNotIn("```python", body)
        self.assertIn("Fix: Guard the collection before indexing.", body)

    def test_success_note_includes_claude_cost_and_cache_write(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="Looks safe enough.",
                overall_risk="low",
                findings=(),
                token_usage=ReviewTokenUsage(
                    input_tokens=900,
                    cached_input_tokens=200,
                    output_tokens=70,
                    cache_creation_input_tokens=150,
                    cost_usd=0.123456,
                    cached_input_included_in_input=False,
                    cache_creation_included_in_input=False,
                ),
                agent_metadata=ReviewAgentMetadata(
                    provider="claude",
                    model="claude-sonnet-4-6",
                    reasoning_effort="high",
                ),
            ),
        )
        self.assertNotIn("cache_write=", body)
        self.assertNotIn("cost_usd=", body)
        self.assertNotIn("input=", body)

    def test_success_note_renders_council_participants_and_models(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="One shared issue.",
                overall_risk="medium",
                findings=(
                    ReviewFinding(
                        "medium",
                        "src/app.py",
                        42,
                        "Potential null access",
                        "This can read from an empty collection.",
                        sources=("codex", "claude"),
                        opinions=(
                            ReviewOpinion("codex", "found", "Raised in the base review."),
                            ReviewOpinion("claude", "found", "Raised in the base review."),
                        ),
                    ),
                ),
                participants=(
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="codex", model="gpt-5.4", reasoning_effort="high"),
                        phases=("review",),
                        token_usage=ReviewTokenUsage(input_tokens=10, cached_input_tokens=2, output_tokens=3),
                        quota_status=ReviewQuotaStatus(remaining_percent=62.5, reset_at="2026-03-22T18:00:00Z"),
                        summary="Codex highlighted the shared null-access path.",
                        overall_risk="medium",
                    ),
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="codex", model="gpt-5.4", reasoning_effort="low"),
                        phases=("synthesis",),
                        token_usage=ReviewTokenUsage(input_tokens=4, cached_input_tokens=1, output_tokens=1),
                        summary="Needs fixes before merge: both reviewers agree on one actionable issue.",
                        overall_risk="medium",
                    ),
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="claude", model="sonnet", reasoning_effort="high"),
                        phases=("review",),
                        summary="Claude independently confirmed the same guard issue.",
                        overall_risk="medium",
                    ),
                ),
            ),
        )
        self.assertNotIn("## Council", body)
        self.assertIn("**Codex** · risk **MEDIUM** · `gpt-5.4` · reasoning `high`", body)
        self.assertIn("quota `62.5% left`", body)
        self.assertIn("reset `2026-03-22T18:00:00Z`", body)
        self.assertIn("**Claude** · risk **MEDIUM** · `sonnet` · reasoning `high`", body)
        self.assertIn("**Council** · overall risk **MEDIUM** · `gpt-5.4` · reasoning `low` (synthesis)", body)
        self.assertIn("(Claude + Codex)", body)
        self.assertNotIn("`Claude`: found independently", body)

    def test_success_note_renders_still_present_and_resolved_sections(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="One issue remains and one was resolved.",
                overall_risk="medium",
                findings=(
                    ReviewFinding("medium", "src/app.py", 42, "Potential null access", "This can read from an empty collection."),
                ),
            ),
            comparison=ReviewComparison(
                current_findings=(
                    ReviewFindingState(
                        finding=ReviewFinding(
                            "medium",
                            "src/app.py",
                            42,
                            "Potential null access",
                            "This can read from an empty collection.",
                        ),
                        status="still_present",
                    ),
                ),
                resolved_findings=(
                    ReviewFinding("low", "src/old.py", 10, "Unused branch", "An old branch was removed."),
                ),
            ),
        )
        self.assertIn("Status: `Still present`", body)
        self.assertIn("## Resolved since previous review", body)
        self.assertIn("- 💡 Low: Unused branch at `src/old.py:10`", body)


if __name__ == "__main__":
    unittest.main()
