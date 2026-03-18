import tempfile
import unittest
from pathlib import Path

from nimble_reviewer.gitlab import GitLabNote
from nimble_reviewer.models import MergeRequestInfo, PreparedCheckout, ReviewFinding, ReviewResult
from nimble_reviewer.service import ReviewService, ServiceDependencies
from nimble_reviewer.store import Store
from nimble_reviewer.trace import TraceSettings


class FakeGitLabClient:
    def __init__(self):
        self.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha2",
            web_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            repo_http_url="https://gitlab.example.com/group/project.git",
        )
        self.note = None

    def get_merge_request_info(self, project_id, mr_iid):
        return self.mr_info

    def get_merge_request_head_sha(self, project_id, mr_iid):
        return self.mr_info.source_sha

    def find_bot_note(self, project_id, mr_iid, marker, preferred_note_id=None):
        return self.note

    def create_note(self, project_id, mr_iid, body):
        self.note = GitLabNote(id=99, body=body)
        return self.note

    def update_note(self, project_id, mr_iid, note_id, body):
        self.note = GitLabNote(id=note_id, body=body)
        return self.note


class FakeRepoManager:
    def __init__(self, workspace: Path, review_rules_path=None, review_rules_text=None):
        self.workspace = workspace
        self.review_rules_path = review_rules_path
        self.review_rules_text = review_rules_text

    def prepare_checkout(self, repo_http_url, source_sha, target_branch, trace=None):
        return PreparedCheckout(
            path=self.workspace,
            merge_base="base123",
            diff_text="diff --git a/file.py b/file.py\n+new line",
            changed_files=["file.py"],
            review_rules_path=self.review_rules_path,
            review_rules_text=self.review_rules_text,
            cleanup=lambda: None,
        )


