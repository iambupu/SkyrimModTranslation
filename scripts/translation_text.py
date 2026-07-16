"""Shared text-shape checks used by translation review scripts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def cjk_present(text: str) -> bool:
    return re.search(r"[\u3400-\u9fff]", text) is not None


def english_present(text: str) -> bool:
    return re.search(r"[A-Za-z]{2,}", text) is not None


def quality_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if not re.fullmatch(r"%\s+[A-Za-z]", token)]


def regex_tokens(value: Any, patterns: Iterable[str | re.Pattern[str]]) -> list[str]:
    if value is None:
        return []
    text = str(value)
    tokens: list[str] = []
    for pattern in patterns:
        tokens.extend(match.group(0) for match in re.finditer(pattern, text))
    return tokens


def row_value(row: dict[str, object], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None:
            return str(value)
    return ""
