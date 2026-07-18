"""Verify project-local plugin output against translation rows.

Byte probes are a fast sanity check, while optional exported JSONL proves the
adapter can still re-read translated records. Neither replaces model review or
real game testing.
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from capability_resolver import resolve_capability, resolve_resource_capability
from game_context import GameContext, resolve_workspace_game_context, supported_game_ids
from model_review_contract import read_report_metric as report_metric
from project_paths import is_under, project_root, resolve_project_path
from project_paths import relative_windows_path as relative_path
from file_utils import sha256_file_upper as sha256_file
from report_utils import markdown_text_cell as markdown_cell
from file_utils import encoded_text_present as text_in_bytes
from plugin_resource_evidence import (
    plugin_resource_descriptor,
    read_plugin_report_traits,
    validate_plugin_master_style_context,
)


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


@dataclass
class ProbeRow:
    Source: str
    Dest: str
    SourcePresentInOutput: bool
    DestPresentInOutput: bool
    SourcePresentInExport: bool | None = None
    DestPresentInExport: bool | None = None






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


def jsonl_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("game_id", "")),
        str(row.get("plugin", "")),
        str(row.get("record_type", "")),
        str(row.get("form_id", "")),
        str(row.get("editor_id", "")),
        str(row.get("field_path", "")),
        str(row.get("subrecord_type", "")),
        str(row.get("subrecord_index", "")),
        str(row.get("occurrence_index", "")),
    )


def parse_output_export_jsonl(
    root: Path,
    value: str,
    issues: list[str],
    context: GameContext,
    plugin_name: str,
) -> dict[tuple[str, ...], str]:
    # Output export rows are keyed by record identity so duplicate source text in
    # different records does not collapse into a false pass/fail result.
    if not value.strip():
        return {}
    path = resolve_project_path(root, value, must_exist=True)
    rows: dict[tuple[str, ...], str] = {}
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
            issues.append(f"Output export JSONL row {line_number} is not an object")
            continue
        if row.get("game_id") != context.game_id:
            issues.append(f"Output export JSONL game_id mismatch at line {line_number}")
        if str(row.get("plugin", "")).lower() != plugin_name.lower():
            issues.append(f"Output export JSONL plugin mismatch at line {line_number}")
        if str(row.get("risk", "")) != "candidate":
            continue
        identity = jsonl_identity(row)
        if identity in rows:
            issues.append(f"Duplicate output export identity at line {line_number}: {identity}")
            continue
        rows[identity] = "" if row.get("source") is None else str(row.get("source"))
    return rows


def parse_translation_jsonl(
    root: Path,
    value: str,
    output_bytes: bytes,
    output_export_rows: dict[tuple[str, ...], str],
    issues: list[str],
    context: GameContext,
    plugin_name: str,
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
            issues.append(f"Translation JSONL row {line_number} is not an object")
            continue
        if str(row.get("risk", "")) != "candidate":
            continue
        if row.get("game_id") != context.game_id:
            issues.append(f"Translation JSONL game_id mismatch at line {line_number}")
        if str(row.get("plugin", "")).lower() != plugin_name.lower():
            issues.append(f"Translation JSONL plugin mismatch at line {line_number}")
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


def normalized_report_path(value: str) -> str:
    return value.strip().replace("\\", "/").removeprefix("./").lower()


def capability_support_label(level: str) -> str:
    return {
        "stable": "stable",
        "experimental_write": "experimental",
        "read_only": "read_only",
    }.get(level, level)


def resolve_report_write_decision(
    context: GameContext,
    root: Path,
    original: Path,
    report_path: Path,
):
    traits = read_plugin_report_traits(report_path)
    context_evidence = validate_plugin_master_style_context(
        report_path,
        project_root=root,
        expected_input=original,
        expected_game=context.game_id,
    )
    if traits.light_context is not context_evidence.light_context:
        raise ValueError(
            "Plugin report light trait does not match master-style context evidence"
        )
    resource = plugin_resource_descriptor(
        context,
        original.relative_to(root),
        traits,
    )
    decision = resolve_resource_capability(context, resource, "write")
    if not decision.supported or not decision.adapter_id:
        raise ValueError(
            f"plugin_text/write is unsupported for report traits: {decision.reason}"
        )
    return decision


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
    context: GameContext,
    plugin_adapter: str,
    plugin_capability_level: str,
    translation_rows_verified: int,
    writeback_reparse_verified: bool,
    structural_validation_verified: bool,
    round_trip_verified: bool,
    translation_jsonl: Path | None,
    output_export_jsonl: Path | None,
    writeback_report: Path | None,
    invariant_report: Path | None,
) -> None:
    original_item = original.stat()
    output_item = output.stat()
    lines: list[str] = [
        "# Plugin Output Verification",
        "",
        f"- game_id: {context.game_id}",
        f"- game_profile_version: {context.schema_version}",
        f"- plugin_adapter: {plugin_adapter}",
        "- plugin_adapter_version: "
        f"{context.capability_option_positive_int('plugin_text', 'adapter_contract_version')}",
        f"- support_level: {capability_support_label(plugin_capability_level)}",
        f"- plugin_text_capability_level: {plugin_capability_level}",
        f"- Original: {relative_path(root, original)}",
        f"- Output: {relative_path(root, output)}",
        f"- Translation JSONL: {relative_path(root, translation_jsonl) if translation_jsonl else ''}",
        f"- Output export JSONL: {relative_path(root, output_export_jsonl) if output_export_jsonl else ''}",
        f"- Writeback report: {relative_path(root, writeback_report) if writeback_report else ''}",
        f"- Invariant report: {relative_path(root, invariant_report) if invariant_report else ''}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Original SHA256: {original_hash}",
        f"- Output SHA256: {output_hash}",
        f"- Translation JSONL SHA256: {sha256_file(translation_jsonl) if translation_jsonl else ''}",
        f"- Output export JSONL SHA256: {sha256_file(output_export_jsonl) if output_export_jsonl else ''}",
        f"- Writeback report SHA256: {sha256_file(writeback_report) if writeback_report else ''}",
        f"- Invariant report SHA256: {sha256_file(invariant_report) if invariant_report else ''}",
        f"- Hash changed: {hash_changed}",
        f"- Original size: {original_item.st_size}",
        f"- Output size: {output_item.st_size}",
        f"- Output last write: {datetime.fromtimestamp(output_item.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Translation rows verified: {translation_rows_verified}",
        f"- Writeback reparse verified: {writeback_reparse_verified}",
        f"- Structural validation verified: {structural_validation_verified}",
        f"- Round-trip verified: {round_trip_verified}",
        f"- Verification passed: {not issues and round_trip_verified}",
        f"- Blocking issues: {len(issues)}",
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
            "- This script did not access real game installation, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
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
    parser.add_argument("--writeback-report-path", default="")
    parser.add_argument("--invariant-report-path", default="")
    parser.add_argument("--require-translation-evidence", action="store_true")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    args = parser.parse_args()

    root = project_root()
    context = resolve_workspace_game_context(root, args.game)
    plugin_write = resolve_capability(context, "plugin_text", "write")
    if not plugin_write.supported or not plugin_write.adapter_id:
        raise ValueError(
            f"plugin_text/write is unsupported for {context.game_id}: {plugin_write.reason}"
        )
    expected_plugin_adapter = plugin_write.adapter_id
    effective_plugin_write = plugin_write
    original = resolve_project_path(root, args.original_plugin_path, must_exist=True)
    output = resolve_project_path(root, args.output_plugin_path, must_exist=True)
    report = resolve_project_path(root, args.report_output_path, must_exist=False)
    if not (is_under(report, root / "qa") or is_under(report, root / "out")):
        raise ValueError(f"ReportOutputPath must be under qa/ or out/: {args.report_output_path}")
    if original.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError(f"OriginalPluginPath must be .esp, .esm, or .esl: {args.original_plugin_path}")
    if output.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError(f"OutputPluginPath must be .esp, .esm, or .esl: {args.output_plugin_path}")

    translation_jsonl = (
        resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
        if args.translation_jsonl_path.strip()
        else None
    )
    output_export_jsonl = (
        resolve_project_path(root, args.output_export_jsonl_path, must_exist=True)
        if args.output_export_jsonl_path.strip()
        else None
    )
    writeback_report: Path | None = None
    invariant_report: Path | None = None

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
    output_export_rows = parse_output_export_jsonl(root, args.output_export_jsonl_path, issues, context, original.name)
    if args.translation_jsonl_path.strip():
        probe_rows.extend(
            parse_translation_jsonl(
                root,
                args.translation_jsonl_path,
                output_bytes,
                output_export_rows,
                issues,
                context,
                original.name,
            )
        )

    writeback_reparse_verified = False
    structural_validation_verified = False
    if args.writeback_report_path.strip():
        writeback_report = resolve_project_path(root, args.writeback_report_path, must_exist=True)
        if not (is_under(writeback_report, root / "qa") or is_under(writeback_report, root / "out")):
            raise ValueError("WritebackReportPath must be under qa/ or out/.")
        report_context_matches = True
        writeback_decision = plugin_write
        try:
            writeback_decision = resolve_report_write_decision(
                context,
                root,
                original,
                writeback_report,
            )
            effective_plugin_write = writeback_decision
        except (OSError, ValueError) as exc:
            issues.append(f"writeback report capability evidence is invalid: {exc}")
            report_context_matches = False
        reported_game = report_metric(writeback_report, "game_id")
        if reported_game != context.game_id:
            issues.append(
                f"writeback report game_id mismatch: expected {context.game_id}, found {reported_game or '<missing>'}"
            )
            report_context_matches = False
        expected_metadata = {
            "game_profile_version": str(context.schema_version),
            "plugin_adapter": writeback_decision.adapter_id or expected_plugin_adapter,
            "plugin_adapter_version": str(
                context.capability_option_positive_int(
                    "plugin_text", "adapter_contract_version"
                )
            ),
            "support_level": capability_support_label(writeback_decision.level),
            "plugin_text_capability_level": writeback_decision.level,
        }
        for key, expected in expected_metadata.items():
            actual = report_metric(writeback_report, key)
            if actual != expected:
                issues.append(f"writeback report {key} mismatch: expected {expected}, found {actual or '<missing>'}")
                report_context_matches = False
        reported_input = normalized_report_path(report_metric(writeback_report, "Input plugin"))
        expected_input = normalized_report_path(relative_path(root, original))
        if reported_input != expected_input:
            issues.append(
                f"writeback report input path mismatch: expected {expected_input}, found {reported_input or '<missing>'}"
            )
            report_context_matches = False
        reported_input_hash = report_metric(writeback_report, "Input SHA256")
        if args.require_translation_evidence and not reported_input_hash:
            issues.append("writeback report input hash missing")
            report_context_matches = False
        elif reported_input_hash and reported_input_hash.upper() != original_hash.upper():
            issues.append("writeback report input hash mismatch")
            report_context_matches = False
        reported_output_value = report_metric(writeback_report, "Output plugin")
        reported_output = normalized_report_path(reported_output_value)
        expected_output = normalized_report_path(relative_path(root, output))
        reported_output_path: Path | None = None
        try:
            reported_output_path = resolve_project_path(root, reported_output_value, must_exist=True)
        except (OSError, ValueError):
            issues.append(f"writeback report output path mismatch: missing or invalid {reported_output or '<missing>'}")
            report_context_matches = False
        if reported_output != expected_output and reported_output_path is not None:
            allowed_reported_roots = (root / "out", root / "translated" / "tool_outputs")
            if not any(is_under(reported_output_path, candidate) for candidate in allowed_reported_roots):
                issues.append(
                    f"writeback report output path mismatch: expected generated output, found {reported_output}"
                )
                report_context_matches = False
            elif sha256_file(reported_output_path) != output_hash:
                issues.append("writeback report output copy hash does not match final output")
                report_context_matches = False
        if args.translation_jsonl_path.strip():
            translation_path = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
            reported_translation = normalized_report_path(report_metric(writeback_report, "Translation JSONL"))
            expected_translation = normalized_report_path(relative_path(root, translation_path))
            if reported_translation != expected_translation:
                issues.append(
                    f"writeback report translation path mismatch: expected {expected_translation}, found {reported_translation or '<missing>'}"
                )
                report_context_matches = False
            reported_translation_hash = report_metric(writeback_report, "Translation SHA256")
            expected_translation_hash = sha256_file(translation_path)
            if args.require_translation_evidence and not reported_translation_hash:
                issues.append("writeback report translation hash missing")
                report_context_matches = False
            elif reported_translation_hash and reported_translation_hash.upper() != expected_translation_hash.upper():
                issues.append("writeback report translation hash mismatch")
                report_context_matches = False
        reported_output_hash = report_metric(writeback_report, "Output SHA256")
        if args.require_translation_evidence and not reported_output_hash:
            issues.append("writeback report output hash missing")
            report_context_matches = False
        elif reported_output_hash and re.fullmatch(r"[0-9A-Fa-f]{64}", reported_output_hash) is None:
            issues.append("writeback report output hash malformed; expected 64 hexadecimal characters")
            report_context_matches = False
        elif reported_output_hash and reported_output_hash.upper() != output_hash.upper():
            issues.append("writeback report output hash mismatch")
            report_context_matches = False
        for metric in ("Candidate rows", "Applied rows"):
            value = report_metric(writeback_report, metric)
            if not value.isdigit() or int(value) != len(probe_rows):
                issues.append(f"writeback report {metric.lower()} mismatch")
                report_context_matches = False
        for metric in ("Missing rows", "Unsupported rows"):
            if report_metric(writeback_report, metric) != "0":
                issues.append(f"writeback report {metric.lower()} must be zero")
                report_context_matches = False
        writeback_reparse_verified = report_context_matches and (
            report_metric(writeback_report, "Reparse succeeded").lower() == "true"
        )
        structural_validation_verified = report_context_matches and (
            report_metric(writeback_report, "Structural validation succeeded").lower() == "true"
        )
        if report_metric(writeback_report, "Parsed structural and payload invariant verified").lower() != "true":
            issues.append("writeback report requires a successful parsed structural and payload invariant")
            structural_validation_verified = False

    if args.invariant_report_path.strip():
        invariant_report = resolve_project_path(root, args.invariant_report_path, must_exist=True)
        invariant_context_matches = True
        invariant_decision = effective_plugin_write
        try:
            invariant_decision = resolve_report_write_decision(
                context,
                root,
                original,
                invariant_report,
            )
            if invariant_decision.level != effective_plugin_write.level:
                raise ValueError(
                    "invariant report capability level does not match writeback evidence"
                )
            effective_plugin_write = invariant_decision
        except (OSError, ValueError) as exc:
            issues.append(f"invariant report capability evidence is invalid: {exc}")
            invariant_context_matches = False
        if report_metric(invariant_report, "Operation").lower() != "verify":
            issues.append("invariant report operation is not verify")
            invariant_context_matches = False
        for key, expected in {
            "game_id": context.game_id,
            "game_profile_version": str(context.schema_version),
            "plugin_adapter": invariant_decision.adapter_id or expected_plugin_adapter,
            "plugin_adapter_version": str(
                context.capability_option_positive_int(
                    "plugin_text", "adapter_contract_version"
                )
            ),
            "support_level": capability_support_label(invariant_decision.level),
            "plugin_text_capability_level": invariant_decision.level,
        }.items():
            actual = report_metric(invariant_report, key)
            if actual != expected:
                issues.append(f"invariant report {key} mismatch: expected {expected}, found {actual or '<missing>'}")
                invariant_context_matches = False
        invariant_input = normalized_report_path(report_metric(invariant_report, "Input plugin"))
        if invariant_input != normalized_report_path(relative_path(root, original)):
            issues.append("invariant report input path mismatch")
            invariant_context_matches = False
        if report_metric(invariant_report, "Input SHA256").upper() != original_hash.upper():
            issues.append("invariant report input hash mismatch")
            invariant_context_matches = False
        if args.translation_jsonl_path.strip():
            translation_path = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
            invariant_translation = normalized_report_path(report_metric(invariant_report, "Translation JSONL"))
            if invariant_translation != normalized_report_path(relative_path(root, translation_path)):
                issues.append("invariant report translation path mismatch")
                invariant_context_matches = False
            if report_metric(invariant_report, "Translation SHA256").upper() != sha256_file(translation_path).upper():
                issues.append("invariant report translation hash mismatch")
                invariant_context_matches = False
        invariant_output = normalized_report_path(report_metric(invariant_report, "Output plugin"))
        if invariant_output != normalized_report_path(relative_path(root, output)):
            issues.append("invariant report output path mismatch")
            invariant_context_matches = False
        if report_metric(invariant_report, "Parsed structural and payload invariant verified").lower() != "true":
            issues.append("invariant report did not verify the parsed structural and payload invariant")
            invariant_context_matches = False
        if report_metric(invariant_report, "Output SHA256").upper() != output_hash:
            issues.append("invariant report output hash mismatch")
            invariant_context_matches = False
        if args.require_translation_evidence and not invariant_context_matches:
            structural_validation_verified = False
    elif args.require_translation_evidence:
        issues.append("Strict verification requires a fresh controlled invariant report.")
        structural_validation_verified = False

    translation_rows_verified = sum(1 for row in probe_rows if row.DestPresentInExport is True)
    if args.require_translation_evidence:
        if not args.translation_jsonl_path.strip() or not args.output_export_jsonl_path.strip():
            issues.append("Strict verification requires translation JSONL and identity-based output export evidence.")
        if not probe_rows:
            issues.append("Strict verification found no translated candidate rows to verify.")
        elif translation_rows_verified != len(probe_rows):
            issues.append(
                f"Strict verification matched {translation_rows_verified} of {len(probe_rows)} translated rows by full identity."
            )
        if not writeback_reparse_verified:
            issues.append("Strict verification requires successful writeback reparse evidence.")
        if not structural_validation_verified:
            issues.append("Strict verification requires successful structural validation evidence.")

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

    round_trip_verified = (
        bool(probe_rows)
        and translation_rows_verified == len(probe_rows)
        and writeback_reparse_verified
        and structural_validation_verified
        and not issues
    )
    write_report(
        root,
        original,
        output,
        report,
        original_hash,
        output_hash,
        hash_changed,
        probe_rows,
        issues,
        warnings,
        context,
        effective_plugin_write.adapter_id or expected_plugin_adapter,
        effective_plugin_write.level,
        translation_rows_verified,
        writeback_reparse_verified,
        structural_validation_verified,
        round_trip_verified,
        translation_jsonl,
        output_export_jsonl,
        writeback_report,
        invariant_report,
    )
    print(f"Plugin verification written to: {report}")
    if issues:
        print(f"Plugin verification found {len(issues)} issue(s).")
        return 1 if args.require_translation_evidence else (0 if args.warn_only else 1)
    print("Plugin verification passed with no blocking issues.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
