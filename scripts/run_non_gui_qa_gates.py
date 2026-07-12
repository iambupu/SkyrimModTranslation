"""Strict non-GUI gate for a single translated Mod output.

This script stitches together mechanical proofread, coverage, archive audit,
final_mod structure checks, binary/text review packet generation, and model
review contract checks. It does not translate text or write binaries.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from game_context import GameContext, game_context_metadata, game_display_label
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from project_paths import plugin_root as default_plugin_root
from project_paths import plugin_script_path
from pex_translation_safety import SOURCE_FIELDS, normalized_pex_translation_line, pex_row_matches, pex_translation_row_protects_source, pex_translation_skip_reason, row_value
from translation_input_discovery import collect_translation_input_files, translation_input_evidence_roots
from workflow_lock import WorkflowLock
from project_paths import project_root
from route_translation_task import current_game_context

MODEL_REVIEWER_RE = re.compile(r"Reviewer:\s*(?:Agent|Codex) model", re.IGNORECASE)


@dataclass
class GateIssue:
    Severity: str
    Gate: str
    Message: str
    Evidence: str = ""


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def run_python_script(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess:
    source_root = default_plugin_root()
    script = plugin_script_path(script_name)
    if not script.is_file():
        raise FileNotFoundError(f"missing plugin script: scripts/{script_name}")
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(root),
        env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def process_output(result: subprocess.CompletedProcess) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_report_metric(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in read_text(path).splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def packet_content_reviewed(model_text: str, packet_path: Path) -> bool:
    # A filename mention is not enough: the model review must quote the current
    # packet hash so an old review cannot pass after final_mod changes.
    if not packet_path.is_file():
        return False
    if packet_path.name not in model_text:
        return False
    packet_hash = read_report_metric(packet_path, "Items SHA256")
    return bool(packet_hash and packet_hash in model_text)


def to_int(value: str | None, default: int = -1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def json_line_property(line: str, name: str) -> str:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return ""
    value = row.get(name)
    return "" if value is None else str(value)


def jsonl_file_values(path: Path, property_name: str) -> set[str]:
    values: set[str] = set()
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = row.get(property_name)
        if value is not None and str(value).strip():
            values.add(str(value).strip())
    return values


def model_text_mentions_path(model_text: str, path_value: str) -> bool:
    normalized_text = model_text.replace("/", "\\").lower()
    normalized_path = path_value.replace("/", "\\").lower()
    if normalized_path in normalized_text:
        return True
    basename = Path(path_value.replace("/", "\\")).name.lower()
    return bool(basename and basename in normalized_text)


def model_review_contract_issues(model_text: str, reviewed_files: set[str]) -> list[str]:
    # The required claims are intentionally literal. They make model review
    # output machine-checkable instead of relying on vague "looks good" wording.
    issues: list[str] = []
    required_claims = {
        "runtime safety": r"No runtime-impacting issues remain",
        "translation completeness": r"No required translation candidates remain untranslated",
        "semantic quality": r"No semantic quality blockers remain",
        "changed file coverage": r"All changed final_mod files listed in the review packets were reviewed",
        "model semantic review": r"Mechanical checks do not replace (?:agent|Codex) model semantic review",
        "final review quality": r"Final review quality audit has 0 blocking issues and 0 warnings",
    }
    for label, pattern in required_claims.items():
        if re.search(pattern, model_text, re.IGNORECASE) is None:
            issues.append(f"Missing required model-review claim: {label}.")
    missing_files = sorted(file for file in reviewed_files if not model_text_mentions_path(model_text, file))
    if missing_files:
        preview = "; ".join(missing_files[:10])
        suffix = "" if len(missing_files) <= 10 else f"; ... {len(missing_files) - 10} more"
        issues.append(f"Model review does not explicitly mention all changed final_mod files: {preview}{suffix}")
    return issues


def model_review_current_content_issues(
    model_text: str,
    final_text_packet: Path,
    final_binary_packet: Path,
    final_text_items_path: Path,
    final_binary_items_path: Path,
) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    for packet in (final_text_packet, final_binary_packet):
        if packet.is_file() and not packet_content_reviewed(model_text, packet):
            issues.append(
                (
                    "Agent model review does not cover the current final_mod review packet content hash; rerun model review.",
                    str(packet),
                )
            )
    reviewed_files = jsonl_file_values(final_text_items_path, "File") | jsonl_file_values(final_binary_items_path, "File")
    for contract_issue in model_review_contract_issues(model_text, reviewed_files):
        issues.append((contract_issue, ""))
    return issues


def count_jsonl_rows(path: Path, property_name: str = "", property_value: str = "") -> int:
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        if not property_name:
            count += 1
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get(property_name, "")) == property_value:
            count += 1
    return count


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def add_issue(issues: list[GateIssue], severity: str, gate: str, message: str, evidence: str = "") -> None:
    issues.append(GateIssue(severity, gate, message, evidence))


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def clean_report_passed(path: Path, pattern: str) -> bool:
    return path.is_file() and re.search(pattern, read_text(path)) is not None


def get_plugin_candidate_count(root: Path, mod_name: str, plugin_path: Path, plugin_name: str) -> int | None:
    export_path = root / "source" / "plugin_exports" / mod_name / f"{plugin_name}_strings.jsonl"
    if not export_path.is_file():
        result = run_python_script(
            root,
            "export_esp_strings.py",
            ["--plugin-path", str(plugin_path), "--mod-name", mod_name],
        )
        if result.returncode != 0:
            return None
    return count_jsonl_rows(export_path, "risk", "candidate")


def get_pex_candidate_count(root: Path, mod_name: str, pex_path: Path, pex_base_name: str) -> int | None:
    output_jsonl = f"source/pex_exports/{mod_name}/{pex_base_name}.strict_final_mod.pex_strings.jsonl"
    report = f"qa/{pex_base_name}.strict_pex_export_report.md"
    result = run_python_script(
        root,
        "invoke_mutagen_pex_string_tool.py",
        [
            "--mode",
            "Export",
            "--input-pex-path",
            str(pex_path),
            "--output-jsonl-path",
            output_jsonl,
            "--report-path",
            report,
        ],
    )
    if result.returncode != 0:
        return None
    return count_jsonl_rows(root / output_jsonl, "risk", "candidate")


def collect_translation_inputs(root: Path, mod_name: str) -> list[Path]:
    return collect_translation_input_files(root, mod_name, suffixes={".jsonl"}, include_derived_pex_apply=True)


def write_translation_input_list(root: Path, mod_name: str, inputs: list[Path]) -> Path:
    gate_dir = root / "work" / "gates" / mod_name
    gate_dir.mkdir(parents=True, exist_ok=True)
    list_path = gate_dir / "translation_inputs.txt"
    if inputs:
        list_path.write_text("\n".join(str(item) for item in inputs) + "\n", encoding="utf-8")
    elif list_path.exists():
        list_path.write_text("", encoding="utf-8")
    return list_path


def report_success_metrics(root: Path, mod_name: str, workspace: Path, final_mod: Path, report_path: Path, strict_complete: bool, issues: list[GateIssue], notes: list[str], metrics: dict[str, object], translation_inputs: list[Path], context: GameContext | None = None) -> None:
    context = context or current_game_context(root)
    if strict_complete:
        # Release readiness is stricter than normal QA: any warning indicates
        # unreviewed uncertainty and becomes blocking for completion claims.
        warnings = [issue for issue in issues if issue.Severity == "warning"]
        for warning in warnings:
            add_issue(
                issues,
                "error",
                "strict-complete",
                f"Strict complete mode treats warning as blocking: {warning.Gate} - {warning.Message}",
                warning.Evidence,
            )

    blocking_count = sum(1 for issue in issues if issue.Severity == "error")
    warning_count = sum(1 for issue in issues if issue.Severity == "warning")
    lines: list[str] = [
        "# Non-GUI QA Gate Report",
        "",
        f"- Game: {game_display_label(context)}",
        f"- Support level: {context.support_level}",
        *[f"- {key}: {value}" for key, value in game_context_metadata(context).items()],
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- FinalModDir: {relative_path(root, final_mod)}",
        f"- Translation inputs: {len(translation_inputs)}",
        f"- Localized string tables: {metrics.get('localized_string_tables', 0)}",
        f"- Coverage audited candidates: {metrics.get('coverage_audited', 'not_run')}",
        f"- Coverage missing: {metrics.get('coverage_missing', 'not_run')}",
        f"- Coverage unverified: {metrics.get('coverage_unverified', 'not_run')}",
        f"- PEX delivery rows: {metrics.get('pex_delivery_rows', 'not_run')}",
        f"- PEX delivery blocking issues: {metrics.get('pex_delivery_blocking', 'not_run')}",
        f"- PEX delivery warnings: {metrics.get('pex_delivery_warnings', 'not_run')}",
        f"- Archive files checked: {metrics.get('archive_files_checked', 'not_run')}",
        f"- Archives missing evidence: {metrics.get('archive_missing_evidence', 'not_run')}",
        f"- Archives invalid evidence: {metrics.get('archive_invalid_evidence', 'not_run')}",
        f"- Archive translatable files: {metrics.get('archive_translatable_files', 'not_run')}",
        f"- Archive loose overrides present: {metrics.get('archive_loose_overrides_present', 'not_run')}",
        f"- Archive loose override exemptions: {metrics.get('archive_loose_override_exemptions', 'not_run')}",
        f"- Archive loose overrides missing: {metrics.get('archive_loose_overrides_missing', 'not_run')}",
        f"- Archive loose override exemption issues: {metrics.get('archive_loose_override_exemption_issues', 'not_run')}",
        f"- Interface runtime files checked: {metrics.get('interface_runtime_files_checked', 'not_run')}",
        f"- Interface runtime warnings: {metrics.get('interface_runtime_warnings', 'not_run')}",
        f"- Final text files checked: {metrics.get('final_text_files_checked', 'not_run')}",
        f"- Final text structure warnings: {metrics.get('final_text_warnings', 'not_run')}",
        f"- Final text review items: {metrics.get('final_text_review_items', 'not_run')}",
        f"- Final text protected review items: {metrics.get('final_text_protected_review_items', 'not_run')}",
        f"- Final binary review items: {metrics.get('final_binary_review_items', 'not_run')}",
        f"- Final binary manual review items: {metrics.get('final_binary_manual_review_items', 'not_run')}",
        f"- Final binary protected review items: {metrics.get('final_binary_protected_review_items', 'not_run')}",
        f"- Final binary export failures: {metrics.get('final_binary_export_failures', 'not_run')}",
        f"- Final review quality rows: {metrics.get('final_review_quality_rows', 'not_run')}",
        f"- Final review quality blocking issues: {metrics.get('final_review_quality_blocking', 'not_run')}",
        f"- Final review quality warnings: {metrics.get('final_review_quality_warnings', 'not_run')}",
        f"- Final plugins checked: {metrics.get('final_plugins_checked', 0)}",
        f"- Final PEX files checked: {metrics.get('final_pex_files_checked', 0)}",
        f"- Strict complete mode: {bool(strict_complete)}",
        f"- WarningPolicyBlocksCompletion: {bool(strict_complete)}",
        f"- Blocking issues: {blocking_count}",
        f"- Warnings: {warning_count}",
        "",
        "## Verdict",
        "",
        "PASS: Non-GUI QA gates have no blocking issues." if blocking_count == 0 else "FAIL: Non-GUI QA gates have blocking issues.",
        "",
        "## Issues",
        "",
    ]
    if not issues:
        lines.append("No gate issues.")
    else:
        lines.extend(["| Severity | Gate | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {issue.Gate} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |")

    lines.extend(["", "## Notes", ""])
    if not notes:
        lines.append("No additional notes.")
    else:
        lines.extend(f"- {note}" for note in notes)

    lines.extend(
        [
            "",
            "## Required Evidence",
            "",
            "- Decoder detection: `qa/decoder_tools_report.md`",
            f"- Mechanical proofread: `qa/{mod_name}.translation_proofread.md`",
            f"- Agent model review: `qa/{mod_name}.model_review.md`",
            f"- Non-GUI coverage: `out/{mod_name}/qa/non_gui_translation_coverage.md`",
            f"- PEX delivery audit: `qa/{mod_name}.pex_delivery_post_build.md`",
            f"- Archive coverage: `qa/{mod_name}.archive_coverage.md`",
            f"- final_mod Interface runtime audit: `qa/{mod_name}.final_interface_runtime.md`",
            f"- final_mod text structure: `qa/{mod_name}.final_text_structure.md`",
            f"- final_mod text model review packet: `qa/{mod_name}.final_text_review_packet.md`",
            f"- final_mod binary model review packet: `qa/{mod_name}.final_binary_review_packet.md`",
            f"- final_mod final review quality audit: `qa/{mod_name}.final_review_quality.md`",
            "- final_mod validation: `qa/final_mod_validation.md`",
            "- Plugin verification reports: `qa/*.gate_plugin_output_verification.md`",
            f"- PEX verification and re-read reports: `qa/{mod_name}.<Script>.pex_output_verification.md`, `qa/*.gate_pex_export_report.md`",
            "",
            "## Safety",
            "",
            "- This gate script does not translate text.",
            "- This gate script does not write plugin or PEX binaries.",
            "- This gate script reads only project-local inputs and writes QA/work/source reports.",
            "- Real game, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run strict non-GUI QA gates for a project-local Skyrim translation output.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--allow-missing-model-review", action="store_true")
    parser.add_argument("--strict-complete", action="store_true")
    args = parser.parse_args()

    root = project_root()
    context = current_game_context(root)
    WorkflowLock(root, "run_non_gui_qa_gates.py").acquire()
    mod_name = args.mod_name
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{mod_name}", must_exist=True)
    workspace = find_data_root(workspace, context=context).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name)), must_exist=True)
    expected_final_mod = default_final_mod_dir(root, mod_name).resolve(strict=False)
    if final_mod.resolve(strict=False) != expected_final_mod:
        raise ValueError(f"FinalModDir must be out/{mod_name}/汉化产出/final_mod: {args.final_mod_dir}")
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.non_gui_qa_gates.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    issues: list[GateIssue] = []
    notes: list[str] = []
    localized_string_tables = sorted(
        item
        for item in workspace.rglob("*")
        if item.is_file() and item.suffix.lower() in context.string_table_extensions
    )
    if localized_string_tables and not context.string_tables_enabled:
        add_issue(
            issues,
            "error",
            "localized-strings",
            f"Localized STRINGS delivery is unsupported and blocked for {context.display_name}.",
            ", ".join(relative_path(root, item) for item in localized_string_tables[:8]),
        )
    metrics: dict[str, object] = {
        "localized_string_tables": len(localized_string_tables),
        "coverage_audited": "not_run",
        "coverage_missing": "not_run",
        "coverage_unverified": "not_run",
        "pex_delivery_rows": "not_run",
        "pex_delivery_blocking": "not_run",
        "pex_delivery_warnings": "not_run",
        "archive_files_checked": "not_run",
        "archive_missing_evidence": "not_run",
        "archive_invalid_evidence": "not_run",
        "archive_translatable_files": "not_run",
        "archive_loose_overrides_present": "not_run",
        "archive_loose_override_exemptions": "not_run",
        "archive_loose_overrides_missing": "not_run",
        "archive_loose_override_exemption_issues": "not_run",
        "interface_runtime_files_checked": "not_run",
        "interface_runtime_warnings": "not_run",
        "final_text_files_checked": "not_run",
        "final_text_warnings": "not_run",
        "final_text_review_items": "not_run",
        "final_text_protected_review_items": "not_run",
        "final_binary_review_items": "not_run",
        "final_binary_manual_review_items": "not_run",
        "final_binary_protected_review_items": "not_run",
        "final_binary_export_failures": "not_run",
        "final_review_quality_rows": "not_run",
        "final_review_quality_warnings": "not_run",
        "final_review_quality_blocking": "not_run",
        "final_plugins_checked": 0,
        "final_pex_files_checked": 0,
    }

    # Proofread only translation intermediates that feed writeback. Generated
    # review packets are handled later as final delivery evidence.
    translation_inputs = collect_translation_inputs(root, mod_name)
    translation_input_list = write_translation_input_list(root, mod_name, translation_inputs)
    # Some Mods expose only binary or structured final review items. Zero
    # standalone text candidates is suspicious, but not automatically fatal
    # until the later final_mod packets are inspected.
    coverage_found_no_candidates = False

    decoder = run_python_script(root, "detect_decoder_tools.py", [])
    if decoder.returncode != 0:
        add_issue(issues, "error", "decoder-tools", "Decoder tool detection failed.", "qa/decoder_tools_report.md")

    pex_delivery = run_python_script(
        root,
        "audit_pex_delivery.py",
        [
            "--mod-name",
            mod_name,
            "--workspace-path",
            str(workspace),
            "--final-mod-dir",
            str(final_mod),
            "--phase",
            "post-build",
        ],
    )
    pex_delivery_report = root / "qa" / f"{mod_name}.pex_delivery_post_build.md"
    if not pex_delivery_report.is_file():
        add_issue(issues, "error", "pex-delivery", "PEX delivery audit did not produce a report.", f"qa/{mod_name}.pex_delivery_post_build.md")
    else:
        metrics["pex_delivery_rows"] = read_report_metric(pex_delivery_report, "Rows checked") or "not_run"
        metrics["pex_delivery_blocking"] = read_report_metric(pex_delivery_report, "Blocking issues") or "not_run"
        metrics["pex_delivery_warnings"] = read_report_metric(pex_delivery_report, "Warnings") or "not_run"
        delivery_blocking = to_int(str(metrics["pex_delivery_blocking"]), 0)
        delivery_warnings = to_int(str(metrics["pex_delivery_warnings"]), 0)
        if delivery_blocking > 0:
            add_issue(issues, "error", "pex-delivery", f"PEX delivery audit has {delivery_blocking} blocking issue(s).", f"qa/{mod_name}.pex_delivery_post_build.md")
        if delivery_warnings > 0:
            add_issue(issues, "warning", "pex-delivery", f"PEX delivery audit has {delivery_warnings} warning(s).", f"qa/{mod_name}.pex_delivery_post_build.md")
        if pex_delivery.returncode != 0 and delivery_blocking == 0:
            add_issue(issues, "error", "pex-delivery", "PEX delivery audit failed to run cleanly.", f"qa/{mod_name}.pex_delivery_post_build.md")

    if translation_inputs:
        proofread = run_python_script(
            root,
            "proofread_translation.py",
            [
                "--input-list-path",
                str(translation_input_list),
                "--report-output-path",
                f"qa/{mod_name}.translation_proofread.md",
                "--issues-jsonl-path",
                f"qa/{mod_name}.translation_proofread_issues.jsonl",
                "--warn-only",
            ],
        )
        proofread_report = root / "qa" / f"{mod_name}.translation_proofread.md"
        if proofread.returncode != 0:
            add_issue(issues, "error", "mechanical-proofread", "Mechanical proofread script failed to run.", f"qa/{mod_name}.translation_proofread.md")
        else:
            blocking = to_int(read_report_metric(proofread_report, "Blocking issues"), 0)
            warnings = to_int(read_report_metric(proofread_report, "Warnings"), 0)
            if blocking > 0:
                add_issue(issues, "error", "mechanical-proofread", f"Mechanical proofread has {blocking} blocking issue(s).", f"qa/{mod_name}.translation_proofread.md")
            if warnings > 0:
                add_issue(issues, "warning", "mechanical-proofread", f"Mechanical proofread has {warnings} warning(s).", f"qa/{mod_name}.translation_proofread.md")
    else:
        severity = "error" if args.strict_complete else "warning"
        evidence = "; ".join(translation_input_evidence_roots(root, mod_name, include_derived_pex_apply=True))
        add_issue(issues, severity, "mechanical-proofread", "No translation JSONL inputs were found for proofread.", evidence)

    extraction = run_python_script(
        root,
        "extract_non_gui_candidates.py",
        ["--mod-name", mod_name, "--workspace-dir", str(workspace)],
    )
    if extraction.returncode != 0:
        add_issue(issues, "error", "coverage", "Non-GUI candidate extraction failed.", f"out/{mod_name}/qa/non_gui_extraction_report.md")
    else:
        coverage = run_python_script(
            root,
            "audit_non_gui_coverage.py",
            ["--mod-name", mod_name, "--final-mod-dir", str(final_mod)],
        )
        coverage_report = root / "out" / mod_name / "qa" / "non_gui_translation_coverage.md"
        if coverage.returncode != 0:
            add_issue(issues, "error", "coverage", "Non-GUI coverage audit failed to run.", f"out/{mod_name}/qa/non_gui_translation_coverage.md")
        else:
            metrics["coverage_audited"] = read_report_metric(coverage_report, "Audited candidates") or "not_run"
            metrics["coverage_missing"] = read_report_metric(coverage_report, "Missing") or "not_run"
            metrics["coverage_unverified"] = read_report_metric(coverage_report, "Unverified") or "not_run"
            missing = to_int(str(metrics["coverage_missing"]), 0)
            unverified = to_int(str(metrics["coverage_unverified"]), 0)
            audited = to_int(str(metrics["coverage_audited"]), 0)
            if missing > 0:
                add_issue(issues, "error", "coverage", f"Non-GUI coverage audit found {missing} missing candidate(s).", f"out/{mod_name}/qa/non_gui_remaining_gaps.jsonl")
            if unverified > 0:
                add_issue(issues, "error", "coverage", f"Non-GUI coverage audit found {unverified} unverified candidate(s).", f"out/{mod_name}/qa/non_gui_unverified_candidates.jsonl")
            if audited == 0:
                coverage_found_no_candidates = True

    archive_args = ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod)]
    if args.strict_complete:
        archive_args.append("--strict-complete")
    archive = run_python_script(root, "audit_archive_coverage.py", archive_args)
    archive_report = root / "qa" / f"{mod_name}.archive_coverage.md"
    if archive.returncode != 0:
        add_issue(issues, "error", "archive-coverage", "Archive coverage audit failed or found blocking issue(s).", f"qa/{mod_name}.archive_coverage.md")
    else:
        metrics["archive_files_checked"] = read_report_metric(archive_report, "Archive files checked") or "not_run"
        metrics["archive_missing_evidence"] = read_report_metric(archive_report, "Archives missing evidence") or "not_run"
        metrics["archive_invalid_evidence"] = read_report_metric(archive_report, "Archives invalid evidence") or "not_run"
        metrics["archive_translatable_files"] = read_report_metric(archive_report, "Archive translatable files") or "not_run"
        metrics["archive_loose_overrides_present"] = read_report_metric(archive_report, "Archive loose overrides present") or "not_run"
        metrics["archive_loose_override_exemptions"] = read_report_metric(archive_report, "Archive loose override exemptions") or "not_run"
        metrics["archive_loose_overrides_missing"] = read_report_metric(archive_report, "Archive loose overrides missing") or "not_run"
        metrics["archive_loose_override_exemption_issues"] = read_report_metric(archive_report, "Archive loose override exemption issues") or "not_run"
        archive_warnings = to_int(read_report_metric(archive_report, "Warnings"), 0)
        if archive_warnings > 0:
            add_issue(issues, "warning", "archive-coverage", f"Archive coverage audit has {archive_warnings} warning(s).", f"qa/{mod_name}.archive_coverage.md")

    interface_runtime = run_python_script(
        root,
        "audit_final_interface_translations.py",
        ["--mod-name", mod_name, "--final-mod-dir", str(final_mod)],
    )
    interface_runtime_report = root / "qa" / f"{mod_name}.final_interface_runtime.md"
    if interface_runtime.returncode != 0:
        add_issue(
            issues,
            "error",
            "interface-runtime",
            "final_mod Interface translation runtime audit failed.",
            f"qa/{mod_name}.final_interface_runtime.md",
        )
    else:
        metrics["interface_runtime_files_checked"] = read_report_metric(interface_runtime_report, "Interface translation files checked") or "not_run"
        metrics["interface_runtime_warnings"] = read_report_metric(interface_runtime_report, "Warnings") or "not_run"
        runtime_warnings = to_int(str(metrics["interface_runtime_warnings"]), 0)
        if runtime_warnings > 0:
            add_issue(
                issues,
                "warning",
                "interface-runtime",
                f"final_mod Interface translation runtime audit has {runtime_warnings} warning(s).",
                f"qa/{mod_name}.final_interface_runtime.md",
            )

    final_text = run_python_script(
        root,
        "validate_final_text_structure.py",
        ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod)],
    )
    final_text_report = root / "qa" / f"{mod_name}.final_text_structure.md"
    final_text_clean = (
        final_text_report.is_file()
        and read_report_metric(final_text_report, "Blocking issues") == "0"
        and read_report_metric(final_text_report, "Warnings") == "0"
    )
    if final_text.returncode != 0 and not final_text_clean:
        add_issue(issues, "error", "final-text-structure", "final_mod text structure validation failed.", f"qa/{mod_name}.final_text_structure.md")
    else:
        metrics["final_text_files_checked"] = read_report_metric(final_text_report, "Files checked") or "not_run"
        metrics["final_text_warnings"] = read_report_metric(final_text_report, "Warnings") or "not_run"
        warnings = to_int(str(metrics["final_text_warnings"]), 0)
        if warnings > 0:
            add_issue(issues, "warning", "final-text-structure", f"final_mod text structure validation has {warnings} warning(s).", f"qa/{mod_name}.final_text_structure.md")

    final_text_review = run_python_script(
        root,
        "new_final_text_review_packet.py",
        ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod)],
    )
    final_text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    final_text_items_path = root / "qa" / f"{mod_name}.final_text_review_items.jsonl"
    if final_text_review.returncode != 0:
        add_issue(issues, "error", "final-text-model-review", "final_mod text model review packet generation failed.", f"qa/{mod_name}.final_text_review_packet.md")
    else:
        metrics["final_text_review_items"] = read_report_metric(final_text_packet, "Review items") or "not_run"
        metrics["final_text_protected_review_items"] = read_report_metric(final_text_packet, "Protected review items") or "not_run"
        protected = to_int(str(metrics["final_text_protected_review_items"]), 0)
        if protected > 0:
            add_issue(issues, "warning", "final-text-model-review", f"final_mod text model review packet has {protected} protected-review item(s).", f"qa/{mod_name}.final_text_review_packet.md")

    # Binary review packet generation re-reads delivered ESP/PEX where possible.
    # This is the last chance to catch protected strings or decoder failures in
    # the actual files that will be packaged.
    final_binary_review = run_python_script(
        root,
        "new_final_binary_review_packet.py",
        ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod), "--reuse-current-if-unchanged"],
    )
    final_binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    final_binary_items_path = root / "qa" / f"{mod_name}.final_binary_review_items.jsonl"
    if final_binary_review.returncode != 0:
        add_issue(issues, "error", "final-binary-model-review", "final_mod binary model review packet generation failed.", f"qa/{mod_name}.final_binary_review_packet.md")
    else:
        metrics["final_binary_review_items"] = read_report_metric(final_binary_packet, "Review items") or "not_run"
        metrics["final_binary_manual_review_items"] = read_report_metric(final_binary_packet, "Manual review items") or "not_run"
        metrics["final_binary_protected_review_items"] = read_report_metric(final_binary_packet, "Protected review items") or "not_run"
        metrics["final_binary_export_failures"] = read_report_metric(final_binary_packet, "Export failures") or "not_run"
        protected = to_int(str(metrics["final_binary_protected_review_items"]), 0)
        failures = to_int(str(metrics["final_binary_export_failures"]), 0)
        if protected > 0:
            add_issue(issues, "error", "final-binary-model-review", f"final_mod binary model review packet has {protected} protected-review item(s).", f"qa/{mod_name}.final_binary_review_packet.md")
        if failures > 0:
            add_issue(issues, "error", "final-binary-model-review", f"final_mod binary model review packet has {failures} export failure(s).", f"qa/{mod_name}.final_binary_review_packet.md")

    final_review_quality = run_python_script(root, "audit_final_review_quality.py", ["--mod-name", mod_name])
    final_review_quality_report = root / "qa" / f"{mod_name}.final_review_quality.md"
    if not final_review_quality_report.is_file():
        add_issue(issues, "error", "final-review-quality", "final_mod final review quality audit did not produce a report.", f"qa/{mod_name}.final_review_quality.md")
    else:
        metrics["final_review_quality_rows"] = read_report_metric(final_review_quality_report, "Rows checked") or "not_run"
        metrics["final_review_quality_blocking"] = read_report_metric(final_review_quality_report, "Blocking issues") or "not_run"
        metrics["final_review_quality_warnings"] = read_report_metric(final_review_quality_report, "Warnings") or "not_run"
        quality_blocking = to_int(str(metrics["final_review_quality_blocking"]), 0)
        quality_warnings = to_int(str(metrics["final_review_quality_warnings"]), 0)
        if quality_blocking > 0:
            add_issue(issues, "error", "final-review-quality", f"final_mod final review quality audit has {quality_blocking} blocking issue(s).", f"qa/{mod_name}.final_review_quality.md")
        if quality_warnings > 0:
            add_issue(issues, "warning", "final-review-quality", f"final_mod final review quality audit has {quality_warnings} warning(s).", f"qa/{mod_name}.final_review_quality.md")
        if final_review_quality.returncode != 0 and quality_blocking == 0 and quality_warnings == 0:
            add_issue(issues, "error", "final-review-quality", "final_mod final review quality audit failed to run cleanly.", f"qa/{mod_name}.final_review_quality.md")

    if coverage_found_no_candidates:
        final_text_items = to_int(str(metrics["final_text_review_items"]), 0)
        final_binary_items = to_int(str(metrics["final_binary_review_items"]), 0)
        if final_text_items > 0 or final_binary_items > 0:
            notes.append(
                "Non-GUI text-resource coverage had no standalone text candidates; "
                f"final_mod review packets cover {final_text_items} text item(s) and {final_binary_items} binary item(s)."
            )
        else:
            severity = "error" if args.strict_complete else "warning"
            add_issue(issues, severity, "coverage", "Non-GUI coverage audit found no translation candidates.", f"out/{mod_name}/qa/non_gui_translation_coverage.md")

    # Model review is a semantic gate over final_mod, not over an early draft
    # translation table. Hash and mtime checks keep it tied to current inputs.
    model_review = root / "qa" / f"{mod_name}.model_review.md"
    contract_refresh = run_python_script(root, "update_model_review_contract.py", ["--mod-name", mod_name, "--create-if-missing"])
    if contract_refresh.returncode != 0:
        add_issue(
            issues,
            "error",
            "model-review",
            "Model review contract scaffold could not be refreshed.",
            f"qa/{mod_name}.model_review.md",
        )
    if not model_review.is_file():
        if translation_inputs:
            run_python_script(
                root,
                "new_model_review_packet.py",
                ["--mod-name", mod_name, "--input-list-path", str(translation_input_list)],
            )
        if not args.allow_missing_model_review:
            add_issue(issues, "error", "model-review", f"Agent model review report is missing. Fill qa/{mod_name}.model_review.md before writeback/final delivery.", f"qa/{mod_name}.model_review.md")
    else:
        model_text = read_text(model_review)
        if re.search(r"\bTODO\b", model_text, re.I):
            add_issue(issues, "error", "model-review", "Agent model review report still contains TODO placeholders.", f"qa/{mod_name}.model_review.md")
        elif MODEL_REVIEWER_RE.search(model_text) is None:
            add_issue(issues, "error", "model-review", "Model review report does not explicitly state Reviewer: Agent model.", f"qa/{mod_name}.model_review.md")
        elif not re.search(r"\bpass\b", model_text, re.I):
            add_issue(issues, "warning", "model-review", "Agent model review report does not contain an explicit pass verdict.", f"qa/{mod_name}.model_review.md")
        if final_text_packet.is_file() and f"{mod_name}.final_text_review_packet.md" not in model_text:
            add_issue(issues, "error", "model-review", "Agent model review does not mention the final_mod text review packet.", f"qa/{mod_name}.final_text_review_packet.md")
        if final_binary_packet.is_file() and f"{mod_name}.final_binary_review_packet.md" not in model_text:
            add_issue(issues, "error", "model-review", "Agent model review does not mention the final_mod binary review packet.", f"qa/{mod_name}.final_binary_review_packet.md")
        # The semantic review is tied to final_mod by packet content hashes.
        # Re-running the workflow may refresh intermediate translation input
        # mtimes without changing delivered final_mod content, so mtimes are not
        # a reliable blocking signal here.
        for contract_issue, evidence in model_review_current_content_issues(
            model_text,
            final_text_packet,
            final_binary_packet,
            final_text_items_path,
            final_binary_items_path,
        ):
            add_issue(issues, "error", "model-review", contract_issue, evidence or f"qa/{mod_name}.model_review.md")

    final_plugins = sorted(item for item in final_mod.iterdir() if item.is_file() and item.suffix.lower() in {".esp", ".esm", ".esl"})
    metrics["final_plugins_checked"] = len(final_plugins)
    for plugin in final_plugins:
        original = workspace / plugin.name
        if not original.is_file():
            add_issue(issues, "error", "plugin-output", f"Original plugin not found for final output: {plugin.name}", relative_path(root, plugin))
            continue
        translation = root / "translated" / "plugin_exports" / mod_name / f"{plugin.name}_strings.zh.jsonl"
        if not translation.is_file():
            if args.strict_complete:
                candidate_count = get_plugin_candidate_count(root, mod_name, original, plugin.name)
                if candidate_count is None:
                    add_issue(issues, "error", "plugin-output", f"No plugin translation JSONL found for {plugin.name}, and candidate export could not be verified.", f"translated/plugin_exports/{mod_name}")
                elif candidate_count > 0:
                    add_issue(issues, "error", "plugin-output", f"No plugin translation JSONL found for {plugin.name}; {candidate_count} candidate row(s) need coverage.", f"translated/plugin_exports/{mod_name}")
                else:
                    notes.append(f"Plugin has no exported candidate rows and no translation JSONL was required: {plugin.name}")
            else:
                add_issue(issues, "warning", "plugin-output", f"No plugin translation JSONL found for {plugin.name}; verification skipped.", f"translated/plugin_exports/{mod_name}")
            continue
        verify_report = f"qa/{plugin.name}.gate_plugin_output_verification.md"
        verify = run_python_script(
            root,
            "verify_plugin_output.py",
            [
                "--original-plugin-path",
                str(original),
                "--output-plugin-path",
                str(plugin),
                "--translation-jsonl-path",
                str(translation),
                "--report-output-path",
                verify_report,
                "--warn-only",
            ],
        )
        verify_path = root / verify_report
        if verify.returncode != 0 and not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "plugin-output", f"Plugin verification failed to run for {plugin.name}.", verify_report)
        elif not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "plugin-output", f"Plugin verification did not pass cleanly for {plugin.name}.", verify_report)

    coverage_complete = (
        to_int(str(metrics.get("coverage_missing")), -1) == 0
        and to_int(str(metrics.get("coverage_unverified")), -1) == 0
    )
    final_pex_files = sorted(item for item in final_mod.rglob("*") if item.is_file() and item.suffix.lower() == ".pex")
    metrics["final_pex_files_checked"] = len(final_pex_files)
    for pex in final_pex_files:
        rel_pex = relative_path(final_mod, pex)
        original = workspace / rel_pex
        if not original.is_file():
            add_issue(issues, "error", "pex-output", f"Original PEX not found for final output: {rel_pex}", relative_path(root, pex))
            continue

        matched_lines: list[str] = []
        skipped_pex_rows = 0
        candidate_rows: list[tuple[str, dict | None]] = []
        for candidate in translation_inputs:
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if json_line_property(line, "ModName").lower() == pex.name.lower():
                        candidate_rows.append((line, None))
                    continue
                if isinstance(row, dict) and pex_row_matches(row, pex):
                    candidate_rows.append((line, row))
        protected_sources = {
            row_value(row, *SOURCE_FIELDS)
            for _line, row in candidate_rows
            if row is not None
            and row_value(row, *SOURCE_FIELDS).strip()
            and pex_translation_row_protects_source(row)
        }
        for line, row in candidate_rows:
            if row is not None:
                source = row_value(row, *SOURCE_FIELDS)
                if source in protected_sources or pex_translation_skip_reason(row):
                    skipped_pex_rows += 1
                    continue
            matched_lines.append(normalized_pex_translation_line(row, pex, line) if row is not None else line)
        if skipped_pex_rows:
            notes.append(f"Skipped protected or non-writable PEX row(s) for {rel_pex}: {skipped_pex_rows}")

        if not matched_lines:
            if sha256(original) != sha256(pex):
                evidence = "; ".join(translation_input_evidence_roots(root, mod_name, include_derived_pex_apply=True))
                add_issue(
                    issues,
                    "error",
                    "pex-output",
                    f"Final PEX differs from original but no matching translation JSONL rows were found: {rel_pex}",
                    evidence,
                )
            elif args.strict_complete:
                if coverage_complete:
                    notes.append(f"PEX unchanged and accepted because non-GUI coverage is complete: {rel_pex}")
                    continue
                candidate_count = get_pex_candidate_count(root, mod_name, pex, pex.stem)
                if candidate_count is None:
                    add_issue(issues, "error", "pex-output", f"PEX unchanged and no translation rows found, but candidate export could not be verified: {rel_pex}", f"qa/{pex.stem}.strict_pex_export_report.md")
                elif candidate_count > 0 and not coverage_complete:
                    add_issue(issues, "error", "pex-output", f"PEX unchanged and no translation rows found, but {candidate_count} candidate row(s) were exported: {rel_pex}", f"source/pex_exports/{mod_name}/{pex.stem}.strict_final_mod.pex_strings.jsonl")
                else:
                    notes.append(f"PEX unchanged and no exported candidate rows found: {rel_pex}")
            else:
                notes.append(f"PEX unchanged and no translation rows found: {rel_pex}")
            continue

        filtered = root / "work" / "gates" / mod_name / f"{pex.stem}.translation.jsonl"
        filtered.parent.mkdir(parents=True, exist_ok=True)
        filtered.write_text("\n".join(matched_lines) + "\n", encoding="utf-8")
        verify_report = f"qa/{mod_name}.{pex.stem}.pex_output_verification.md"
        verify = run_python_script(
            root,
            "verify_pex_output.py",
            [
                "--original-pex-path",
                str(original),
                "--output-pex-path",
                str(pex),
                "--translation-jsonl-path",
                str(filtered),
                "--report-output-path",
                verify_report,
                "--warn-only",
            ],
        )
        verify_path = root / verify_report
        if verify.returncode != 0 and not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "pex-output", f"PEX verification failed to run for {rel_pex}.", verify_report)
        elif not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "pex-output", f"PEX verification did not pass cleanly for {rel_pex}.", verify_report)

        export_report = f"qa/{pex.stem}.gate_pex_export_report.md"
        export_jsonl = f"source/pex_exports/{mod_name}/{pex.stem}.gate_final_mod.pex_strings.jsonl"
        export = run_python_script(
            root,
            "invoke_mutagen_pex_string_tool.py",
            [
                "--mode",
                "Export",
                "--input-pex-path",
                str(pex),
                "--output-jsonl-path",
                export_jsonl,
                "--report-path",
                export_report,
            ],
        )
        if export.returncode != 0:
            add_issue(issues, "error", "pex-output", f"Final PEX could not be re-read by Mutagen: {rel_pex}", export_report)

    final_validation = run_python_script(root, "validate_final_mod.py", ["--final-mod-dir", str(final_mod)])
    final_validation_report = root / "qa" / "final_mod_validation.md"
    if final_validation.returncode != 0:
        add_issue(issues, "error", "final-mod", "final_mod validation failed.", "qa/final_mod_validation.md")
    else:
        text = read_text(final_validation_report)
        if "No blocking errors." not in text:
            add_issue(issues, "error", "final-mod", "final_mod validation did not report clean blocking status.", "qa/final_mod_validation.md")
        sidecar_overlay_zero = re.search(r"Language sidecar overlays:\s*0", text) is not None
        original_sidecar_warning_only = sidecar_overlay_zero and "- Language sidecar file exists in final_mod;" in text
        if "No warnings." not in text and not original_sidecar_warning_only:
            add_issue(issues, "warning", "final-mod", "final_mod validation reported warning(s).", "qa/final_mod_validation.md")
        if not re.search(r"Delivery mode:\s*direct-replacement-final-mod", text):
            add_issue(issues, "error", "final-mod", "final_mod is not confirmed as direct-replacement delivery.", "qa/final_mod_validation.md")
        if not re.search(r"Language sidecar overlays:\s*0", text):
            add_issue(issues, "error", "final-mod", "final_mod contains language sidecar overlay(s), which is not direct replacement delivery.", "qa/final_mod_validation.md")

    report_success_metrics(
        root,
        mod_name,
        workspace,
        final_mod,
        report_path,
        args.strict_complete,
        issues,
        notes,
        metrics,
        translation_inputs,
        context,
    )
    blocking_count = sum(1 for issue in issues if issue.Severity == "error")
    warning_count = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Non-GUI QA gate report written to: {report_path}")
    print(f"Blocking issues: {blocking_count}")
    print(f"Warnings: {warning_count}")
    print(f"WarningPolicyBlocksCompletion: {bool(args.strict_complete)}")
    return 1 if blocking_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
