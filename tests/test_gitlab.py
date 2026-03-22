"""Tests for nimble_reviewer.gitlab — GitLabClient and helpers.

All HTTP calls are intercepted via unittest.mock.patch on urllib.request.urlopen
so no real network access is needed.
"""

from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from nimble_reviewer.gitlab import (
    GitLabClient,
    GitLabDiffPosition,
    GitLabError,
    GitLabNote,
    _parse_discussion,
    _parse_note,
)
from nimble_reviewer.models import MergeRequestInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(payload, *, status=200, next_page: str = "") -> MagicMock:
    """Return a fake urllib response context-manager."""
    body = json.dumps(payload).encode("utf-8")
    response = MagicMock()
    response.read.return_value = body
    response.headers = {"X-Next-Page": next_page}
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


def _make_error_response(code: int, body: str = "error"):
    from urllib.error import HTTPError

    return HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body.encode()),
    )


def _client() -> GitLabClient:
    return GitLabClient("https://gitlab.example.com", "secret-token")


# ---------------------------------------------------------------------------
# _parse_note / _parse_discussion
# ---------------------------------------------------------------------------


class ParseNoteTests(unittest.TestCase):
    def test_basic(self):
        note = _parse_note({"id": 1, "body": "hello", "author": {"id": 42}, "system": False, "resolvable": True, "resolved": False})
        self.assertEqual(note.id, 1)
        self.assertEqual(note.body, "hello")
        self.assertEqual(note.author_id, 42)
        self.assertFalse(note.system)
        self.assertTrue(note.resolvable)
        self.assertFalse(note.resolved)

    def test_fallback_body(self):
        note = _parse_note({"id": 2, "author": {}}, fallback_body="fallback")
        self.assertEqual(note.body, "fallback")

    def test_missing_author_id(self):
        note = _parse_note({"id": 3, "body": "x", "author": {}})
        self.assertIsNone(note.author_id)


class ParseDiscussionTests(unittest.TestCase):
    def test_without_position(self):
        payload = {
            "id": "abc",
            "individual_note": False,
            "notes": [{"id": 1, "body": "hi", "author": {"id": 5}}],
            "resolved": False,
            "resolvable": True,
        }
        discussion = _parse_discussion(payload)
        self.assertEqual(discussion.id, "abc")
        self.assertIsNone(discussion.position)
        self.assertEqual(len(discussion.notes), 1)

    def test_with_position(self):
        payload = {
            "id": "d1",
            "individual_note": False,
            "notes": [],
            "resolved": False,
            "resolvable": True,
            "position": {
                "base_sha": "aaa",
                "start_sha": "bbb",
                "head_sha": "ccc",
                "new_path": "file.py",
                "old_path": "file.py",
                "new_line": 10,
                "old_line": None,
            },
        }
        discussion = _parse_discussion(payload)
        self.assertIsNotNone(discussion.position)
        assert discussion.position is not None
        self.assertEqual(discussion.position.new_path, "file.py")
        self.assertEqual(discussion.position.new_line, 10)
        self.assertIsNone(discussion.position.old_line)


# ---------------------------------------------------------------------------
# GitLabClient — HTTP error handling
# ---------------------------------------------------------------------------


class GitLabClientErrorHandlingTests(unittest.TestCase):
    def test_http_error_raises_gitlab_error(self):
        client = _client()
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=_make_error_response(404, '{"message":"Not found"}')):
            with self.assertRaises(GitLabError) as ctx:
                client.get_project(99)
        self.assertIn("404", str(ctx.exception))

    def test_url_error_raises_gitlab_error(self):
        from urllib.error import URLError

        client = _client()
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=URLError("connection refused")):
            with self.assertRaises(GitLabError) as ctx:
                client.get_project(99)
        self.assertIn("network error", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# GitLabClient — get_current_user (with caching)
# ---------------------------------------------------------------------------


class GetCurrentUserTests(unittest.TestCase):
    def test_returns_user(self):
        client = _client()
        resp = _make_response({"id": 7, "username": "bot"})
        with patch("nimble_reviewer.gitlab.urlopen", return_value=resp) as mock_open:
            user = client.get_current_user()
        self.assertEqual(user.id, 7)
        self.assertEqual(user.username, "bot")
        mock_open.assert_called_once()

    def test_caches_result(self):
        client = _client()
        resp = _make_response({"id": 7, "username": "bot"})
        with patch("nimble_reviewer.gitlab.urlopen", return_value=resp) as mock_open:
            client.get_current_user()
            client.get_current_user()
        # Second call must not hit the network
        mock_open.assert_called_once()


# ---------------------------------------------------------------------------
# GitLabClient — get_merge_request_info
# ---------------------------------------------------------------------------


class GetMergeRequestInfoTests(unittest.TestCase):
    def _mr_payload(self):
        return {
            "sha": "deadbeef",
            "title": "Fix bug",
            "description": "desc",
            "source_branch": "feature",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/g/p/-/merge_requests/1",
        }

    def _project_payload(self):
        return {"http_url_to_repo": "https://gitlab.example.com/g/p.git"}

    def test_assembles_merge_request_info(self):
        client = _client()
        responses = iter([_make_response(self._mr_payload()), _make_response(self._project_payload())])
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=lambda *a, **kw: next(responses)):
            info = client.get_merge_request_info(1, 1)

        self.assertIsInstance(info, MergeRequestInfo)
        self.assertEqual(info.source_sha, "deadbeef")
        self.assertEqual(info.repo_http_url, "https://gitlab.example.com/g/p.git")
        self.assertEqual(info.title, "Fix bug")

    def test_raises_when_sha_missing(self):
        client = _client()
        mr = self._mr_payload()
        del mr["sha"]
        responses = iter([_make_response(mr), _make_response(self._project_payload())])
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=lambda *a, **kw: next(responses)):
            with self.assertRaises(GitLabError):
                client.get_merge_request_info(1, 1)


