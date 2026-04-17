import os
import unittest
from unittest.mock import patch

from nimble_reviewer.config import Settings


class SettingsTests(unittest.TestCase):
    def test_codex_cmd_defaults_to_container_safe_flags(self):
        env = {
            "GITLAB_URL": "https://gitlab.example.com",
            "GITLAB_TOKEN": "token",
            "GITLAB_WEBHOOK_SECRET": "secret",
            "SQLITE_PATH": "/tmp/nimble-reviewer.db",
            "REPO_CACHE_DIR": "/tmp/repos",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(
            settings.codex_cmd,
            (
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "-m",
                "gpt-5.4",
                "-c",
                "model_reasoning_effort=high",
                "-",
            ),
        )
