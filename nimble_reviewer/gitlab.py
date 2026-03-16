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
class GitLabNote:
    id: int
    body: str


class GitLabClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.api_base = f"{base_url.rstrip('/')}/api/v4"
        self.token = token

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

    def get_note(self, project_id: int, mr_iid: int, note_id: int) -> GitLabNote | None:
        try:
            note = self._request_json("GET", f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}")
        except GitLabError as exc:
            if "404" in str(exc):
                return None
            raise
        return GitLabNote(id=int(note["id"]), body=note.get("body", ""))

    def find_bot_note(self, project_id: int, mr_iid: int, marker: str, preferred_note_id: int | None = None) -> GitLabNote | None:
        if preferred_note_id:
            LOGGER.info("Looking up preferred note project=%s mr=%s note_id=%s", project_id, mr_iid, preferred_note_id)
            note = self.get_note(project_id, mr_iid, preferred_note_id)
            if note and marker in note.body:
                return note

        LOGGER.info("Scanning MR notes for existing bot note project=%s mr=%s", project_id, mr_iid)
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
            notes.extend(GitLabNote(id=int(note["id"]), body=note.get("body", "")) for note in payload)
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
        return GitLabNote(id=int(note["id"]), body=note.get("body", body))

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
        return GitLabNote(id=int(note["id"]), body=note.get("body", body))

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
