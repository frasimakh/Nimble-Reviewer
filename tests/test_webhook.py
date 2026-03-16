import unittest

from nimble_reviewer.webhook import parse_merge_request_event


def _payload(action: str, **attributes):
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


class ParseMergeRequestEventTests(unittest.TestCase):
    def test_open_non_draft_triggers(self):
        event = parse_merge_request_event(_payload("open"))
        self.assertIsNotNone(event)
        self.assertEqual(event.source_sha, "abc123")

    def test_open_draft_is_ignored(self):
        event = parse_merge_request_event(_payload("open", work_in_progress=True))
        self.assertIsNone(event)

    def test_update_with_new_commit_is_ignored(self):
        event = parse_merge_request_event(_payload("update", oldrev="old456"))
        self.assertIsNone(event)

    def test_update_without_commit_change_is_ignored(self):
        event = parse_merge_request_event(_payload("update", oldrev="abc123"))
        self.assertIsNone(event)

    def test_update_from_draft_to_ready_triggers(self):
        payload = _payload("update", work_in_progress=False)
        payload["changes"] = {"work_in_progress": {"previous": True}}
        event = parse_merge_request_event(payload)
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
