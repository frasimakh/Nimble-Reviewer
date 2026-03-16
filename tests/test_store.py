import tempfile
import unittest
from pathlib import Path

from nimble_reviewer.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmpdir.name) / "state.db")
        self.store.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_dedupes_same_sha(self):
        first = self.store.enqueue_run(1, 2, "sha1", None)
        second = self.store.enqueue_run(1, 2, "sha1", None)
        self.assertTrue(first.enqueued)
        self.assertFalse(second.enqueued)
        self.assertIn("duplicate", second.reason)

    def test_new_sha_supersedes_running_run(self):
        first = self.store.enqueue_run(1, 2, "sha1", None)
        claimed = self.store.claim_next_run()
        self.assertEqual(claimed.id, first.run_id)
        second = self.store.enqueue_run(1, 2, "sha2", None)
        first_run = self.store.get_run(first.run_id)
        second_run = self.store.get_run(second.run_id)
        self.assertEqual(first_run.status, "superseded")
        self.assertEqual(first_run.superseded_by, second.run_id)
        self.assertEqual(second_run.status, "queued")


if __name__ == "__main__":
    unittest.main()
