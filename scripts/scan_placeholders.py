"""Scan source/target rows for placeholder and control-token mismatches."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from route_translation_task import is_under, project_root, resolve_project_path
from validate_translation import PLACEHOLDER_PATTERNS, read_lines


SUPPORTED_EXTENSIONS = {".jsonl", ".json", ".xml"}


def placeholder_tokens(text: Any) -> list[str]:
    if text is None:
        return []
    value = str(text)
    tokens: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        tokens.extend(match.group(0) for match in re.finditer(pattern, value))
    return tokens


def test_placeholder_pair(source: Any, target: Any, label: str, issues: list[str]) -> None:
    source_counts = Counter(placeholder_tokens(source))
    target_counts = Counter(placeholder_tokens(target))
    for token in sorted(set(source_counts) | set(target_counts)):
        if source_counts[token] != target_counts[token]:
            issues.append(f"{label} placeholder mismatch '{token}' source={source_counts[token]} target={target_counts[token]}")


def read_text_auto(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp936"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def scan_xml(path: Path, issues: list[str]) -> int:
    try:
        root = ET.fromstring(read_text_auto(path))
    except Exception as exc:
        issues.append(f"{path} invalid XML: {exc}")
        return 0

    checked = 0
    for index, node in enumerate(root.findall(".//String"), start=1):
        source_node = node.find("Source")
        target_node = node.find("Dest")
        if source_node is None or target_node is None:
            issues.append(f"{path}:String[{index}] missing Source or Dest node")
            continue
        checked += 1
        source_text = "" if source_node.text is None else source_node.text
        target_text = "" if target_node.text is None else target_node.text
        test_placeholder_pair(source_text, target_text, f"{path}:String[{index}]", issues)
    return checked


def parse_json_records(path: Path, issues: list[str]) -> list[tuple[str, dict[str, Any]]]:
    text = read_text_auto(path)
    records: list[tuple[str, dict[str, Any]]] = []
    non_empty_lines = [line for line in text.splitlines() if line.strip()]

    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                records.append((str(path), payload))
                return records
            if isinstance(payload, list):
                for index, item in enumerate(payload, start=1):
                    if isinstance(item, dict):
                        records.append((f"{path}:{index}", item))
                    else:
                        issues.append(f"{path}:{index} JSON array item is not an object")
                return records
            issues.append(f"{path} JSON root is not an object or array")
            return records
        except Exception:
            pass

    for index, line in enumerate(non_empty_lines, start=1):
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                issues.append(f"{path}:{index} JSON record is not an object")
                continue
            records.append((f"{path}:{index}", payload))
        except Exception as exc:
            issues.append(f"{path}:{index} invalid JSON: {exc}")
    return records


def scan_json_like(path: Path, issues: list[str]) -> int:
    checked = 0
    for label, payload in parse_json_records(path, issues):
        checked += 1
        test_placeholder_pair(payload.get("source"), payload.get("target"), label, issues)
    return checked


def collect_input_files(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        return sorted(
            (item for item in input_path.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda item: str(item).lower(),
        )
    return [input_path]


def scan_files(input_files: list[Path]) -> tuple[int, list[str]]:
    issues: list[str] = []
    checked_rows = 0
    for file_path in input_files:
        extension = file_path.suffix.lower()
        if extension == ".xml":
            checked_rows += scan_xml(file_path, issues)
        elif extension in {".jsonl", ".json"}:
            checked_rows += scan_json_like(file_path, issues)
        else:
            issues.append(f"{file_path} unsupported file extension for placeholder scan: {extension}")
    return checked_rows, issues


def write_report(report_path: Path, input_path: Path, input_files: list[Path], checked_rows: int, issues: list[str]) -> None:
    lines = [
        "# Placeholder Report",
        "",
        f"- Input: {input_path}",
        f"- Files checked: {len(input_files)}",
        f"- Rows checked: {checked_rows}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("No placeholder differences.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan project-local JSONL/JSON/XML files for placeholder count mismatches.")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--report-output-path", default="qa/placeholder_report.md")
    args = parser.parse_args()

    root = project_root()
    input_path = resolve_project_path(root, args.input_path, must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    input_files = collect_input_files(input_path)
    checked_rows, issues = scan_files(input_files)
    write_report(report_path, input_path, input_files, checked_rows, issues)

    if issues:
        for issue in issues:
            print(f"- {issue}")
        print(f"Placeholder report written to: {report_path}")
        return 1
    print("Placeholder scan passed: no issues.")
    print(f"Placeholder report written to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