# ---------------------------------------------------------------------------
# GitLabClient — list_notes (pagination)
# ---------------------------------------------------------------------------


class ListNotesTests(unittest.TestCase):
    def test_single_page(self):
        client = _client()
        notes_payload = [{"id": 1, "body": "first", "author": {"id": 5}}]
        resp = _make_response(notes_payload, next_page="")
        with patch("nimble_reviewer.gitlab.urlopen", return_value=resp):
            notes = client.list_notes(1, 1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].id, 1)

    def test_multiple_pages(self):
        client = _client()
        page1 = _make_response([{"id": 1, "body": "a", "author": {"id": 5}}], next_page="2")
        page2 = _make_response([{"id": 2, "body": "b", "author": {"id": 5}}], next_page="")
        pages = iter([page1, page2])
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=lambda *a, **kw: next(pages)):
            notes = client.list_notes(1, 1)
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[1].id, 2)


# ---------------------------------------------------------------------------
# GitLabClient — find_bot_note
# ---------------------------------------------------------------------------


class FindBotNoteTests(unittest.TestCase):
    def test_finds_preferred_note_by_id(self):
        client = _client()
        note_payload = {"id": 99, "body": "<!-- nimble-marker -->review", "author": {"id": 7}}
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(note_payload)):
            note = client.find_bot_note(1, 1, "<!-- nimble-marker -->", preferred_note_id=99)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertEqual(note.id, 99)

    def test_returns_none_when_marker_missing(self):
        client = _client()
        note_payload = {"id": 99, "body": "no marker here", "author": {"id": 7}}
        notes_payload = [note_payload]
        responses = iter([
            _make_response(note_payload),        # get_note attempt
            _make_response(notes_payload, next_page=""),  # list_notes fallback
        ])
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=lambda *a, **kw: next(responses)):
            note = client.find_bot_note(1, 1, "<!-- nimble-marker -->", preferred_note_id=99)
        self.assertIsNone(note)

    def test_falls_back_to_list_scan(self):
        client = _client()
        notes_payload = [
            {"id": 10, "body": "unrelated", "author": {"id": 7}},
            {"id": 11, "body": "<!-- nimble-marker -->review", "author": {"id": 7}},
        ]
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(notes_payload, next_page="")):
            note = client.find_bot_note(1, 1, "<!-- nimble-marker -->")
        self.assertIsNotNone(note)
        assert note is not None
        self.assertEqual(note.id, 11)


# ---------------------------------------------------------------------------
# GitLabClient — create_note / update_note
# ---------------------------------------------------------------------------


class NoteWriteTests(unittest.TestCase):
    def test_create_note_returns_parsed_note(self):
        client = _client()
        resp_payload = {"id": 55, "body": "hello", "author": {"id": 7}}
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(resp_payload)):
            note = client.create_note(1, 1, "hello")
        self.assertEqual(note.id, 55)
        self.assertEqual(note.body, "hello")

    def test_update_note_returns_parsed_note(self):
        client = _client()
        resp_payload = {"id": 55, "body": "updated", "author": {"id": 7}}
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(resp_payload)):
            note = client.update_note(1, 1, 55, "updated")
        self.assertEqual(note.body, "updated")


# ---------------------------------------------------------------------------
# GitLabClient — list_merge_request_discussions (pagination)
# ---------------------------------------------------------------------------


