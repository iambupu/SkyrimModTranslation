import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from typing import Any


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return cleaned.strip()


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


def load_config(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None


def evidence_path(root: Path, mod_name: str, archive_path: Path) -> Path:
    safe_name = safe_file_name(archive_path.stem)
    return root / "out" / mod_name / "archive_audits" / safe_name / "manifest.json"


def validate_manifest(root: Path, archive_path: Path, manifest_path: Path) -> tuple[bool, list[str]]:
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


def collect_archives(root: Path, mod_name: str, workspace: Path, final_mod: Path) -> list[ArchiveRow]:
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
    bsa_ready: bool,
    ba2_ready: bool,
    archives: list[ArchiveRow],
    blocking: int,
    warnings: int,
) -> None:
    with_evidence = sum(1 for item in archives if item.EvidencePresent)
    missing_evidence = sum(1 for item in archives if not item.EvidencePresent)
    invalid_evidence = sum(1 for item in archives if item.EvidencePresent and not item.EvidenceValid)
    lines = [
        "# Archive Coverage Audit",
        "",
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(project_root(), workspace)}",
        f"- FinalModDir: {relative_path(project_root(), final_mod)}",
        f"- Strict complete mode: {bool(strict_complete)}",
        f"- BSA extractor ready: {bsa_ready}",
        f"- BA2 extractor ready: {ba2_ready}",
        f"- Archive files checked: {len(archives)}",
        f"- Archives with evidence: {with_evidence}",
        f"- Archives missing evidence: {missing_evidence}",
        f"- Archives invalid evidence: {invalid_evidence}",
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

    lines.extend(
        [
            "",
            "## Required Evidence",
            "",
            "- BSA/BA2 archives may hide Interface, Scripts, STRINGS, MCM, JSON, XML, TXT, or other translatable resources.",
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
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--strict-complete", action="store_true")
    parser.add_argument("--as-json", action="store_true")
    args = parser.parse_args()

    root = project_root()
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{args.mod_name}", must_exist=True)
    workspace = find_data_root(workspace).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, args.mod_name)), must_exist=True)
    config_path = resolve_project_path(root, args.config_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{args.mod_name}.archive_coverage.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    config = load_config(config_path)
    bsa_ready = configured_tool_ready(root, config, "BsaExtractorPath")
    ba2_ready = configured_tool_ready(root, config, "Ba2ExtractorPath")
    archives = collect_archives(root, args.mod_name, workspace, final_mod)
    missing_evidence = sum(1 for item in archives if not item.EvidencePresent)
    invalid_evidence = sum(1 for item in archives if item.EvidencePresent and not item.EvidenceValid)
    evidence_issues = missing_evidence + invalid_evidence
    blocking = evidence_issues if args.strict_complete else 0
    warnings = 0 if args.strict_complete else evidence_issues

    write_report(report_path, args.mod_name, workspace, final_mod, args.strict_complete, bsa_ready, ba2_ready, archives, blocking, warnings)

    if args.as_json:
        print(
            json.dumps(
                {
                    "ModName": args.mod_name,
                    "Workspace": relative_path(root, workspace),
                    "FinalModDir": relative_path(root, final_mod),
                    "StrictComplete": bool(args.strict_complete),
                    "BsaExtractorReady": bsa_ready,
                    "Ba2ExtractorReady": ba2_ready,
                    "ArchiveFilesChecked": len(archives),
                    "ArchivesWithEvidence": sum(1 for item in archives if item.EvidencePresent),
                    "ArchivesMissingEvidence": missing_evidence,
                    "ArchivesInvalidEvidence": invalid_evidence,
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
