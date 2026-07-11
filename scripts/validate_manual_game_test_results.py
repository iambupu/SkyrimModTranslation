"""Validate player-operated runtime test evidence after the player provides it.

Codex does not run Skyrim, MO2, or Vortex. This script only checks that submitted
results, package hashes, checklist rows, and evidence artifacts match current
project-local outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, project_root, relative_path, resolve_project_path, safe_file_name


ALLOWED_ARTIFACT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".txt", ".log", ".md", ".json", ".csv"}


@dataclass
class RuntimeIssue:
    Severity: str
    ModName: str
    Area: str
    Message: str
    Evidence: str


@dataclass
class RuntimeArtifact:
    CheckName: str
    Path: str
    Sha256: str
    SizeBytes: int


@dataclass
class RuntimeRow:
    ModName: str
    Status: str
    ValidationStatus: str
    RequiredChecks: int
    PassedChecks: int
    ArtifactFiles: int
    ArtifactManifestSha256: str
    Artifacts: list[RuntimeArtifact]
    PackagePath: str
    Issues: list[str]


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_manifest_sha(artifacts: list[RuntimeArtifact]) -> str:
    if not artifacts:
        return ""
    payload = [asdict(artifact) for artifact in artifacts]
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def add_issue(
    issues: list[RuntimeIssue],
    *,
    mod_name: str,
    area: str,
    message: str,
    evidence: str,
    severity: str = "error",
) -> None:
    issues.append(RuntimeIssue(severity, mod_name, area, message, evidence))


def result_rows_by_mod(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("Rows", [])
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


def required_check_names(plan_row: dict[str, Any]) -> list[str]:
    checks = plan_row.get("RequiredChecks", [])
    if not isinstance(checks, list):
        return []
    return [str(item).strip() for item in checks if str(item).strip()]


def check_results_by_name(result_row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks = result_row.get("CheckResults", [])
    if not isinstance(checks, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = str(check.get("Name", "")).strip()
        if name:
            result[name] = check
    return result


def check_artifact_values(check: dict[str, Any]) -> list[str]:
    values = check.get("EvidenceArtifacts", [])
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def concrete_text(value: object, *, min_length: int = 8) -> bool:
    text = str(value or "").strip()
    if len(text) < min_length:
        return False
    # Reject generic success words. Evidence must describe what the player
    # actually observed, triggered, or captured in game.
    generic = {"ok", "okay", "pass", "passed", "done", "yes", "true", "none", "no issues", "无", "通过", "已通过", "完成", "正常"}
    return text.lower() not in generic


def date_like(value: object) -> bool:
    text = str(value or "").strip()
    if not re.search(r"\d{4}-\d{1,2}-\d{1,2}", text):
        return False
    if not re.search(r"[\sT]\d{1,2}:\d{2}", text):
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return bool(re.search(r"\d{4}-\d{1,2}-\d{1,2}[\sT]\d{1,2}:\d{2}", text))
    return True


def validate_environment(row: dict[str, Any], issues: list[RuntimeIssue], mod_name: str) -> None:
    env = row.get("TestEnvironment")
    if not isinstance(env, dict):
        add_issue(issues, mod_name=mod_name, area="environment", message="TestEnvironment is missing.", evidence="qa/manual_game_test_results.json")
        return
    for key in ("Game", "GameVersion", "ModManager", "Profile", "LoadOrderNotes"):
        if not concrete_text(env.get(key, ""), min_length=3):
            add_issue(issues, mod_name=mod_name, area="environment", message=f"TestEnvironment.{key} requires concrete runtime context.", evidence="qa/manual_game_test_results.json")


def validate_check_artifacts(root: Path, mod_name: str, check_name: str, check: dict[str, Any], fail) -> list[RuntimeArtifact]:
    artifacts = check_artifact_values(check)
    if not artifacts:
        fail("checks", f"Manual check needs at least one project-local evidence artifact: {check.get('Name', '')}")
        return []
    # Artifacts are restricted to a per-Mod evidence folder. This prevents a
    # report from pointing at private game, profile, or AppData files.
    artifact_root = root / "qa" / "manual_game_test_artifacts" / safe_file_name(mod_name)
    valid_artifacts: list[RuntimeArtifact] = []
    seen_paths: set[str] = set()
    for artifact in artifacts:
        try:
            path = resolve_project_path(root, artifact, must_exist=True)
        except Exception as exc:
            fail("checks", f"Evidence artifact cannot be read: {artifact} ({exc})")
            continue
        if not is_under(path, artifact_root):
            fail("checks", f"Evidence artifact must be under {relative_path(root, artifact_root)}: {artifact}")
            continue
        if not path.is_file():
            fail("checks", f"Evidence artifact is not a file: {artifact}")
            continue
        if path.stat().st_size <= 0:
            fail("checks", f"Evidence artifact is empty: {artifact}")
            continue
        if path.suffix.lower() not in ALLOWED_ARTIFACT_EXTENSIONS:
            fail("checks", f"Evidence artifact file type is not allowed: {artifact}")
            continue
        rel_path = relative_path(root, path)
        if rel_path.lower() in seen_paths:
            fail("checks", f"Evidence artifact is listed more than once: {artifact}")
            continue
        seen_paths.add(rel_path.lower())
        valid_artifacts.append(
            RuntimeArtifact(
                CheckName=check_name,
                Path=rel_path,
                Sha256=sha256_file(path),
                SizeBytes=path.stat().st_size,
            )
        )
    return valid_artifacts


def validate_row(root: Path, plan_row: dict[str, Any], result_row: dict[str, Any] | None, issues: list[RuntimeIssue]) -> RuntimeRow:
    mod_name = str(plan_row.get("ModName", "")).strip()
    row_issues: list[str] = []
    required_names = required_check_names(plan_row)
    passed_checks = 0
    artifact_files = 0
    artifacts: list[RuntimeArtifact] = []
    package_rel = str(plan_row.get("PackagePath", "")).strip()

    def fail(area: str, message: str, evidence: str = "qa/manual_game_test_results.json") -> None:
        row_issues.append(message)
        add_issue(issues, mod_name=mod_name, area=area, message=message, evidence=evidence)

    if result_row is None:
        fail("result", "Missing manual runtime result row.")
        return RuntimeRow(mod_name, "missing", "failed", len(required_names), 0, 0, "", [], package_rel, row_issues)

    if str(result_row.get("Status", "")).strip() != "passed":
        fail("result", "Runtime result row is not marked passed.")
    if not date_like(result_row.get("CheckedAt", "")):
        fail("result", "CheckedAt must include a concrete date and time.")
    if not concrete_text(result_row.get("Tester", ""), min_length=2):
        fail("result", "Tester is required and cannot be a placeholder.")

    validate_environment(result_row, issues, mod_name)

    if str(result_row.get("PackagePath", "")).replace("/", "\\") != package_rel.replace("/", "\\"):
        fail("package", "PackagePath does not match the manual game test plan.")
    try:
        package_path = resolve_project_path(root, package_rel, must_exist=True)
        expected_package_hash = sha256_file(package_path)
    except Exception as exc:
        expected_package_hash = ""
        fail("package", f"Current package cannot be read: {exc}", package_rel)
    if expected_package_hash and str(result_row.get("PackageSha256", "")).lower() != expected_package_hash.lower():
        fail("package", "PackageSha256 does not match the current CHS package.")

    final_rel = str(plan_row.get("FinalModDir", "")).strip()
    if str(result_row.get("FinalModDir", "")).replace("/", "\\") != final_rel.replace("/", "\\"):
        fail("final-mod", "FinalModDir does not match the manual game test plan.")
    try:
        final_dir = resolve_project_path(root, final_rel, must_exist=True)
        manifest_path = final_dir / "meta" / "manifest.json"
        expected_manifest_hash = sha256_file(manifest_path)
    except Exception as exc:
        expected_manifest_hash = ""
        fail("final-mod", f"Current final_mod manifest cannot be read: {exc}", final_rel)
    if expected_manifest_hash and str(result_row.get("FinalManifestSha256", "")).lower() != expected_manifest_hash.lower():
        fail("final-mod", "FinalManifestSha256 does not match the current final_mod manifest.")

    result_checks = check_results_by_name(result_row)
    unexpected_checks = sorted(set(result_checks) - set(required_names))
    for name in unexpected_checks:
        fail("checks", f"Unexpected manual check not present in current plan: {name}")
    for name in required_names:
        check = result_checks.get(name)
        if check is None:
            fail("checks", f"Missing required manual check: {name}")
            continue
        if str(check.get("Status", "")).strip() != "passed":
            fail("checks", f"Manual check is not passed: {name}")
            continue
        if not concrete_text(check.get("Evidence", ""), min_length=12):
            fail("checks", f"Manual check needs concrete evidence, not a generic pass note: {name}")
            continue
        check_artifacts = validate_check_artifacts(root, mod_name, name, check, fail)
        artifact_files += len(check_artifacts)
        artifacts.extend(check_artifacts)
        passed_checks += 1

    runtime_issues = str(result_row.get("RuntimeIssues", "")).strip().lower()
    if runtime_issues not in {"none", "无", "no issues", "no runtime issues"}:
        fail("runtime", "RuntimeIssues must explicitly state none after testing.")

    validation_status = "passed" if not row_issues else "failed"
    return RuntimeRow(
        ModName=mod_name,
        Status=str(result_row.get("Status", "")),
        ValidationStatus=validation_status,
        RequiredChecks=len(required_names),
        PassedChecks=passed_checks,
        ArtifactFiles=artifact_files,
        ArtifactManifestSha256=artifact_manifest_sha(artifacts),
        Artifacts=artifacts,
        PackagePath=package_rel,
        Issues=row_issues,
    )


def write_reports(root: Path, report_path: Path, json_path: Path, rows: list[RuntimeRow], issues: list[RuntimeIssue]) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    status = "passed" if blocking == 0 and rows else "failed"
    lines = [
        "# Player-Operated Game Test Results Validation",
        "",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Status: {status}",
        f"- Blocking issues: {blocking}",
        f"- Mods validated: {len(rows)}",
        "",
        "## Verdict",
        "",
        "PASS: Player-operated runtime results are complete and match the current CHS packages." if status == "passed" else "FAIL: Player-operated runtime results are missing, incomplete, stale, or not fully passed.",
        "",
        "## Mod Results",
        "",
        "| ModName | Validation | Required checks | Passed checks | Artifact files | Artifact manifest | Package | Issues |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {markdown_cell(row.ModName)} | {row.ValidationStatus} | {row.RequiredChecks} | {row.PassedChecks} | {row.ArtifactFiles} | "
            f"{markdown_cell(row.ArtifactManifestSha256)} | "
            f"{markdown_cell(row.PackagePath)} | {markdown_cell('; '.join(row.Issues) if row.Issues else 'none')} |"
        )

    lines.extend(["", "## Evidence Artifacts", ""])
    artifact_rows = [artifact for row in rows for artifact in row.Artifacts]
    if not artifact_rows:
        lines.append("No evidence artifacts recorded.")
    else:
        lines.extend(["| Check | Path | SHA256 | Size bytes |", "|---|---|---|---:|"])
        for artifact in artifact_rows:
            lines.append(
                f"| {markdown_cell(artifact.CheckName)} | {markdown_cell(artifact.Path)} | {artifact.Sha256} | {artifact.SizeBytes} |"
            )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No player-operated runtime validation issues.")
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
            "- This validator reads only project-local plans, packages, manifests, player-provided artifacts, and result JSON.",
            "- It does not access the real game, MO2, Vortex, Steam, AppData, or Documents/My Games paths.",
            "- Agent must not perform the runtime checks directly; this validator only verifies player-operated evidence after the fact.",
            "- It does not modify plugin, PEX, archive, or package binaries.",
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
                "Status": status,
                "BlockingIssues": blocking,
                "Rows": [asdict(row) for row in rows],
                "Issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate player-operated Skyrim runtime test results against the current test plan and CHS packages.")
    parser.add_argument("--plan-json-path", default="qa/manual_game_test_plan.json")
    parser.add_argument("--results-json-path", default="qa/manual_game_test_results.json")
    parser.add_argument("--report-output-path", default="qa/manual_game_test_results_validation.md")
    parser.add_argument("--json-output-path", default="qa/manual_game_test_results_validation.json")
    args = parser.parse_args()

    root = project_root()
    issues: list[RuntimeIssue] = []
    plan_path = resolve_project_path(root, args.plan_json_path, must_exist=True)
    plan = read_json(plan_path)
    result_path = resolve_project_path(root, args.results_json_path, must_exist=False)
    results = read_json(result_path)

    plan_rows = plan.get("Rows", [])
    if not isinstance(plan_rows, list) or not plan_rows:
        add_issue(issues, mod_name="*", area="plan", message="Manual game test plan has no rows.", evidence=relative_path(root, plan_path))
        plan_rows = []
    if not result_path.is_file():
        add_issue(issues, mod_name="*", area="results", message="Manual game test results file is missing.", evidence=relative_path(root, result_path))
        results = {}
    elif results.get("Status") != "passed":
        add_issue(issues, mod_name="*", area="results", message="Manual game test results top-level Status is not passed.", evidence=relative_path(root, result_path))
    elif str(results.get("SourcePlanPath", "")).replace("/", "\\") != args.plan_json_path.replace("/", "\\"):
        add_issue(issues, mod_name="*", area="results", message="Manual game test results SourcePlanPath does not match the current plan path.", evidence=relative_path(root, result_path))

    by_mod = result_rows_by_mod(results)
    planned_mods = {str(row.get("ModName", "")).strip() for row in plan_rows if isinstance(row, dict) and str(row.get("ModName", "")).strip()}
    extra_mods = sorted(set(by_mod) - planned_mods)
    for mod_name in extra_mods:
        add_issue(issues, mod_name=mod_name, area="results", message="Manual game test results contain a ModName not present in the current plan.", evidence=relative_path(root, result_path))
    rows = [validate_row(root, plan_row, by_mod.get(str(plan_row.get("ModName", "")).strip()), issues) for plan_row in plan_rows if isinstance(plan_row, dict)]
    duplicate_count = 0
    raw_rows = results.get("Rows", [])
    if isinstance(raw_rows, list):
        names = [str(row.get("ModName", "")).strip() for row in raw_rows if isinstance(row, dict)]
        duplicate_count = len(names) - len(set(name for name in names if name))
    if duplicate_count:
        add_issue(issues, mod_name="*", area="results", message="Manual game test results contain duplicate ModName rows.", evidence=relative_path(root, result_path))

    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    write_reports(root, report_path, json_path, rows, issues)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    print(f"Player-operated game test results validation written to: {report_path}")
    print(f"Player-operated game test results validation JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
