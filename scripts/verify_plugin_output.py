"""Verify project-local plugin output against translation rows.

Byte probes are a fast sanity check, while optional exported JSONL proves the
adapter can still re-read translated records. Neither replaces model review or
real game testing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


@dataclass
class ProbeRow:
    Source: str
    Dest: str
    SourcePresentInOutput: bool
    DestPresentInOutput: bool
    SourcePresentInExport: bool | None = None
    DestPresentInExport: bool | None = None


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True))).replace("/", "\\")
    except ValueError:
        return str(value).replace("/", "\\")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def has_byte_pattern(data: bytes, pattern: bytes) -> bool:
    return bool(pattern) and data.find(pattern) >= 0


def text_in_bytes(data: bytes, text: str) -> bool:
    if not text:
        return False
    return has_byte_pattern(data, text.encode("utf-8")) or has_byte_pattern(data, text.encode("utf-16-le"))


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def xml_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext())


def parse_translation_xml(root: Path, value: str, output_bytes: bytes, issues: list[str]) -> list[ProbeRow]:
    path = resolve_project_path(root, value, must_exist=True)
    rows: list[ProbeRow] = []
    try:
        tree = ET.parse(path)
    except Exception as exc:
        issues.append(f"Failed to parse TranslationXmlPath: {exc}")
        return rows
    for string_node in tree.findall(".//String"):
        source = xml_text(string_node.find("Source"))
        dest = xml_text(string_node.find("Dest"))
        if not source and not dest:
            continue
        rows.append(ProbeRow(source, dest, text_in_bytes(output_bytes, source), text_in_bytes(output_bytes, dest)))
    return rows


def jsonl_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("record_type", "")),
        str(row.get("form_id", "")),
        str(row.get("editor_id", "")),
        str(row.get("subrecord_type", "")),
        str(row.get("subrecord_index", "")),
    )


def parse_output_export_jsonl(root: Path, value: str, issues: list[str]) -> dict[tuple[str, str, str, str, str], str]:
    # Output export rows are keyed by record identity so duplicate source text in
    # different records does not collapse into a false pass/fail result.
    if not value.strip():
        return {}
    path = resolve_project_path(root, value, must_exist=True)
    rows: dict[tuple[str, str, str, str, str], str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception as exc:
        issues.append(f"Failed to read OutputExportJsonlPath: {exc}")
        return rows
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"Invalid output export JSONL at line {line_number}: {exc}")
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("risk", "")) != "candidate":
            continue
        rows[jsonl_identity(row)] = "" if row.get("source") is None else str(row.get("source"))
    return rows


def parse_translation_jsonl(
    root: Path,
    value: str,
    output_bytes: bytes,
    output_export_rows: dict[tuple[str, str, str, str, str], str],
    issues: list[str],
) -> list[ProbeRow]:
    # Prefer identity-based re-read evidence when present; fall back to byte
    # probes so the report still catches obviously missing target strings.
    path = resolve_project_path(root, value, must_exist=True)
    rows: list[ProbeRow] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception as exc:
        issues.append(f"Failed to read TranslationJsonlPath: {exc}")
        return rows
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"Invalid JSONL at line {line_number}: {exc}")
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("risk", "")) != "candidate":
            continue
        target = "" if row.get("target") is None else str(row.get("target"))
        if not target.strip():
            continue
        source = "" if row.get("source") is None else str(row.get("source"))
        exported_source = output_export_rows.get(jsonl_identity(row))
        rows.append(
            ProbeRow(
                source,
                target,
                text_in_bytes(output_bytes, source),
                text_in_bytes(output_bytes, target),
                None if exported_source is None else exported_source == source,
                None if exported_source is None else exported_source == target,
            )
        )
    return rows


def write_report(
    root: Path,
    original: Path,
    output: Path,
    report: Path,
    original_hash: str,
    output_hash: str,
    hash_changed: bool,
    probe_rows: list[ProbeRow],
    issues: list[str],
    warnings: list[str],
) -> None:
    original_item = original.stat()
    output_item = output.stat()
    lines: list[str] = [
        "# Plugin Output Verification",
        "",
        f"- Original: {relative_path(root, original)}",
        f"- Output: {relative_path(root, output)}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Original SHA256: {original_hash}",
        f"- Output SHA256: {output_hash}",
        f"- Hash changed: {hash_changed}",
        f"- Original size: {original_item.st_size}",
        f"- Output size: {output_item.st_size}",
        f"- Output last write: {datetime.fromtimestamp(output_item.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Translation String Probe",
        "",
    ]
    if not probe_rows:
        lines.append("No TranslationXmlPath or TranslationJsonlPath was supplied, or no rows were parsed.")
    else:
        has_export_probe = any(row.SourcePresentInExport is not None or row.DestPresentInExport is not None for row in probe_rows)
        if has_export_probe:
            lines.extend(
                [
                    "| Source | Dest | Source bytes | Dest bytes | Source export | Dest export |",
                    "|---|---|---:|---:|---:|---:|",
                ]
            )
        else:
            lines.extend(["| Source | Dest | Source bytes | Dest bytes |", "|---|---|---:|---:|"])
        for row in probe_rows:
            if has_export_probe:
                lines.append(
                    f"| {markdown_cell(row.Source)} | {markdown_cell(row.Dest)} | "
                    f"{row.SourcePresentInOutput} | {row.DestPresentInOutput} | "
                    f"{row.SourcePresentInExport} | {row.DestPresentInExport} |"
                )
            else:
                lines.append(
                    f"| {markdown_cell(row.Source)} | {markdown_cell(row.Dest)} | "
                    f"{row.SourcePresentInOutput} | {row.DestPresentInOutput} |"
                )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No blocking issues.")
    else:
        lines.extend(f"- {issue}" for issue in issues)

    lines.extend(["", "## Warnings", ""])
    if not warnings:
        lines.append("No warnings.")
    else:
        lines.extend(f"- {warning}" for warning in warnings)

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script only read project-local plugin files.",
            "- This script did not modify plugin binaries.",
            "- This script did not access real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
        ]
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a project-local ESP/ESM/ESL output contains expected translated strings.")
    parser.add_argument("--original-plugin-path", required=True)
    parser.add_argument("--output-plugin-path", required=True)
    parser.add_argument("--translation-xml-path", default="")
    parser.add_argument("--translation-jsonl-path", default="")
    parser.add_argument("--output-export-jsonl-path", default="")
    parser.add_argument("--report-output-path", default="qa/plugin_output_verification.md")
    parser.add_argument("--allow-unchanged", action="store_true")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()

    root = project_root()
    original = resolve_project_path(root, args.original_plugin_path, must_exist=True)
    output = resolve_project_path(root, args.output_plugin_path, must_exist=True)
    report = resolve_project_path(root, args.report_output_path, must_exist=False)
    if not (is_under(report, root / "qa") or is_under(report, root / "out")):
        raise ValueError(f"ReportOutputPath must be under qa/ or out/: {args.report_output_path}")
    if original.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError(f"OriginalPluginPath must be .esp, .esm, or .esl: {args.original_plugin_path}")
    if output.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError(f"OutputPluginPath must be .esp, .esm, or .esl: {args.output_plugin_path}")

    issues: list[str] = []
    warnings: list[str] = []
    mod_root = resolve_project_path(root, "mod", must_exist=False)
    out_root = resolve_project_path(root, "out", must_exist=False)
    translated_tool_root = resolve_project_path(root, "translated/tool_outputs", must_exist=False)
    if is_under(output, mod_root):
        issues.append(f"OutputPluginPath points under mod/ and must not be modified: {relative_path(root, output)}")
    relative_out = relative_path(out_root, output) if is_under(output, out_root) else ""
    is_known_out_root = re.match(
        r"^[^\\]+\\(tool_outputs|final_mod|汉化产出\\final_mod)(\\|$)",
        relative_out,
        re.IGNORECASE,
    ) is not None
    if not (is_known_out_root or is_under(output, translated_tool_root)):
        warnings.append(f"OutputPluginPath is project-local but outside the usual tool/final output roots: {relative_path(root, output)}")

    original_hash = sha256_file(original)
    output_hash = sha256_file(output)
    hash_changed = original_hash != output_hash
    if not hash_changed and not args.allow_unchanged:
        issues.append("Output plugin hash is unchanged from original.")

    output_bytes = output.read_bytes()
    probe_rows: list[ProbeRow] = []
    if args.translation_xml_path.strip():
        probe_rows.extend(parse_translation_xml(root, args.translation_xml_path, output_bytes, issues))
    output_export_rows = parse_output_export_jsonl(root, args.output_export_jsonl_path, issues)
    if args.translation_jsonl_path.strip():
        probe_rows.extend(parse_translation_jsonl(root, args.translation_jsonl_path, output_bytes, output_export_rows, issues))

    if probe_rows:
        source_still_present = sum(
            1
            for row in probe_rows
            if row.SourcePresentInExport is True or (row.SourcePresentInExport is None and row.SourcePresentInOutput)
        )
        dest_present = sum(
            1
            for row in probe_rows
            if row.DestPresentInExport is True or (row.DestPresentInExport is None and row.DestPresentInOutput)
        )
        dest_missing_after_source_gone = sum(
            1
            for row in probe_rows
            if not (row.SourcePresentInExport is True or (row.SourcePresentInExport is None and row.SourcePresentInOutput))
            and not (row.DestPresentInExport is True or (row.DestPresentInExport is None and row.DestPresentInOutput))
        )
        if dest_present == 0 and source_still_present > 0:
            issues.append("No translated destination strings were found in the output plugin, while source strings remain.")
        elif source_still_present > 0:
            warnings.append(f"Some source strings are still present in the output plugin: {source_still_present}")
        if dest_missing_after_source_gone > 0:
            warnings.append(f"Some source strings are gone but the expected destination string was not directly found: {dest_missing_after_source_gone}")

    write_report(root, original, output, report, original_hash, output_hash, hash_changed, probe_rows, issues, warnings)
    print(f"Plugin verification written to: {report}")
    if issues:
        print(f"Plugin verification found {len(issues)} issue(s).")
        return 0 if args.warn_only else 1
    print("Plugin verification passed with no blocking issues.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
