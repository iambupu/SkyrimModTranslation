import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from route_translation_task import is_under, project_root, resolve_project_path


IDENTITY_FIELDS = ("id", "plugin", "type", "field", "source")
PLACEHOLDER_PATTERNS = (
    r"%[sdf]",
    r"\{(?:0|1|name)\}",
    r"<[^>\r\n]+>",
    r"\$[\w_][\w\d_]*",
    r"\\r\\n",
    r"\\n",
)
LONG_ENGLISH_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){4,}")


def read_lines(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-16", "cp936"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def json_value(obj: dict[str, Any], field: str) -> Any:
    return obj.get(field)


def placeholder_tokens(text: Any) -> list[str]:
    if text is None:
        return []
    value = str(text)
    tokens: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        tokens.extend(match.group(0) for match in re.finditer(pattern, value))
    return tokens


def validate_pair(source_path: Path, translated_path: Path) -> list[str]:
    errors: list[str] = []
    source_lines = read_lines(source_path)
    translated_lines = read_lines(translated_path)

    if len(source_lines) != len(translated_lines):
        errors.append(f"Line count mismatch: source={len(source_lines)}, translated={len(translated_lines)}")

    max_lines = max(len(source_lines), len(translated_lines))
    for index in range(max_lines):
        line_number = index + 1
        if index >= len(source_lines):
            errors.append(f"Line {line_number}: missing source line")
            continue
        if index >= len(translated_lines):
            errors.append(f"Line {line_number}: missing translated line")
            continue

        source_object: dict[str, Any] | None = None
        translated_object: dict[str, Any] | None = None
        try:
            source_object = json.loads(source_lines[index])
            if not isinstance(source_object, dict):
                errors.append(f"Line {line_number}: source JSON is not an object")
                source_object = None
        except Exception as exc:
            errors.append(f"Line {line_number}: source is not valid JSON: {exc}")

        try:
            translated_object = json.loads(translated_lines[index])
            if not isinstance(translated_object, dict):
                errors.append(f"Line {line_number}: translated JSON is not an object")
                translated_object = None
        except Exception as exc:
            errors.append(f"Line {line_number}: translated is not valid JSON: {exc}")

        if source_object is None or translated_object is None:
            continue

        for field in IDENTITY_FIELDS:
            source_value = "" if json_value(source_object, field) is None else str(json_value(source_object, field))
            translated_value = "" if json_value(translated_object, field) is None else str(json_value(translated_object, field))
            if source_value != translated_value:
                errors.append(
                    f"Line {line_number}: field '{field}' was modified. source='{source_value}' translated='{translated_value}'"
                )

        target = "" if json_value(translated_object, "target") is None else str(json_value(translated_object, "target"))
        if not target.strip():
            errors.append(f"Line {line_number}: target is empty")

        source_counts = Counter(placeholder_tokens(json_value(source_object, "source")))
        target_counts = Counter(placeholder_tokens(target))
        for token, source_count in source_counts.items():
            if target_counts[token] < source_count:
                errors.append(f"Line {line_number}: placeholder missing from target: {token}")

        if LONG_ENGLISH_RE.search(target):
            errors.append(f"Line {line_number}: target appears to contain an untranslated English long sentence")

    return errors


def write_report(report_path: Path, source_path: Path, translated_path: Path, errors: list[str]) -> None:
    lines = [
        "# Validation Errors",
        "",
        f"- Source: {source_path}",
        f"- Translated: {translated_path}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    if errors:
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.append("No validation errors.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate paired project-local translation JSONL files.")
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--translated-path", required=True)
    parser.add_argument("--error-output-path", default="qa/validation_errors.md")
    args = parser.parse_args()

    root = project_root()
    source_path = resolve_project_path(root, args.source_path, must_exist=True)
    translated_path = resolve_project_path(root, args.translated_path, must_exist=True)
    report_path = resolve_project_path(root, args.error_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ErrorOutputPath must be under qa/: {args.error_output_path}")

    errors = validate_pair(source_path, translated_path)
    write_report(report_path, source_path, translated_path, errors)
    if errors:
        for error in errors:
            print(f"- {error}")
        print(f"Validation report written to: {report_path}")
        return 1
    print("Validation passed: no errors.")
    print(f"Validation report written to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
