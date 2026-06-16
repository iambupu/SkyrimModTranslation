"""Build a manifest for extracted BSA/BA2 contents under work/.

The manifest is coverage evidence for strict gates; it does not modify archives
or extracted files.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


ARCHIVE_EXTENSIONS = {".bsa", ".ba2"}
TEXT_EXTENSIONS = {".json", ".xml", ".ini", ".csv", ".txt", ".md", ".jsonl"}
BETHESDA_STRING_EXTENSIONS = {".strings", ".dlstrings", ".ilstrings"}
FLASH_INTERFACE_EXTENSIONS = {".swf", ".gfx"}


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


def require_under(path: Path, parent: Path, label: str) -> None:
    if not is_under(path, parent):
        raise ValueError(f"{label} must be under {parent}: {path}")


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True))).replace("\\", "/")
    except ValueError:
        return str(value)


def relative_child_path(parent: Path, child: Path) -> str:
    return str(child.resolve(strict=False).relative_to(parent.resolve(strict=True))).replace("\\", "/")


def safe_file_name(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().strip(".")
    if not safe:
        raise ValueError("Archive base name cannot be empty after sanitization.")
    return safe


def archive_content_route(file: Path, relative_inside_archive: str) -> tuple[str, str, str, str]:
    relative_for_match = relative_inside_archive.replace("/", "\\")
    extension = file.suffix.lower()
    if re.search(r"(^|\\)interface\\translations\\.*\.txt$", relative_for_match, re.IGNORECASE):
        return (
            "interface-translation",
            "translatable",
            ".codex/skills/text-resource-translation",
            "Translate as direct replacement text resource.",
        )
    if extension in TEXT_EXTENSIONS:
        return (
            "text-resource",
            "translatable",
            ".codex/skills/text-resource-translation",
            "Parse structurally and preserve keys/tags/placeholders.",
        )
    if extension == ".pex":
        return (
            "pex-visible-strings",
            "decoder-required",
            ".codex/skills/pex-visible-strings-translation",
            "Export visible strings with PexStringToolPath before translation.",
        )
    if extension == ".psc":
        return (
            "psc-read-only",
            "manual-review",
            ".codex/skills/pex-visible-strings-translation",
            "Read-only context extraction only; do not rewrite or compile.",
        )
    if extension in BETHESDA_STRING_EXTENSIONS:
        return (
            "bethesda-strings",
            "decoder-required",
            ".codex/skills/esp-esm-esl-translation",
            "Requires a supported STRINGS decoder/importer before translation.",
        )
    if extension in FLASH_INTERFACE_EXTENSIONS:
        return (
            "flash-interface",
            "manual-review",
            "manual-review",
            "May contain UI text; no project-local decoder is configured yet.",
        )
    return ("non-text-or-unknown", "not-routed", "none", "No translation route inferred.")


def collect_file_rows(root: Path, extracted_dir: Path) -> list[ArchiveFileRow]:
    rows: list[ArchiveFileRow] = []
    for file in sorted((item for item in extracted_dir.rglob("*") if item.is_file()), key=lambda item: str(item).lower()):
        relative_inside_archive = relative_child_path(extracted_dir, file)
        kind, risk, skill, notes = archive_content_route(file, relative_inside_archive)
        rows.append(
            ArchiveFileRow(
                RelativePath=relative_inside_archive,
                ProjectPath=relative_path(root, file),
                Extension=file.suffix.lower(),
                Size=file.stat().st_size,
                Kind=kind,
                Risk=risk,
                RecommendedSkill=skill,
                Notes=notes,
            )
        )
    return rows


def count_by(rows: list[ArchiveFileRow], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(getattr(row, field))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_manifest(
    root: Path,
    mod_name: str,
    archive_path: Path,
    extracted_dir: Path,
    output_dir: Path,
    report_path: Path,
    rows: list[ArchiveFileRow],
) -> None:
    translatable_count = sum(1 for row in rows if row.Risk == "translatable")
    decoder_required_count = sum(1 for row in rows if row.Risk == "decoder-required")
    manual_review_count = sum(1 for row in rows if row.Risk == "manual-review")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest = {
        "ModName": mod_name,
        "ArchivePath": relative_path(root, archive_path),
        "ExtractedDir": relative_path(root, extracted_dir),
        "GeneratedAt": generated_at,
        "FilesScanned": len(rows),
        "TranslatableFiles": translatable_count,
        "DecoderRequiredFiles": decoder_required_count,
        "ManualReviewFiles": manual_review_count,
        "ByKind": count_by(rows, "Kind"),
        "ByRisk": count_by(rows, "Risk"),
        "Files": [asdict(row) for row in rows],
        "Safety": {
            "ProjectLocalOnly": True,
            "ArchiveModified": False,
            "ExtractedContentModified": False,
            "RealGameDirectoriesAccessed": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    files_jsonl_path = output_dir / "files.jsonl"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with files_jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")

    lines = [
        "# Archive Audit Manifest Report",
        "",
        f"- ModName: {mod_name}",
        f"- Archive: {relative_path(root, archive_path)}",
        f"- ExtractedDir: {relative_path(root, extracted_dir)}",
        f"- Manifest: {relative_path(root, manifest_path)}",
        f"- Generated at: {generated_at}",
        f"- Files scanned: {len(rows)}",
        f"- Translatable files: {translatable_count}",
        f"- Decoder-required files: {decoder_required_count}",
        f"- Manual-review files: {manual_review_count}",
        "",
        "## By Kind",
        "",
    ]
    by_kind = count_by(rows, "Kind")
    if not by_kind:
        lines.append("No files were found in ExtractedDir.")
    else:
        lines.extend(["| Kind | Count |", "|---|---:|"])
        for key, count in by_kind.items():
            lines.append(f"| {markdown_cell(key)} | {count} |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script did not extract, modify, repack, or delete archive content.",
            "- ArchivePath, ExtractedDir, OutputDir, and ReportOutputPath were checked to stay inside the project.",
            "- ExtractedDir must be under work/.",
            "- This manifest is evidence for `scripts/audit_archive_coverage.py`; it is not proof of translation quality by itself.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a project-local manifest for already extracted BSA/BA2 archive contents.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--extracted-dir", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = project_root()
    archive_path = resolve_project_path(root, args.archive_path, must_exist=True)
    extracted_dir = resolve_project_path(root, args.extracted_dir, must_exist=True)
    work_root = resolve_project_path(root, "work", must_exist=False)
    out_root = resolve_project_path(root, "out", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)

    if archive_path.suffix.lower() not in ARCHIVE_EXTENSIONS:
        raise ValueError(f"ArchivePath must be .bsa or .ba2: {args.archive_path}")
    if not extracted_dir.is_dir():
        raise ValueError(f"ExtractedDir must be an existing project-local directory: {args.extracted_dir}")
    require_under(extracted_dir, work_root, "ExtractedDir")
    if is_under(extracted_dir, archive_path):
        raise ValueError("ExtractedDir must not be inside the archive path.")

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
    require_under(output_dir, out_root, "OutputDir")
    require_under(report_path, qa_root, "ReportOutputPath")
    if output_dir.exists() and any(output_dir.iterdir()) and not args.force:
        raise ValueError(f"OutputDir exists and is not empty. Re-run with --force to replace manifest files: {args.output_dir or output_dir}")

    rows = collect_file_rows(root, extracted_dir)
    write_manifest(root, args.mod_name, archive_path, extracted_dir, output_dir, report_path, rows)

    translatable_count = sum(1 for row in rows if row.Risk == "translatable")
    decoder_required_count = sum(1 for row in rows if row.Risk == "decoder-required")
    manual_review_count = sum(1 for row in rows if row.Risk == "manual-review")
    print(f"Archive audit manifest written to: {output_dir / 'manifest.json'}")
    print(f"Archive audit report written to: {report_path}")
    print(f"Files scanned: {len(rows)}")
    print(f"Translatable files: {translatable_count}")
    print(f"Decoder-required files: {decoder_required_count}")
    print(f"Manual-review files: {manual_review_count}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Archive audit manifest failed: {exc}", file=sys.stderr)
        sys.exit(1)
