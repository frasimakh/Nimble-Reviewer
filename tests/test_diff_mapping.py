import unittest

from nimble_reviewer.diff_mapping import build_diff_mapping
from nimble_reviewer.gitlab import GitLabDiffVersion


class DiffMappingTests(unittest.TestCase):
    def test_maps_added_line_to_gitlab_position(self):
        diff_text = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+line2\n"
            " line3\n"
        )
        mapping = build_diff_mapping(diff_text)

        self.assertTrue(mapping.has_changes_near("src/app.py", 2))
        position = mapping.to_position(
            "src/app.py",
            2,
            GitLabDiffVersion(id=1, base_sha="base", start_sha="start", head_sha="head"),
        )

        self.assertIsNotNone(position)
        self.assertEqual(position.new_path, "src/app.py")
        self.assertEqual(position.new_line, 2)
        self.assertIsNone(position.old_line)

    def test_returns_none_for_unmapped_line(self):
        diff_text = (
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            " line1\n"
        )
        mapping = build_diff_mapping(diff_text)
        position = mapping.to_position(
            "src/app.py",
            1,
            GitLabDiffVersion(id=1, base_sha="base", start_sha="start", head_sha="head"),
        )
        self.assertIsNone(position)

    def test_maps_added_line_in_new_file_without_dev_null_old_path(self):
        diff_text = (
            "diff --git a/src/new_app.py b/src/new_app.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/new_app.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
        )
        mapping = build_diff_mapping(diff_text)

        position = mapping.to_position(
            "src/new_app.py",
            2,
            GitLabDiffVersion(id=1, base_sha="base", start_sha="start", head_sha="head"),
        )

        self.assertIsNotNone(position)
        assert position is not None
        self.assertEqual(position.old_path, "src/new_app.py")
        self.assertEqual(position.new_path, "src/new_app.py")
        self.assertEqual(position.new_line, 2)
        self.assertIsNone(position.old_line)


if __name__ == "__main__":
    unittest.main()
