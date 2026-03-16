import json
import unittest

from nimble_reviewer.review_agent import ReviewAgentError


class ReviewAgentTests(unittest.TestCase):
    def test_claude_runner_accepts_json_envelope_result(self):
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": json.dumps(
                    {
                        "summary": "Looks fine.",
                        "overall_risk": "low",
                        "findings": [],
                    }
                ),
            }
        )

        result = _parse_with_claude_envelope(raw)

        self.assertEqual(result["summary"], "Looks fine.")
        self.assertEqual(result["overall_risk"], "low")
        self.assertEqual(result["findings"], [])

    def test_claude_runner_raises_on_error_envelope(self):
        raw = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "Authentication required",
            }
        )

        with self.assertRaises(ReviewAgentError):
            _parse_with_claude_envelope(raw)

    def test_extract_claude_result_from_stream_json(self):
        raw = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "duration_ms": 1234,
                        "num_turns": 2,
                        "total_cost_usd": 0.12,
                        "result": json.dumps(
                            {
                                "summary": "Looks fine.",
                                "overall_risk": "low",
                                "findings": [],
                            }
                        ),
                    }
                ),
            ]
        )

        result = _extract_claude_result_from_stream(raw)

        self.assertEqual(result["summary"], "Looks fine.")
        self.assertEqual(result["overall_risk"], "low")
        self.assertEqual(result["findings"], [])

    def test_record_claude_stream_events_extracts_usage_and_cost(self):
        trace = _MemoryTrace()
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "id": "msg-1",
                        "message": {
                            "usage": {
                                "input_tokens": 120,
                                "cache_read_input_tokens": 50,
                                "cache_creation_input_tokens": 30,
                                "output_tokens": 12,
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "id": "msg-1",
                        "message": {
                            "usage": {
                                "input_tokens": 120,
                                "cache_read_input_tokens": 50,
                                "cache_creation_input_tokens": 30,
                                "output_tokens": 12,
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "total_cost_usd": 0.123456,
                        "result": "{\"summary\":\"ok\",\"overall_risk\":\"low\",\"findings\":[]}",
                    }
                ),
            ]
        )

        usage = _record_claude_events(raw, "stream-json", trace)

        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 120)
        self.assertEqual(usage.cached_input_tokens, 50)
        self.assertEqual(usage.cache_creation_input_tokens, 30)
        self.assertEqual(usage.output_tokens, 12)
        self.assertEqual(usage.total_tokens, 212)
        self.assertAlmostEqual(usage.cost_usd, 0.123456)
        self.assertTrue(any(entry["event"] == "claude.usage" for entry in trace.entries))

    def test_record_codex_events_extracts_usage(self):
        trace = _MemoryTrace()
        raw = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "abc"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 10}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 3}}),
            ]
        )

        usage = _record_codex_events(raw, trace)

        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 120)
        self.assertEqual(usage.cached_input_tokens, 30)
        self.assertEqual(usage.output_tokens, 13)
        self.assertEqual(usage.total_tokens, 133)
        self.assertTrue(any(entry["event"] == "codex.usage" for entry in trace.entries))

    def test_extract_codex_final_message_from_event_stream(self):
        raw = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "abc"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "{\"summary\":\"ok\",\"overall_risk\":\"low\",\"findings\":[]}",
                        },
                    }
                ),
            ]
        )

        result = _extract_codex_final_message(raw)

        self.assertEqual(result, "{\"summary\":\"ok\",\"overall_risk\":\"low\",\"findings\":[]}")

    def test_extract_agent_metadata_reads_model_and_reasoning(self):
        metadata = _extract_agent_metadata(
            "codex",
            ("codex", "exec", "-m", "gpt-5.4", "-c", 'model_reasoning_effort="xhigh"', "-"),
        )

        self.assertEqual(metadata.provider, "codex")
        self.assertEqual(metadata.model, "gpt-5.4")
        self.assertEqual(metadata.reasoning_effort, "xhigh")

    def test_extract_agent_metadata_reads_claude_model_and_effort(self):
        metadata = _extract_agent_metadata(
            "claude",
            ("claude", "-p", "--model", "claude-sonnet-4-5", "--effort", "high"),
        )

        self.assertEqual(metadata.provider, "claude")
        self.assertEqual(metadata.model, "claude-sonnet-4-5")
        self.assertEqual(metadata.reasoning_effort, "high")

    def test_claude_stream_json_adds_verbose(self):
        command = _normalize_claude_command(("claude", "-p", "--output-format", "stream-json"))

        self.assertIn("--verbose", command)

    def test_claude_stream_json_keeps_existing_verbose(self):
        command = _normalize_claude_command(("claude", "-p", "--output-format", "stream-json", "--verbose"))

        self.assertEqual(command.count("--verbose"), 1)


def _parse_with_claude_envelope(raw: str) -> dict:
    from nimble_reviewer.review_agent import _extract_claude_result, _load_json_object

    return _load_json_object(_extract_claude_result(raw))


def _parse_review_result(raw: str):
    from nimble_reviewer.review_agent import _parse_review_result

    return _parse_review_result(raw)


def _record_codex_events(raw: str, trace):
    from nimble_reviewer.review_agent import _record_codex_events

    return _record_codex_events(raw, trace)


def _record_claude_events(raw: str, output_format: str, trace):
    from nimble_reviewer.review_agent import _record_claude_events

    return _record_claude_events(raw, output_format, trace)


def _extract_codex_final_message(raw: str):
    from nimble_reviewer.review_agent import _extract_codex_final_message

    return _extract_codex_final_message(raw)


def _extract_agent_metadata(provider: str, command: tuple[str, ...]):
    from nimble_reviewer.review_agent import _extract_agent_metadata

    return _extract_agent_metadata(provider, command)


def _normalize_claude_command(command: tuple[str, ...]) -> tuple[str, ...]:
    from nimble_reviewer.review_agent import _normalize_claude_command

    return _normalize_claude_command(list(command))


def _extract_claude_result_from_stream(raw: str) -> dict:
    from nimble_reviewer.review_agent import _extract_claude_result_from_stream, _load_json_object

    return _load_json_object(_extract_claude_result_from_stream(raw))


class _MemoryTrace:
    def __init__(self):
        self.entries = []

    def write(self, source, event, **payload):
        self.entries.append({"source": source, "event": event, **payload})


if __name__ == "__main__":
    unittest.main()
