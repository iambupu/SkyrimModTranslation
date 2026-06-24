"""Write a compact status report from current QA evidence."""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import packaged_mod_path
from project_paths import find_data_root

from workflow_lock import WorkflowLock
from project_paths import project_root


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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_metric(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in read_text(path).splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def packet_content_reviewed(model_text: str, packet_path: Path) -> bool:
    if not packet_path.is_file():
        return False
    if packet_path.name not in model_text:
        return False
    packet_hash = read_metric(packet_path, "Items SHA256")
    return bool(packet_hash and packet_hash in model_text)


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def exists(path: Path) -> bool:
    return path.exists()


def count_files(path: Path, predicate=None) -> int:
    if not path.is_dir():
        return 0
    count = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        if predicate is None or predicate(item):
            count += 1
    return count


def table_status_from_report(path: Path, pass_pattern: str, warning_pattern: str | None = None) -> str:
    if not path.is_file():
        return "not_run"
    text = read_text(path)
    if re.search(pass_pattern, text):
        if warning_pattern is None or re.search(warning_pattern, text):
            return "passed"
    if re.search(r"Blocking issues:\s+[1-9]", text):
        return "failed"
    return "needs_review"


def workflow_value(root: Path, name: str) -> str:
    return read_metric(root / "qa" / "workflow_report.md", name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write Skyrim translation project status report.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--report-output-path", default="qa/status.md")
    args = parser.parse_args()

    root = project_root()
    WorkflowLock(root, "write_translation_status.py").acquire()
    mod_name = args.mod_name.strip() or workflow_value(root, "ModName")
    workspace_value = args.workspace_path.strip() or workflow_value(root, "Workspace")
    if not mod_name:
        raise ValueError("ModName could not be inferred. Pass --mod-name.")

    workspace = resolve_project_path(root, workspace_value, must_exist=False) if workspace_value else None
    if workspace is not None and workspace.is_dir():
        workspace = find_data_root(workspace).resolve(strict=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    paths = {
        "WorkflowReport": "qa/workflow_report.md",
        "InventoryReport": "qa/mod_inventory.md",
        "RoutingReport": "qa/routing_report.md",
        "ToolPrefsAudit": "qa/tool_prefs_audit.md",
        "McmExtractionReport": "qa/mcm_extraction_report.md",
        "EspRetryReport": "qa/esp_retry_report.md",
        "PluginVerification": "qa/plugin_output_verification.md",
        "PlaceholderReport": "qa/placeholder_report.md",
        "EspXmlPlaceholderReport": "qa/esp_xml_placeholder_report.md",
        "PexToolWriteback": "qa/pex_tool_writeback.md",
        "CleanupReport": "qa/cleanup_report.md",
        "SkillAudit": "qa/skill_audit.md",
        "OutLayoutMigration": "qa/out_layout_migration.md",
        "ArchiveCoverage": f"qa/{mod_name}.archive_coverage.md",
        "FinalTextStructure": f"qa/{mod_name}.final_text_structure.md",
        "FinalTextReviewPacket": f"qa/{mod_name}.final_text_review_packet.md",
        "FinalBinaryReviewPacket": f"qa/{mod_name}.final_binary_review_packet.md",
        "FinalModValidation": "qa/final_mod_validation.md",
    }
    existing_reports = [value for value in paths.values() if (root / value).exists()]

    translated_interface_dir = root / "translated" / "interface" / mod_name
    translated_text_asset_dir = root / "translated" / "text_assets" / mod_name
    lextranslator_ready_dir = root / "translated" / "lextranslator_ready" / mod_name
    xtranslator_ready_dir = root / "translated" / "xtranslator_ready" / mod_name
    tool_output_dir = root / "out" / mod_name / "tool_outputs"
    final_mod_dir = default_final_mod_dir(root, mod_name)
    package_path = packaged_mod_path(root, mod_name)
    mcm_jsonl = root / "work" / "normalized" / mod_name / "mcm_text_candidates.jsonl"
    pex_visible_jsonl = root / "work" / "normalized" / mod_name / "pex_visible_strings.jsonl"
    final_manifest_path = final_mod_dir / "meta" / "manifest.json"

    pex_lex_ready_files = []
    if lextranslator_ready_dir.is_dir():
        pex_lex_ready_files = [
            item
            for item in lextranslator_ready_dir.iterdir()
            if item.is_file() and re.search(r"(?i)pex.*\.(jsonl|xml)$|\.pex\.", item.name)
        ]
    pex_tool_output_files = []
    if tool_output_dir.is_dir():
        pex_tool_output_files = [item for item in tool_output_dir.rglob("*") if item.is_file() and item.suffix.lower() == ".pex"]

    tool_prefs_audit = root / "qa" / "tool_prefs_audit.md"
    tool_prefs_status = "not_run"
    if tool_prefs_audit.is_file():
        tool_prefs_status = "risk_found" if "Risky path marker" in read_text(tool_prefs_audit) else "passed"

    workspace_files = []
    if workspace and workspace.is_dir():
        workspace_files = [item for item in workspace.rglob("*") if item.is_file()]
    plugin_files = [item for item in workspace_files if item.suffix.lower() in {".esp", ".esm", ".esl"}]
    pex_files = [item for item in workspace_files if item.suffix.lower() == ".pex"]
    interface_files = [item for item in workspace_files if re.search(r"(?i)\\interface\\translations\\.*\.txt$", str(item))]
    mcm_files = [item for item in workspace_files if re.search(r"(?i)\\MCM\\", str(item))]

    plugin_verification_path = root / "qa" / "plugin_output_verification.md"
    plugin_output_status = "not_applicable"
    if plugin_files:
        if plugin_verification_path.is_file():
            plugin_output_status = "verified" if "No blocking issues." in read_text(plugin_verification_path) else "failed_verification"
        elif tool_output_dir.exists():
            plugin_output_status = "needs_verification"
        else:
            plugin_output_status = "pending"

    proofread_path = root / "qa" / f"{mod_name}.translation_proofread.md"
    proofread_status = "not_run"
    if proofread_path.is_file():
        text = read_text(proofread_path)
        proofread_status = "passed" if re.search(r"Blocking issues:\s+0", text) and re.search(r"Warnings:\s+0", text) else "needs_review"

    final_text_review_packet_path = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    final_binary_review_packet_path = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    model_review_path = root / "qa" / f"{mod_name}.model_review.md"
    model_review_status = "not_run"
    if model_review_path.is_file():
        model_text = read_text(model_review_path)
        model_review_status = "passed" if re.search(r"\bpass\b", model_text, re.I) and not re.search(r"\bTODO\b", model_text, re.I) else "needs_review"
        if final_text_review_packet_path.is_file():
            if f"{mod_name}.final_text_review_packet.md" not in model_text:
                model_review_status = "needs_final_text_review"
            elif not packet_content_reviewed(model_text, final_text_review_packet_path):
                model_review_status = "stale_final_text_review"
        if final_binary_review_packet_path.is_file():
            if f"{mod_name}.final_binary_review_packet.md" not in model_text:
                model_review_status = "needs_final_binary_review"
            elif not packet_content_reviewed(model_text, final_binary_review_packet_path):
                model_review_status = "stale_final_binary_review"

    non_gui_gate_path = root / "qa" / f"{mod_name}.non_gui_qa_gates.md"
    non_gui_gate_status = "not_run"
    non_gui_gate_final_pex_checked = 0
    non_gui_gate_strict_complete = False
    if non_gui_gate_path.is_file():
        text = read_text(non_gui_gate_path)
        non_gui_gate_status = "passed" if "PASS: Non-GUI QA gates have no blocking issues" in text and re.search(r"Warnings:\s+0", text) else "failed_or_warned"
        if non_gui_gate_status == "passed" and re.search(r"Strict complete mode:\s*True", text):
            non_gui_gate_status = "passed_strict_complete"
            non_gui_gate_strict_complete = True
        match = re.search(r"Final PEX files checked:\s+([0-9]+)", text)
        if match:
            non_gui_gate_final_pex_checked = int(match.group(1))

    archive_coverage_path = root / "qa" / f"{mod_name}.archive_coverage.md"
    archive_coverage_status = "not_run"
    archive_files_checked = 0
    if archive_coverage_path.is_file():
        text = read_text(archive_coverage_path)
        match = re.search(r"Archive files checked:\s+([0-9]+)", text)
        if match:
            archive_files_checked = int(match.group(1))
        if re.search(r"Blocking issues:\s+0", text) and re.search(r"Warnings:\s+0", text):
            archive_coverage_status = "not_applicable" if archive_files_checked == 0 else "passed"
        elif re.search(r"Blocking issues:\s+[1-9]", text):
            archive_coverage_status = "failed"
        else:
            archive_coverage_status = "needs_evidence"

    final_text_structure_path = root / "qa" / f"{mod_name}.final_text_structure.md"
    final_text_structure_status = "not_run"
    final_text_files_checked = 0
    if final_text_structure_path.is_file():
        text = read_text(final_text_structure_path)
        match = re.search(r"Files checked:\s+([0-9]+)", text)
        if match:
            final_text_files_checked = int(match.group(1))
        if "PASS: final_mod text structure has no blocking issues" in text and re.search(r"Warnings:\s+0", text):
            final_text_structure_status = "passed"
        elif re.search(r"Blocking issues:\s+[1-9]", text):
            final_text_structure_status = "failed"
        else:
            final_text_structure_status = "needs_review"

    final_text_review_packet_status = "not_run"
    final_text_review_items = 0
    if final_text_review_packet_path.is_file():
        text = read_text(final_text_review_packet_path)
        final_text_review_items = to_int(read_metric(final_text_review_packet_path, "Review items"))
        final_text_review_packet_status = "ready" if re.search(r"Protected review items:\s+0", text) else "needs_review"

    final_binary_review_packet_status = "not_run"
    final_binary_review_items = 0
    if final_binary_review_packet_path.is_file():
        text = read_text(final_binary_review_packet_path)
        final_binary_review_items = to_int(read_metric(final_binary_review_packet_path, "Review items"))
        final_binary_review_packet_status = "ready" if re.search(r"Protected review items:\s+0", text) and re.search(r"Export failures:\s+0", text) else "needs_review"

    final_mod_status = "pending"
    if final_mod_dir.exists():
        final_mod_status = "built"
        if final_manifest_path.is_file():
            try:
                manifest = json.loads(read_text(final_manifest_path))
                if str(manifest.get("DeliveryMode", "")) == "direct-replacement-final-mod" and str(manifest.get("PackagedModNameSuffix", "")) == "CHS":
                    final_mod_status = "built_direct_replacement"
            except json.JSONDecodeError:
                final_mod_status = "built_manifest_needs_review"
        if final_mod_status == "built_direct_replacement" and not package_path.is_file():
            final_mod_status = "built_direct_replacement_needs_chs_package"
        if final_mod_status == "built_direct_replacement" and non_gui_gate_status in {"passed", "passed_strict_complete"}:
            final_mod_status = "built_direct_replacement_verified"

    pex_visible_status = "not_applicable"
    pex_visible_evidence = f"{len(pex_files)} .pex files"
    if pex_files:
        if non_gui_gate_status in {"passed", "passed_strict_complete"} and non_gui_gate_final_pex_checked > 0:
            pex_visible_status = "verified_in_final_mod"
            pex_visible_evidence = f"qa/{mod_name}.non_gui_qa_gates.md"
        elif pex_tool_output_files:
            pex_visible_status = "tool_output_needs_verification"
            pex_visible_evidence = f"out/{mod_name}/tool_outputs"
        elif pex_visible_jsonl.exists():
            pex_visible_status = "prepared_tool_required"
            pex_visible_evidence = (
                f"translated/lextranslator_ready/{mod_name}/{pex_lex_ready_files[0].name}"
                if pex_lex_ready_files
                else f"work/normalized/{mod_name}/pex_visible_strings.jsonl"
            )
        else:
            pex_visible_status = "manual_or_tool_required"

    status_rows = [
        ("Workspace", "done" if workspace_files else "missing", f"{len(workspace_files)} files" if workspace_files else "No workspace files found"),
        ("Tool prefs audit", tool_prefs_status, "qa/tool_prefs_audit.md"),
        ("Interface text", "done" if translated_interface_dir.exists() else ("pending" if interface_files else "not_applicable"), f"translated/interface/{mod_name}"),
        ("Text assets", "done" if translated_text_asset_dir.exists() else "pending", f"translated/text_assets/{mod_name}"),
        ("MCM extraction", "done" if mcm_jsonl.exists() else ("pending" if mcm_files else "not_applicable"), f"work/normalized/{mod_name}/mcm_text_candidates.jsonl"),
        ("LexTranslator ready", "prepared" if lextranslator_ready_dir.exists() else ("pending" if plugin_files or pex_files else "not_applicable"), f"translated/lextranslator_ready/{mod_name}"),
        ("xTranslator ready", "prepared" if xtranslator_ready_dir.exists() else ("pending" if plugin_files else "not_applicable"), f"translated/xtranslator_ready/{mod_name}"),
        ("Tool plugin output", plugin_output_status, f"out/{mod_name}/tool_outputs"),
        ("PEX visible strings", pex_visible_status, pex_visible_evidence),
        ("Mechanical proofread", proofread_status, f"qa/{mod_name}.translation_proofread.md"),
        ("Codex model review", model_review_status, f"qa/{mod_name}.model_review.md"),
        ("Non-GUI QA gates", non_gui_gate_status, f"qa/{mod_name}.non_gui_qa_gates.md"),
        ("Archive coverage", archive_coverage_status, f"qa/{mod_name}.archive_coverage.md"),
        ("Final text structure", final_text_structure_status, f"qa/{mod_name}.final_text_structure.md"),
        ("Final text review packet", final_text_review_packet_status, f"qa/{mod_name}.final_text_review_packet.md"),
        ("Final binary review packet", final_binary_review_packet_status, f"qa/{mod_name}.final_binary_review_packet.md"),
        ("Final mod", final_mod_status, relative_path(root, final_mod_dir)),
        ("Packaged CHS mod", "ready" if package_path.is_file() else "missing", relative_path(root, package_path)),
    ]

    lines: list[str] = [
        "# Translation Project Status",
        "",
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if workspace:
        lines.append(f"- Workspace: {relative_path(root, workspace)}")
    lines.extend(["", "## Phase Status", "", "| Phase | Status | Evidence |", "|---|---|---|"])
    for phase, status, evidence in status_rows:
        lines.append(f"| {phase} | {status} | {evidence} |")

    lines.extend(
        [
            "",
            "## Counts",
            "",
            f"- Workspace files: {len(workspace_files)}",
            f"- Plugin files: {len(plugin_files)}",
            f"- PEX files: {len(pex_files)}",
            f"- Interface translation files: {len(interface_files)}",
            f"- MCM files: {len(mcm_files)}",
            f"- BSA/BA2 archive files checked: {archive_files_checked}",
            f"- final_mod text files checked: {final_text_files_checked}",
            f"- final_mod text review items: {final_text_review_items}",
            f"- final_mod binary review items: {final_binary_review_items}",
            "",
            "## Current Gate",
            "",
        ]
    )

    if tool_prefs_status == "risk_found" and non_gui_gate_status in {"passed", "passed_strict_complete"}:
        lines.append("- GUI tool preference audit found risky path markers. This does not block the current non-GUI direct-replacement final_mod, but any GUI save/finalize automation must remain project-local and guarded.")
    elif tool_prefs_status == "risk_found":
        lines.append("- Tool preference audit found risky path markers. GUI save/finalize automation must remain project-local and guarded.")
    if plugin_output_status == "failed_verification":
        lines.append("- Plugin output verification failed. Do not treat ESP/ESM/ESL translation as complete.")
    elif plugin_output_status == "needs_verification":
        lines.append("- Plugin output exists but has not been verified. Run `python scripts/verify_plugin_output.py`.")
    elif plugin_output_status == "pending":
        lines.append("- Plugin output has not been produced yet. Use xTranslator/LexTranslator only with project-local inputs and outputs.")
    if proofread_status == "needs_review":
        lines.append("- Mechanical proofread needs review before writeback or final delivery.")
    if model_review_status != "passed":
        lines.append("- Codex model review is not passed; translation quality gate remains open.")
    if non_gui_gate_status == "failed_or_warned":
        lines.append("- Non-GUI QA gates failed or warned. Do not treat final_mod as complete.")
    if archive_coverage_status in {"failed", "needs_evidence"}:
        lines.append("- Archive coverage is not clean. BSA/BA2 content may hide untranslated resources.")
    if final_text_structure_status in {"failed", "needs_review"}:
        lines.append("- final_mod text structure is not clean. Do not treat direct replacement text files as complete.")
    if final_text_review_packet_status == "needs_review":
        lines.append("- final_mod text review packet contains protected-review items. Model review must resolve them before delivery.")
    if final_binary_review_packet_status == "needs_review":
        lines.append("- final_mod binary review packet contains protected-review items or export failures. Model review must resolve them before delivery.")
    if "direct_replacement" not in final_mod_status:
        lines.append("- Final mod is not confirmed as direct-replacement delivery.")
    if non_gui_gate_strict_complete:
        lines.append("- Strict complete mode passed: missing binary translation tables, unresolved coverage, and warnings are blocking in the latest gate.")
    if plugin_output_status == "verified" and proofread_status == "passed" and model_review_status == "passed" and non_gui_gate_status in {"passed", "passed_strict_complete"}:
        lines.append("No blocking gate is currently detected.")
    elif tool_prefs_status != "risk_found" and plugin_output_status == "not_applicable":
        lines.append("No plugin translation gate is currently required.")

    lines.extend(["", "## Reports", ""])
    if existing_reports:
        lines.extend(f"- {item}" for item in existing_reports)
    else:
        lines.append("No QA reports found.")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Status report written to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