class FakeReviewAgentRunner:
    provider_name = "fake"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.last_prompt = None

    def review(self, prompt, cwd, trace=None):
        self.last_prompt = prompt
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmpdir.name) / "state.db")
        self.store.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_successful_run_marks_done_and_updates_note(self):
        decision = self.store.enqueue_run(1, 2, "sha2", None)
        run = self.store.claim_next_run()
        gitlab = FakeGitLabClient()
        service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(Path(self.tmpdir.name)),
                review_agent=FakeReviewAgentRunner(
                    result=ReviewResult(
                        summary="One issue found.",
                        overall_risk="medium",
                        findings=(ReviewFinding("medium", "file.py", 10, "Bug", "Needs fixing"),),
                    )
                ),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )

        service.process_run(run)

        state = self.store.get_merge_request_state(1, 2)
        stored_run = self.store.get_run(decision.run_id)
        self.assertEqual(stored_run.status, "done")
        self.assertEqual(state.note_id, 99)
        self.assertEqual(state.last_reviewed_sha, "sha2")
        self.assertIn("One issue found.", gitlab.note.body)

    def test_stale_run_is_not_posted(self):
        self.store.enqueue_run(1, 2, "sha1", None)
        run = self.store.claim_next_run()
        gitlab = FakeGitLabClient()
        service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(Path(self.tmpdir.name)),
                review_agent=FakeReviewAgentRunner(
                    result=ReviewResult(summary="unused", overall_risk="low", findings=())
                ),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )
        service.process_run(run)
        stored_run = self.store.get_run(run.id)
        self.assertEqual(stored_run.status, "superseded")
        self.assertIsNone(gitlab.note)

    def test_failed_run_preserves_existing_review_body(self):
        self.store.enqueue_run(1, 2, "sha2", None)
        first_run = self.store.claim_next_run()
        gitlab = FakeGitLabClient()
        success_service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(Path(self.tmpdir.name)),
                review_agent=FakeReviewAgentRunner(
                    result=ReviewResult(summary="Stable", overall_risk="low", findings=())
                ),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )
        success_service.process_run(first_run)

        self.store.enqueue_run(1, 2, "sha2b", None)
        second_run = self.store.claim_next_run()
        gitlab.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha2b",
            web_url=gitlab.mr_info.web_url,
            repo_http_url=gitlab.mr_info.repo_http_url,
        )
        failed_service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(Path(self.tmpdir.name)),
                review_agent=FakeReviewAgentRunner(error="timeout"),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )
        failed_service.process_run(second_run)

        self.assertIn("Stable", gitlab.note.body)
        self.assertIn("failed", gitlab.note.body.lower())

    def test_repo_specific_rules_are_added_to_prompt(self):
        self.store.enqueue_run(1, 2, "sha2", None)
        run = self.store.claim_next_run()
        gitlab = FakeGitLabClient()
        review_agent = FakeReviewAgentRunner(result=ReviewResult(summary="ok", overall_risk="low", findings=()))
        service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(
                    Path(self.tmpdir.name),
                    review_rules_path="NIMBLE-REVIEWER.MD",
                    review_rules_text="- Treat auth bypasses as high severity.",
                ),
                review_agent=review_agent,
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )

        service.process_run(run)

        self.assertIsNotNone(review_agent.last_prompt)
        self.assertIn("Repository-specific review rules", review_agent.last_prompt)
        self.assertIn("NIMBLE-REVIEWER.MD", review_agent.last_prompt)
        self.assertIn("Treat auth bypasses as high severity.", review_agent.last_prompt)

    def test_success_note_includes_snippet_from_checkout(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        self.store.enqueue_run(1, 2, "sha2", None)
        run = self.store.claim_next_run()
        gitlab = FakeGitLabClient()
        service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(workspace),
                review_agent=FakeReviewAgentRunner(
                    result=ReviewResult(
                        summary="One issue found.",
                        overall_risk="medium",
                        findings=(
                            ReviewFinding(
                                "medium",
                                "file.py",
                                3,
                                "Bug",
                                "Needs fixing",
                                suggestion="Add a guard before using the value.",
                            ),
                        ),
                    )
                ),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )

        service.process_run(run)

        self.assertNotIn("```python", gitlab.note.body)
        self.assertNotIn(">>3 | line3", gitlab.note.body)
        self.assertIn("Source: `file.py:3`", gitlab.note.body)
        self.assertIn("Fix: Add a guard before using the value.", gitlab.note.body)

    def test_second_successful_run_marks_still_present_and_resolved_findings(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        gitlab = FakeGitLabClient()

        self.store.enqueue_run(1, 2, "sha1", None)
        first_run = self.store.claim_next_run()
        gitlab.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha1",
            web_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            repo_http_url="https://gitlab.example.com/group/project.git",
        )
        first_service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(workspace),
                review_agent=FakeReviewAgentRunner(
                    result=ReviewResult(
                        summary="Two issues found.",
                        overall_risk="medium",
                        findings=(
                            ReviewFinding("medium", "file.py", 3, "Bug", "Needs fixing"),
                            ReviewFinding("low", "old.py", 7, "Old issue", "No longer relevant"),
                        ),
                    )
                ),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )
        first_service.process_run(first_run)

        self.store.enqueue_run(1, 2, "sha2", None)
        second_run = self.store.claim_next_run()
        gitlab.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha2",
            web_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            repo_http_url="https://gitlab.example.com/group/project.git",
        )
        second_service = ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=FakeRepoManager(workspace),
                review_agent=FakeReviewAgentRunner(
                    result=ReviewResult(
                        summary="One issue remains and one is new.",
                        overall_risk="medium",
                        findings=(
                            ReviewFinding("medium", "file.py", 3, "Bug", "Needs fixing"),
                            ReviewFinding("medium", "new.py", 9, "New issue", "Brand new finding"),
                        ),
                    )
                ),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )
        second_service.process_run(second_run)

        self.assertIn("Status: `Still present`", gitlab.note.body)
        self.assertIn("Status: `New`", gitlab.note.body)
        self.assertIn("## Resolved since previous review", gitlab.note.body)
        self.assertIn("- 💡 Low: Old issue at `old.py:7`", gitlab.note.body)


if __name__ == "__main__":
    unittest.main()
