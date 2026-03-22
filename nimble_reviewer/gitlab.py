from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nimble_reviewer.models import MergeRequestInfo

LOGGER = logging.getLogger(__name__)


class GitLabError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitLabUser:
    id: int
    username: str | None = None


@dataclass(frozen=True)
class GitLabNote:
    id: int
    body: str
    author_id: int | None = None
    system: bool = False
    resolvable: bool = False
    resolved: bool = False


@dataclass(frozen=True)
class GitLabDiffVersion:
    id: int
    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True)
class GitLabDiffPosition:
    base_sha: str
    start_sha: str
    head_sha: str
    old_path: str
    new_path: str
    old_line: int | None = None
    new_line: int | None = None

    def to_api_payload(self) -> dict[str, Any]:
        payload = {
            "position_type": "text",
            "base_sha": self.base_sha,
            "start_sha": self.start_sha,
            "head_sha": self.head_sha,
            "old_path": self.old_path,
            "new_path": self.new_path,
        }
        if self.old_line is not None:
            payload["old_line"] = self.old_line
        if self.new_line is not None:
            payload["new_line"] = self.new_line
        return payload


@dataclass(frozen=True)
class GitLabDiscussion:
    id: str
    individual_note: bool
    notes: tuple[GitLabNote, ...]
    resolved: bool = False
    resolvable: bool = False
    position: GitLabDiffPosition | None = None

    @property
    def root_note(self) -> GitLabNote | None:
        return self.notes[0] if self.notes else None


class GitLabClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.api_base = f"{base_url.rstrip('/')}/api/v4"
        self.token = token
        self._current_user: GitLabUser | None = None

    def get_current_user(self) -> GitLabUser:
        if self._current_user is None:
            LOGGER.info("Fetching current GitLab user")
            payload = self._request_json("GET", "/user")
            self._current_user = GitLabUser(
                id=int(payload["id"]),
                username=payload.get("username"),
            )
        return self._current_user

    def get_merge_request(self, project_id: int, mr_iid: int) -> dict[str, Any]:
        LOGGER.info("Fetching merge request project=%s mr=%s", project_id, mr_iid)
        return self._request_json("GET", f"/projects/{project_id}/merge_requests/{mr_iid}")

    def get_merge_request_info(self, project_id: int, mr_iid: int) -> MergeRequestInfo:
        mr = self.get_merge_request(project_id, mr_iid)
        project = self.get_project(project_id)
        source_sha = mr.get("sha") or mr.get("diff_refs", {}).get("head_sha")
        if not source_sha:
            raise GitLabError(f"Merge request {project_id}/{mr_iid} has no source SHA")
        return MergeRequestInfo(
            project_id=project_id,
            mr_iid=mr_iid,
            title=mr.get("title") or "",
            description=mr.get("description") or "",
            source_branch=mr.get("source_branch") or "",
            target_branch=mr.get("target_branch") or "",
            source_sha=source_sha,
            web_url=mr.get("web_url") or "",
            repo_http_url=project.get("http_url_to_repo") or "",
        )

    def get_project(self, project_id: int) -> dict[str, Any]:
        LOGGER.info("Fetching project metadata project=%s", project_id)
        return self._request_json("GET", f"/projects/{project_id}")

    def get_merge_request_head_sha(self, project_id: int, mr_iid: int) -> str:
        LOGGER.info("Fetching MR head SHA project=%s mr=%s", project_id, mr_iid)
        mr = self.get_merge_request(project_id, mr_iid)
        return mr.get("sha") or mr.get("diff_refs", {}).get("head_sha") or ""

    def get_merge_request_versions(self, project_id: int, mr_iid: int) -> list[GitLabDiffVersion]:
        LOGGER.info("Fetching MR diff versions project=%s mr=%s", project_id, mr_iid)
        payload = self._request_json("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/versions")
        versions = [
            GitLabDiffVersion(
                id=int(item["id"]),
                base_sha=item.get("base_commit_sha") or "",
                start_sha=item.get("start_commit_sha") or "",
                head_sha=item.get("head_commit_sha") or "",
            )
            for item in payload or []
        ]
        return versions

    def get_latest_merge_request_version(self, project_id: int, mr_iid: int) -> GitLabDiffVersion:
        versions = self.get_merge_request_versions(project_id, mr_iid)
        if not versions:
            raise GitLabError(f"Merge request {project_id}/{mr_iid} has no diff versions")
        return versions[0]

    def get_note(self, project_id: int, mr_iid: int, note_id: int) -> GitLabNote | None:
        try:
            note = self._request_json("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}")
        except GitLabError as exc:
            if "404" in str(exc):
                return None
            raise
        return _parse_note(note)

    def find_bot_note(
        self,
        project_id: int,
        mr_iid: int,
        marker: str,
        preferred_note_id: int | None = None,
    ) -> GitLabNote | None:
        if preferred_note_id:
            LOGGER.info(
                "Looking up preferred summary note project=%s mr=%s note_id=%s",
                project_id,
                mr_iid,
                preferred_note_id,
            )
            note = self.get_note(project_id, mr_iid, preferred_note_id)
            if note and marker in note.body:
                return note

        LOGGER.info("Scanning MR notes for existing bot summary note project=%s mr=%s", project_id, mr_iid)
        for note in self.list_notes(project_id, mr_iid):
            if marker in note.body:
                return note
        return None

    def list_notes(self, project_id: int, mr_iid: int) -> list[GitLabNote]:
        notes: list[GitLabNote] = []
        page = 1
        while True:
            LOGGER.info("Listing MR notes project=%s mr=%s page=%s", project_id, mr_iid, page)
            payload, headers = self._request_json(
                "GET",
                f"/projects/{project_id}/merge_requests/{mr_iid}/notes?per_page=100&page={page}",
                return_headers=True,
            )
            notes.extend(_parse_note(note) for note in payload)
            next_page = headers.get("X-Next-Page", "")
            if not next_page:
                return notes
            page = int(next_page)

    def create_note(self, project_id: int, mr_iid: int, body: str) -> GitLabNote:
        LOGGER.info("Creating MR note project=%s mr=%s body_bytes=%s", project_id, mr_iid, len(body.encode("utf-8")))
        note = self._request_json(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            data={"body": body},
        )
        return _parse_note(note, fallback_body=body)

    def update_note(self, project_id: int, mr_iid: int, note_id: int, body: str) -> GitLabNote:
        LOGGER.info(
            "Updating MR note project=%s mr=%s note_id=%s body_bytes=%s",
            project_id,
            mr_iid,
            note_id,
            len(body.encode("utf-8")),
        )
        note = self._request_json(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}",
            data={"body": body},
        )
        return _parse_note(note, fallback_body=body)

    def list_merge_request_discussions(self, project_id: int, mr_iid: int) -> list[GitLabDiscussion]:
        discussions: list[GitLabDiscussion] = []
        page = 1
        while True:
            LOGGER.info("Listing MR discussions project=%s mr=%s page=%s", project_id, mr_iid, page)
            payload, headers = self._request_json(
                "GET",
                f"/projects/{project_id}/merge_requests/{mr_iid}/discussions?per_page=100&page={page}",
                return_headers=True,
            )
            discussions.extend(_parse_discussion(item) for item in payload or [])
            next_page = headers.get("X-Next-Page", "")
            if not next_page:
                return discussions
            page = int(next_page)

    def create_diff_discussion(
        self,
        project_id: int,
        mr_iid: int,
        body: str,
        position: GitLabDiffPosition,
    ) -> GitLabDiscussion:
        LOGGER.info("Creating diff discussion project=%s mr=%s", project_id, mr_iid)
        payload = self._request_json(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            data={"body": body, "position": position.to_api_payload()},
        )
        return _parse_discussion(payload)

    def add_discussion_note(self, project_id: int, mr_iid: int, discussion_id: str, body: str) -> GitLabNote:
        LOGGER.info("Creating discussion reply project=%s mr=%s discussion=%s", project_id, mr_iid, discussion_id)
        note = self._request_json(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes",
            data={"body": body},
        )
        return _parse_note(note, fallback_body=body)

    def set_discussion_resolved(
        self,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        *,
        resolved: bool,
    ) -> GitLabDiscussion:
        LOGGER.info(
            "Updating discussion resolved state project=%s mr=%s discussion=%s resolved=%s",
            project_id,
            mr_iid,
            discussion_id,
            resolved,
        )
        payload = self._request_json(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
            data={"resolved": resolved},
        )
        return _parse_discussion(payload)

    def _request_json(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        return_headers: bool = False,
    ) -> Any:
        url = f"{self.api_base}{path}"
        raw = None
        headers = {"PRIVATE-TOKEN": self.token}
        if data is not None:
            raw = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=raw, method=method, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else None
                if return_headers:
                    return payload, response.headers
                return payload
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GitLabError(f"GitLab API error {exc.code} for {method} {path}: {body}") from exc
        except URLError as exc:
            raise GitLabError(f"GitLab API network error for {method} {path}: {exc}") from exc


def _parse_note(payload: dict[str, Any], fallback_body: str | None = None) -> GitLabNote:
    author = payload.get("author") or {}
    return GitLabNote(
        id=int(payload["id"]),
        body=payload.get("body", fallback_body or ""),
        author_id=int(author["id"]) if author.get("id") is not None else None,
        system=bool(payload.get("system")),
        resolvable=bool(payload.get("resolvable")),
        resolved=bool(payload.get("resolved")),
    )


def _parse_discussion(payload: dict[str, Any]) -> GitLabDiscussion:
    position_payload = payload.get("position") or None
    position = None
    if isinstance(position_payload, dict):
        position = GitLabDiffPosition(
            base_sha=position_payload.get("base_sha") or "",
            start_sha=position_payload.get("start_sha") or "",
            head_sha=position_payload.get("head_sha") or "",
            old_path=position_payload.get("old_path") or position_payload.get("new_path") or "",
            new_path=position_payload.get("new_path") or position_payload.get("old_path") or "",
            old_line=position_payload.get("old_line"),
            new_line=position_payload.get("new_line"),
        )
    notes_payload = payload.get("notes") or []
    return GitLabDiscussion(
        id=str(payload["id"]),
        individual_note=bool(payload.get("individual_note")),
        notes=tuple(_parse_note(note) for note in notes_payload),
        resolved=bool(payload.get("resolved")),
        resolvable=bool(payload.get("resolvable")),
        position=position,
    )
