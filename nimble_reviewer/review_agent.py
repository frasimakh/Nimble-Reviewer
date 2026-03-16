from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from nimble_reviewer.models import (
    ReviewAgentMetadata,
    ReviewFinding,
    ReviewResult,
    ReviewTokenUsage,
)
from nimble_reviewer.trace import RunTrace

VALID_SEVERITIES = {"high", "medium", "low"}


class ReviewAgentError(RuntimeError):
    pass


class ReviewAgentRunner(Protocol):
    provider_name: str

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        ...


class CodexRunner:
    provider_name = "codex"

    def __init__(self, command: tuple[str, ...], timeout_sec: int) -> None:
        self.command = command
        self.timeout_sec = timeout_sec
        self.agent_metadata = _extract_agent_metadata(self.provider_name, command)

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        errors: list[str] = []
        for attempt in range(2):
            raw, usage = self._run_once(prompt, cwd, trace)
            try:
                result = _parse_review_result(raw)
                if usage:
                    return ReviewResult(
                        summary=result.summary,
                        overall_risk=result.overall_risk,
                        findings=result.findings,
                        token_usage=usage,
                        agent_metadata=self.agent_metadata,
                    )
                return ReviewResult(
                    summary=result.summary,
                    overall_risk=result.overall_risk,
                    findings=result.findings,
                    token_usage=result.token_usage,
                    agent_metadata=self.agent_metadata,
                )
            except ReviewAgentError as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
        raise ReviewAgentError("; ".join(errors))

    def _run_once(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[str, ReviewTokenUsage | None]:
        if not trace:
            raw = _run_command(self.command, prompt, cwd, self.timeout_sec, self.provider_name)
            return raw, None

        if any(option in self.command for option in ("--json", "--output-last-message", "-o")):
            raise ReviewAgentError(
                "CODEX_CMD must not include --json, --output-last-message, or -o; the service manages those flags internally"
            )

        with tempfile.TemporaryDirectory(prefix="nimble-reviewer-codex-") as tmpdir:
            final_path = Path(tmpdir) / "final-message.txt"
            command = [*self.command, "--json", "--output-last-message", str(final_path)]
            stdout = _run_command(tuple(command), prompt, cwd, self.timeout_sec, self.provider_name)
            usage = _record_codex_events(stdout, trace)
            if final_path.exists():
                return final_path.read_text(encoding="utf-8", errors="replace").strip(), usage
            return _extract_codex_final_message(stdout), usage


class ClaudeRunner:
    provider_name = "claude"

    def __init__(self, command: tuple[str, ...], timeout_sec: int) -> None:
        self.command = command
        self.timeout_sec = timeout_sec
        self.agent_metadata = _extract_agent_metadata(self.provider_name, command)

    def review(self, prompt: str, cwd: Path, trace: RunTrace | None = None) -> ReviewResult:
        errors: list[str] = []
        for attempt in range(2):
            try:
                raw, usage = self._run_once(prompt, cwd, trace)
                result = _parse_review_result(raw)
                return ReviewResult(
                    summary=result.summary,
                    overall_risk=result.overall_risk,
                    findings=result.findings,
                    token_usage=usage or result.token_usage,
                    agent_metadata=self.agent_metadata,
                )
            except ReviewAgentError as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
        raise ReviewAgentError("; ".join(errors))

    def _run_once(
        self,
        prompt: str,
        cwd: Path,
        trace: RunTrace | None = None,
    ) -> tuple[str, ReviewTokenUsage | None]:
        command = list(self.command)
        output_format = _detect_claude_output_format(command)
        if trace and output_format is None:
            command.extend(["--output-format", "stream-json"])
            output_format = "stream-json"
        command = list(_normalize_claude_command(command))

        raw = _run_command(tuple(command), prompt, cwd, self.timeout_sec, self.provider_name)

        usage = _record_claude_events(raw, output_format or "text", trace) if trace else None

        if output_format == "stream-json":
            return _extract_claude_result_from_stream(raw), usage
        return _extract_claude_result(raw), usage


def _run_command(
    command: tuple[str, ...],
    prompt: str,
    cwd: Path,
    timeout_sec: int,
    provider_name: str,
) -> str:
    try:
        completed = subprocess.run(
            list(command),
            input=prompt,
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewAgentError(f"{provider_name} CLI timed out after {timeout_sec}s") from exc

    if completed.returncode != 0:
        raise ReviewAgentError(
            completed.stderr.strip() or completed.stdout.strip() or f"{provider_name} CLI failed"
        )
    return completed.stdout.strip()


def _extract_claude_result(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise ReviewAgentError("Empty Claude CLI output")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate

    if not isinstance(payload, dict):
        return candidate

    if payload.get("type") != "result" or "result" not in payload:
        return candidate

    result_text = str(payload.get("result", "")).strip()
    if payload.get("is_error"):
        raise ReviewAgentError(result_text or "Claude CLI returned an error result")
    if not result_text:
        raise ReviewAgentError("Claude CLI returned an empty result field")
    return result_text


def _extract_claude_result_from_stream(raw: str) -> str:
    final_payload: dict | None = None
    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            final_payload = payload

    if not final_payload:
        raise ReviewAgentError("Claude stream did not include a final result event")

    result_text = str(final_payload.get("result", "")).strip()
    if final_payload.get("is_error"):
        raise ReviewAgentError(result_text or "Claude CLI returned an error result")
    if not result_text:
        raise ReviewAgentError("Claude CLI returned an empty result field")
    return result_text


def _extract_codex_final_message(raw: str) -> str:
    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item") or {}
        if item.get("type") != "agent_message":
            continue
        text = str(item.get("text", "")).strip()
        if text:
            return text
    raise ReviewAgentError("Codex event stream did not include a final agent message")


def _record_codex_events(raw: str, trace: RunTrace) -> ReviewTokenUsage | None:
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    saw_usage = False

    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            trace.write("provider", "codex.event.invalid_json", raw=event)
            continue

        trace.write("provider", "codex.event", payload=payload)
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input_tokens", 0) or 0)
        cached_input_tokens += int(usage.get("cached_input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)
        saw_usage = True

    if not saw_usage:
        return None

    token_usage = ReviewTokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    trace.write(
        "provider",
        "codex.usage",
        input_tokens=token_usage.input_tokens,
        cached_input_tokens=token_usage.cached_input_tokens,
        output_tokens=token_usage.output_tokens,
        total_tokens=token_usage.total_tokens,
    )
    return token_usage


def _record_claude_events(raw: str, output_format: str, trace: RunTrace) -> ReviewTokenUsage | None:
    if output_format == "stream-json":
        usage = _record_claude_stream_events(raw, trace)
        if usage:
            trace.write(
                "provider",
                "claude.usage",
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=usage.cost_usd,
            )
        return usage

    if output_format == "json":
        usage = _record_claude_json_event(raw, trace)
        if usage:
            trace.write(
                "provider",
                "claude.usage",
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=usage.cost_usd,
            )
        return usage

    trace.write("provider", "claude.output.text", output=raw)
    return None


def _record_claude_stream_events(raw: str, trace: RunTrace) -> ReviewTokenUsage | None:
    message_usage_by_id: dict[str, dict[str, int]] = {}
    anonymous_usage: list[dict[str, int]] = []
    final_payload: dict | None = None

    for line in raw.splitlines():
        event = line.strip()
        if not event:
            continue
        try:
            payload = json.loads(event)
        except json.JSONDecodeError:
            trace.write("provider", "claude.event.invalid_json", raw=event)
            continue

        trace.write("provider", "claude.event", payload=payload)
        if not isinstance(payload, dict):
            continue

        usage = _extract_claude_usage_dict(payload)
        if usage:
            message_id = str(payload.get("message_id") or payload.get("id") or "").strip()
            if message_id:
                previous = message_usage_by_id.get(message_id, {})
                message_usage_by_id[message_id] = _merge_usage_counts(previous, usage)
            else:
                anonymous_usage.append(usage)

        if payload.get("type") == "result":
            final_payload = payload

    if final_payload:
        final_usage = _extract_claude_usage_dict(final_payload)
        if final_usage:
            return _build_claude_token_usage(final_usage, final_payload)

    if not message_usage_by_id and not anonymous_usage:
        return None

    aggregate = {"input_tokens": 0, "cached_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
    for usage in message_usage_by_id.values():
        aggregate = _merge_usage_counts(aggregate, usage, additive=True)
    for usage in anonymous_usage:
        aggregate = _merge_usage_counts(aggregate, usage, additive=True)
    return _build_claude_token_usage(aggregate, final_payload)


def _record_claude_json_event(raw: str, trace: RunTrace) -> ReviewTokenUsage | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        trace.write("provider", "claude.event.invalid_json", raw=raw)
        return None
    trace.write("provider", "claude.event", payload=payload)
    if not isinstance(payload, dict):
        return None
    usage = _extract_claude_usage_dict(payload)
    if not usage:
        return None
    return _build_claude_token_usage(usage, payload)


def _extract_claude_usage_dict(payload: dict) -> dict[str, int] | None:
    candidates: list[dict] = []
    usage = payload.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)
    message = payload.get("message")
    if isinstance(message, dict):
        message_usage = message.get("usage")
        if isinstance(message_usage, dict):
            candidates.append(message_usage)

    for candidate in candidates:
        values = {
            "input_tokens": int(candidate.get("input_tokens", 0) or 0),
            "cached_input_tokens": int(candidate.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(candidate.get("cache_creation_input_tokens", 0) or 0),
            "output_tokens": int(candidate.get("output_tokens", 0) or 0),
        }
        if any(values.values()):
            return values
    return None


def _merge_usage_counts(
    base: dict[str, int],
    update: dict[str, int],
    additive: bool = False,
) -> dict[str, int]:
    merged = dict(base)
    for key in ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"):
        if additive:
            merged[key] = int(merged.get(key, 0) or 0) + int(update.get(key, 0) or 0)
        else:
            merged[key] = max(int(merged.get(key, 0) or 0), int(update.get(key, 0) or 0))
    return merged


def _build_claude_token_usage(
    usage: dict[str, int],
    payload: dict | None,
) -> ReviewTokenUsage:
    cost_usd: float | None = None
    if isinstance(payload, dict) and payload.get("total_cost_usd") is not None:
        try:
            cost_usd = float(payload["total_cost_usd"])
        except (TypeError, ValueError):
            cost_usd = None
    return ReviewTokenUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        cost_usd=cost_usd,
        cached_input_included_in_input=False,
        cache_creation_included_in_input=False,
    )


def _parse_review_result(raw: str) -> ReviewResult:
    payload = _load_json_object(raw)
    summary = str(payload.get("summary", "")).strip()
    overall_risk = str(payload.get("overall_risk", "")).strip().lower()
    if not summary:
        raise ReviewAgentError("Missing summary field")
    if overall_risk not in VALID_SEVERITIES:
        raise ReviewAgentError(f"Invalid overall_risk value: {overall_risk!r}")

    findings_payload = payload.get("findings", [])
    if not isinstance(findings_payload, list):
        raise ReviewAgentError("findings must be an array")

    findings: list[ReviewFinding] = []
    for item in findings_payload:
        severity = str(item.get("severity", "")).strip().lower()
        file_path = str(item.get("file", "")).strip()
        title = str(item.get("title", "")).strip()
        body = str(item.get("body", "")).strip()
        suggestion = str(item.get("suggestion", "")).strip()
        line = item.get("line")
        if severity not in VALID_SEVERITIES:
            raise ReviewAgentError(f"Invalid finding severity: {severity!r}")
        if not file_path or not title or not body:
            raise ReviewAgentError("Each finding requires file, title, and body")
        if not isinstance(line, int) or line < 1:
            raise ReviewAgentError("Each finding line must be a positive integer")
        findings.append(
            ReviewFinding(
                severity=severity,
                file=file_path,
                line=line,
                title=title,
                body=body,
                suggestion=suggestion or None,
            )
        )

    return ReviewResult(
        summary=summary,
        overall_risk=overall_risk,
        findings=tuple(findings),
    )


def _load_json_object(raw: str) -> dict:
    candidate = raw.strip()
    if not candidate:
        raise ReviewAgentError("Empty review agent output")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ReviewAgentError("Output was not valid JSON")
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ReviewAgentError("Output was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise ReviewAgentError("Top-level JSON must be an object")
    return payload


def _extract_agent_metadata(provider_name: str, command: tuple[str, ...]) -> ReviewAgentMetadata:
    model: str | None = None
    reasoning_effort: str | None = None

    index = 0
    while index < len(command):
        part = command[index]
        if part in {"-m", "--model"} and index + 1 < len(command):
            model = command[index + 1]
            index += 2
            continue
        if part.startswith("--model="):
            model = part.split("=", 1)[1]
            index += 1
            continue
        if part in {"--effort"} and index + 1 < len(command):
            reasoning_effort = command[index + 1]
            index += 2
            continue
        if part.startswith("--effort="):
            reasoning_effort = part.split("=", 1)[1]
            index += 1
            continue
        if part in {"-c", "--config"} and index + 1 < len(command):
            reasoning_effort = _extract_reasoning_effort(command[index + 1]) or reasoning_effort
            index += 2
            continue
        if part.startswith("--config="):
            reasoning_effort = _extract_reasoning_effort(part.split("=", 1)[1]) or reasoning_effort
            index += 1
            continue
        index += 1

    return ReviewAgentMetadata(
        provider=provider_name,
        model=_clean_value(model),
        reasoning_effort=_clean_value(reasoning_effort),
    )


def _detect_claude_output_format(command: list[str]) -> str | None:
    index = 0
    while index < len(command):
        part = command[index]
        if part == "--output-format" and index + 1 < len(command):
            return command[index + 1].strip()
        if part.startswith("--output-format="):
            return part.split("=", 1)[1].strip()
        index += 1
    return None


def _normalize_claude_command(command: list[str]) -> tuple[str, ...]:
    output_format = _detect_claude_output_format(command)
    normalized = list(command)
    if output_format == "stream-json" and "--verbose" not in normalized:
        normalized.append("--verbose")
    return tuple(normalized)


def _extract_reasoning_effort(value: str) -> str | None:
    if "=" not in value:
        return None
    key, raw_value = value.split("=", 1)
    if key.strip() not in {"model_reasoning_effort", "reasoning_effort"}:
        return None
    return raw_value


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\"'")
    return cleaned or None
