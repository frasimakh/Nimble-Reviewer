import unittest

from nimble_reviewer.models import ReviewAgentMetadata, ReviewFinding, ReviewResult, ReviewTokenUsage
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
        self.assertIn("### 1. High: First", body)
        self.assertIn("### 2. Warning: Second", body)
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
        self.assertIn("Tokens:", body)
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
                        snippet=" 40 | items = []\n 41 | if not ready:\n>>42 | value = items[0]",
                        snippet_language="python",
                    ),
                ),
            ),
        )
        self.assertIn("Source: `src/app.py:42`", body)
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
                ),
                agent_metadata=ReviewAgentMetadata(
                    provider="claude",
                    model="claude-sonnet-4-6",
                    reasoning_effort="high",
                ),
            ),
        )
        self.assertIn("cache_write=150", body)
        self.assertIn("cost_usd=0.123456", body)


if __name__ == "__main__":
    unittest.main()
