"""Audit whether the project-local proofreading objective is complete.

Runtime playtesting is deliberately separated: player evidence may be validated
when present, but missing real-game evidence does not block project-local
translation proofreading completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, project_root, relative_path, resolve_project_path
from validate_chs_package import sha256_file


REQUIRED_MODEL_CLAIMS = (
    "No runtime-impacting issues remain",
    "No required translation candidates remain untranslated",
    "No semantic quality blockers remain",
    "All changed final_mod files listed in the review packets were reviewed",
    "Mechanical checks do not replace Codex model semantic review",
    "Final review quality audit has 0 blocking issues and 0 warnings",
)


@dataclass
class ModGoalRow:
    ModName: str
    StaticQaStatus: str
    RuntimeStatus: str
    Coverage: str
    ModelReview: str
    FinalReviewQuality: str
    DictionaryEntries: int
    PackageValidation: str
    FinalTextItems: str
    FinalBinaryItems: str
    Issues: list[str]


@dataclass
class ObjectiveRow:
    Requirement: str
    Status: str
    Evidence: str
    RemainingEvidence: str


@dataclass
class GoalIssue:
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


def zero(value: object) -> bool:
    return str(value).strip() in {"0", "0.0"}


def report_metric(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    prefix = f"- {name}:"
    for line in read_text(path).splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def to_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


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


def report_not_older_than(report: Path, dependencies: list[Path]) -> bool:
    if not report.is_file():
        return False
    if any(not path.exists() for path in dependencies):
        return False
    report_mtime = report.stat().st_mtime
    return all(latest_file_mtime(path) <= report_mtime + 1e-6 for path in dependencies)


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def add_issue(
    issues: list[GoalIssue],
    *,
    mod_name: str,
    area: str,
    message: str,
    evidence: str,
    severity: str = "error",
) -> None:
    issues.append(GoalIssue(severity, mod_name, area, message, evidence))


def translation_dictionary_status(root: Path, mod_name: str) -> tuple[int, list[str]]:
    dictionary_dir = root / "out" / mod_name / "汉化产出" / "intermediate" / "translation_text_dictionary"
    manifest_path = dictionary_dir / "manifest.json"
    dictionary_jsonl = dictionary_dir / "translation_dictionary.jsonl"
    issues: list[str] = []
    manifest_entries = 0

    if not dictionary_dir.is_dir():
        return 0, ["translation dictionary directory missing"]
    manifest = read_json(manifest_path)
    if not manifest:
        issues.append("translation dictionary manifest missing")
    elif manifest.get("_invalid_json"):
        issues.append("translation dictionary manifest invalid")
    else:
        manifest_entries = to_int(manifest.get("TranslatedEntryCount", 0))
        if manifest_entries <= 0:
            issues.append("translation dictionary manifest has no entries")

    if not dictionary_jsonl.is_file():
        issues.append("translation dictionary jsonl missing")
        return 0, issues

    line_count = 0
    invalid_rows = 0
    translated_rows = 0
    for line in read_text(dictionary_jsonl).splitlines():
        if not line.strip():
            continue
        line_count += 1
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            invalid_rows += 1
            continue
        if not isinstance(payload, dict):
            invalid_rows += 1
            continue
        source = str(payload.get("source", "")).strip()
        target = str(payload.get("target", "")).strip()
        if source and target and source != target:
            translated_rows += 1

    if invalid_rows:
        issues.append("translation dictionary jsonl invalid")
    if line_count <= 0:
        issues.append("translation dictionary jsonl empty")
    if manifest_entries and line_count != manifest_entries:
        issues.append("translation dictionary line count differs from manifest")
    if translated_rows <= 0:
        issues.append("translation dictionary has no translated source-target entries")
    return translated_rows, issues


def packet_content_reviewed(model_text: str, packet_path: Path) -> bool:
    if not packet_path.is_file():
        return False
    if packet_path.name not in model_text:
        return False
    packet_hash = report_metric(packet_path, "Items SHA256")
    return bool(packet_hash and packet_hash in model_text)


def changed_files_from_packets(root: Path, mod_name: str) -> set[str]:
    files: set[str] = set()
    for suffix in ("final_text_review_items.jsonl", "final_binary_review_items.jsonl"):
        for row in read_jsonl(root / "qa" / f"{mod_name}.{suffix}"):
            file_value = row.get("File")
            if isinstance(file_value, str) and file_value.strip():
                files.add(file_value.strip())
    return files


def final_review_quality_rows(root: Path, mod_name: str) -> int:
    payload = read_json(root / "qa" / f"{mod_name}.final_review_quality.json")
    return to_int(payload.get("RowsChecked", 0))


def final_review_quality_status(root: Path, mod_name: str) -> tuple[str, str, str, bool]:
    report_path = root / "qa" / f"{mod_name}.final_review_quality.md"
    json_path = root / "qa" / f"{mod_name}.final_review_quality.json"
    text_items = root / "qa" / f"{mod_name}.final_text_review_items.jsonl"
    binary_items = root / "qa" / f"{mod_name}.final_binary_review_items.jsonl"
    if not report_path.is_file() or not json_path.is_file():
        return "missing", "missing", "missing", False
    payload = read_json(json_path)
    if payload.get("_invalid_json"):
        return "invalid_json", "invalid_json", "invalid_json", False
    status = str(payload.get("Status", "")).strip() or "unknown"
    blocking = str(payload.get("BlockingIssues", "")).strip() or "?"
    warnings = str(payload.get("Warnings", "")).strip() or "?"
    current = report_not_older_than(report_path, [text_items, binary_items])
    return status, blocking, warnings, current


def set_from_rows(rows: object) -> set[str]:
    if not isinstance(rows, list):
        return set()
    result: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            mod_name = str(row.get("ModName", "")).strip()
            if mod_name:
                result.add(mod_name)
    return result


def row_by_mod(rows: object) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mod_name = str(row.get("ModName", "")).strip()
        if mod_name:
            result[mod_name] = row
    return result


def runtime_statuses(results: dict[str, Any]) -> dict[str, str]:
    rows = results.get("Rows", [])
    if results.get("Status") != "passed" or not zero(results.get("BlockingIssues", "")) or not isinstance(rows, list):
        return {}
    statuses: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mod_name = str(row.get("ModName", "")).strip()
        status = str(row.get("ValidationStatus", "")).strip()
        if mod_name:
            statuses[mod_name] = status
    return statuses


def artifact_manifest_sha_from_records(artifacts: list[dict[str, Any]]) -> str:
    if not artifacts:
        return ""
    normalized: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        normalized.append(
            {
                "CheckName": str(artifact.get("CheckName", "")),
                "Path": str(artifact.get("Path", "")),
                "Sha256": str(artifact.get("Sha256", "")),
                "SizeBytes": to_int(artifact.get("SizeBytes", 0)),
            }
        )
    if not normalized:
        return ""
    data = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_manual_validation_evidence(root: Path, validation: dict[str, Any], issues: list[GoalIssue]) -> list[Path]:
    # Manual game evidence is accepted only after the player records artifacts
    # under qa/manual_game_test_artifacts/. The script verifies hashes; it does
    # not perform game testing itself.
    rows = validation.get("Rows", [])
    artifact_paths: list[Path] = []
    if not isinstance(rows, list) or not rows:
        add_issue(
            issues,
            mod_name="*",
            area="manual-game-test",
            message="Manual game test validation has no rows.",
            evidence="qa/manual_game_test_results_validation.json",
        )
        return artifact_paths

    artifact_root = root / "qa" / "manual_game_test_artifacts"
    for row in rows:
        if not isinstance(row, dict):
            continue
        mod_name = str(row.get("ModName", "")).strip() or "*"
        artifacts = row.get("Artifacts", [])
        if not isinstance(artifacts, list) or not artifacts:
            add_issue(
                issues,
                mod_name=mod_name,
                area="manual-game-test",
                message="Manual game test validation row has no recorded evidence artifact hashes.",
                evidence="qa/manual_game_test_results_validation.json",
            )
            continue

        expected_manifest = artifact_manifest_sha_from_records([artifact for artifact in artifacts if isinstance(artifact, dict)])
        recorded_manifest = str(row.get("ArtifactManifestSha256", "")).strip()
        if not expected_manifest or recorded_manifest.lower() != expected_manifest.lower():
            add_issue(
                issues,
                mod_name=mod_name,
                area="manual-game-test",
                message="Manual game test artifact manifest hash does not match the recorded artifact list.",
                evidence="qa/manual_game_test_results_validation.json",
            )

        seen_paths: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact record is not an object.",
                    evidence="qa/manual_game_test_results_validation.json",
                )
                continue
            rel = str(artifact.get("Path", "")).strip()
            if not rel:
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact record has no path.",
                    evidence="qa/manual_game_test_results_validation.json",
                )
                continue
            try:
                path = resolve_project_path(root, rel, must_exist=True)
            except Exception as exc:
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message=f"Manual game test artifact cannot be read: {exc}",
                    evidence=rel,
                )
                continue
            if not is_under(path, artifact_root):
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact is outside qa/manual_game_test_artifacts.",
                    evidence=rel,
                )
                continue
            if not path.is_file():
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact path is not a file.",
                    evidence=rel,
                )
                continue
            lowered = relative_path(root, path).lower()
            if lowered in seen_paths:
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact is duplicated in the validation record.",
                    evidence=rel,
                )
                continue
            seen_paths.add(lowered)
            artifact_paths.append(path)
            expected_sha = str(artifact.get("Sha256", "")).strip().lower()
            current_sha = sha256_file(path).lower()
            if expected_sha != current_sha:
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact SHA256 no longer matches the validation report.",
                    evidence=rel,
                )
            expected_size = to_int(artifact.get("SizeBytes", 0))
            if expected_size != path.stat().st_size:
                add_issue(
                    issues,
                    mod_name=mod_name,
                    area="manual-game-test",
                    message="Manual game test artifact size no longer matches the validation report.",
                    evidence=rel,
                )

    if not artifact_paths:
        add_issue(
            issues,
            mod_name="*",
            area="manual-game-test",
            message="Manual game test validation does not record any readable evidence artifacts.",
            evidence="qa/manual_game_test_results_validation.json",
        )
    return artifact_paths


def model_review_clean(root: Path, mod_name: str) -> bool:
    # Treat model review as stale unless it names the final review packets,
    # includes their hashes, lists changed files, and covers the quality audit.
    review_path = root / "qa" / f"{mod_name}.model_review.md"
    if not review_path.is_file():
        return False
    text = read_text(review_path)
    final_text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    final_binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    final_quality_report = root / "qa" / f"{mod_name}.final_review_quality.md"
    rows_checked = final_review_quality_rows(root, mod_name)
    return (
        "Reviewer: Codex model" in text
        and all(claim in text for claim in REQUIRED_MODEL_CLAIMS)
        and packet_content_reviewed(text, final_text_packet)
        and packet_content_reviewed(text, final_binary_packet)
        and all(changed_file in text for changed_file in changed_files_from_packets(root, mod_name))
        and final_quality_report.name in text
        and rows_checked > 0
        and str(rows_checked) in text
    )


def audit_mod(root: Path, output: dict[str, Any], runtime_by_mod: dict[str, str]) -> ModGoalRow:
    mod_name = str(output.get("ModName", "")).strip()
    issues: list[str] = []
    coverage = f"Missing:{output.get('CoverageMissing', '')} Unverified:{output.get('CoverageUnverified', '')}"
    dictionary_entries, dictionary_issues = translation_dictionary_status(root, mod_name)
    quality_status, quality_blocking, quality_warnings, quality_current = final_review_quality_status(root, mod_name)
    final_review_quality = f"{quality_status} / B:{quality_blocking} W:{quality_warnings}"
    package_path = root / str(output.get("PackagedModPath", ""))
    package_sha = sha256_file(package_path) if package_path.is_file() else ""

    static_checks = {
        "workflow blocking": zero(output.get("WorkflowBlockingIssues", "")),
        "workflow warnings": zero(output.get("WorkflowWarnings", "")),
        "strict gate blocking": zero(output.get("StrictGateBlockingIssues", "")),
        "strict gate warnings": zero(output.get("StrictGateWarnings", "")),
        "coverage missing": zero(output.get("CoverageMissing", "")),
        "coverage unverified": zero(output.get("CoverageUnverified", "")),
        "final text protected": zero(output.get("FinalTextProtectedItems", "")),
        "final binary protected": zero(output.get("FinalBinaryProtectedItems", "")),
        "final binary export failures": zero(output.get("FinalBinaryExportFailures", "")),
        "final review quality": quality_status == "passed" and zero(quality_blocking) and zero(quality_warnings),
        "final review quality freshness": quality_current,
        "readiness final review quality": str(output.get("FinalReviewQualityStatus", "")) == "passed"
        and zero(output.get("FinalReviewQualityBlockingIssues", ""))
        and zero(output.get("FinalReviewQualityWarnings", "")),
        "model review status": str(output.get("ModelReviewStatus", "")) == "passed",
        "model review current packet contract": model_review_clean(root, mod_name),
        "translation dictionary": dictionary_entries > 0 and not dictionary_issues,
        "readiness dictionary": str(output.get("TranslationDictionaryStatus", "")) == "present" and to_int(output.get("TranslationDictionaryEntries", 0)) == dictionary_entries,
        "package validation": str(output.get("PackageValidationStatus", "")) == "passed" and zero(output.get("PackageValidationBlockingIssues", "")),
        "package hash": bool(package_sha) and str(output.get("PackageValidationSha256", "")).lower() == package_sha.lower(),
        "final_mod exists": bool(output.get("FinalModExists")),
        "CHS package exists": bool(output.get("PackagedModExists")),
    }
    issues.extend(dictionary_issues)
    for name, passed in static_checks.items():
        if not passed:
            issues.append(name)

    # Runtime validation can strengthen confidence, but the proofreading goal is
    # bounded to project-local evidence. Missing player evidence remains
    # out-of-scope instead of turning a clean static QA row into a failure.
    runtime_status = runtime_by_mod.get(mod_name, "out_of_scope_player_validation_pending")
    if runtime_status != "passed":
        runtime_status = "out_of_scope_player_validation_pending"

    return ModGoalRow(
        ModName=mod_name,
        StaticQaStatus="passed" if not issues else "failed",
        RuntimeStatus=runtime_status,
        Coverage=coverage,
        ModelReview=str(output.get("ModelReviewStatus", "")),
        FinalReviewQuality=final_review_quality,
        DictionaryEntries=dictionary_entries,
        PackageValidation=f"{output.get('PackageValidationStatus', '')} / B:{output.get('PackageValidationBlockingIssues', '')}",
        FinalTextItems=str(output.get("FinalTextReviewItems", "")),
        FinalBinaryItems=str(output.get("FinalBinaryReviewItems", "")),
        Issues=issues,
    )


def build_objective_rows(rows: list[ModGoalRow], project_static_clean: bool, runtime_complete: bool) -> list[ObjectiveRow]:
    # These rows are worded to prevent the common mistake of equating
    # "not game-tested yet" with "proofreading workflow failed".
    static_mods_clean = all(row.StaticQaStatus == "passed" for row in rows)
    return [
        ObjectiveRow(
            Requirement="Strict proofreading after every translation batch",
            Status="passed" if project_static_clean and static_mods_clean else "failed",
            Evidence="qa/*.non_gui_qa_gates.md, qa/*.final_review_quality.md, qa/*.model_review.md, qa/workflow_health.md, qa/project_completion_audit.md",
            RemainingEvidence="None for project-local static QA.",
        ),
        ObjectiveRow(
            Requirement="All translatable files are read and checked",
            Status="passed" if static_mods_clean else "failed",
            Evidence="qa/*.final_text_review_items.jsonl, qa/*.final_binary_review_items.jsonl, coverage Missing:0 Unverified:0",
            RemainingEvidence="None for project-local file review. Player-operated in-game visibility checks are external validation.",
        ),
        ObjectiveRow(
            Requirement="No required translation candidates remain untranslated",
            Status="passed" if static_mods_clean else "failed",
            Evidence="coverage Missing:0 Unverified:0 and model claim 'No required translation candidates remain untranslated'",
            RemainingEvidence="None for project-local extracted candidates.",
        ),
        ObjectiveRow(
            Requirement="No semantic quality blockers remain",
            Status="passed" if static_mods_clean else "failed",
            Evidence="qa/*.final_review_quality.md plus qa/*.model_review.md with Codex reviewer marker and required semantic quality claim",
            RemainingEvidence="None for proofreading workflow. Player-operated in-game reading can still find presentation issues as external follow-up.",
        ),
        ObjectiveRow(
            Requirement="No runtime-impacting issues remain",
            Status="passed" if static_mods_clean and project_static_clean else "failed",
            Evidence="Project-local protected-token, structure, ESP/PEX export, package, and strict gate checks are clean.",
            RemainingEvidence="None for project-local proofreading workflow. Player-operated real game validation is external evidence.",
        ),
        ObjectiveRow(
            Requirement="Player-operated real game validation",
            Status="recorded" if runtime_complete else "out_of_scope_for_proofreading_workflow",
            Evidence="qa/manual_game_test_plan.md and qa/manual_game_test_results.template.json bind the player test workflow to current CHS packages.",
            RemainingEvidence="Optional external player evidence; not required for proofreading workflow completion.",
        ),
        ObjectiveRow(
            Requirement="Intermediate output contains a translation text dictionary",
            Status="passed" if all(row.DictionaryEntries > 0 for row in rows) else "failed",
            Evidence="out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl",
            RemainingEvidence="None.",
        ),
    ]


def write_reports(
    root: Path,
    report_path: Path,
    json_path: Path,
    rows: list[ModGoalRow],
    objectives: list[ObjectiveRow],
    issues: list[GoalIssue],
    runtime_pending: int,
) -> None:
    static_blockers = sum(1 for issue in issues if issue.Severity == "error")
    status = "project_local_blocked" if static_blockers else "complete"
    lines = [
        "# Translation Goal Compliance Audit",
        "",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Overall goal status: {status}",
        f"- Project-local blocking issues: {static_blockers}",
        f"- Player runtime validations pending (out of scope): {runtime_pending}",
        "",
        "## Verdict",
        "",
    ]
    if status == "complete":
        lines.append("PASS: The project-local proofreading and translation workflow objective is proven by strict QA. Player-operated real game/MO2/Vortex validation is external follow-up evidence, not a proofreading workflow blocker.")
    else:
        lines.append("FAIL: Project-local evidence has blocking issues.")

    lines.extend(
        [
            "",
            "## Objective Matrix",
            "",
            "| Requirement | Status | Evidence | Remaining evidence |",
            "|---|---|---|---|",
        ]
    )
    for row in objectives:
        lines.append(
            f"| {markdown_cell(row.Requirement)} | {markdown_cell(row.Status)} | {markdown_cell(row.Evidence)} | {markdown_cell(row.RemainingEvidence)} |"
        )

    lines.extend(
        [
            "",
            "## Mod Evidence",
            "",
            "| ModName | Static QA | Runtime | Coverage | Model review | Final review quality | Dictionary | Package validation | Text items | Binary items | Issues |",
            "|---|---|---|---|---|---|---:|---|---:|---:|---|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {markdown_cell(row.ModName)} | {row.StaticQaStatus} | {row.RuntimeStatus} | {markdown_cell(row.Coverage)} | "
            f"{markdown_cell(row.ModelReview)} | {markdown_cell(row.FinalReviewQuality)} | {row.DictionaryEntries} | "
            f"{markdown_cell(row.PackageValidation)} | {markdown_cell(row.FinalTextItems)} | {markdown_cell(row.FinalBinaryItems)} | "
            f"{markdown_cell('; '.join(row.Issues) if row.Issues else 'none')} |"
        )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No project-local goal compliance issues.")
    else:
        lines.extend(["| Severity | ModName | Area | Message | Evidence |", "|---|---|---|---|---|"])
        for issue in issues:
            lines.append(
                f"| {issue.Severity} | {markdown_cell(issue.ModName)} | {issue.Area} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |"
            )

    lines.extend(
        [
            "",
            "## Runtime Validation Contract",
            "",
            "- This audit does not access the real game, Steam, MO2, Vortex, AppData, or Documents/My Games paths.",
            "- Player-operated real game validation is outside the proofreading workflow completion boundary.",
            "- If the player wants runtime evidence recorded, the player fills `qa/manual_game_test_results.json` from `qa/manual_game_test_results.template.json` after testing each CHS package in game.",
            "- Codex must not operate the real game or mod manager paths; Codex only validates the player-provided project-local evidence afterward.",
            "- Then run `python .\\scripts\\validate_manual_game_test_results.py`; this audit only trusts a current `qa/manual_game_test_results_validation.json` whose evidence artifact hashes still match, but missing player evidence does not block proofreading workflow completion.",
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
                "OverallGoalStatus": status,
                "ProjectLocalBlockingIssues": static_blockers,
                "RuntimeValidationsPending": runtime_pending,
                "PlayerRuntimeValidationsPending": runtime_pending,
                "RuntimeValidationScope": "out_of_scope_for_proofreading_workflow",
                "Rows": [asdict(row) for row in rows],
                "Objectives": [asdict(row) for row in objectives],
                "Issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit translation outputs against the user's strict quality objective.")
    parser.add_argument("--report-output-path", default="qa/translation_goal_compliance.md")
    parser.add_argument("--json-output-path", default="qa/translation_goal_compliance.json")
    parser.add_argument("--require-runtime-complete", action="store_true")
    args = parser.parse_args()

    root = project_root()
    readiness_path = root / "qa" / "translation_readiness.json"
    project_completion_path = root / "qa" / "project_completion_audit.json"
    health_path = root / "qa" / "workflow_health.json"
    manual_plan_path = root / "qa" / "manual_game_test_plan.json"
    manual_template_path = root / "qa" / "manual_game_test_results.template.json"
    manual_results_path = root / "qa" / "manual_game_test_results.json"
    manual_validation_path = root / "qa" / "manual_game_test_results_validation.json"
    readiness = read_json(readiness_path)
    project_completion = read_json(project_completion_path)
    health = read_json(health_path)
    manual_plan = read_json(manual_plan_path)
    manual_template = read_json(manual_template_path)
    manual_validation = read_json(manual_validation_path)
    runtime_by_mod: dict[str, str] = {}
    issues: list[GoalIssue] = []

    if readiness.get("OverallStatus") != "ready_for_manual_test" or not zero(readiness.get("BlockingIssues", "")) or not zero(readiness.get("Warnings", "")):
        add_issue(issues, mod_name="*", area="readiness", message="Translation readiness is not clean.", evidence="qa/translation_readiness.json")
    if project_completion.get("Verdict") != "PASS" or not zero(project_completion.get("BlockingIssues", "")) or not zero(project_completion.get("Warnings", "")):
        add_issue(issues, mod_name="*", area="project-completion", message="Project completion audit is not clean.", evidence="qa/project_completion_audit.json")
    if health.get("Verdict") != "PASS" or not zero(health.get("BlockingIssues", "")) or not zero(health.get("Warnings", "")):
        add_issue(issues, mod_name="*", area="workflow-health", message="Workflow health is not clean.", evidence="qa/workflow_health.json")
    if manual_plan.get("Status") != "pending_manual_game_test":
        add_issue(issues, mod_name="*", area="manual-game-test", message="Manual game test plan is missing or has an unexpected status.", evidence="qa/manual_game_test_plan.json")
    if not report_not_older_than(project_completion_path, [readiness_path, health_path]):
        add_issue(issues, mod_name="*", area="evidence-freshness", message="Project completion audit is older than readiness or workflow health evidence.", evidence="qa/project_completion_audit.json")
    if not report_not_older_than(manual_plan_path, [readiness_path]):
        add_issue(issues, mod_name="*", area="manual-game-test", message="Manual game test plan is older than current readiness evidence.", evidence="qa/manual_game_test_plan.json")
    if not report_not_older_than(manual_template_path, [manual_plan_path]):
        add_issue(issues, mod_name="*", area="manual-game-test", message="Manual game test result template is older than the manual test plan.", evidence="qa/manual_game_test_results.template.json")

    if manual_validation.get("Status") == "passed" and zero(manual_validation.get("BlockingIssues", "")):
        manual_validation_issue_count = len(issues)
        artifact_paths = validate_manual_validation_evidence(root, manual_validation, issues)
        required_manual_dependencies = [manual_results_path, manual_plan_path, manual_template_path]
        missing_dependencies = [relative_path(root, path) for path in required_manual_dependencies if not path.is_file()]
        manual_validation_current = False
        if missing_dependencies:
            add_issue(
                issues,
                mod_name="*",
                area="manual-game-test",
                message=f"Manual game test validation depends on missing files: {', '.join(missing_dependencies)}",
                evidence="qa/manual_game_test_results_validation.json",
            )
        else:
            manual_validation_current = report_not_older_than(manual_validation_path, required_manual_dependencies + artifact_paths)
            if not manual_validation_current:
                add_issue(
                    issues,
                    mod_name="*",
                    area="manual-game-test",
                    message="Manual game test validation is older than the current results, plan, template, or evidence artifacts.",
                    evidence="qa/manual_game_test_results_validation.json",
                )
        if len(issues) == manual_validation_issue_count and manual_validation_current:
            runtime_by_mod = runtime_statuses(manual_validation)

    outputs = readiness.get("KnownModOutputs", [])
    if not isinstance(outputs, list) or not outputs:
        add_issue(issues, mod_name="*", area="readiness", message="No known Mod outputs found.", evidence="qa/translation_readiness.json")
        outputs = []
    output_mods = set_from_rows(outputs)
    # Manual game testing is only generated for deliverables that passed static QA.
    # Blocked outputs remain in project-completion scope, but are not test-plan omissions.
    ready_output_mods = {
        str(output.get("ModName", "")).strip()
        for output in outputs
        if isinstance(output, dict)
        and str(output.get("ModName", "")).strip()
        and output.get("OverallStatus") == "ready_for_manual_test"
    }
    completion_mods = set_from_rows(project_completion.get("Rows", []))
    plan_mods = set_from_rows(manual_plan.get("Rows", []))
    template_mods = set_from_rows(manual_template.get("Rows", []))
    if completion_mods != output_mods:
        add_issue(
            issues,
            mod_name="*",
            area="evidence-scope",
            message="Project completion Mod list does not match readiness outputs.",
            evidence="qa/project_completion_audit.json",
        )
    for label, mods, evidence in (
        ("manual game test plan", plan_mods, "qa/manual_game_test_plan.json"),
        ("manual game test result template", template_mods, "qa/manual_game_test_results.template.json"),
    ):
        if mods != ready_output_mods:
            add_issue(
                issues,
                mod_name="*",
                area="evidence-scope",
                message=f"{label} Mod list does not match ready-for-manual-test readiness outputs.",
                evidence=evidence,
            )

    completion_by_mod = row_by_mod(project_completion.get("Rows", []))
    for output in outputs:
        if not isinstance(output, dict):
            continue
        mod_name = str(output.get("ModName", "")).strip()
        completion_row = completion_by_mod.get(mod_name, {})
        if completion_row:
            if str(completion_row.get("Status", "")) != "passed":
                add_issue(issues, mod_name=mod_name, area="project-completion", message="Project completion row is not passed.", evidence="qa/project_completion_audit.json")
            if str(completion_row.get("PackagePath", "")) != str(output.get("PackagedModPath", "")):
                add_issue(issues, mod_name=mod_name, area="project-completion", message="Project completion package path differs from readiness.", evidence="qa/project_completion_audit.json")
            if to_int(completion_row.get("DictionaryEntries", 0)) != to_int(output.get("TranslationDictionaryEntries", 0)):
                add_issue(issues, mod_name=mod_name, area="project-completion", message="Project completion dictionary count differs from readiness.", evidence="qa/project_completion_audit.json")
            completion_quality = str(completion_row.get("FinalReviewQuality", ""))
            readiness_quality = (
                f"{output.get('FinalReviewQualityStatus', '')} / "
                f"B:{output.get('FinalReviewQualityBlockingIssues', '')} "
                f"W:{output.get('FinalReviewQualityWarnings', '')}"
            )
            if completion_quality != readiness_quality:
                add_issue(issues, mod_name=mod_name, area="project-completion", message="Project completion final review quality differs from readiness.", evidence="qa/project_completion_audit.json")

    rows = [audit_mod(root, output, runtime_by_mod) for output in outputs if isinstance(output, dict)]
    for row in rows:
        if row.StaticQaStatus != "passed":
            add_issue(
                issues,
                mod_name=row.ModName,
                area="static-qa",
                message="Static QA evidence is incomplete.",
                evidence=", ".join(row.Issues),
            )

    runtime_pending = sum(1 for row in rows if row.RuntimeStatus != "passed")
    project_static_clean = not any(issue.Severity == "error" for issue in issues)
    runtime_complete = runtime_pending == 0 and bool(rows)
    objectives = build_objective_rows(rows, project_static_clean, runtime_complete)
    write_reports(root, root / args.report_output_path, root / args.json_output_path, rows, objectives, issues, runtime_pending)

    print(f"Translation goal compliance audit written to: {root / args.report_output_path}")
    print(f"Translation goal compliance audit JSON written to: {root / args.json_output_path}")
    print(f"Project-local blocking issues: {sum(1 for issue in issues if issue.Severity == 'error')}")
    print(f"Player runtime validations pending (out of scope): {runtime_pending}")
    if args.require_runtime_complete and runtime_pending:
        return 1
    return 1 if any(issue.Severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
