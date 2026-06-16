import argparse
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from route_translation_task import is_under, project_root, resolve_project_path
from validate_translation import PLACEHOLDER_PATTERNS


LONG_ENGLISH_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-]+){4,}")


@dataclass
class SplitLine:
    has_tab: bool
    key: str
    text: str


def read_lines_auto(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "utf-16", "cp936"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def placeholder_tokens(text: str | None) -> list[str]:
    if text is None:
        return []
    tokens: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        tokens.extend(match.group(0) for match in re.finditer(pattern, text))
    return tokens


def split_translation_line(line: str) -> SplitLine:
    index = line.find("\t")
    if index < 0:
        return SplitLine(False, line, "")
    return SplitLine(True, line[:index], line[index + 1 :])


def validate_interface(source_path: Path, translated_path: Path) -> tuple[list[str], list[str], int, int]:
    source_lines = read_lines_auto(source_path)
    translated_lines = read_lines_auto(translated_path)
    errors: list[str] = []
    warnings: list[str] = []

    if len(source_lines) != len(translated_lines):
        errors.append(f"Line count mismatch: source={len(source_lines)}, translated={len(translated_lines)}")

    max_lines = max(len(source_lines), len(translated_lines))
    for index in range(max_lines):
        line_number = index + 1
        if index >= len(source_lines):
            errors.append(f"Line {line_number}: extra translated line")
            continue
        if index >= len(translated_lines):
            errors.append(f"Line {line_number}: missing translated line")
            continue

        source_line = split_translation_line(source_lines[index])
        translated_line = split_translation_line(translated_lines[index])
        if not source_line.has_tab:
            errors.append(f"Line {line_number}: source line has no tab separator")
            continue
        if not translated_line.has_tab:
            errors.append(f"Line {line_number}: translated line has no tab separator")
            continue
        if source_line.key != translated_line.key:
            errors.append(f"Line {line_number}: key changed. source='{source_line.key}' translated='{translated_line.key}'")
        if source_line.text.strip() and not translated_line.text.strip():
            errors.append(f"Line {line_number}: translated text is empty")

        source_counts = Counter(placeholder_tokens(source_line.text))
        target_counts = Counter(placeholder_tokens(translated_line.text))
        for token, source_count in source_counts.items():
            if target_counts[token] < source_count:
                errors.append(f"Line {line_number}: placeholder or tag missing from translated text: {token}")

        if LONG_ENGLISH_RE.search(translated_line.text):
            warnings.append(f"Line {line_number}: translated text may still contain a long English sentence")

    return errors, warnings, len(source_lines), len(translated_lines)


def write_report(
    report_path: Path,
    source_path: Path,
    translated_path: Path,
    source_line_count: int,
    translated_line_count: int,
    errors: list[str],
    warnings: list[str],
) -> None:
    lines = [
        "# Interface Translation Validation",
        "",
        f"- Source: {source_path}",
        f"- Translated: {translated_path}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Source lines: {source_line_count}",
        f"- Translated lines: {translated_line_count}",
        f"- Blocking errors: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        "",
        "## Errors",
        "",
    ]
    if errors:
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.append("No blocking errors.")
    lines.extend(["", "## Warnings", ""])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("No warnings.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Source and translated files were read inside the current project.",
            "- This validation did not modify mod/ or final_mod.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate project-local Skyrim Interface/translations text files.")
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--translated-path", required=True)
    parser.add_argument("--report-output-path", default="qa/interface_translation_validation.md")
    args = parser.parse_args()

    root = project_root()
    source_path = resolve_project_path(root, args.source_path, must_exist=True)
    translated_path = resolve_project_path(root, args.translated_path, must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    errors, warnings, source_line_count, translated_line_count = validate_interface(source_path, translated_path)
    write_report(report_path, source_path, translated_path, source_line_count, translated_line_count, errors, warnings)
    print(f"Interface translation validation written to: {report_path}")
    if errors:
        print(f"Validation failed with {len(errors)} error(s).")
        return 1
    print("Interface translation validation completed with no blocking errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
