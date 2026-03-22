from __future__ import annotations

import re
from dataclasses import dataclass, field

from nimble_reviewer.gitlab import GitLabDiffPosition, GitLabDiffVersion

_HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")


@dataclass(frozen=True)
class DiffLineAnchor:
    old_path: str
    new_path: str
    old_line: int | None
    new_line: int | None


@dataclass
class DiffMapping:
    changed_lines_by_file: dict[str, set[int]] = field(default_factory=dict)
    anchors_by_file: dict[str, dict[int, DiffLineAnchor]] = field(default_factory=dict)

    def has_changes_near(self, file_path: str, line: int, *, radius: int = 2) -> bool:
        changed_lines = self.changed_lines_by_file.get(file_path, set())
        return any(abs(changed_line - line) <= radius for changed_line in changed_lines)

    def to_position(self, file_path: str, line: int, version: GitLabDiffVersion) -> GitLabDiffPosition | None:
        anchor = self.anchors_by_file.get(file_path, {}).get(line)
        if anchor is None:
            return None
        return GitLabDiffPosition(
            base_sha=version.base_sha,
            start_sha=version.start_sha,
            head_sha=version.head_sha,
            old_path=anchor.old_path,
            new_path=anchor.new_path,
            old_line=anchor.old_line,
            new_line=anchor.new_line,
        )


def build_diff_mapping(diff_text: str) -> DiffMapping:
    mapping = DiffMapping()
    current_old_path: str | None = None
    current_new_path: str | None = None
    old_line = 0
    new_line = 0
    in_hunk = False

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current_old_path = None
            current_new_path = None
            in_hunk = False
            continue
        if raw_line.startswith("--- "):
            current_old_path = _normalize_path(raw_line[4:].strip())
            continue
        if raw_line.startswith("+++ "):
            current_new_path = _normalize_path(raw_line[4:].strip())
            continue

        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            old_line = int(hunk_match.group("old_start"))
            new_line = int(hunk_match.group("new_start"))
            in_hunk = True
            continue

        if not in_hunk or current_new_path is None or current_old_path is None:
            continue
        if not raw_line:
            continue

        prefix = raw_line[0]
        if prefix == "+":
            anchor = DiffLineAnchor(
                old_path=_discussion_old_path(current_old_path, current_new_path),
                new_path=_discussion_new_path(current_old_path, current_new_path),
                old_line=None,
                new_line=new_line,
            )
            mapping.changed_lines_by_file.setdefault(current_new_path, set()).add(new_line)
            mapping.anchors_by_file.setdefault(current_new_path, {})[new_line] = anchor
            new_line += 1
            continue
        if prefix == "-":
            old_line += 1
            continue
        if prefix == " ":
            old_line += 1
            new_line += 1
            continue
        if prefix == "\\":
            continue

    return mapping


def _normalize_path(value: str) -> str:
    if value == "/dev/null":
        return value
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def _discussion_old_path(old_path: str, new_path: str) -> str:
    if old_path == "/dev/null" and new_path != "/dev/null":
        return new_path
    return old_path


def _discussion_new_path(old_path: str, new_path: str) -> str:
    if new_path == "/dev/null" and old_path != "/dev/null":
        return old_path
    return new_path
