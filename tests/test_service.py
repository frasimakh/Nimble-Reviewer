import tempfile
import unittest
from pathlib import Path

from nimble_reviewer.gitlab import (
    GitLabDiffPosition,
    GitLabDiffVersion,
    GitLabDiscussion,
    GitLabError,
    GitLabNote,
    GitLabUser,
)
from nimble_reviewer.models import MergeRequestInfo, PreparedCheckout, ReviewFinding, ReviewResult, TrackedFinding
from nimble_reviewer.service import ReviewService, ServiceDependencies
from nimble_reviewer.store import Store
from nimble_reviewer.trace import TraceSettings


class FakeGitLabClient:
    def __init__(self):
        self.current_user = GitLabUser(id=900, username="nimble-reviewer")
        self.mr_info = MergeRequestInfo(
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
        self.summary_note = None
        self.discussions = []
        self.resolved_changes = []
        self.raise_on_create_diff_discussion = False
        self.head_sha_override = None

    def get_current_user(self):
        return self.current_user

    def get_merge_request_info(self, project_id, mr_iid):
        return self.mr_info

    def get_merge_request_head_sha(self, project_id, mr_iid):
        return self.head_sha_override or self.mr_info.source_sha

    def get_latest_merge_request_version(self, project_id, mr_iid):
        return GitLabDiffVersion(id=1, base_sha="base", start_sha="start", head_sha=self.mr_info.source_sha)

    def list_merge_request_discussions(self, project_id, mr_iid):
        return list(self.discussions)

    def find_bot_note(self, project_id, mr_iid, marker, preferred_note_id=None):
        return self.summary_note

    def create_note(self, project_id, mr_iid, body):
        self.summary_note = GitLabNote(id=99, body=body, author_id=self.current_user.id)
        return self.summary_note

    def update_note(self, project_id, mr_iid, note_id, body):
        self.summary_note = GitLabNote(id=note_id, body=body, author_id=self.current_user.id)
        return self.summary_note

    def create_diff_discussion(self, project_id, mr_iid, body, position):
        if self.raise_on_create_diff_discussion:
            raise GitLabError('GitLab API error 500 for POST /projects/1/merge_requests/2/discussions: {"message":"500 Internal Server Error"}')
        note_id = 100 + len(self.discussions) * 10
        discussion = GitLabDiscussion(
            id=f"d{len(self.discussions) + 1}",
            individual_note=False,
            notes=(GitLabNote(id=note_id, body=body, author_id=self.current_user.id),),
            resolved=False,
            resolvable=True,
            position=position,
        )
        self.discussions.append(discussion)
        return discussion

    def add_discussion_note(self, project_id, mr_iid, discussion_id, body):
        note_id = 1000 + sum(len(d.notes) for d in self.discussions)
        note = GitLabNote(id=note_id, body=body, author_id=self.current_user.id)
        for index, discussion in enumerate(self.discussions):
            if discussion.id != discussion_id:
                continue
            self.discussions[index] = GitLabDiscussion(
                id=discussion.id,
                individual_note=discussion.individual_note,
                notes=tuple([*discussion.notes, note]),
                resolved=discussion.resolved,
                resolvable=discussion.resolvable,
                position=discussion.position,
            )
            return note
        raise AssertionError(f"Unknown discussion {discussion_id}")

    def set_discussion_resolved(self, project_id, mr_iid, discussion_id, *, resolved):
        for index, discussion in enumerate(self.discussions):
            if discussion.id != discussion_id:
                continue
            updated = GitLabDiscussion(
                id=discussion.id,
                individual_note=discussion.individual_note,
                notes=discussion.notes,
                resolved=resolved,
                resolvable=discussion.resolvable,
                position=discussion.position,
            )
            self.discussions[index] = updated
            self.resolved_changes.append((discussion_id, resolved))
            return updated
        raise AssertionError(f"Unknown discussion {discussion_id}")


class FakeRepoManager:
    def __init__(self, workspace: Path, diff_text: str | None = None, changed_files=None, review_rules_path=None, review_rules_text=None):
        self.workspace = workspace
        self.review_rules_path = review_rules_path
        self.review_rules_text = review_rules_text
        self.prepare_calls = []
        self.diff_text = diff_text or (
            "diff --git a/file.py b/file.py\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+new line\n"
            "+second line\n"
        )
        self.changed_files = changed_files or ["file.py"]

    def prepare_checkout(self, repo_http_url, source_sha, target_branch, trace=None):
        self.prepare_calls.append((repo_http_url, source_sha, target_branch))
        return PreparedCheckout(
            path=self.workspace,
            merge_base="base123",
            diff_text=self.diff_text,
            changed_files=self.changed_files,
            review_rules_path=self.review_rules_path,
            review_rules_text=self.review_rules_text,
            cleanup=lambda: None,
        )


class FakeReviewAgentRunner:
    provider_name = "fake-council"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.last_prompt = None

    def review(self, prompt, cwd, trace=None):
        self.last_prompt = prompt
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class FakeDiscussionReconcileAgent:
    provider_name = "codex"

    def __init__(self, payload=None):
        self.payload = payload or {"decision": "no_action", "reason": "No action."}
        self.last_prompt = None
        self.last_cwd = None
        self.agent_metadata = None

    def run_json(self, prompt, cwd, trace=None):
        self.last_prompt = prompt
        self.last_cwd = cwd
        return self.payload, None


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmpdir.name) / "state.db")
        self.store.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _service(self, gitlab, repo_manager, review_agent, reconcile_agent=None):
        return ReviewService(
            ServiceDependencies(
                store=self.store,
                gitlab=gitlab,
                repo_manager=repo_manager,
                review_agent=review_agent,
                discussion_reconcile_agent=reconcile_agent or FakeDiscussionReconcileAgent(),
                trace_settings=TraceSettings(Path(self.tmpdir.name) / "traces"),
            )
        )

    def test_full_review_creates_summary_note_and_inline_discussion(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )

        decision = self.store.enqueue_run(1, 2, "sha1", None)
        run = self.store.claim_next_run()
        service.process_run(run)

        state = self.store.get_merge_request_state(1, 2)
        tracked = self.store.list_tracked_findings(1, 2)
        self.assertEqual(decision.run_id, run.id)
        self.assertEqual(state.summary_note_id, 99)
        self.assertEqual(len(gitlab.discussions), 1)
        self.assertEqual(len(tracked), 1)
        self.assertEqual(tracked[0].status, "open")
        self.assertEqual(tracked[0].thread_owner, "bot")
        self.assertIn("Open findings: `1`", gitlab.summary_note.body)

    def test_full_review_falls_back_to_summary_when_inline_publish_fails_but_head_is_stable(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        gitlab.raise_on_create_diff_discussion = True
        service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )

        self.store.enqueue_run(1, 2, "sha1", None)
        run = self.store.claim_next_run()
        service.process_run(run)

        tracked = self.store.list_tracked_findings(1, 2)
        stored_run = self.store.get_run(run.id)
        self.assertEqual(stored_run.status, "done")
        self.assertEqual(len(gitlab.discussions), 0)
        self.assertEqual(tracked[0].thread_owner, "summary-only")
        self.assertIn("`1 unplaced`", gitlab.summary_note.body)

    def test_full_review_retries_summary_only_finding_as_inline_on_next_sha(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        failing_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )
        gitlab.raise_on_create_diff_discussion = True
        self.store.enqueue_run(1, 2, "sha1", None)
        first_run = self.store.claim_next_run()
        failing_service.process_run(first_run)

        gitlab.raise_on_create_diff_discussion = False
        gitlab.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha2",
            web_url=gitlab.mr_info.web_url,
            repo_http_url=gitlab.mr_info.repo_http_url,
        )
        retry_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )
        self.store.enqueue_run(1, 2, "sha2", None)
        second_run = self.store.claim_next_run()
        retry_service.process_run(second_run)

        tracked = self.store.list_tracked_findings(1, 2)
        self.assertEqual(len(gitlab.discussions), 1)
        self.assertEqual(tracked[0].thread_owner, "bot")
        self.assertNotIn("`0 unplaced`", gitlab.summary_note.body)

    def test_full_review_is_superseded_when_inline_publish_fails_after_head_changes(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        gitlab.raise_on_create_diff_discussion = True
        gitlab.head_sha_override = "sha2"
        service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )

        self.store.enqueue_run(1, 2, "sha1", None)
        run = self.store.claim_next_run()
        service.process_run(run)

        stored_run = self.store.get_run(run.id)
        self.assertEqual(stored_run.status, "superseded")
        self.assertIsNone(gitlab.summary_note)
        self.assertEqual(self.store.list_tracked_findings(1, 2), [])

    def test_second_full_review_reuses_existing_discussion_and_marks_still_present(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        first_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )

        self.store.enqueue_run(1, 2, "sha1", None)
        first_run = self.store.claim_next_run()
        first_service.process_run(first_run)

        gitlab.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha2",
            web_url=gitlab.mr_info.web_url,
            repo_http_url=gitlab.mr_info.repo_http_url,
        )
        second_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="Same issue remains.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )

        self.store.enqueue_run(1, 2, "sha2", None)
        second_run = self.store.claim_next_run()
        second_service.process_run(second_run)

        self.assertEqual(len(gitlab.discussions), 1)
        self.assertNotIn("`0 new`", gitlab.summary_note.body)
        self.assertIn("`1 still present`", gitlab.summary_note.body)

    def test_full_review_resolves_bot_owned_discussion_when_finding_disappears(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        first_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )
        self.store.enqueue_run(1, 2, "sha1", None)
        first_run = self.store.claim_next_run()
        first_service.process_run(first_run)

        gitlab.mr_info = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha2",
            web_url=gitlab.mr_info.web_url,
            repo_http_url=gitlab.mr_info.repo_http_url,
        )
        second_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(result=ReviewResult(summary="Looks clear.", overall_risk="low", findings=())),
        )
        self.store.enqueue_run(1, 2, "sha2", None)
        second_run = self.store.claim_next_run()
        second_service.process_run(second_run)

        tracked = self.store.list_tracked_findings(1, 2)
        self.assertEqual(tracked[0].status, "resolved")
        self.assertEqual(gitlab.resolved_changes, [("d1", True)])
        self.assertIn("`1 resolved`", gitlab.summary_note.body)

    def test_discussion_reconcile_dismisses_bot_thread_and_resolves_it(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        full_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )
        self.store.enqueue_run(1, 2, "sha1", None)
        full_run = self.store.claim_next_run()
        full_service.process_run(full_run)

        human_reply = GitLabNote(id=501, body="This is safe because we guard it upstream.", author_id=123)
        discussion = gitlab.discussions[0]
        gitlab.discussions[0] = GitLabDiscussion(
            id=discussion.id,
            individual_note=discussion.individual_note,
            notes=tuple([*discussion.notes, human_reply]),
            resolved=False,
            resolvable=True,
            position=discussion.position,
        )

        reconcile_service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(result=ReviewResult(summary="unused", overall_risk="low", findings=())),
            reconcile_agent=FakeDiscussionReconcileAgent(
                payload={
                    "decision": "dismissed_by_discussion",
                    "reason": "The human explanation removes the risk.",
                    "reply_body": "Makes sense. Marking this as dismissed-by-discussion.",
                }
            ),
        )
        self.store.enqueue_run(
            1,
            2,
            "sha1",
            None,
            kind="discussion_reconcile",
            trigger_discussion_id=discussion.id,
            trigger_note_id=human_reply.id,
            trigger_author_id=human_reply.author_id,
        )
        reconcile_run = self.store.claim_next_run()
        reconcile_service.process_run(reconcile_run)

        tracked = self.store.list_tracked_findings(1, 2)
        self.assertEqual(tracked[0].status, "dismissed_by_discussion")
        self.assertTrue(gitlab.discussions[0].resolved)
        self.assertIn("dismissed-by-discussion", gitlab.discussions[0].notes[-1].body)
        self.assertIn("`1 dismissed by discussion`", gitlab.summary_note.body)

    def test_discussion_reconcile_replies_in_human_thread_without_resolving(self):
        gitlab = FakeGitLabClient()
        discussion = GitLabDiscussion(
            id="human-1",
            individual_note=False,
            notes=(GitLabNote(id=700, body="I think this path can still break.", author_id=123),),
            resolved=False,
            resolvable=True,
            position=GitLabDiffPosition(
                base_sha="base",
                start_sha="start",
                head_sha="sha1",
                old_path="file.py",
                new_path="file.py",
                new_line=1,
            ),
        )
        gitlab.discussions.append(discussion)
        tracked = TrackedFinding(
            project_id=1,
            mr_iid=2,
            fingerprint="fp1",
            status="open",
            severity="medium",
            file="file.py",
            line=1,
            title="Bug",
            body="Needs fixing",
            discussion_id="human-1",
            root_note_id=700,
            thread_owner="human",
            opened_sha="sha1",
            last_seen_sha="sha1",
        )
        self.store.upsert_tracked_finding(tracked)

        service = self._service(
            gitlab,
            FakeRepoManager(Path(self.tmpdir.name)),
            FakeReviewAgentRunner(result=ReviewResult(summary="unused", overall_risk="low", findings=())),
            reconcile_agent=FakeDiscussionReconcileAgent(
                payload={
                    "decision": "reply_only",
                    "reason": "The bot should answer but keep the concern open.",
                    "reply_body": "I still see a risk here because the guard is not local to this path.",
                }
            ),
        )
        self.store.enqueue_run(
            1,
            2,
            "sha1",
            None,
            kind="discussion_reconcile",
            trigger_discussion_id="human-1",
            trigger_note_id=700,
            trigger_author_id=123,
        )
        run = self.store.claim_next_run()
        service.process_run(run)

        updated = self.store.list_tracked_findings(1, 2)[0]
        self.assertEqual(updated.status, "open")
        self.assertFalse(gitlab.discussions[0].resolved)
        self.assertIn("I still see a risk here", gitlab.discussions[0].notes[-1].body)

    def test_discussion_reconcile_runs_agent_from_checkout_path(self):
        workspace = Path(self.tmpdir.name)
        gitlab = FakeGitLabClient()
        discussion = GitLabDiscussion(
            id="d1",
            individual_note=False,
            notes=(
                GitLabNote(id=100, body="Bot finding", author_id=gitlab.current_user.id),
                GitLabNote(id=101, body="I fixed this by adding a guard.", author_id=123),
            ),
            resolved=False,
            resolvable=True,
            position=GitLabDiffPosition(
                base_sha="base",
                start_sha="start",
                head_sha="sha1",
                old_path="file.py",
                new_path="file.py",
                new_line=1,
            ),
        )
        gitlab.discussions.append(discussion)
        self.store.upsert_tracked_finding(
            TrackedFinding(
                project_id=1,
                mr_iid=2,
                fingerprint="fp1",
                status="open",
                severity="medium",
                file="file.py",
                line=1,
                title="Bug",
                body="Needs fixing",
                discussion_id="d1",
                root_note_id=100,
                thread_owner="bot",
                opened_sha="sha1",
                last_seen_sha="sha1",
            )
        )
        repo_manager = FakeRepoManager(workspace)
        reconcile_agent = FakeDiscussionReconcileAgent()
        service = self._service(
            gitlab,
            repo_manager,
            FakeReviewAgentRunner(result=ReviewResult(summary="unused", overall_risk="low", findings=())),
            reconcile_agent=reconcile_agent,
        )

        self.store.enqueue_run(
            1,
            2,
            "sha1",
            None,
            kind="discussion_reconcile",
            trigger_discussion_id="d1",
            trigger_note_id=101,
            trigger_author_id=123,
        )
        run = self.store.claim_next_run()
        service.process_run(run)

        self.assertEqual(repo_manager.prepare_calls, [(gitlab.mr_info.repo_http_url, "sha1", gitlab.mr_info.target_branch)])
        self.assertEqual(reconcile_agent.last_cwd, workspace)

    def test_full_review_does_not_attach_to_nearby_human_discussion_on_different_line(self):
        workspace = Path(self.tmpdir.name)
        (workspace / "file.py").write_text("new line\nsecond line\n", encoding="utf-8")
        gitlab = FakeGitLabClient()
        gitlab.discussions.append(
            GitLabDiscussion(
                id="human-1",
                individual_note=False,
                notes=(GitLabNote(id=700, body="Please rename this variable.", author_id=123),),
                resolved=False,
                resolvable=True,
                position=GitLabDiffPosition(
                    base_sha="base",
                    start_sha="start",
                    head_sha="sha1",
                    old_path="file.py",
                    new_path="file.py",
                    new_line=2,
                ),
            )
        )
        service = self._service(
            gitlab,
            FakeRepoManager(workspace),
            FakeReviewAgentRunner(
                result=ReviewResult(
                    summary="One issue found.",
                    overall_risk="medium",
                    findings=(ReviewFinding("medium", "file.py", 1, "Bug", "Needs fixing"),),
                )
            ),
        )

        self.store.enqueue_run(1, 2, "sha1", None)
        run = self.store.claim_next_run()
        service.process_run(run)

        self.assertEqual(len(gitlab.discussions), 2)
        self.assertEqual(gitlab.discussions[0].id, "human-1")
        self.assertEqual(len(gitlab.discussions[0].notes), 1)
        self.assertEqual(gitlab.discussions[1].root_note.author_id, gitlab.current_user.id)


if __name__ == "__main__":
    unittest.main()
