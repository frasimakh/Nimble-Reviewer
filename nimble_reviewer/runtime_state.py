from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def prepare_claude_state(home: Path) -> None:
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    config_link = home / ".claude.json"
    config_target = claude_dir / ".claude.json"
    backup_dir = claude_dir / "backups"

    if not config_link.exists() and not config_link.is_symlink():
        try:
            config_link.symlink_to(config_target)
            LOGGER.info("Created Claude config symlink %s -> %s", config_link, config_target)
        except OSError as exc:
            LOGGER.warning("Failed to create Claude config symlink %s -> %s: %s", config_link, config_target, exc)

    if config_target.exists():
        return

    backups = sorted(backup_dir.glob(".claude.json.backup.*"))
    if not backups:
        return

    latest_backup = backups[-1]
    try:
        config_target.write_text(latest_backup.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        LOGGER.info("Restored Claude config from backup %s", latest_backup)
    except OSError as exc:
        LOGGER.warning("Failed to restore Claude config from backup %s: %s", latest_backup, exc)
