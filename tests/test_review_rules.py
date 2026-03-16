import tempfile
import unittest
from pathlib import Path

from nimble_reviewer.review_rules import MAX_RULE_CHARS, load_repo_review_rules


class ReviewRulesTests(unittest.TestCase):
    def test_loads_root_rules_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "NIMBLE-REVIEWER.MD").write_text("root rules", encoding="utf-8")

            rules = load_repo_review_rules(root)

            self.assertIsNotNone(rules)
            self.assertEqual(rules.path, "NIMBLE-REVIEWER.MD")
            self.assertEqual(rules.text, "root rules")
            self.assertFalse(rules.truncated)

    def test_returns_none_when_rules_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            rules = load_repo_review_rules(root)

            self.assertIsNone(rules)

    def test_truncates_large_rules_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "NIMBLE-REVIEWER.MD").write_text("x" * (MAX_RULE_CHARS + 50), encoding="utf-8")

            rules = load_repo_review_rules(root)

            self.assertIsNotNone(rules)
            self.assertTrue(rules.truncated)
            self.assertEqual(len(rules.text), MAX_RULE_CHARS)


if __name__ == "__main__":
    unittest.main()
