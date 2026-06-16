"""Validate the installable <ModName>_CHS.zip against final_mod and evidence.

The package must be a byte-for-byte archive view of final_mod. intermediate/ is
validated as sibling evidence but is not included in the installable zip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import intermediate_output_dir
from project_paths import packaged_mod_path, project_root, relative_path, resolve_project_path


@dataclass
class PackageIssue:
    Severity: str
    Area: str
    Message: str
    Evidence: str


@dataclass
class PackageRow:
    Path: str
    FinalSha256: str
    PackageSha256: str
    SizeBytes: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(chunks) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def normalized_rel(path: Path) -> str:
    return path.as_posix()


def safe_zip_name(name: str) -> str | None:
    # Reject absolute, empty, current-directory, and traversal entries before
    # comparing hashes. A package with unsafe paths is never installable output.
    normalized = name.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return normalized


def final_files(final_mod: Path) -> dict[str, PackageRow]:
    rows: dict[str, PackageRow] = {}
    for path in sorted(item for item in final_mod.rglob("*") if item.is_file()):
        relative = normalized_rel(path.relative_to(final_mod))
        rows[relative] = PackageRow(relative, sha256_file(path), "", path.stat().st_size)
    return rows


def package_files(package_path: Path, issues: list[PackageIssue]) -> dict[str, PackageRow]:
    rows: dict[str, PackageRow] = {}
    try:
        archive = zipfile.ZipFile(package_path, "r")
    except zipfile.BadZipFile:
        issues.append(PackageIssue("error", "package", "CHS package is not a valid zip file.", str(package_path)))
        return rows
    with archive:
        seen: set[str] = set()
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = safe_zip_name(info.filename)
            if name is None:
                issues.append(PackageIssue("error", "package", "Unsafe zip entry path.", info.filename))
                continue
            if name in seen:
                issues.append(PackageIssue("error", "package", "Duplicate zip entry path.", name))
                continue
            seen.add(name)
            with archive.open(info, "r") as handle:
                digest = sha256_bytes(iter(lambda: handle.read(1024 * 1024), b""))
            rows[name] = PackageRow(name, "", digest, int(info.file_size))
    return rows


def read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def translation_dictionary_status(root: Path, mod_name: str, issues: list[PackageIssue]) -> tuple[str, int, int]:
    # The dictionary is release evidence, not a game file. The CHS package can be
    # valid only when the sibling intermediate dictionary is present and nonempty.
    dictionary_dir = intermediate_output_dir(root, mod_name) / "translation_text_dictionary"
    manifest_path = dictionary_dir / "manifest.json"
    dictionary_jsonl = dictionary_dir / "translation_dictionary.jsonl"
    dictionary_rel = relative_path(root, dictionary_jsonl)
    entries = 0
    source_files = 0

    if not dictionary_dir.is_dir():
        issues.append(PackageIssue("error", "intermediate", "Intermediate translation text dictionary directory is missing.", relative_path(root, dictionary_dir)))
        return dictionary_rel, entries, source_files
    if not manifest_path.is_file():
        issues.append(PackageIssue("error", "intermediate", "Translation text dictionary manifest is missing.", relative_path(root, manifest_path)))
    else:
        manifest = read_json(manifest_path)
        if manifest is None:
            issues.append(PackageIssue("error", "intermediate", "Translation text dictionary manifest is not valid JSON.", relative_path(root, manifest_path)))
        else:
            try:
                entries = int(manifest.get("TranslatedEntryCount", 0) or 0)
            except (TypeError, ValueError):
                issues.append(PackageIssue("error", "intermediate", "TranslatedEntryCount is not numeric.", relative_path(root, manifest_path)))
            try:
                source_files = int(manifest.get("SourceFileCount", 0) or 0)
            except (TypeError, ValueError):
                issues.append(PackageIssue("error", "intermediate", "SourceFileCount is not numeric.", relative_path(root, manifest_path)))

    if not dictionary_jsonl.is_file():
        issues.append(PackageIssue("error", "intermediate", "Normalized translation dictionary JSONL is missing.", dictionary_rel))
        return dictionary_rel, entries, source_files

    line_count = 0
    invalid_rows = 0
    translated_rows = 0
    for line in dictionary_jsonl.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        line_count += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid_rows += 1
            continue
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        source = str(row.get("source", "")).strip()
        target = str(row.get("target", "")).strip()
        if source and target and source != target:
            translated_rows += 1
    if invalid_rows:
        issues.append(PackageIssue("error", "intermediate", f"Translation dictionary has invalid JSONL row(s): {invalid_rows}.", dictionary_rel))
    if entries and line_count != entries:
        issues.append(PackageIssue("error", "intermediate", f"Translation dictionary line count does not match manifest: jsonl={line_count} manifest={entries}.", dictionary_rel))
    entries = max(entries, translated_rows)
    if entries <= 0 or translated_rows <= 0:
        issues.append(PackageIssue("error", "intermediate", "Intermediate translation text dictionary has no translated source-target entries.", dictionary_rel))
    return dictionary_rel, entries, source_files


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_reports(
    root: Path,
    report_path: Path,
    json_path: Path,
    mod_name: str,
    final_mod: Path,
    package_path: Path,
    rows: list[PackageRow],
    issues: list[PackageIssue],
    dictionary_path: str,
    dictionary_entries: int,
    dictionary_source_files: int,
) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    status = "passed" if blocking == 0 else "failed"
    lines = [
        "# CHS Package Validation",
        "",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- ModName: {mod_name}",
        f"- FinalModDir: {relative_path(root, final_mod)}",
        f"- PackagePath: {relative_path(root, package_path)}",
        f"- Status: {status}",
        f"- Blocking issues: {blocking}",
        f"- Final files: {len(rows)}",
        f"- Package size bytes: {package_path.stat().st_size if package_path.is_file() else 0}",
        f"- Package SHA256: {sha256_file(package_path) if package_path.is_file() else ''}",
        f"- Translation dictionary: {dictionary_path}",
        f"- Translation dictionary entries: {dictionary_entries}",
        f"- Translation dictionary source files: {dictionary_source_files}",
        "",
        "## Verdict",
        "",
        "PASS: The CHS package contents match final_mod exactly." if status == "passed" else "FAIL: The CHS package does not match final_mod.",
        "",
        "## Issues",
        "",
    ]
    if not issues:
        lines.append("No package validation issues.")
    else:
        lines.extend(["| Severity | Area | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {issue.Area} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |")
    lines.extend(
        [
            "",
            "## Matched Files",
            "",
            "| Path | SizeBytes | SHA256 |",
            "|---|---:|---|",
        ]
    )
    preview = rows[:200]
    for row in preview:
        lines.append(f"| {markdown_cell(row.Path)} | {row.SizeBytes} | {row.FinalSha256 or row.PackageSha256} |")
    if len(rows) > len(preview):
        lines.append(f"| ... | {len(rows) - len(preview)} more | ... |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This validation reads only project-local final_mod and CHS package files.",
            "- It does not modify plugin, PEX, archive, or package binaries.",
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
                "ModName": mod_name,
                "FinalModDir": relative_path(root, final_mod),
                "PackagePath": relative_path(root, package_path),
                "Status": status,
                "BlockingIssues": blocking,
                "FinalFileCount": len(rows),
                "PackageSizeBytes": package_path.stat().st_size if package_path.is_file() else 0,
                "PackageSha256": sha256_file(package_path) if package_path.is_file() else "",
                "TranslationDictionaryPath": dictionary_path,
                "TranslationDictionaryEntries": dictionary_entries,
                "TranslationDictionarySourceFiles": dictionary_source_files,
                "Rows": [asdict(row) for row in rows],
                "Issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def validate_with_intermediate(
    root: Path, mod_name: str, final_mod: Path, package_path: Path
) -> tuple[list[PackageRow], list[PackageIssue], str, int, int]:
    issues: list[PackageIssue] = []
    dictionary_path, dictionary_entries, dictionary_source_files = translation_dictionary_status(root, mod_name, issues)
    if not final_mod.is_dir():
        issues.append(PackageIssue("error", "final-mod", "final_mod directory is missing.", relative_path(root, final_mod)))
    if not package_path.is_file():
        issues.append(PackageIssue("error", "package", "CHS package is missing.", relative_path(root, package_path)))
    elif not package_path.name.endswith("_CHS.zip"):
        issues.append(PackageIssue("error", "package", "CHS package name must end with _CHS.zip.", relative_path(root, package_path)))
    final = final_files(final_mod) if final_mod.is_dir() else {}
    packaged = package_files(package_path, issues) if package_path.is_file() else {}
    final_keys = set(final)
    package_keys = set(packaged)
    for missing in sorted(final_keys - package_keys):
        issues.append(PackageIssue("error", "package", "File exists in final_mod but not in CHS package.", missing))
    for extra in sorted(package_keys - final_keys):
        issues.append(PackageIssue("error", "package", "File exists in CHS package but not in final_mod.", extra))
    matched_rows: list[PackageRow] = []
    for key in sorted(final_keys & package_keys):
        final_row = final[key]
        package_row = packaged[key]
        if final_row.FinalSha256 != package_row.PackageSha256:
            issues.append(PackageIssue("error", "package", "Packaged file content does not match final_mod.", key))
        else:
            matched_rows.append(PackageRow(key, final_row.FinalSha256, package_row.PackageSha256, final_row.SizeBytes))
    if not final and final_mod.is_dir():
        issues.append(PackageIssue("error", "final-mod", "final_mod has no files.", relative_path(root, final_mod)))
    return matched_rows, issues, dictionary_path, dictionary_entries, dictionary_source_files


def validate(root: Path, mod_name: str, final_mod: Path, package_path: Path) -> tuple[list[PackageRow], list[PackageIssue]]:
    rows, issues, _dictionary_path, _dictionary_entries, _dictionary_source_files = validate_with_intermediate(root, mod_name, final_mod, package_path)
    return rows, issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate that out/<ModName>/汉化产出/<ModName>_CHS.zip exactly matches final_mod.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--package-path", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--json-output-path", default="")
    args = parser.parse_args()

    root = project_root()
    final_mod = resolve_project_path(root, args.final_mod_dir, must_exist=False) if args.final_mod_dir else default_final_mod_dir(root, args.mod_name)
    package_path = resolve_project_path(root, args.package_path, must_exist=False) if args.package_path else packaged_mod_path(root, args.mod_name)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False) if args.report_output_path else root / "qa" / f"{args.mod_name}.chs_package_validation.md"
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False) if args.json_output_path else root / "qa" / f"{args.mod_name}.chs_package_validation.json"
    rows, issues, dictionary_path, dictionary_entries, dictionary_source_files = validate_with_intermediate(root, args.mod_name, final_mod, package_path)
    write_reports(root, report_path, json_path, args.mod_name, final_mod, package_path, rows, issues, dictionary_path, dictionary_entries, dictionary_source_files)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    print(f"CHS package validation written to: {report_path}")
    print(f"CHS package validation JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
