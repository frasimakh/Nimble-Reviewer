import unittest

from nimble_reviewer.webhook import parse_merge_request_event, parse_note_event, parse_review_request_event


def _mr_payload(action: str, **attributes):
    payload = {
        "object_kind": "merge_request",
        "project": {"id": 42},
        "object_attributes": {
            "action": action,
            "iid": 7,
            "state": "opened",
            "title": "Add feature",
            "work_in_progress": False,
            "oldrev": None,
            "last_commit": {"id": "abc123"},
        },
        "changes": {},
    }
    payload["object_attributes"].update(attributes)
    return payload


def _note_payload(action: str = "create", **attributes):
    payload = {
        "object_kind": "note",
        "project": {"id": 42},
        "user": {"id": 777},
        "merge_request": {"iid": 7, "sha": "abc123"},
        "object_attributes": {
            "action": action,
            "id": 88,
            "discussion_id": "discussion-1",
            "noteable_type": "MergeRequest",
            "noteable_iid": 7,
            "system": False,
            "note": "Looks risky.",
        },
    }
    payload["object_attributes"].update(attributes)
    return payload


class ParseWebhookEventTests(unittest.TestCase):
    def test_open_non_draft_triggers_full_review(self):
        event = parse_merge_request_event(_mr_payload("open"))
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "full_review")
        self.assertEqual(event.source_sha, "abc123")

    def test_open_draft_is_ignored(self):
        event = parse_merge_request_event(_mr_payload("open", work_in_progress=True))
        self.assertIsNone(event)

    def test_update_from_draft_to_ready_triggers(self):
        payload = _mr_payload("update", work_in_progress=False)
        payload["changes"] = {"work_in_progress": {"previous": True}}
        event = parse_merge_request_event(payload)
        self.assertIsNotNone(event)

    def test_note_hook_for_merge_request_creates_reconcile_event(self):
        event = parse_note_event(_note_payload(), bot_user_id=900)
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "discussion_reconcile")
        self.assertEqual(event.trigger_discussion_id, "discussion-1")
        self.assertEqual(event.trigger_note_id, 88)

    def test_note_hook_ignores_bot_authored_note(self):
        payload = _note_payload()
        payload["user"]["id"] = 900
        self.assertIsNone(parse_note_event(payload, bot_user_id=900))

    def test_note_hook_ignores_non_merge_request_notes(self):
        payload = _note_payload(noteable_type="Commit")
        self.assertIsNone(parse_review_request_event(payload, bot_user_id=900))


if __name__ == "__main__":
    unittest.main()
