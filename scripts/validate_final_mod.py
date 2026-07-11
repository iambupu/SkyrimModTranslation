"""Validate the final_mod directory shape and direct-replacement delivery rules."""

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from project_paths import LOCALIZATION_OUTPUT_DIR, final_mod_dir as default_final_mod_dir
from project_paths import intermediate_output_dir, packaged_mod_path
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


def read_json(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def write_text(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_final_file(final_mod: Path, path: Path) -> str:
    final_relative = relative_path(final_mod, path).replace("\\", "/")
    return f"final_mod/{final_relative}"


def source_is_mod_input(root: Path, source_value: str) -> bool:
    if not source_value or source_value.startswith("generated:"):
        return False
    source_base = source_value.split("::", 1)[0]
    try:
        source_candidate = resolve_project_path(root, source_base, must_exist=False)
    except ValueError:
        return False
    mod_root = resolve_project_path(root, "mod", must_exist=False)
    extracted_root = resolve_project_path(root, "work/extracted_mods", must_exist=False)
    return is_under(source_candidate, mod_root) or is_under(source_candidate, extracted_root)


def docs_directory_is_mod_shipped(root: Path, final_mod: Path, docs_dir: Path, rows_by_file: dict[str, dict[str, object]]) -> bool:
    docs_files = sorted(item for item in docs_dir.rglob("*") if item.is_file())
    if not docs_files:
        return False
    for file_path in docs_files:
        expected = normalized_final_file(final_mod, file_path).lower()
        row = rows_by_file.get(expected)
        if row is None:
            return False
        if str(row.get("transform", "")) != "original-copy":
            return False
        if not source_is_mod_input(root, str(row.get("source", ""))):
            return False
    return True


def read_provenance_rows(path: Path, errors: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"Provenance JSONL line {line_number} is invalid JSON: {exc.msg}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"Provenance JSONL line {line_number} is not an object.")
            continue
        rows.append(payload)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a project-local out/<ModName>/汉化产出/final_mod directory.")
    parser.add_argument("--final-mod-dir", required=True)
    parser.add_argument("--report-output-path", default="qa/final_mod_validation.md")
    args = parser.parse_args()

    root = project_root()
    out_root = resolve_project_path(root, "out", must_exist=True)
    final_mod = resolve_project_path(root, args.final_mod_dir, must_exist=True)
    if not final_mod.is_dir():
        raise ValueError(f"FinalModDir must be a directory: {args.final_mod_dir}")
    if not is_under(final_mod, out_root):
        raise ValueError(f"FinalModDir must be under out/<ModName>/汉化产出/final_mod: {args.final_mod_dir}")
    relative_final = str(final_mod.resolve(strict=True).relative_to(out_root.resolve(strict=True))).replace("/", "\\")
    layout_match = re.match(rf"^([^\\]+)\\{re.escape(LOCALIZATION_OUTPUT_DIR)}\\final_mod$", relative_final, re.I)
    if not layout_match:
        raise ValueError(f"FinalModDir must be exactly out/<ModName>/汉化产出/final_mod: {args.final_mod_dir}")
    mod_name = layout_match.group(1)
    expected_final_mod = default_final_mod_dir(root, mod_name).resolve(strict=False)
    if final_mod.resolve(strict=False) != expected_final_mod:
        raise ValueError(f"FinalModDir does not match canonical localization output layout: {args.final_mod_dir}")

    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    files = sorted(item for item in final_mod.rglob("*") if item.is_file())
    dirs = sorted(item for item in final_mod.rglob("*") if item.is_dir())
    errors: list[str] = []
    warnings: list[str] = []

    plugin_files = [item for item in files if item.suffix.lower() in {".esp", ".esm", ".esl"}]
    common_dirs = ["Interface", "Scripts", "SKSE", "Meshes", "Textures", "Sound", "Seq", "MCM"]
    existing_top_dirs = {item.name.lower(): item.name for item in final_mod.iterdir() if item.is_dir()}
    present_dirs = [name for name in common_dirs if name.lower() in existing_top_dirs]
    missing_dirs = [name for name in common_dirs if name.lower() not in existing_top_dirs]

    manifest_path = final_mod / "meta" / "manifest.json"
    provenance_path = final_mod / "meta" / "provenance.jsonl"
    redistribution_notes_path = final_mod / "meta" / "redistribution_notes.md"
    intermediate_dir = intermediate_output_dir(root, mod_name)
    dictionary_dir = intermediate_dir / "translation_text_dictionary"
    dictionary_manifest_path = dictionary_dir / "manifest.json"
    dictionary_jsonl_path = dictionary_dir / "translation_dictionary.jsonl"
    package_path = packaged_mod_path(root, mod_name)
    manifest: dict[str, object] | None = None
    dictionary_manifest: dict[str, object] | None = None
    if not manifest_path.is_file():
        errors.append("Missing meta/manifest.json")
    else:
        manifest = read_json(manifest_path)
        if manifest is None:
            errors.append("meta/manifest.json is not valid JSON")
    if not redistribution_notes_path.is_file():
        errors.append("Missing meta/redistribution_notes.md")
    if not provenance_path.is_file():
        errors.append("Missing meta/provenance.jsonl")
    if not intermediate_dir.is_dir():
        errors.append(f"Missing intermediate output directory: {relative_path(root, intermediate_dir)}")
    elif not dictionary_dir.is_dir():
        errors.append(f"Missing intermediate translation text dictionary: {relative_path(root, dictionary_dir)}")
    else:
        if not dictionary_manifest_path.is_file():
            errors.append(f"Missing translation text dictionary manifest: {relative_path(root, dictionary_manifest_path)}")
        else:
            dictionary_manifest = read_json(dictionary_manifest_path)
            if dictionary_manifest is None:
                errors.append(f"Translation text dictionary manifest is not valid JSON: {relative_path(root, dictionary_manifest_path)}")
        if not dictionary_jsonl_path.is_file():
            errors.append(f"Missing normalized translation dictionary JSONL: {relative_path(root, dictionary_jsonl_path)}")
    if not package_path.is_file():
        errors.append(f"Missing packaged CHS mod: {relative_path(root, package_path)}")
    elif not package_path.name.lower().endswith("_chs.zip"):
        errors.append(f"Packaged mod name must end with _CHS.zip: {relative_path(root, package_path)}")

    for item in [*files, *dirs]:
        relative = relative_path(final_mod, item).replace("/", "\\")
        if re.search(r"(?i)(^|\\)Data\\Data(\\|$)", relative):
            errors.append(f"Nested Data/Data detected: {relative}")
        if re.search(r"(?i)(^|\\)mod\\mod(\\|$)", relative):
            errors.append(f"Nested mod/mod detected: {relative}")

    for archive in [item for item in files if item.suffix.lower() in {".zip", ".rar", ".7z"}]:
        errors.append(f"Archive residue in final_mod: {relative_path(root, archive)}")

    project_dir_names = {"work", "qa", "glossary", "tools", "skills", "translated"}
    root_docs_dirs: list[Path] = []
    for item in final_mod.iterdir():
        if item.is_dir() and item.name.lower() in project_dir_names:
            errors.append(f"Project engineering directory mixed into final_mod root: {relative_path(root, item)}")
        elif item.is_dir() and item.name.lower() == "docs":
            root_docs_dirs.append(item)

    empty_dirs = [item for item in dirs if not any(item.iterdir())]
    if len(empty_dirs) > 20:
        warnings.append(f"Many empty directories detected: {len(empty_dirs)}")
    if not plugin_files and not present_dirs:
        warnings.append("No .esp/.esm/.esl plugin file found. This may be valid for asset-only mods.")
    if not present_dirs:
        warnings.append("No common Skyrim Data directories were found among Interface, Scripts, SKSE, Meshes, Textures, Sound, Seq, MCM.")

    delivery_mode = "unknown"
    replacement_count = 0
    added_overlay_count = 0
    binary_tool_output_count = 0
    provenance_entry_count = 0
    provenance_missing_count = 0
    provenance_hash_mismatch_count = 0
    provenance_source_mismatch_count = 0
    packaged_mod_manifest = ""
    intermediate_manifest = ""
    dictionary_entry_count = 0
    dictionary_source_file_count = 0
    added_overlay_paths: list[str] = []
    if manifest is not None:
        delivery_mode = str(manifest.get("DeliveryMode", "unknown"))
        if delivery_mode != "direct-replacement-final-mod":
            warnings.append(f"Manifest DeliveryMode is not direct-replacement-final-mod: {delivery_mode}")
        replacement_count = len(manifest.get("ReplacementFilesApplied", []) or [])
        added_overlay_paths = [str(item) for item in (manifest.get("AddedOverlayFiles", []) or [])]
        added_overlay_count = len(added_overlay_paths)
        binary_tool_output_count = len(manifest.get("BinaryToolOutputsApplied", []) or [])
        try:
            provenance_entry_count = int(manifest.get("ProvenanceEntryCount", 0) or 0)
        except (TypeError, ValueError):
            errors.append("Manifest ProvenanceEntryCount is not numeric.")
        packaged_mod_manifest = str(manifest.get("PackagedModPath", "") or "")
        intermediate_manifest = str(manifest.get("IntermediateOutputDir", "") or "")
        provenance_manifest = str(manifest.get("ProvenancePath", "") or "")
        if provenance_manifest and provenance_manifest.replace("/", "\\").lower() != relative_path(root, provenance_path).replace("/", "\\").lower():
            errors.append(f"Manifest ProvenancePath does not match expected provenance path: {provenance_manifest}")
        if str(manifest.get("OutputLayout", "") or "") != "mod-root/localization-output/final_mod-intermediate-package":
            errors.append("Manifest OutputLayout does not confirm the required localization output layout.")
        if packaged_mod_manifest and packaged_mod_manifest.replace("/", "\\").lower() != relative_path(root, package_path).replace("/", "\\").lower():
            errors.append(f"Manifest PackagedModPath does not match expected CHS package: {packaged_mod_manifest}")
        if intermediate_manifest and intermediate_manifest.replace("/", "\\").lower() != relative_path(root, intermediate_dir).replace("/", "\\").lower():
            errors.append(f"Manifest IntermediateOutputDir does not match expected intermediate directory: {intermediate_manifest}")
        manifest_dictionary_count = manifest.get("TranslationDictionaryEntryCount", None)
        if manifest_dictionary_count is None:
            errors.append("Manifest does not record TranslationDictionaryEntryCount.")

    if dictionary_manifest is not None:
        try:
            dictionary_entry_count = int(dictionary_manifest.get("TranslatedEntryCount", 0) or 0)
        except (TypeError, ValueError):
            errors.append("Translation text dictionary TranslatedEntryCount is not numeric.")
        try:
            dictionary_source_file_count = int(dictionary_manifest.get("SourceFileCount", 0) or 0)
        except (TypeError, ValueError):
            errors.append("Translation text dictionary SourceFileCount is not numeric.")
        dictionary_jsonl_manifest = str(dictionary_manifest.get("DictionaryJsonl", "") or "")
        if dictionary_jsonl_manifest and dictionary_jsonl_manifest.replace("/", "\\").lower() != relative_path(root, dictionary_jsonl_path).replace("/", "\\").lower():
            errors.append(f"Translation text dictionary manifest points to unexpected JSONL path: {dictionary_jsonl_manifest}")
    if dictionary_jsonl_path.is_file():
        translated_lines = [line for line in dictionary_jsonl_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if dictionary_entry_count and len(translated_lines) != dictionary_entry_count:
            errors.append(
                f"Translation text dictionary line count does not match manifest: jsonl={len(translated_lines)} manifest={dictionary_entry_count}"
            )
        dictionary_entry_count = max(dictionary_entry_count, len(translated_lines))
    if dictionary_entry_count <= 0:
        errors.append("Intermediate translation text dictionary has no translated source-target entries.")

    rows_by_file: dict[str, dict[str, object]] = {}
    if provenance_path.is_file():
        provenance_rows = read_provenance_rows(provenance_path, errors)
        if provenance_entry_count and len(provenance_rows) != provenance_entry_count:
            errors.append(
                f"Provenance entry count does not match manifest: jsonl={len(provenance_rows)} manifest={provenance_entry_count}"
            )
        provenance_entry_count = max(provenance_entry_count, len(provenance_rows))
        required_keys = {"file", "file_sha256", "source", "source_sha256", "transform", "tool", "status"}
        for row in provenance_rows:
            missing_keys = sorted(key for key in required_keys if key not in row)
            row_file = str(row.get("file", ""))
            if missing_keys:
                errors.append(f"Provenance row is missing required keys {missing_keys}: {row_file or '(unknown file)'}")
            normalized = row_file.replace("\\", "/").lower()
            if not normalized.startswith("final_mod/"):
                errors.append(f"Provenance file path must start with final_mod/: {row_file}")
                continue
            if normalized in rows_by_file:
                errors.append(f"Duplicate provenance row for final_mod file: {row_file}")
                continue
            rows_by_file[normalized] = row

        for file_path in files:
            expected = normalized_final_file(final_mod, file_path)
            if expected.lower() == "final_mod/meta/provenance.jsonl":
                continue
            row = rows_by_file.get(expected.lower())
            if row is None:
                provenance_missing_count += 1
                errors.append(f"Missing provenance row for final_mod file: {expected}")
                continue
            recorded_sha = str(row.get("file_sha256", "")).lower()
            actual_sha = sha256_file(file_path).lower()
            if recorded_sha != actual_sha:
                provenance_hash_mismatch_count += 1
                errors.append(f"Provenance file_sha256 mismatch for {expected}")

            source_value = str(row.get("source", ""))
            source_sha = str(row.get("source_sha256", "")).lower()
            if source_value and not source_value.startswith("generated:"):
                source_base = source_value.split("::", 1)[0]
                source_candidate = resolve_project_path(root, source_base, must_exist=False)
                if not source_candidate.is_file():
                    provenance_source_mismatch_count += 1
                    errors.append(f"Provenance source file is missing: {source_value}")
                elif source_sha != sha256_file(source_candidate).lower():
                    provenance_source_mismatch_count += 1
                    errors.append(f"Provenance source_sha256 mismatch for {expected}: {source_value}")

        self_row = rows_by_file.get("final_mod/meta/provenance.jsonl")
        if self_row is None:
            errors.append("Provenance JSONL must include a self-referential row for final_mod/meta/provenance.jsonl.")
        elif str(self_row.get("status", "")) != "self-referential":
            errors.append("Provenance self row must use status self-referential.")

    for docs_dir in root_docs_dirs:
        if not docs_directory_is_mod_shipped(root, final_mod, docs_dir, rows_by_file):
            errors.append(
                "Root Docs directory is allowed only for files copied unchanged from the original Mod input with "
                f"original-copy provenance: {relative_path(root, docs_dir)}"
            )

    language_sidecar_files = []
    for file_path in files:
        relative = relative_path(final_mod, file_path).replace("/", "\\")
        if re.match(r"(?i)^Interface\\translations\\[^\\]+_(chinese|cn|zh|zhcn|zh_cn|schinese)\.txt$", relative):
            language_sidecar_files.append(file_path)

    language_sidecar_overlays: list[str] = []
    added_overlay_set = {item.lower() for item in added_overlay_paths}
    for file_path in language_sidecar_files:
        project_relative = relative_path(root, file_path)
        if project_relative.lower() in added_overlay_set:
            language_sidecar_overlays.append(project_relative)
    if language_sidecar_overlays:
        for sidecar in language_sidecar_overlays:
            errors.append(f"Language sidecar overlay added to final_mod instead of direct replacement: {sidecar}")
    elif language_sidecar_files:
        for sidecar in language_sidecar_files:
            warnings.append(
                f"Language sidecar file exists in final_mod; confirm it came from the original mod or has explicit loader evidence: {relative_path(root, sidecar)}"
            )

    lines = [
        "# Final Mod Validation",
        "",
        f"- FinalModDir: {final_mod}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Files: {len(files)}",
        f"- Directories: {len(dirs)}",
        "",
        "## Structure",
        "",
        f"- Plugin files: {len(plugin_files)}",
        f"- Present common directories: {', '.join(present_dirs)}",
        f"- Missing common directories: {', '.join(missing_dirs)}",
        f"- Manifest: {'present' if manifest_path.is_file() else 'missing'}",
        f"- Redistribution notes: {'present' if redistribution_notes_path.is_file() else 'missing'}",
        f"- Provenance: {'present' if provenance_path.is_file() else 'missing'}",
        "",
        "## Delivery",
        "",
        f"- Delivery mode: {delivery_mode}",
        f"- Localization output dir: {relative_path(root, final_mod.parent)}",
        f"- Intermediate output dir: {relative_path(root, intermediate_dir)} ({intermediate_dir.is_dir()})",
        f"- Translation text dictionary: {relative_path(root, dictionary_dir)} ({dictionary_dir.is_dir()})",
        f"- Translation dictionary entries: {dictionary_entry_count}",
        f"- Translation dictionary source files: {dictionary_source_file_count}",
        f"- Packaged CHS mod: {relative_path(root, package_path)} ({package_path.is_file()})",
        f"- Direct replacement files: {replacement_count}",
        f"- Added overlay files: {added_overlay_count}",
        f"- Binary tool outputs applied: {binary_tool_output_count}",
        f"- Language sidecar files: {len(language_sidecar_files)}",
        f"- Language sidecar overlays: {len(language_sidecar_overlays)}",
        "",
        "## Provenance",
        "",
        f"- Provenance path: {relative_path(root, provenance_path)}",
        f"- Provenance entries: {provenance_entry_count}",
        f"- Missing provenance rows: {provenance_missing_count}",
        f"- Final file SHA256 mismatches: {provenance_hash_mismatch_count}",
        f"- Source SHA256 mismatches: {provenance_source_mismatch_count}",
        "",
        "## Errors",
        "",
    ]
    lines.extend([f"- {item}" for item in errors] or ["No blocking errors."])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["No warnings."])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This validation did not modify final_mod content.",
            "- This validation did not access real Skyrim or MO2/Vortex directories.",
        ]
    )

    write_text(report_path, lines)
    print(f"Final mod validation written to: {report_path}")
    if errors:
        print(f"Validation failed with {len(errors)} error(s).")
        return 1
    print("Validation completed with no blocking errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
