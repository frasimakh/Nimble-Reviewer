import tempfile
import unittest
from pathlib import Path


class AppTests(unittest.TestCase):
    def test_prepare_claude_state_creates_symlink_and_restores_backup(self):
        from nimble_reviewer.runtime_state import prepare_claude_state

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            backup_dir = home / ".claude" / "backups"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / ".claude.json.backup.123"
            backup.write_text('{"ok":true}', encoding="utf-8")

            prepare_claude_state(home)

            config_link = home / ".claude.json"
            config_target = home / ".claude" / ".claude.json"
            self.assertTrue(config_link.is_symlink())
            self.assertEqual(config_link.resolve(), config_target.resolve())
            self.assertEqual(config_target.read_text(encoding="utf-8"), '{"ok":true}')


if __name__ == "__main__":
    unittest.main()
