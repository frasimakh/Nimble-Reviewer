from __future__ import annotations

import re

from nimble_reviewer.models import ReviewRequestEvent

DRAFT_PREFIX_RE = re.compile(r"^\s*(draft:|wip:|\[draft\]|\(draft\))", re.IGNORECASE)


def parse_review_request_event(payload: dict, *, bot_user_id: int | None = None) -> ReviewRequestEvent | None:
    object_kind = str(payload.get("object_kind") or "").strip().lower()
    if object_kind == "merge_request":
        return parse_merge_request_event(payload)
    if object_kind == "note":
        return parse_note_event(payload, bot_user_id=bot_user_id)
    return None


def parse_merge_request_event(payload: dict) -> ReviewRequestEvent | None:
    if payload.get("object_kind") != "merge_request":
        return None

    attributes = payload.get("object_attributes") or {}
    action = (attributes.get("action") or "").strip().lower()
    state = (attributes.get("state") or "").strip().lower()
    work_in_progress = bool(attributes.get("work_in_progress") or attributes.get("draft"))
    source_sha = _extract_source_sha(attributes, payload.get("merge_request") or {})

    if not action or not source_sha:
        return None
    if state in {"closed", "merged"}:
        return None

    if action in {"open", "reopen"}:
        if work_in_progress:
            return None
        return _build_merge_request_event(payload, attributes, action, source_sha)

    if action != "update":
        return None

    if work_in_progress:
        return None

    if _became_ready(attributes, payload) or _commit_pushed(payload):
        return _build_merge_request_event(payload, attributes, action, source_sha)

    return None


def parse_note_event(payload: dict, *, bot_user_id: int | None = None) -> ReviewRequestEvent | None:
    if payload.get("object_kind") != "note":
        return None

    attributes = payload.get("object_attributes") or {}
    merge_request = payload.get("merge_request") or {}
    noteable_type = str(attributes.get("noteable_type") or "").strip().lower()
    action = str(attributes.get("action") or "").strip().lower()
    author_id = _safe_int((payload.get("user") or {}).get("id"))
    project_id = _safe_int((payload.get("project") or {}).get("id"))
    mr_iid = _safe_int(merge_request.get("iid") or attributes.get("noteable_iid"))

    if noteable_type != "mergerequest":
        return None
    if action not in {"create", "update"}:
        return None
    if bool(attributes.get("system")):
        return None
    if bot_user_id is not None and author_id == bot_user_id:
        return None
    if project_id is None or mr_iid is None:
        return None

    return ReviewRequestEvent(
        project_id=project_id,
        mr_iid=mr_iid,
        kind="discussion_reconcile",
        source_sha=_extract_source_sha(attributes, merge_request),
        target_sha=_extract_target_sha(attributes, merge_request),
        action=action,
        trigger_discussion_id=_extract_discussion_id(attributes),
        trigger_note_id=_safe_int(attributes.get("id")),
        trigger_author_id=author_id,
    )


def _build_merge_request_event(payload: dict, attributes: dict, action: str, source_sha: str) -> ReviewRequestEvent:
    return ReviewRequestEvent(
        project_id=int(payload["project"]["id"]),
        mr_iid=int(attributes["iid"]),
        kind="full_review",
        source_sha=source_sha,
        target_sha=_extract_target_sha(attributes, payload.get("merge_request") or {}),
        action=action,
    )


def _extract_source_sha(attributes: dict, merge_request: dict) -> str | None:
    last_commit = attributes.get("last_commit") or {}
    mr_last_commit = merge_request.get("last_commit") or {}
    source = (
        last_commit.get("id")
        or mr_last_commit.get("id")
        or attributes.get("last_commit_id")
        or attributes.get("sha")
        or merge_request.get("sha")
        or merge_request.get("diff_head_sha")
        or attributes.get("commit_id")
        or attributes.get("source", {}).get("last_commit", {}).get("id")
    )
    return str(source).strip() or None


def _extract_target_sha(attributes: dict, merge_request: dict) -> str | None:
    source = (
        (attributes.get("target_last_commit") or {}).get("id")
        or (merge_request.get("target") or {}).get("last_commit", {}).get("id")
        or merge_request.get("target_branch_sha")
    )
    return str(source).strip() or None if source else None


def _extract_discussion_id(attributes: dict) -> str | None:
    for key in ("discussion_id", "discussionId"):
        value = attributes.get(key)
        if value:
            return str(value)
    return None


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _commit_pushed(payload: dict) -> bool:
    changes = payload.get("changes") or {}
    last_commit = changes.get("last_commit") or {}
    previous = last_commit.get("previous") or last_commit.get("before")
    current = last_commit.get("current") or last_commit.get("after")
    return bool(previous and current and previous != current)


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
