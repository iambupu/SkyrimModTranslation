"""Audit BSA/BA2 coverage evidence for project-local outputs.

If an archive exists in the workspace or final_mod, the workflow must have a
project-local manifest proving its extracted contents were inspected. Without
that manifest, strict completion cannot claim full coverage.
"""

import argparse
import importlib.util
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from typing import Any
from project_paths import project_root
from project_paths import safe_file_name


@dataclass
class ArchiveRow:
    Scope: str
    Path: str
    Extension: str
    Size: int
    Evidence: str
    EvidencePresent: bool
    EvidenceValid: bool
    EvidenceIssues: list[str]


@dataclass
class LooseOverrideRow:
    Archive: str
    RelativePath: str
    FinalModPath: str
    Status: str
    ExemptionReason: str
    Issues: list[str]


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


def normalize_archive_relative_path(value: object) -> str:
    text = "" if value is None else str(value)
    normalized = text.replace("/", "\\").strip().lstrip("\\")
    while normalized.lower().startswith("data\\"):
        normalized = normalized[5:]
    return normalized


def relative_key(value: object) -> str:
    return normalize_archive_relative_path(value).lower()


def archive_match_values(archive_path: str, manifest_archive_path: str = "") -> set[str]:
    values = {archive_path.lower(), Path(archive_path.replace("/", "\\")).name.lower()}
    if manifest_archive_path:
        values.add(manifest_archive_path.lower())
        values.add(Path(manifest_archive_path.replace("/", "\\")).name.lower())
    return {value for value in values if value}


