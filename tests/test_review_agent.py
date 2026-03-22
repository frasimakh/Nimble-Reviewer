import json
import tempfile
import unittest
from pathlib import Path

from nimble_reviewer.models import ReviewAgentMetadata, ReviewFinding, ReviewQuotaStatus, ReviewResult, ReviewTokenUsage
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

    def test_probe_quota_status_reads_remaining_percent_and_reset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "quota.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'remaining_percent': 62.5, 'reset_at': '2026-03-22T18:00:00Z'}))\n",
                encoding="utf-8",
            )

            quota_status = _probe_quota_status(("python", str(script)), Path(tmpdir))

        self.assertEqual(
            quota_status,
            ReviewQuotaStatus(remaining_percent=62.5, reset_at="2026-03-22T18:00:00Z"),
        )

    def test_probe_quota_status_clamps_remaining_percent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "quota.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'remaining_percent': 140, 'reset_at': '2026-03-22T18:00:00Z'}))\n",
                encoding="utf-8",
            )

            quota_status = _probe_quota_status(("python", str(script)), Path(tmpdir))

        self.assertEqual(
            quota_status,
            ReviewQuotaStatus(remaining_percent=100.0, reset_at="2026-03-22T18:00:00Z"),
        )

    def test_council_runner_combines_two_reviews_and_separate_synthesis_profile(self):
        from pathlib import Path

        from nimble_reviewer.review_agent import CouncilRunner

        codex_review_runner = _FakeJsonRunner(
            provider="codex",
            model="gpt-5.4",
            reasoning="high",
            review_result=ReviewResult(
                summary="Codex found one issue.",
                overall_risk="medium",
                findings=(ReviewFinding("medium", "app.py", 10, "Null guard", "Need a guard."),),
                token_usage=ReviewTokenUsage(input_tokens=10, cached_input_tokens=2, output_tokens=3),
            ),
        )
        claude_review_runner = _FakeJsonRunner(
            provider="claude",
            model="sonnet",
            reasoning="high",
            review_result=ReviewResult(
                summary="Claude found the same issue.",
                overall_risk="medium",
                findings=(ReviewFinding("medium", "app.py", 11, "Missing guard", "Still needs a guard."),),
                token_usage=ReviewTokenUsage(
                    input_tokens=20,
                    cached_input_tokens=5,
                    output_tokens=6,
                    cache_creation_input_tokens=7,
                    cached_input_included_in_input=False,
                    cache_creation_included_in_input=False,
                ),
            ),
        )
        codex_synth_runner = _FakeJsonRunner(
            provider="codex",
            model="gpt-5.4",
            reasoning="low",
            json_responses=[
                (
                    {
                        "summary": "One shared actionable issue.",
                        "overall_risk": "medium",
                        "reviewer_overview": {
                            "codex": "Codex highlighted the missing null guard.",
                            "claude": "Claude confirmed the missing guard path.",
                        },
                        "findings": [
                            {
                                "severity": "medium",
                                "file": "app.py",
                                "line": 10,
                                "title": "Missing null guard",
                                "body": "Guard the value before indexing.",
                                "suggestion": "Add an early return before indexing.",
                                "sources": ["codex", "claude"],
                                "opinions": [
                                    {"provider": "codex", "verdict": "found", "reason": "Flagged the guard issue directly."},
                                    {"provider": "claude", "verdict": "found", "reason": "Raised the same issue independently."},
                                ],
                            }
                        ],
                    },
                    ReviewTokenUsage(input_tokens=6, cached_input_tokens=1, output_tokens=2),
                ),
            ],
        )

        runner = CouncilRunner(
            codex_runner=codex_review_runner,
            claude_runner=claude_review_runner,
            synthesizer=codex_synth_runner,
        )

        result = runner.review("base prompt", Path("."))

        self.assertEqual(result.summary, "One shared actionable issue.")
        self.assertEqual(result.findings[0].sources, ("codex", "claude"))
        self.assertEqual(len(result.findings[0].opinions), 2)
        self.assertEqual(len(result.participants), 3)
        self.assertEqual(result.participants[0].phases, ("review",))
        self.assertEqual(result.participants[0].overall_risk, "medium")
        self.assertEqual(result.participants[0].summary, "Codex highlighted the missing null guard.")
        self.assertEqual(result.participants[1].metadata.provider, "codex")
        self.assertEqual(result.participants[1].metadata.reasoning_effort, "low")
        self.assertEqual(result.participants[1].phases, ("synthesis",))
        self.assertEqual(result.participants[2].metadata.provider, "claude")
        self.assertEqual(result.participants[2].phases, ("review",))
        self.assertEqual(result.participants[2].overall_risk, "medium")
        self.assertEqual(result.participants[2].summary, "Claude confirmed the missing guard path.")

    def test_council_runner_writes_stage_snapshots(self):
        from pathlib import Path

        from nimble_reviewer.review_agent import CouncilRunner

        trace = _MemoryTrace()
        codex_review_runner = _FakeJsonRunner(
            provider="codex",
            model="gpt-5.4",
            reasoning="high",
            review_result=ReviewResult(summary="Codex", overall_risk="low", findings=()),
        )
        claude_review_runner = _FakeJsonRunner(
            provider="claude",
            model="sonnet",
            reasoning="high",
            review_result=ReviewResult(summary="Claude", overall_risk="low", findings=()),
        )
        synthesizer = _FakeJsonRunner(
            provider="codex",
            model="gpt-5.4",
            reasoning="low",
            json_responses=[({"summary": "Final", "overall_risk": "low", "findings": []}, None)],
        )

        runner = CouncilRunner(
            codex_runner=codex_review_runner,
            claude_runner=claude_review_runner,
            synthesizer=synthesizer,
        )

        runner.review("base prompt", Path("."), trace=trace)

        self.assertIn("codex.review", trace.snapshots)
        self.assertIn("claude.review", trace.snapshots)
        self.assertIn("codex.synthesis", trace.snapshots)

    def test_council_runner_preserves_findings_omitted_by_synthesis(self):
        from pathlib import Path

        from nimble_reviewer.review_agent import CouncilRunner

        codex_review_runner = _FakeJsonRunner(
            provider="codex",
            model="gpt-5.4",
            reasoning="high",
            review_result=ReviewResult(
                summary="Codex found two issues.",
                overall_risk="high",
                findings=(
                    ReviewFinding("high", "app.py", 10, "Null guard", "Need a guard."),
                    ReviewFinding("medium", "db.py", 20, "Retry logic", "Missing retry on timeout."),
                ),
            ),
        )
        claude_review_runner = _FakeJsonRunner(
            provider="claude",
            model="sonnet",
            reasoning="high",
            review_result=ReviewResult(
                summary="Claude found one issue.",
                overall_risk="medium",
                findings=(
                    ReviewFinding("medium", "worker.py", 30, "Cleanup ordering", "Cleanup can run too early."),
                ),
            ),
        )
        codex_synth_runner = _FakeJsonRunner(
            provider="codex",
            model="gpt-5.4",
            reasoning="low",
            json_responses=[
                (
                    {
                        "summary": "Only one issue made it into synthesis.",
                        "overall_risk": "medium",
                        "findings": [
                            {
                                "severity": "high",
                                "file": "app.py",
                                "line": 10,
                                "title": "Null guard",
                                "body": "Need a guard.",
                                "sources": ["codex"],
                            }
                        ],
                    },
                    ReviewTokenUsage(input_tokens=6, cached_input_tokens=1, output_tokens=2),
                ),
            ],
        )

        runner = CouncilRunner(
            codex_runner=codex_review_runner,
            claude_runner=claude_review_runner,
            synthesizer=codex_synth_runner,
        )

        result = runner.review("base prompt", Path("."))

        self.assertEqual(len(result.findings), 3)
        self.assertTrue(any(f.file == "db.py" and f.sources == ("codex",) for f in result.findings))
        self.assertTrue(any(f.file == "worker.py" and f.sources == ("claude",) for f in result.findings))
        self.assertIn("Preserved 2 finding(s) from the base reviews.", result.summary)
        self.assertEqual(result.overall_risk, "high")


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


