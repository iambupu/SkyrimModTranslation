from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import project_root, relative_path
from validate_chs_package import validate as validate_chs_package_contents


REQUIRED_MODEL_CLAIMS = (
    "No runtime-impacting issues remain",
    "No required translation candidates remain untranslated",
    "No semantic quality blockers remain",
    "All changed final_mod files listed in the review packets were reviewed",
    "Mechanical checks do not replace Codex model semantic review",
    "Final review quality audit has 0 blocking issues and 0 warnings",
)


@dataclass
class AuditRow:
    ModName: str
    Status: str
    FinalModDir: str
    PackagePath: str
    PackageMatch: str
    DictionaryEntries: int
    FinalTextItems: str
    FinalBinaryItems: str
    FinalReviewQuality: str
    Issues: list[str]


@dataclass
class AuditIssue:
    Severity: str
    ModName: str
    Area: str
    Message: str
    Evidence: str


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        return {"_invalid_json": True}
    return payload if isinstance(payload, dict) else {"_invalid_json": True}


def zero(value: object) -> bool:
    return str(value).strip() in {"0", "0.0"}


def report_metric(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in read_text(path).splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def latest_file_mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_mtime
    latest = path.stat().st_mtime
    for item in path.rglob("*"):
        if item.is_file():
            latest = max(latest, item.stat().st_mtime)
    return latest


def report_covers_artifacts(report: Path, artifacts: list[Path]) -> bool:
    if not report.is_file():
        return False
    if any(not path.exists() for path in artifacts):
        return False
    report_mtime = report.stat().st_mtime
    return all(latest_file_mtime(path) <= report_mtime + 1e-6 for path in artifacts)


def packet_content_reviewed(model_text: str, packet_path: Path) -> bool:
    if not packet_path.is_file():
        return False
    if packet_path.name not in model_text:
        return False
    packet_hash = report_metric(packet_path, "Items SHA256")
    return bool(packet_hash and packet_hash in model_text)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def add_issue(
    issues: list[AuditIssue],
    *,
    mod_name: str,
    area: str,
    message: str,
    evidence: str,
    severity: str = "error",
) -> None:
    issues.append(AuditIssue(severity, mod_name, area, message, evidence))


def changed_files_from_packets(root: Path, mod_name: str) -> set[str]:
    files: set[str] = set()
    for suffix in ("final_text_review_items.jsonl", "final_binary_review_items.jsonl"):
        for row in read_jsonl(root / "qa" / f"{mod_name}.{suffix}"):
            value = row.get("File")
            if isinstance(value, str) and value.strip():
                files.add(value.strip())
    return files


def audit_mod(root: Path, output: dict[str, Any]) -> tuple[AuditRow, list[AuditIssue]]:
    mod_name = str(output.get("ModName", "")).strip()
    issues: list[AuditIssue] = []
    final_mod = root / str(output.get("FinalModDir", ""))
    package = root / str(output.get("PackagedModPath", ""))
    gate = root / "qa" / f"{mod_name}.non_gui_qa_gates.md"
    review = root / "qa" / f"{mod_name}.model_review.md"
    final_text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    final_binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    final_text_items = root / "qa" / f"{mod_name}.final_text_review_items.jsonl"
    final_binary_items = root / "qa" / f"{mod_name}.final_binary_review_items.jsonl"
    final_quality_report = root / "qa" / f"{mod_name}.final_review_quality.md"
    final_quality_json = root / "qa" / f"{mod_name}.final_review_quality.json"
    package_validation_report = root / "qa" / f"{mod_name}.chs_package_validation.md"
    package_validation_json = root / "qa" / f"{mod_name}.chs_package_validation.json"
    manifest_path = final_mod / "meta" / "manifest.json"
    dictionary_manifest_path = root / "out" / mod_name / "汉化产出" / "intermediate" / "translation_text_dictionary" / "manifest.json"
    dictionary_jsonl = root / "out" / mod_name / "汉化产出" / "intermediate" / "translation_text_dictionary" / "translation_dictionary.jsonl"

    if not mod_name:
        add_issue(issues, mod_name=mod_name, area="readiness", message="Known output row has no ModName.", evidence="qa/translation_readiness.json")

    if str(output.get("OverallStatus", "")) != "ready_for_manual_test":
        add_issue(issues, mod_name=mod_name, area="readiness", message="Known output is not ready_for_manual_test.", evidence="qa/translation_readiness.json")
    for key, area in (
        ("WorkflowBlockingIssues", "workflow"),
        ("WorkflowWarnings", "workflow"),
        ("StrictGateBlockingIssues", "strict-gate"),
        ("StrictGateWarnings", "strict-gate"),
        ("CoverageMissing", "coverage"),
        ("CoverageUnverified", "coverage"),
        ("FinalTextProtectedItems", "final-text-review"),
        ("FinalBinaryProtectedItems", "final-binary-review"),
        ("FinalBinaryExportFailures", "final-binary-review"),
    ):
        if not zero(output.get(key, "")):
            add_issue(issues, mod_name=mod_name, area=area, message=f"{key} is not zero.", evidence="qa/translation_readiness.json")

    if str(output.get("ModelReviewStatus", "")) != "passed":
        add_issue(issues, mod_name=mod_name, area="model-review", message="Model review status is not passed.", evidence=f"qa/{mod_name}.model_review.md")

    if not final_mod.is_dir():
        add_issue(issues, mod_name=mod_name, area="final-mod", message="final_mod directory is missing.", evidence=relative_path(root, final_mod))
    if not package.is_file() or not package.name.endswith("_CHS.zip"):
        add_issue(issues, mod_name=mod_name, area="package", message="CHS package is missing or does not use _CHS.zip suffix.", evidence=relative_path(root, package))
    package_rows, package_issues = validate_chs_package_contents(root, mod_name, final_mod, package)
    for package_issue in package_issues:
        add_issue(
            issues,
            mod_name=mod_name,
            area=f"package-{package_issue.Area}",
            message=package_issue.Message,
            evidence=package_issue.Evidence,
        )
    package_validation_payload = read_json(package_validation_json)
    if not package_validation_report.is_file() or not package_validation_json.is_file():
        add_issue(
            issues,
            mod_name=mod_name,
            area="package-validation",
            message="CHS package validation report is missing.",
            evidence=relative_path(root, package_validation_report),
        )
    elif package_validation_payload.get("Status") != "passed" or not zero(package_validation_payload.get("BlockingIssues", "")):
        add_issue(
            issues,
            mod_name=mod_name,
            area="package-validation",
            message="CHS package validation report is not clean.",
            evidence=relative_path(root, package_validation_report),
        )
    elif not report_covers_artifacts(package_validation_report, [final_mod, package]):
        add_issue(
            issues,
            mod_name=mod_name,
            area="package-validation",
            message="CHS package validation report is older than current final_mod or package content.",
            evidence=relative_path(root, package_validation_report),
        )

    gate_text = read_text(gate) if gate.is_file() else ""
    expected_final = relative_path(root, final_mod).replace("/", "\\").lower()
    actual_final = report_metric(gate, "FinalModDir").replace("/", "\\").lower()
    if not gate.is_file():
        add_issue(issues, mod_name=mod_name, area="strict-gate", message="Strict gate report is missing.", evidence=relative_path(root, gate))
    elif actual_final != expected_final:
        add_issue(issues, mod_name=mod_name, area="strict-gate", message="Strict gate final_mod path is stale.", evidence=relative_path(root, gate))
    elif not all(fragment in gate_text for fragment in ("Strict complete mode: True", "Blocking issues: 0", "Warnings: 0", "PASS: Non-GUI QA gates")):
        add_issue(issues, mod_name=mod_name, area="strict-gate", message="Strict gate report is not clean.", evidence=relative_path(root, gate))
    elif not report_covers_artifacts(gate, [final_mod, dictionary_jsonl, dictionary_manifest_path]):
        add_issue(
            issues,
            mod_name=mod_name,
            area="strict-gate",
            message="Strict gate report is older than current final_mod or translation dictionary content.",
            evidence=relative_path(root, gate),
        )

    manifest = read_json(manifest_path)
    if not manifest:
        add_issue(issues, mod_name=mod_name, area="manifest", message="final_mod manifest is missing or invalid.", evidence=relative_path(root, manifest_path))
    else:
        expected = {
            "DeliveryMode": "direct-replacement-final-mod",
            "OutputLayout": "mod-root/localization-output/final_mod-intermediate-package",
            "PackagedModNameSuffix": "CHS",
        }
        for key, value in expected.items():
            if str(manifest.get(key, "")) != value:
                add_issue(issues, mod_name=mod_name, area="manifest", message=f"Manifest {key} is not {value}.", evidence=relative_path(root, manifest_path))
        if int(manifest.get("TranslationDictionaryEntryCount", 0) or 0) <= 0:
            add_issue(issues, mod_name=mod_name, area="manifest", message="Manifest has no translation dictionary entries.", evidence=relative_path(root, manifest_path))

    dictionary = read_json(dictionary_manifest_path)
    dictionary_count = int(dictionary.get("TranslatedEntryCount", 0) or 0) if dictionary else 0
    dictionary_lines = len(read_jsonl(dictionary_jsonl))
    if not dictionary_manifest_path.is_file() or not dictionary_jsonl.is_file():
        add_issue(issues, mod_name=mod_name, area="dictionary", message="Translation text dictionary is missing.", evidence=relative_path(root, dictionary_jsonl))
    elif dictionary_count <= 0 or dictionary_lines != dictionary_count:
        add_issue(issues, mod_name=mod_name, area="dictionary", message="Translation text dictionary count is empty or inconsistent.", evidence=relative_path(root, dictionary_jsonl))

    binary_text = read_text(final_binary_packet) if final_binary_packet.is_file() else ""
    text_packet_text = read_text(final_text_packet) if final_text_packet.is_file() else ""
    if "Protected review items: 0" not in binary_text or "Export failures: 0" not in binary_text:
        add_issue(issues, mod_name=mod_name, area="final-binary-review", message="Final binary packet is not clean.", evidence=relative_path(root, final_binary_packet))
    if "Protected review items: 0" not in text_packet_text:
        add_issue(issues, mod_name=mod_name, area="final-text-review", message="Final text packet is not clean.", evidence=relative_path(root, final_text_packet))

    final_quality = read_json(final_quality_json)
    if not final_quality_report.is_file() or not final_quality_json.is_file():
        add_issue(
            issues,
            mod_name=mod_name,
            area="final-review-quality",
            message="Final review quality audit report is missing.",
            evidence=relative_path(root, final_quality_report),
        )
    elif (
        final_quality.get("Status") != "passed"
        or not zero(final_quality.get("BlockingIssues", ""))
        or not zero(final_quality.get("Warnings", ""))
    ):
        add_issue(
            issues,
            mod_name=mod_name,
            area="final-review-quality",
            message="Final review quality audit is not clean.",
            evidence=relative_path(root, final_quality_report),
        )
    elif not report_covers_artifacts(final_quality_report, [final_text_items, final_binary_items]):
        add_issue(
            issues,
            mod_name=mod_name,
            area="final-review-quality",
            message="Final review quality audit is older than current final review item files.",
            evidence=relative_path(root, final_quality_report),
        )

    review_text = read_text(review) if review.is_file() else ""
    if "Reviewer: Codex model" not in review_text:
        add_issue(issues, mod_name=mod_name, area="model-review", message="Model review is missing Codex reviewer marker.", evidence=relative_path(root, review))
    for claim in REQUIRED_MODEL_CLAIMS:
        if claim not in review_text:
            add_issue(issues, mod_name=mod_name, area="model-review", message=f"Model review is missing required claim: {claim}", evidence=relative_path(root, review))
    for packet_name in (f"{mod_name}.final_text_review_packet.md", f"{mod_name}.final_binary_review_packet.md"):
        if packet_name not in review_text:
            add_issue(issues, mod_name=mod_name, area="model-review", message=f"Model review does not mention {packet_name}.", evidence=relative_path(root, review))
    for packet in (final_text_packet, final_binary_packet):
        if not packet_content_reviewed(review_text, packet):
            add_issue(
                issues,
                mod_name=mod_name,
                area="model-review",
                message=f"Model review does not include the current packet Items SHA256: {packet.name}",
                evidence=relative_path(root, review),
            )
    for changed_file in changed_files_from_packets(root, mod_name):
        if changed_file not in review_text:
            add_issue(issues, mod_name=mod_name, area="model-review", message=f"Model review does not mention changed file: {changed_file}", evidence=relative_path(root, review))

    row = AuditRow(
        ModName=mod_name,
        Status="passed" if not issues else "failed",
        FinalModDir=relative_path(root, final_mod),
        PackagePath=relative_path(root, package),
        PackageMatch=f"matched:{len(package_rows)}" if not package_issues else "failed",
        DictionaryEntries=dictionary_count,
        FinalTextItems=str(output.get("FinalTextReviewItems", "")),
        FinalBinaryItems=str(output.get("FinalBinaryReviewItems", "")),
        FinalReviewQuality=f"{final_quality.get('Status', 'missing')} / B:{final_quality.get('BlockingIssues', 'missing')} W:{final_quality.get('Warnings', 'missing')}",
        Issues=[issue.Message for issue in issues],
    )
    return row, issues


def write_reports(root: Path, report_path: Path, json_path: Path, rows: list[AuditRow], issues: list[AuditIssue]) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    lines = [
        "# Project Completion Audit",
        "",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Mods audited: {len(rows)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Verdict",
        "",
        "PASS: All known Mod outputs satisfy the strict project-local completion audit." if blocking == 0 else "FAIL: Completion audit has blocking issues.",
        "",
        "## Scope",
        "",
        "This audit proves project-local delivery evidence only. It does not prove real Skyrim/MO2/Vortex runtime behavior; use `qa/translation_goal_compliance.md` and recorded manual game test results for the full objective.",
        "",
        "## Mod Results",
        "",
        "| ModName | Status | Package match | Dictionary | Text items | Binary items | Final review quality | final_mod | CHS package |",
        "|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {markdown_cell(row.ModName)} | {row.Status} | {markdown_cell(row.PackageMatch)} | {row.DictionaryEntries} | {markdown_cell(row.FinalTextItems)} | "
            f"{markdown_cell(row.FinalBinaryItems)} | {markdown_cell(row.FinalReviewQuality)} | {markdown_cell(row.FinalModDir)} | {markdown_cell(row.PackagePath)} |"
        )
    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No completion audit issues.")
    else:
        lines.extend(["| Severity | ModName | Area | Message | Evidence |", "|---|---|---|---|---|"])
        for issue in issues:
            lines.append(
                f"| {issue.Severity} | {markdown_cell(issue.ModName)} | {issue.Area} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |"
            )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This audit reads project-local QA reports and final_mod metadata only.",
            "- This audit does not write plugin or PEX binaries.",
            "- Real Skyrim, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "ProjectRoot": str(root),
                "CheckedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "BlockingIssues": blocking,
                "Warnings": warnings,
                "Verdict": "PASS" if blocking == 0 else "FAIL",
                "Rows": [asdict(row) for row in rows],
                "Issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def audit_manual_game_test_plan(root: Path, issues: list[AuditIssue]) -> None:
    plan_path = root / "qa" / "manual_game_test_plan.md"
    plan_json_path = root / "qa" / "manual_game_test_plan.json"
    payload = read_json(plan_json_path)
    if not plan_path.is_file() or not plan_json_path.is_file():
        add_issue(
            issues,
            mod_name="*",
            area="manual-game-test",
            message="Manual game test plan is missing.",
            evidence="qa/manual_game_test_plan.md",
        )
        return
    rows = payload.get("Rows", [])
    if payload.get("Status") != "pending_manual_game_test" or not isinstance(rows, list) or not rows:
        add_issue(
            issues,
            mod_name="*",
            area="manual-game-test",
            message="Manual game test plan is empty or has an unexpected status.",
            evidence="qa/manual_game_test_plan.json",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit all known Skyrim translation outputs for strict completion evidence.")
    parser.add_argument("--report-output-path", default="qa/project_completion_audit.md")
    parser.add_argument("--json-output-path", default="qa/project_completion_audit.json")
    args = parser.parse_args()

    root = project_root()
    readiness = read_json(root / "qa" / "translation_readiness.json")
    health = read_json(root / "qa" / "workflow_health.json")
    rows: list[AuditRow] = []
    issues: list[AuditIssue] = []

    if readiness.get("OverallStatus") != "ready_for_manual_test" or not zero(readiness.get("BlockingIssues", "")) or not zero(readiness.get("Warnings", "")):
        add_issue(issues, mod_name="*", area="readiness", message="Project readiness is not clean.", evidence="qa/translation_readiness.json")
    if health.get("Verdict") != "PASS" or not zero(health.get("BlockingIssues", "")) or not zero(health.get("Warnings", "")):
        add_issue(issues, mod_name="*", area="workflow-health", message="Workflow health is not clean.", evidence="qa/workflow_health.json")

    outputs = readiness.get("KnownModOutputs", [])
    if not isinstance(outputs, list) or not outputs:
        add_issue(issues, mod_name="*", area="readiness", message="No known Mod outputs found.", evidence="qa/translation_readiness.json")
    else:
        for output in outputs:
            if not isinstance(output, dict):
                continue
            row, mod_issues = audit_mod(root, output)
            rows.append(row)
            issues.extend(mod_issues)
    audit_manual_game_test_plan(root, issues)

    report_path = root / args.report_output_path
    json_path = root / args.json_output_path
    write_reports(root, report_path, json_path, rows, issues)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Project completion audit written to: {report_path}")
    print(f"Project completion audit JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