def configured_path(root: Path, value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def configured_tool_ready(root: Path, config: dict[str, Any] | None, property_name: str) -> bool:
    if not config:
        return False
    decoder_tools = config.get("DecoderTools")
    if not isinstance(decoder_tools, dict):
        return False
    path = configured_path(root, decoder_tools.get(property_name))
    return bool(path and path.is_file())


def python_package_ready(package_name: str) -> bool:
    return importlib.util.find_spec(package_name) is not None


def load_config(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def evidence_path(root: Path, mod_name: str, archive_path: Path) -> Path:
    safe_name = safe_file_name(archive_path.stem)
    return root / "out" / mod_name / "archive_audits" / safe_name / "manifest.json"


def load_loose_override_exemptions(root: Path, exemptions_path: Path) -> tuple[dict[tuple[str, str], dict[str, str]], list[str]]:
    exemptions: dict[tuple[str, str], dict[str, str]] = {}
    issues: list[str] = []
    if not exemptions_path.is_file():
        return exemptions, issues

    try:
        lines = exemptions_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        return exemptions, [f"exemptions-read-failed:{exc}"]

    accepted_statuses = {"accepted", "approved", "exempted"}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"exemption-line-{line_number}-json-invalid:{exc.lineno}:{exc.colno}")
            continue
        if not isinstance(row, dict):
            issues.append(f"exemption-line-{line_number}-not-object")
            continue

        archive = str(row.get("Archive") or row.get("ArchivePath") or "").strip()
        relative = normalize_archive_relative_path(row.get("RelativePath"))
        status = str(row.get("Status") or "").strip().lower()
        reason = str(row.get("Reason") or "").strip()
        reviewer = str(row.get("Reviewer") or row.get("ApprovedBy") or "").strip()
        evidence_path_value = str(row.get("EvidencePath") or "").strip()

        if not archive:
            issues.append(f"exemption-line-{line_number}-missing-Archive")
        if not relative:
            issues.append(f"exemption-line-{line_number}-missing-RelativePath")
        if status not in accepted_statuses:
            issues.append(f"exemption-line-{line_number}-status-not-accepted")
        if not reason:
            issues.append(f"exemption-line-{line_number}-missing-Reason")
        if not reviewer:
            issues.append(f"exemption-line-{line_number}-missing-Reviewer")
        if evidence_path_value:
            try:
                evidence = resolve_project_path(root, evidence_path_value, must_exist=True)
                if not evidence.is_file():
                    issues.append(f"exemption-line-{line_number}-EvidencePath-not-file")
            except (OSError, ValueError):
                issues.append(f"exemption-line-{line_number}-EvidencePath-invalid")

        if archive and relative and status in accepted_statuses and reason and reviewer:
            exemptions[(archive.lower(), relative_key(relative))] = {
                "Reason": reason,
                "Reviewer": reviewer,
                "Status": status,
            }
    return exemptions, issues


def find_exemption(
    exemptions: dict[tuple[str, str], dict[str, str]],
    archive_values: set[str],
    relative_path: str,
) -> dict[str, str] | None:
    keys = archive_values | {"*"}
    rel_key = relative_key(relative_path)
    for archive_key in keys:
        found = exemptions.get((archive_key.lower(), rel_key))
        if found:
            return found
    return None


def validate_manifest(root: Path, archive_path: Path, manifest_path: Path) -> tuple[bool, list[str]]:
    # Manifest validation checks both shape and safety claims. A stale or
    # hand-written file missing these fields is not enough coverage evidence.
    issues: list[str] = []
    if not manifest_path.is_file():
        return False, ["manifest-missing"]
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return False, [f"manifest-json-invalid:{exc.lineno}:{exc.colno}"]
    if not isinstance(data, dict):
        return False, ["manifest-root-not-object"]

    required_keys = ("ModName", "ArchivePath", "ExtractedDir", "FilesScanned", "ByKind", "ByRisk", "Files", "Safety")
    for key in required_keys:
        if key not in data:
            issues.append(f"missing-key:{key}")

    archive_value = str(data.get("ArchivePath", ""))
    if archive_path.name.lower() not in archive_value.lower():
        issues.append("archive-path-does-not-reference-archive-name")

    files = data.get("Files")
    if not isinstance(files, list):
        issues.append("files-not-list")
    else:
        files_scanned = data.get("FilesScanned")
        if not isinstance(files_scanned, int):
            issues.append("files-scanned-not-int")
        elif files_scanned != len(files):
            issues.append("files-scanned-count-mismatch")
        for index, row in enumerate(files[:2000]):
            if not isinstance(row, dict):
                issues.append(f"file-row-{index}-not-object")
                continue
            for field in ("RelativePath", "ProjectPath", "Extension", "Kind", "Risk", "RecommendedSkill"):
                if field not in row:
                    issues.append(f"file-row-{index}-missing-{field}")
            project_path = row.get("ProjectPath")
            if isinstance(project_path, str) and project_path:
                try:
                    resolved = resolve_project_path(root, project_path, must_exist=False)
                    if not is_under(resolved, root / "work"):
                        issues.append(f"file-row-{index}-project-path-not-under-work")
                except ValueError:
                    issues.append(f"file-row-{index}-project-path-outside-project")

    if not isinstance(data.get("ByKind"), dict):
        issues.append("by-kind-not-object")
    if not isinstance(data.get("ByRisk"), dict):
        issues.append("by-risk-not-object")

    safety = data.get("Safety")
    if not isinstance(safety, dict):
        issues.append("safety-not-object")
    else:
        expected_safety = {
            "ProjectLocalOnly": True,
            "ArchiveModified": False,
            "ExtractedContentModified": False,
            "RealGameDirectoriesAccessed": False,
        }
        for key, expected in expected_safety.items():
            if safety.get(key) is not expected:
                issues.append(f"safety-{key}-not-{str(expected).lower()}")

    return len(issues) == 0, issues


def collect_loose_override_rows(
    root: Path,
    final_mod: Path,
    archives: list[ArchiveRow],
    exemptions: dict[tuple[str, str], dict[str, str]],
) -> list[LooseOverrideRow]:
    rows: list[LooseOverrideRow] = []
    seen: set[tuple[str, str]] = set()
    for archive in archives:
        if not archive.EvidencePresent or not archive.EvidenceValid:
            continue
        manifest_path = resolve_project_path(root, archive.Evidence, must_exist=True)
        manifest = load_json_file(manifest_path)
        if not manifest:
            continue
        manifest_archive_path = str(manifest.get("ArchivePath") or "")
        archive_values = archive_match_values(archive.Path, manifest_archive_path)
        files = manifest.get("Files")
        if not isinstance(files, list):
            continue
        for item in files:
            if not isinstance(item, dict):
                continue
            if str(item.get("Risk") or "").strip().lower() != "translatable":
                continue
            relative_inside_archive = normalize_archive_relative_path(item.get("RelativePath"))
            if not relative_inside_archive:
                continue
            dedupe_key = (relative_path(root, manifest_path).lower(), relative_key(relative_inside_archive))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            final_path = (final_mod / Path(relative_inside_archive)).resolve(strict=False)
            final_rel = relative_path(root, final_path)
            issues: list[str] = []
            status = "missing"
            exemption_reason = ""

            if not is_under(final_path, final_mod):
                issues.append("relative-path-escapes-final-mod")
            elif final_path.is_file():
                status = "loose-override-present"
            else:
                exemption = find_exemption(exemptions, archive_values, relative_inside_archive)
                if exemption:
                    status = "exempted"
                    exemption_reason = exemption["Reason"]
                else:
                    issues.append("missing-loose-override-or-exemption")

            rows.append(
                LooseOverrideRow(
                    Archive=archive.Path,
                    RelativePath=relative_inside_archive,
                    FinalModPath=final_rel,
                    Status=status,
                    ExemptionReason=exemption_reason,
                    Issues=issues,
                )
            )
    return rows


def collect_archives(root: Path, mod_name: str, workspace: Path, final_mod: Path) -> list[ArchiveRow]:
    # Check both source workspace and final_mod. A package can inherit archives
    # unchanged, but unchanged archives still need content audit evidence.
    rows: list[ArchiveRow] = []
    for scope, base in (("workspace", workspace), ("final_mod", final_mod)):
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*"), key=lambda path: str(path).lower()):
            if not item.is_file() or item.suffix.lower() not in {".bsa", ".ba2"}:
                continue
            evidence = evidence_path(root, mod_name, item)
            evidence_valid, evidence_issues = validate_manifest(root, item, evidence)
            rows.append(
                ArchiveRow(
                    Scope=scope,
                    Path=relative_path(root, item),
                    Extension=item.suffix.lower(),
                    Size=item.stat().st_size,
                    Evidence=relative_path(root, evidence),
                    EvidencePresent=evidence.is_file(),
                    EvidenceValid=evidence_valid,
                    EvidenceIssues=evidence_issues,
                )
            )
    seen: set[str] = set()
    unique: list[ArchiveRow] = []
    for row in rows:
        key = f"{row.Extension}|{row.Path}".lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_report(
    report_path: Path,
    mod_name: str,
    workspace: Path,
    final_mod: Path,
    strict_complete: bool,
    bsa_audit_ready: bool,
    bsa_safe_extractor_ready: bool,
    ba2_ready: bool,
    archives: list[ArchiveRow],
    loose_overrides: list[LooseOverrideRow],
    exemption_path: Path,
    exemption_issues: list[str],
    blocking: int,
    warnings: int,
) -> None:
    with_evidence = sum(1 for item in archives if item.EvidencePresent)
    missing_evidence = sum(1 for item in archives if not item.EvidencePresent)
    invalid_evidence = sum(1 for item in archives if item.EvidencePresent and not item.EvidenceValid)
    loose_present = sum(1 for item in loose_overrides if item.Status == "loose-override-present")
    loose_exempted = sum(1 for item in loose_overrides if item.Status == "exempted")
    loose_missing = sum(1 for item in loose_overrides if item.Issues)
    lines = [
        "# Archive Coverage Audit",
        "",
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(project_root(), workspace)}",
        f"- FinalModDir: {relative_path(project_root(), final_mod)}",
        f"- Strict complete mode: {bool(strict_complete)}",
        f"- BSA audit ready: {bsa_audit_ready}",
        f"- BSA safe extractor ready: {bsa_safe_extractor_ready}",
        f"- BA2 extractor ready: {ba2_ready}",
        f"- Archive files checked: {len(archives)}",
        f"- Archives with evidence: {with_evidence}",
        f"- Archives missing evidence: {missing_evidence}",
        f"- Archives invalid evidence: {invalid_evidence}",
        f"- Archive translatable files: {len(loose_overrides)}",
        f"- Archive loose overrides present: {loose_present}",
        f"- Archive loose override exemptions: {loose_exempted}",
        f"- Archive loose overrides missing: {loose_missing}",
        f"- Archive loose override exemption file: {relative_path(project_root(), exemption_path)}",
        f"- Archive loose override exemption issues: {len(exemption_issues)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Verdict",
        "",
        "PASS: Archive coverage gate has no blocking issues." if blocking == 0 else "FAIL: Archive coverage gate has blocking issues.",
        "",
        "## Archives",
        "",
    ]
    if not archives:
        lines.append("No BSA/BA2 archives were found in the workspace or final_mod.")
    else:
        lines.extend(["| Scope | Archive | Type | Evidence present | Evidence valid | Evidence | Issues |", "|---|---|---|---:|---:|---|---|"])
        for archive in archives:
            lines.append(
                f"| {archive.Scope} | {markdown_cell(archive.Path)} | {archive.Extension} | {archive.EvidencePresent} | {archive.EvidenceValid} | {markdown_cell(archive.Evidence)} | {markdown_cell('; '.join(archive.EvidenceIssues))} |"
            )

    lines.extend(["", "## Translatable Archive Loose Overrides", ""])
    if not loose_overrides:
        lines.append("No translatable archive entries were found in valid archive manifests.")
    else:
        lines.extend(
            [
                "| Archive | Relative path | final_mod loose path | Status | Exemption reason | Issues |",
                "|---|---|---|---|---|---|",
            ]
        )
        for row in loose_overrides:
            lines.append(
                f"| {markdown_cell(row.Archive)} | {markdown_cell(row.RelativePath)} | {markdown_cell(row.FinalModPath)} | {row.Status} | {markdown_cell(row.ExemptionReason)} | {markdown_cell('; '.join(row.Issues))} |"
            )

    lines.extend(["", "## Loose Override Exemptions", ""])
    if exemption_path.is_file():
        lines.append(f"Exemption file present: `{relative_path(project_root(), exemption_path)}`")
    else:
        lines.append(f"Exemption file absent: `{relative_path(project_root(), exemption_path)}`")
    if exemption_issues:
        lines.extend(f"- {issue}" for issue in exemption_issues)
    else:
        lines.append("No exemption format issues.")

    lines.extend(
        [
            "",
            "## Required Evidence",
            "",
            "- BSA/BA2 archives may hide Interface, Scripts, STRINGS, MCM, JSON, XML, TXT, or other translatable resources.",
            "- BSA audit should use `scripts/new_bsa_archive_manifest.py` / `bethesda-structs` first; BSA extraction, when required, must use `scripts/invoke_bsa_file_extractor_safe.py`.",
            "- Translated BSA content should be delivered as same-path loose override in final_mod; the original BSA should remain unchanged.",
            "- Every `Risk=translatable` archive manifest row must have the same relative path present as a final_mod loose file, or a JSONL exemption row in `qa/<ModName>.archive_loose_override_exemptions.jsonl`.",
            "- Exemption rows must include `Archive`, `RelativePath`, `Status` (`accepted`, `approved`, or `exempted`), `Reason`, and `Reviewer`; optional `EvidencePath` must point to an existing project-local file.",
            "- BSA repacking is a high-risk future adapter path only when manual testing proves loose override does not load or causes a Mod-specific issue.",
            "- Strict complete mode requires a project-local archive audit manifest for every BSA/BA2 archive before final delivery can be called complete.",
            "- Expected evidence path: `out/<ModName>/archive_audits/<ArchiveName>/manifest.json`.",
            "- Until a decoder/extractor flow is configured and the archive is audited, the workflow must not claim full localization coverage for mods with BSA/BA2 archives.",
            "",
            "## Safety",
            "",
            "- This script is read-only.",
            "- This script does not open, extract, modify, or repack BSA/BA2 archives.",
            "- This script does not access real Skyrim, Steam, MO2/Vortex, AppData, or Documents/My Games paths.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit project-local BSA/BA2 archive coverage evidence for final_mod delivery.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--loose-override-exemptions-path", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--strict-complete", action="store_true")
    parser.add_argument("--as-json", action="store_true")
    args = parser.parse_args()

    root = project_root()
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{args.mod_name}", must_exist=True)
    workspace = find_data_root(workspace).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, args.mod_name)), must_exist=True)
    config_path = resolve_project_path(root, args.config_path, must_exist=False)
    exemption_path = resolve_project_path(
        root,
        args.loose_override_exemptions_path or f"qa/{args.mod_name}.archive_loose_override_exemptions.jsonl",
        must_exist=False,
    )
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{args.mod_name}.archive_coverage.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(exemption_path, qa_root):
        raise ValueError(f"LooseOverrideExemptionsPath must be under qa/: {args.loose_override_exemptions_path}")
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    config = load_config(config_path)
    bsa_audit_ready = python_package_ready("bethesda_structs")
    bsa_safe_extractor_ready = configured_tool_ready(root, config, "BsaFileExtractorPath") or configured_tool_ready(root, config, "BsaExtractorPath")
    ba2_ready = configured_tool_ready(root, config, "Ba2ExtractorPath")
    archives = collect_archives(root, args.mod_name, workspace, final_mod)
    exemptions, exemption_issues = load_loose_override_exemptions(root, exemption_path)
    loose_overrides = collect_loose_override_rows(root, final_mod, archives, exemptions)
    missing_evidence = sum(1 for item in archives if not item.EvidencePresent)
    invalid_evidence = sum(1 for item in archives if item.EvidencePresent and not item.EvidenceValid)
    loose_override_issues = sum(1 for item in loose_overrides if item.Issues)
    evidence_issues = missing_evidence + invalid_evidence + loose_override_issues + len(exemption_issues)
    blocking = evidence_issues if args.strict_complete else 0
    warnings = 0 if args.strict_complete else evidence_issues

    write_report(
        report_path,
        args.mod_name,
        workspace,
        final_mod,
        args.strict_complete,
        bsa_audit_ready,
        bsa_safe_extractor_ready,
        ba2_ready,
        archives,
        loose_overrides,
        exemption_path,
        exemption_issues,
        blocking,
        warnings,
    )

    if args.as_json:
        print(
            json.dumps(
                {
                    "ModName": args.mod_name,
                    "Workspace": relative_path(root, workspace),
                    "FinalModDir": relative_path(root, final_mod),
                    "StrictComplete": bool(args.strict_complete),
                    "BsaAuditReady": bsa_audit_ready,
                    "BsaSafeExtractorReady": bsa_safe_extractor_ready,
                    "BsaExtractorReady": bsa_safe_extractor_ready,
                    "Ba2ExtractorReady": ba2_ready,
                    "ArchiveFilesChecked": len(archives),
                    "ArchivesWithEvidence": sum(1 for item in archives if item.EvidencePresent),
                    "ArchivesMissingEvidence": missing_evidence,
                    "ArchivesInvalidEvidence": invalid_evidence,
                    "ArchiveTranslatableFiles": len(loose_overrides),
                    "ArchiveLooseOverridesPresent": sum(1 for item in loose_overrides if item.Status == "loose-override-present"),
                    "ArchiveLooseOverrideExemptions": sum(1 for item in loose_overrides if item.Status == "exempted"),
                    "ArchiveLooseOverridesMissing": loose_override_issues,
                    "ArchiveLooseOverrideExemptionFile": relative_path(root, exemption_path),
                    "ArchiveLooseOverrideExemptionIssues": exemption_issues,
                    "LooseOverrides": [asdict(item) for item in loose_overrides],
                    "BlockingIssues": blocking,
                    "Warnings": warnings,
                    "Archives": [asdict(item) for item in archives],
                    "Report": relative_path(root, report_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Archive coverage report written to: {report_path}")
        print(f"Archive files checked: {len(archives)}")
        print(f"Blocking issues: {blocking}")
        print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
