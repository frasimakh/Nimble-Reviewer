from __future__ import annotations

import unittest

from nimble_reviewer.models import MergeRequestInfo
from nimble_reviewer.prompts import build_discussion_reconcile_prompt


class DiscussionReconcilePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mr = MergeRequestInfo(
            project_id=1,
            mr_iid=2,
            title="Title",
            description="Description",
            source_branch="feature",
            target_branch="main",
            source_sha="sha123",
            web_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            repo_http_url="https://gitlab.example.com/group/project.git",
        )

    def test_uses_relevant_file_diff_instead_of_first_diff_chunk(self) -> None:
        early_file_block = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        filler = "x" * 25_000 + "\n"
        target_file_block = (
            "diff --git a/bin/alembic b/bin/alembic\n"
            "--- a/bin/alembic\n"
            "+++ b/bin/alembic\n"
            "@@ -1,4 +1,4 @@\n"
            '-exec "../runtime/bin/python"\n'
            '+exec "$(dirname "$0")/python"\n'
        )
        prompt = build_discussion_reconcile_prompt(
            self.mr,
            discussion_id="discussion-1",
            discussion_text="Human: fixed",
            trigger_note_body="fixed",
            linked_finding_payload={"file": "bin/alembic", "line": 4},
            diff_text=early_file_block + filler + target_file_block,
            finding_file="bin/alembic",
        )

        self.assertIn('diff --git a/bin/alembic b/bin/alembic', prompt)
        self.assertIn('+exec "$(dirname "$0")/python"', prompt)
        self.assertNotIn("diff --git a/a.py b/a.py", prompt)

    def test_falls_back_to_full_diff_when_finding_file_is_absent(self) -> None:
        diff_text = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        prompt = build_discussion_reconcile_prompt(
            self.mr,
            discussion_id="discussion-1",
            discussion_text="Human: fixed",
            trigger_note_body="fixed",
            linked_finding_payload={"file": "missing.py", "line": 1},
            diff_text=diff_text,
            finding_file="missing.py",
        )

        self.assertIn("diff --git a/a.py b/a.py", prompt)


if __name__ == "__main__":
    unittest.main()