class ListDiscussionsTests(unittest.TestCase):
    def _discussion_payload(self, discussion_id: str, note_id: int):
        return {
            "id": discussion_id,
            "individual_note": False,
            "notes": [{"id": note_id, "body": "comment", "author": {"id": 5}}],
            "resolved": False,
            "resolvable": True,
        }

    def test_single_page(self):
        client = _client()
        payload = [self._discussion_payload("d1", 1)]
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(payload, next_page="")):
            discussions = client.list_merge_request_discussions(1, 1)
        self.assertEqual(len(discussions), 1)
        self.assertEqual(discussions[0].id, "d1")

    def test_multiple_pages(self):
        client = _client()
        page1 = _make_response([self._discussion_payload("d1", 1)], next_page="2")
        page2 = _make_response([self._discussion_payload("d2", 2)], next_page="")
        pages = iter([page1, page2])
        with patch("nimble_reviewer.gitlab.urlopen", side_effect=lambda *a, **kw: next(pages)):
            discussions = client.list_merge_request_discussions(1, 1)
        self.assertEqual(len(discussions), 2)
        self.assertEqual(discussions[1].id, "d2")


# ---------------------------------------------------------------------------
# GitLabClient — create_diff_discussion
# ---------------------------------------------------------------------------


class CreateDiffDiscussionTests(unittest.TestCase):
    def test_creates_discussion(self):
        client = _client()
        resp_payload = {
            "id": "new-d",
            "individual_note": False,
            "notes": [{"id": 100, "body": "finding", "author": {"id": 7}}],
            "resolved": False,
            "resolvable": True,
        }
        position = GitLabDiffPosition(
            base_sha="aaa",
            start_sha="bbb",
            head_sha="ccc",
            old_path="file.py",
            new_path="file.py",
            new_line=5,
        )
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(resp_payload)):
            discussion = client.create_diff_discussion(1, 1, "finding", position)
        self.assertEqual(discussion.id, "new-d")
        self.assertEqual(len(discussion.notes), 1)


# ---------------------------------------------------------------------------
# GitLabClient — set_discussion_resolved
# ---------------------------------------------------------------------------


class SetDiscussionResolvedTests(unittest.TestCase):
    def test_resolves_discussion(self):
        client = _client()
        resp_payload = {
            "id": "d1",
            "individual_note": False,
            "notes": [],
            "resolved": True,
            "resolvable": True,
        }
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(resp_payload)):
            discussion = client.set_discussion_resolved(1, 1, "d1", resolved=True)
        self.assertTrue(discussion.resolved)


# ---------------------------------------------------------------------------
# GitLabClient — get_merge_request_versions
# ---------------------------------------------------------------------------


class GetMergeRequestVersionsTests(unittest.TestCase):
    def test_returns_versions(self):
        client = _client()
        payload = [
            {"id": 3, "base_commit_sha": "base3", "start_commit_sha": "start3", "head_commit_sha": "head3"},
            {"id": 2, "base_commit_sha": "base2", "start_commit_sha": "start2", "head_commit_sha": "head2"},
        ]
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response(payload)):
            versions = client.get_merge_request_versions(1, 1)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].id, 3)
        self.assertEqual(versions[0].head_sha, "head3")

    def test_get_latest_raises_when_empty(self):
        client = _client()
        with patch("nimble_reviewer.gitlab.urlopen", return_value=_make_response([])):
            with self.assertRaises(GitLabError):
                client.get_latest_merge_request_version(1, 1)


# ---------------------------------------------------------------------------
# GitLabDiffPosition.to_api_payload
# ---------------------------------------------------------------------------


class DiffPositionPayloadTests(unittest.TestCase):
    def test_includes_new_line_only(self):
        pos = GitLabDiffPosition(
            base_sha="b", start_sha="s", head_sha="h",
            old_path="f.py", new_path="f.py", new_line=7,
        )
        payload = pos.to_api_payload()
        self.assertEqual(payload["new_line"], 7)
        self.assertNotIn("old_line", payload)

    def test_omits_none_lines(self):
        pos = GitLabDiffPosition(
            base_sha="b", start_sha="s", head_sha="h",
            old_path="f.py", new_path="f.py",
        )
        payload = pos.to_api_payload()
        self.assertNotIn("new_line", payload)
        self.assertNotIn("old_line", payload)

    def test_normalizes_dev_null_old_path_for_new_file(self):
        pos = GitLabDiffPosition(
            base_sha="b",
            start_sha="s",
            head_sha="h",
            old_path="/dev/null",
            new_path="f.py",
            new_line=7,
        )
        payload = pos.to_api_payload()
        self.assertEqual(payload["old_path"], "f.py")
        self.assertEqual(payload["new_path"], "f.py")


if __name__ == "__main__":
    unittest.main()
