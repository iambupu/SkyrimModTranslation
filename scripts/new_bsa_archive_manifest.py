"""Build a read-only manifest for BSA/BA2 contents with bethesda-structs.

This script reads archive entries and classifies them for QA coverage. It does
not extract files, modify archives, or write loose archive contents.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from bethesda_structs.archive.bsa import BSAArchive
from bethesda_structs.archive.btdx import BTDXArchive

from new_archive_audit_manifest import archive_content_route, count_by, safe_file_name
from project_paths import is_under, project_root, relative_path, resolve_project_path


@dataclass
class ArchiveFileRow:
    RelativePath: str
    ProjectPath: str
    Extension: str
    Size: int
    Kind: str
    Risk: str
    RecommendedSkill: str
    Notes: str


def normalize_archive_path(value: Path) -> str:
    return str(value).replace("/", "\\")


def read_archive_rows(archive_path: Path) -> list[ArchiveFileRow]:
    suffix = archive_path.suffix.lower()
    archive_class = BSAArchive if suffix == ".bsa" else BTDXArchive
    archive = archive_class.parse(archive_path.read_bytes(), filepath=str(archive_path))
    rows: list[ArchiveFileRow] = []
    for entry in archive.iter_files():
        relative_inside_archive = normalize_archive_path(entry.filepath)
        kind, risk, skill, notes = archive_content_route(Path(relative_inside_archive), relative_inside_archive)
        rows.append(
            ArchiveFileRow(
                RelativePath=relative_inside_archive,
                ProjectPath="",
                Extension=Path(relative_inside_archive).suffix.lower(),
                Size=entry.size,
                Kind=kind,
                Risk=risk,
                RecommendedSkill=skill,
                Notes=notes,
            )
        )
    return rows


def write_manifest(root: Path, mod_name: str, archive_path: Path, output_dir: Path, report_path: Path, rows: list[ArchiveFileRow]) -> None:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest_path = output_dir / "manifest.json"
    files_path = output_dir / "files.jsonl"
    translatable_count = sum(1 for row in rows if row.Risk == "translatable")
    decoder_required_count = sum(1 for row in rows if row.Risk == "decoder-required")
    manual_review_count = sum(1 for row in rows if row.Risk == "manual-review")

    manifest = {
        "ModName": mod_name,
        "ArchivePath": relative_path(root, archive_path),
        "ExtractedDir": "",
        "AuditMode": "bethesda-structs-read-only",
        "GeneratedAt": generated_at,
        "FilesScanned": len(rows),
        "ByKind": count_by(rows, "Kind"),
        "ByRisk": count_by(rows, "Risk"),
        "Files": [asdict(row) for row in rows],
        "Safety": {
            "ProjectLocalOnly": True,
            "ArchiveModified": False,
            "ExtractedContentModified": False,
            "RealGameDirectoriesAccessed": False,
            "ReadOnlyArchiveInventory": True,
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with files_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

    lines = [
        "# BSA/BA2 Archive Manifest Report",
        "",
        f"- ModName: {mod_name}",
        f"- Archive: {relative_path(root, archive_path)}",
        "- AuditMode: bethesda-structs-read-only",
        f"- Manifest: {relative_path(root, manifest_path)}",
        f"- Generated at: {generated_at}",
        f"- Files scanned: {len(rows)}",
        f"- Translatable files: {translatable_count}",
        f"- Decoder-required files: {decoder_required_count}",
        f"- Manual-review files: {manual_review_count}",
        "",
        "## Safety",
        "",
        "- This script read archive entries with bethesda-structs.",
        "- This script did not extract, modify, repack, or delete archive content.",
        "- ArchivePath, OutputDir, and ReportOutputPath were checked to stay inside the project.",
        "- This manifest is evidence for `scripts/audit_archive_coverage.py`; it is not proof of translation quality by itself.",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a read-only BSA/BA2 archive manifest with bethesda-structs.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--report-output-path", default="")
    args = parser.parse_args()

    root = project_root()
    archive_path = resolve_project_path(root, args.archive_path, must_exist=True)
    out_root = resolve_project_path(root, "out", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if archive_path.suffix.lower() not in {".bsa", ".ba2"}:
        raise ValueError(f"ArchivePath must be .bsa or .ba2: {args.archive_path}")

    safe_archive_name = safe_file_name(archive_path.stem)
    output_dir = resolve_project_path(
        root,
        args.output_dir or f"out/{args.mod_name}/archive_audits/{safe_archive_name}",
        must_exist=False,
    )
    report_path = resolve_project_path(
        root,
        args.report_output_path or f"qa/{args.mod_name}.{safe_archive_name}.archive_audit_manifest.md",
        must_exist=False,
    )
    if not is_under(output_dir, out_root):
        raise ValueError("OutputDir must be under out/.")
    if not is_under(report_path, qa_root):
        raise ValueError("ReportOutputPath must be under qa/.")

    rows = read_archive_rows(archive_path)
    write_manifest(root, args.mod_name, archive_path, output_dir, report_path, rows)
    print(f"Archive audit manifest written to: {output_dir / 'manifest.json'}")
    print(f"Archive audit report written to: {report_path}")
    print(f"Files scanned: {len(rows)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BSA archive manifest failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
