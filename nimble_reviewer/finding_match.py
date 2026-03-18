from __future__ import annotations

import re

from nimble_reviewer.models import ReviewFinding


def findings_match(left: ReviewFinding, right: ReviewFinding) -> bool:
    if left.file != right.file:
        return False

    if left.line == right.line:
        return True

    left_title = _normalize_text(left.title)
    right_title = _normalize_text(right.title)
    if left_title and left_title == right_title:
        return True

    title_overlap = _token_overlap(left.title, right.title)
    combined_overlap = _token_overlap(f"{left.title} {left.body}", f"{right.title} {right.body}")
    close_lines = abs(left.line - right.line) <= 3

    if title_overlap >= 0.6:
        return True
    if close_lines and combined_overlap >= 0.28:
        return True
    return False


def _token_overlap(left: str, right: str) -> float:
    left_tokens = _text_tokens(left)
    right_tokens = _text_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def _text_tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", value.lower()) if len(token) > 2}


def _normalize_text(value: str) -> str:
    return " ".join(sorted(_text_tokens(value)))