def _probe_quota_status(command: tuple[str, ...], cwd: Path):
    from nimble_reviewer.review_agent import _probe_quota_status

    return _probe_quota_status("codex", command, cwd, None)


class _MemoryTrace:
    def __init__(self):
        self.entries = []
        self.snapshots = {}

    def write(self, source, event, **payload):
        self.entries.append({"source": source, "event": event, **payload})

    def write_snapshot(self, name, payload):
        self.snapshots[name] = payload


class _FakeJsonRunner:
    def __init__(
        self,
        provider: str,
        model: str,
        reasoning: str,
        review_result: ReviewResult | None = None,
        json_responses: list[tuple[dict, ReviewTokenUsage | None]] | None = None,
    ):
        self.provider_name = provider
        self.agent_metadata = ReviewAgentMetadata(provider=provider, model=model, reasoning_effort=reasoning)
        self._review_result = review_result
        self._json_responses = list(json_responses or [])

    def review(self, prompt, cwd, trace=None):
        if self._review_result is None:
            raise AssertionError("Unexpected review() call")
        return self._review_result

    def run_json(self, prompt, cwd, trace=None):
        if not self._json_responses:
            raise AssertionError("Unexpected run_json() call")
        return self._json_responses.pop(0)


if __name__ == "__main__":
    unittest.main()
