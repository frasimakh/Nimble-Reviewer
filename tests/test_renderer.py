import unittest

from nimble_reviewer.models import (
    ReviewComparison,
    ReviewAgentMetadata,
    ReviewFinding,
    ReviewFindingState,
    ReviewOpinion,
    ReviewParticipant,
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
        self.assertIn("### 1. High: First", body)
        self.assertIn("### 2. Warning: Second", body)
        self.assertIn("Status: `New`", body)
        self.assertLess(body.find("### 1. High: First"), body.find("### 2. Warning: Second"))

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
        self.assertIn("Tokens: `total=1280`", body)
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
        self.assertIn("Council:", body)
        self.assertIn("`Codex`: found independently - Flagged the empty collection access.", body)
        self.assertIn("```python", body)
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
        self.assertIn("Tokens: `total=1320`", body)
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
                    ),
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="codex", model="gpt-5.4", reasoning_effort="low"),
                        phases=("synthesis",),
                        token_usage=ReviewTokenUsage(input_tokens=4, cached_input_tokens=1, output_tokens=1),
                    ),
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="claude", model="sonnet", reasoning_effort="high"),
                        phases=("review",),
                    ),
                ),
            ),
        )
        self.assertIn("## Council", body)
        self.assertIn("**Review**", body)
        self.assertIn("**Synthesis**", body)
        self.assertIn("- Codex: model `gpt-5.4`, reasoning `high`, tokens `13`", body)
        self.assertIn("- Claude: model `sonnet`, reasoning `high`", body)
        self.assertIn("- Codex: model `gpt-5.4`, reasoning `low`, tokens `5`", body)
        self.assertIn("Found by: `both`", body)
        self.assertIn("`Claude`: found independently - Raised in the base review.", body)

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
        self.assertIn("- Low: Unused branch at `src/old.py:10`", body)


if __name__ == "__main__":
    unittest.main()
