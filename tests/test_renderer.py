import unittest

from nimble_reviewer.models import (
    ReviewAgentMetadata,
    ReviewComparison,
    ReviewFinding,
    ReviewParticipant,
    ReviewResult,
    ReviewSummaryMetrics,
)
from nimble_reviewer.renderer import extract_review_block, render_failure_note, render_success_note


class RendererTests(unittest.TestCase):
    def test_success_note_contains_markers_metrics_and_unplaced_findings(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="Needs fixes before merge.",
                overall_risk="medium",
                findings=(ReviewFinding("medium", "src/app.py", 42, "Potential null access", "Guard the list."),),
            ),
            metrics=ReviewSummaryMetrics(
                open_count=1,
                new_count=1,
                still_present_count=0,
                resolved_count=0,
                dismissed_count=2,
                unplaced_count=1,
            ),
            unplaced_findings=(ReviewFinding("medium", "src/app.py", 42, "Potential null access", "Guard the list."),),
        )

        self.assertIn("<!-- nimble-reviewer:1:2 -->", body)
        self.assertIn("Open findings: `1`", body)
        self.assertIn("`1 new`", body)
        self.assertIn("`2 dismissed by discussion`", body)
        self.assertIn("## Unplaced Findings", body)
        self.assertIn("Potential null access", body)
        self.assertNotIn("## Current findings", body)

    def test_failure_note_keeps_previous_review(self):
        success = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(summary="Looks risky.", overall_risk="medium", findings=()),
            metrics=ReviewSummaryMetrics(
                open_count=0,
                new_count=0,
                still_present_count=0,
                resolved_count=0,
                dismissed_count=0,
                unplaced_count=0,
            ),
        )
        failed = render_failure_note(1, 2, "fedcba654321", "timeout", success)
        self.assertIn("failed", failed.lower())
        self.assertIn("Looks risky.", failed)
        self.assertEqual(extract_review_block(success), extract_review_block(failed))

    def test_success_note_renders_council_participants(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(
                summary="One shared issue.",
                overall_risk="medium",
                findings=(),
                participants=(
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="codex", model="gpt-5.4", reasoning_effort="high"),
                        phases=("review",),
                        summary="Codex highlighted one missing guard.",
                        overall_risk="medium",
                    ),
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="claude", model="sonnet", reasoning_effort="high"),
                        phases=("review",),
                        summary="Claude confirmed the same path.",
                        overall_risk="medium",
                    ),
                    ReviewParticipant(
                        metadata=ReviewAgentMetadata(provider="codex", model="gpt-5.4", reasoning_effort="low"),
                        phases=("synthesis",),
                        summary="Needs fixes before merge.",
                        overall_risk="medium",
                    ),
                ),
            ),
            metrics=ReviewSummaryMetrics(
                open_count=1,
                new_count=0,
                still_present_count=1,
                resolved_count=0,
                dismissed_count=0,
                unplaced_count=0,
            ),
        )

        self.assertIn("**Codex** · risk **MEDIUM** · `gpt-5.4` · reasoning `high`", body)
        self.assertIn("**Claude** · risk **MEDIUM** · `sonnet` · reasoning `high`", body)
        self.assertIn("**Council** · overall risk **MEDIUM** · `gpt-5.4` · reasoning `low` (synthesis)", body)

    def test_success_note_renders_resolved_section_when_present(self):
        body = render_success_note(
            1,
            2,
            "abcdef123456",
            ReviewResult(summary="One issue remains.", overall_risk="medium", findings=()),
            metrics=ReviewSummaryMetrics(
                open_count=1,
                new_count=0,
                still_present_count=1,
                resolved_count=1,
                dismissed_count=0,
                unplaced_count=0,
            ),
            comparison=ReviewComparison(
                current_findings=(),
                resolved_findings=(ReviewFinding("low", "src/old.py", 10, "Unused branch", "Removed branch."),),
            ),
        )

        self.assertIn("## Resolved In Code", body)
        self.assertIn("Unused branch", body)


if __name__ == "__main__":
    unittest.main()
