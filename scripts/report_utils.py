"""Shared formatting and report-output helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _escape_markdown_cell(text: str, *, escape_backslash: bool) -> str:
    if escape_backslash:
        text = text.replace("\\", "\\\\")
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def markdown_cell(value: object) -> str:
    return _escape_markdown_cell("" if value is None else str(value), escape_backslash=True)


def markdown_cell_plain(value: object) -> str:
    return _escape_markdown_cell("" if value is None else str(value), escape_backslash=False)


def markdown_text_cell(value: str) -> str:
    return _escape_markdown_cell(value, escape_backslash=False)


def markdown_object_cell(value: object) -> str:
    return _escape_markdown_cell(str(value), escape_backslash=False)


def markdown_text_cell_backslash(value: str) -> str:
    return _escape_markdown_cell(value, escape_backslash=True)


def append_scoped_issue(
    issues: list[Any],
    *,
    issue_type: Callable[..., Any],
    mod_name: str,
    area: str,
    message: str,
    evidence: str,
    severity: str = "error",
) -> None:
    issues.append(issue_type(severity, mod_name, area, message, evidence))


def write_text_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def subprocess_output_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_zero_metric(value: object) -> bool:
    return str(value).strip() in {"0", "0.0"}


def to_int(value: str | None, default: int = -1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
