"""Token estimation utilities."""

from __future__ import annotations

import re


def estimate_tokens(text: str) -> int:
    """Estimate mixed Chinese/English token count without external tokenizer."""

    ascii_tokens = re.findall(r"[A-Za-z0-9_]+", text)
    han_chars = re.findall(r"[\u4e00-\u9fff]", text)
    punctuation = re.findall(r"[^\w\s\u4e00-\u9fff]", text)
    return max(1, len(ascii_tokens) + len(han_chars) + len(punctuation) // 2)


def estimate_messages_tokens(parts: list[str]) -> int:
    return sum(estimate_tokens(part) for part in parts)
