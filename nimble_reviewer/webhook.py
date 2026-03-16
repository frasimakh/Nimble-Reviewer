from __future__ import annotations

import re

from nimble_reviewer.models import MergeRequestEvent

DRAFT_PREFIX_RE = re.compile(r"^\s*(draft:|wip:|\[draft\]|\(draft\))", re.IGNORECASE)


def parse_merge_request_event(payload: dict) -> MergeRequestEvent | None:
    if payload.get("object_kind") != "merge_request":
        return None

    attributes = payload.get("object_attributes") or {}
    action = (attributes.get("action") or "").strip().lower()
    state = (attributes.get("state") or "").strip().lower()
    work_in_progress = bool(attributes.get("work_in_progress") or attributes.get("draft"))
    source_sha = _extract_source_sha(attributes)

    if not action or not source_sha:
        return None
    if state in {"closed", "merged"}:
        return None

    if action in {"open", "reopen"}:
        if work_in_progress:
            return None
        return _build_event(payload, attributes, action, source_sha)

    if action != "update":
        return None

    if _became_ready(attributes, payload):
        return _build_event(payload, attributes, action, source_sha)

    return None


def _build_event(payload: dict, attributes: dict, action: str, source_sha: str) -> MergeRequestEvent:
    return MergeRequestEvent(
        project_id=int(payload["project"]["id"]),
        mr_iid=int(attributes["iid"]),
        source_sha=source_sha,
        target_sha=attributes.get("target_last_commit", {}).get("id"),
        action=action,
    )


def _extract_source_sha(attributes: dict) -> str | None:
    last_commit = attributes.get("last_commit") or {}
    return (
        last_commit.get("id")
        or attributes.get("last_commit_id")
        or attributes.get("sha")
        or attributes.get("source", {}).get("last_commit", {}).get("id")
    )

def _became_ready(attributes: dict, payload: dict) -> bool:
    changes = payload.get("changes") or {}
    previous_wip = changes.get("work_in_progress", {}).get("previous")
    if previous_wip is True and not attributes.get("work_in_progress"):
        return True

    previous_title = changes.get("title", {}).get("previous")
    current_title = attributes.get("title", "")
    return bool(previous_title and _is_draft_title(previous_title) and not _is_draft_title(current_title))


def _is_draft_title(value: str) -> bool:
    return bool(DRAFT_PREFIX_RE.match(value or ""))
