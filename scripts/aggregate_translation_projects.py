"""Aggregate validated L5 child translation overlays inside one workspace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from build_final_mod import create_package, write_provenance_jsonl
from capability_resolver import resolve_resource_capability
from file_utils import sha256_file, validate_regular_path_under
from game_context import game_context_metadata
from project_paths import (
    final_mod_dir,
    intermediate_output_dir,
    is_under,
    localization_output_root,
    packaged_mod_path,
    project_root,
    relative_path,
    resolve_project_path,
    safe_file_name,
)
from report_utils import utc_now, write_text_lines
from resource_model import classify_resource
from route_translation_task import current_game_context


@dataclass(frozen=True)
class ChildProject:
    name: str
    root: Path
    overlay: Path
    order: int
    dependencies: frozenset[str]
    overrides: frozenset[str]
    files: tuple[Path, ...]
    dictionary_rows: tuple[dict[str, Any], ...]
    provenance_by_file: dict[str, dict[str, Any]]


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return payload


def read_jsonl_objects(path: Path, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {line_number} is not an object: {path}")
        rows.append(value)
    return rows


def canonical_relative(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    if normalized.casefold().startswith("final_mod/"):
        normalized = normalized[len("final_mod/") :]
    relative = Path(*normalized.split("/"))
    if not normalized or relative.anchor or ".." in relative.parts:
        raise ValueError(f"Invalid aggregate overlay path: {value}")
    return relative.as_posix()


def dictionary_pair(row: dict[str, Any]) -> tuple[str, str]:
    source = str(row.get("source") or row.get("Source") or row.get("original") or "").strip()
    target = str(row.get("target") or row.get("Target") or row.get("Result") or row.get("translation") or "").strip()
    if not source or not target:
        raise ValueError("Aggregate translation dictionary rows require non-empty source and target")
    return source, target


def load_child_project(root: Path, child_root: Path, context_game_id: str) -> ChildProject:
    validate_regular_path_under(child_root, root, kind="directory", label="Aggregate child project")
    manifest_path = validate_regular_path_under(child_root / "manifest.json", child_root, kind="file", label="Child manifest")
    provenance_path = validate_regular_path_under(child_root / "provenance.jsonl", child_root, kind="file", label="Child provenance")
    dictionary_path = validate_regular_path_under(child_root / "translation_dictionary.jsonl", child_root, kind="file", label="Child dictionary")
    coverage_path = validate_regular_path_under(child_root / "coverage.json", child_root, kind="file", label="Child coverage")
    overlay = validate_regular_path_under(child_root / "final_overlay", child_root, kind="directory", label="Child final overlay")

    manifest = read_json_object(manifest_path, "Child manifest")
    coverage = read_json_object(coverage_path, "Child coverage")
    name = safe_file_name(str(manifest.get("project_name") or child_root.name))
    if not name or name != child_root.name:
        raise ValueError(f"Child project_name must match its directory name: {child_root.name}")
    if manifest.get("status") != "passed" or coverage.get("status") != "passed":
        raise ValueError(f"Child project is not QA-passed: {name}")
    if manifest.get("game_id") != context_game_id:
        raise ValueError(f"Child project game_id does not match aggregate workspace: {name}")
    order = manifest.get("order", 0)
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise ValueError(f"Child project order must be a non-negative integer: {name}")
    override_values = manifest.get("overrides", [])
    if not isinstance(override_values, list) or not all(isinstance(value, str) and value for value in override_values):
        raise ValueError(f"Child project overrides must be a string list: {name}")
    dependency_values = manifest.get("dependencies", [])
    if not isinstance(dependency_values, list) or not all(isinstance(value, str) and value for value in dependency_values):
        raise ValueError(f"Child project dependencies must be a string list: {name}")

    provenance = read_jsonl_objects(provenance_path, "Child provenance")
    provenance_by_file: dict[str, dict[str, Any]] = {}
    for row in provenance:
        relative = canonical_relative(str(row.get("file") or ""))
        key = relative.casefold()
        if key in provenance_by_file:
            raise ValueError(f"Child provenance contains a duplicate path: {name}/{relative}")
        if row.get("game_id") != context_game_id or row.get("status") != "assembled":
            raise ValueError(f"Child provenance identity or status is invalid: {name}/{relative}")
        if row.get("replaces_existing") is not True:
            raise ValueError(f"Child overlay is not proven to replace an existing file: {name}/{relative}")
        provenance_by_file[key] = row
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(overlay, topdown=True, followlinks=False):
        current_path = Path(current)
        for directory_name in directory_names:
            validate_regular_path_under(current_path / directory_name, overlay, kind="directory", label="Child overlay directory")
        for file_name in file_names:
            file_path = validate_regular_path_under(current_path / file_name, overlay, kind="file", label="Child overlay file")
            relative = file_path.relative_to(overlay).as_posix()
            row = provenance_by_file.get(relative.casefold())
            if row is None:
                raise ValueError(f"Child provenance does not cover overlay file: {name}/{relative}")
            if str(row.get("file_sha256") or "").casefold() != sha256_file(file_path).casefold():
                raise ValueError(f"Child provenance hash mismatch: {name}/{relative}")
            files.append(file_path)
    if not files:
        raise ValueError(f"Child project has no overlay files: {name}")
    file_keys = {path.relative_to(overlay).as_posix().casefold() for path in files}
    extra_provenance = sorted(set(provenance_by_file) - file_keys)
    if extra_provenance:
        raise ValueError(
            f"Child provenance contains files outside final_overlay: {name}/{extra_provenance[0]}"
        )
    dictionary_rows = read_jsonl_objects(dictionary_path, "Child dictionary")
    for row in dictionary_rows:
        dictionary_pair(row)
    if not dictionary_rows:
        raise ValueError(f"Child project has no dictionary rows: {name}")
    if coverage.get("overlay_files") != len(files) or coverage.get("dictionary_entries") != len(
        dictionary_rows
    ):
        raise ValueError(f"Child coverage counts do not match project artifacts: {name}")
    return ChildProject(
        name=name,
        root=child_root,
        overlay=overlay,
        order=order,
        dependencies=frozenset(dependency_values),
        overrides=frozenset(override_values),
        files=tuple(sorted(files, key=lambda path: str(path).casefold())),
        dictionary_rows=tuple(dictionary_rows),
        provenance_by_file=provenance_by_file,
    )


def analyze_projects(projects: list[ChildProject]) -> tuple[dict[str, tuple[ChildProject, Path]], list[str], list[str], list[dict[str, Any]]]:
    selected: dict[str, tuple[ChildProject, Path]] = {}
    conflicts: list[str] = []
    overlaps: list[str] = []
    combined_dictionary: list[dict[str, Any]] = []
    targets_by_source: dict[str, tuple[str, str]] = {}
    seen_dictionary_rows: set[str] = set()

    for project in sorted(projects, key=lambda item: (item.order, item.name.casefold())):
        for file_path in project.files:
            relative = file_path.relative_to(project.overlay).as_posix()
            key = relative.casefold()
            previous = selected.get(key)
            if previous is not None:
                previous_project, previous_path = previous
                if sha256_file(previous_path) == sha256_file(file_path):
                    overlaps.append(f"Identical path reused by {previous_project.name} and {project.name}: {relative}")
                    continue
                if previous_project.name not in project.overrides:
                    conflicts.append(
                        f"Path conflict without declared override: {relative} ({previous_project.name} vs {project.name})"
                    )
                    continue
                overlaps.append(f"Declared override: {project.name} replaces {previous_project.name}: {relative}")
            selected[key] = (project, file_path)

        for row in project.dictionary_rows:
            source, target = dictionary_pair(row)
            source_key = source.casefold()
            previous_target = targets_by_source.get(source_key)
            if previous_target is not None and previous_target[0] != target:
                conflicts.append(
                    f"Dictionary conflict for source {source!r}: {previous_target[0]!r} ({previous_target[1]}) vs {target!r} ({project.name})"
                )
                continue
            targets_by_source[source_key] = (target, project.name)
            normalized = {**row, "source": source, "target": target, "aggregate_project": project.name}
            row_key = json.dumps({"source": source, "target": target, "context": row.get("context", {})}, ensure_ascii=False, sort_keys=True)
            if row_key not in seen_dictionary_rows:
                seen_dictionary_rows.add(row_key)
                combined_dictionary.append(normalized)
    return selected, conflicts, overlaps, combined_dictionary


def remove_owned(path: Path, owner: Path) -> None:
    if not is_under(path, owner):
        raise ValueError(f"Refusing to remove aggregate output outside owner: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate QA-passed L5 child translation overlays.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--input-root", default="work/aggregate_inputs")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = project_root()
    context = current_game_context(root)
    mod_name = safe_file_name(args.mod_name)
    if not mod_name:
        raise ValueError("ModName cannot be empty")
    input_root = resolve_project_path(root, args.input_root, must_exist=True)
    required_input_root = resolve_project_path(root, "work/aggregate_inputs", must_exist=True)
    if not is_under(input_root, required_input_root):
        raise ValueError("Aggregate inputs must stay under work/aggregate_inputs")
    child_roots = sorted((path for path in input_root.iterdir() if path.is_dir()), key=lambda path: path.name.casefold())
    if not child_roots:
        raise ValueError("No aggregate child projects were found")
    projects = sorted(
        (load_child_project(input_root, child, context.game_id) for child in child_roots),
        key=lambda item: (item.order, item.name.casefold()),
    )
    projects_by_name = {project.name: project for project in projects}
    names = set(projects_by_name)
    for project in projects:
        unknown = project.overrides - names
        if unknown:
            raise ValueError(f"Child project {project.name} declares unknown overrides: {', '.join(sorted(unknown))}")
        unknown_dependencies = project.dependencies - names
        if unknown_dependencies:
            raise ValueError(
                f"Child project {project.name} declares unknown dependencies: {', '.join(sorted(unknown_dependencies))}"
            )
        if project.name in project.dependencies:
            raise ValueError(f"Child project {project.name} cannot depend on itself")
        invalid_order = sorted(
            dependency
            for dependency in project.dependencies
            if projects_by_name[dependency].order >= project.order
        )
        if invalid_order:
            raise ValueError(
                f"Child project {project.name} dependencies must have a lower order: {', '.join(invalid_order)}"
            )

    selected, conflicts, overlaps, combined_dictionary = analyze_projects(projects)
    aggregate_root = root / "out" / mod_name / "aggregate"
    aggregate_root.mkdir(parents=True, exist_ok=True)
    conflict_report = aggregate_root / "conflict_report.md"
    write_text_lines(
        conflict_report,
        [
            "# Aggregate Conflict Report",
            "",
            f"- Mod: {mod_name}",
            f"- Child projects: {len(projects)}",
            f"- Path/resource selections: {len(selected)}",
            f"- Blocking conflicts: {len(conflicts)}",
            f"- Resolved or identical overlaps: {len(overlaps)}",
            "",
            "## Blocking Conflicts",
            "",
            *([f"- {item}" for item in conflicts] or ["No blocking conflicts."]),
            "",
            "## Overlaps",
            "",
            *([f"- {item}" for item in overlaps] or ["No overlaps."]),
        ],
    )
    if conflicts:
        raise ValueError(f"Aggregate project has {len(conflicts)} unresolved conflict(s); see {conflict_report}")
    for _key, (_project, source_file) in sorted(selected.items()):
        relative = source_file.relative_to(_project.overlay)
        descriptor = classify_resource(context, relative)
        decision = resolve_resource_capability(context, descriptor, "write")
        if descriptor.category != "loose_text" or not decision.supported:
            raise ValueError(
                "Aggregate transfer currently requires QA-passed loose-text overlays; "
                f"adapter lineage transfer is required for {relative.as_posix()} "
                f"({descriptor.category}, {decision.level})."
            )

    output = final_mod_dir(root, mod_name)
    localization_root = localization_output_root(root, mod_name)
    if output.exists() and any(output.iterdir()):
        if not args.force:
            raise ValueError(f"Aggregate final_mod already exists; re-run with --force: {output}")
        remove_owned(output, root / "out" / mod_name)
    output.mkdir(parents=True, exist_ok=True)
    aggregate_overlay = aggregate_root / "final_overlay"
    if aggregate_overlay.exists():
        if not args.force:
            raise ValueError(
                f"Aggregate final_overlay already exists; re-run with --force: {aggregate_overlay}"
            )
        remove_owned(aggregate_overlay, aggregate_root)
    aggregate_overlay.mkdir(parents=True, exist_ok=False)
    overlay_records: list[dict[str, object]] = []
    for _key, (project, source_file) in sorted(selected.items()):
        relative = source_file.relative_to(project.overlay)
        aggregate_source = (aggregate_overlay / relative).resolve(strict=False)
        if not is_under(aggregate_source, aggregate_overlay):
            raise ValueError(f"Unsafe aggregate source destination: {relative}")
        aggregate_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, aggregate_source)
        destination = (output / relative).resolve(strict=False)
        if not is_under(destination, output):
            raise ValueError(f"Unsafe aggregate destination: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(aggregate_source, destination)
        child_manifest = project.root / "manifest.json"
        child_provenance = project.root / "provenance.jsonl"
        overlay_records.append(
            {
                "Source": relative_path(root, aggregate_source),
                "SourceSha256": sha256_file(aggregate_source),
                "Destination": relative_path(root, destination),
                "Extension": source_file.suffix.casefold(),
                "ReplacesExistingFile": True,
                "AggregateProject": project.name,
                "ProvenanceTransform": "text-resource-translation",
                "ProvenanceTool": "aggregate_translation_projects.py",
                "AggregateChildProvenance": {
                    "project": project.name,
                    "manifest": relative_path(root, child_manifest),
                    "manifest_sha256": sha256_file(child_manifest),
                    "provenance": relative_path(root, child_provenance),
                    "provenance_sha256": sha256_file(child_provenance),
                },
            }
        )

    combined_dictionary_path = aggregate_root / "combined_dictionary.jsonl"
    with combined_dictionary_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in combined_dictionary:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    coverage_path = aggregate_root / "combined_coverage.md"
    write_text_lines(
        coverage_path,
        [
            "# Aggregate Coverage",
            "",
            "- Status: passed",
            f"- Child projects: {len(projects)}",
            f"- Overlay files: {len(overlay_records)}",
            f"- Dictionary entries: {len(combined_dictionary)}",
        ],
    )
    aggregate_manifest_path = aggregate_root / "aggregate_manifest.json"
    aggregate_manifest = {
        "schema_version": 1,
        "report_type": "translation-project-aggregate",
        "generated_at": utc_now(),
        "status": "passed",
        "mod_name": mod_name,
        "game_id": context.game_id,
        "projects": [
            {
                "name": project.name,
                "order": project.order,
                "dependencies": sorted(project.dependencies),
                "overrides": sorted(project.overrides),
                "manifest_sha256": sha256_file(project.root / "manifest.json"),
                "coverage_sha256": sha256_file(project.root / "coverage.json"),
                "provenance_sha256": sha256_file(project.root / "provenance.jsonl"),
                "dictionary_sha256": sha256_file(
                    project.root / "translation_dictionary.jsonl"
                ),
            }
            for project in projects
        ],
        "overlay_files": len(overlay_records),
        "dictionary_entries": len(combined_dictionary),
        "conflict_report": relative_path(root, conflict_report).replace("\\", "/"),
        "coverage_report": relative_path(root, coverage_path).replace("\\", "/"),
    }
    aggregate_manifest_path.write_text(json.dumps(aggregate_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    intermediate = intermediate_output_dir(root, mod_name)
    dictionary_dir = intermediate / "translation_text_dictionary"
    dictionary_dir.mkdir(parents=True, exist_ok=True)
    dictionary_jsonl = dictionary_dir / "translation_dictionary.jsonl"
    shutil.copy2(combined_dictionary_path, dictionary_jsonl)
    dictionary_manifest = {
        "ModName": mod_name,
        "TranslatedEntryCount": len(combined_dictionary),
        "SourceFileCount": len(projects),
        "DictionaryJsonl": relative_path(root, dictionary_jsonl),
        "AggregateSource": relative_path(root, combined_dictionary_path),
    }
    (dictionary_dir / "manifest.json").write_text(json.dumps(dictionary_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    meta = output / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    redistribution_notes = meta / "redistribution_notes.md"
    write_text_lines(
        redistribution_notes,
        [
            "# Redistribution Notes",
            "",
            "This translation-overlay package requires the original Mod and does not include its complete assets.",
            "Public redistribution still requires permission and manual review.",
        ],
    )
    build_report = meta / "build_report.md"
    write_text_lines(
        build_report,
        [
            "# Aggregate Final Mod Build Report",
            "",
            f"- ModName: {mod_name}",
            f"- Built: {datetime.now().isoformat(timespec='seconds')}",
            "- DeliveryMode: translation-overlay-package",
            f"- Child projects: {len(projects)}",
            f"- Overlay files: {len(overlay_records)}",
        ],
    )
    package_path = packaged_mod_path(root, mod_name)
    provenance_path = meta / "provenance.jsonl"
    manifest_path = meta / "manifest.json"
    provenance_count = len([path for path in output.rglob("*") if path.is_file() and path != provenance_path]) + 2
    manifest = {
        **game_context_metadata(context),
        "ModName": mod_name,
        "BuildTime": datetime.now().isoformat(timespec="seconds"),
        "DeliveryMode": "translation-overlay-package",
        "OutputLayout": "mod-root/localization-output/final_mod-intermediate-package",
        "LocalizationOutputDir": relative_path(root, localization_root),
        "IntermediateOutputDir": relative_path(root, intermediate),
        "PackagedModPath": relative_path(root, package_path),
        "PackagedModNameSuffix": "CHS",
        "LanguagePatchOnly": False,
        "RequiresOriginalMod": True,
        "IncludesOriginalFiles": False,
        "AggregateProject": True,
        "AggregateReport": relative_path(root, aggregate_manifest_path),
        "AggregateReportSha256": sha256_file(aggregate_manifest_path),
        "SourceModDir": relative_path(root, input_root),
        "OutputDir": relative_path(root, output),
        "LocalTestingOutput": True,
        "PublicRedistributionCleared": False,
        "RedistributionNotes": relative_path(root, redistribution_notes),
        "ProvenancePath": relative_path(root, provenance_path),
        "ProvenanceEntryCount": provenance_count,
        "CopiedFiles": [],
        "OverlayFiles": [record["Destination"] for record in overlay_records],
        "ReplacementFilesApplied": [record["Destination"] for record in overlay_records],
        "AddedOverlayFiles": [],
        "BinaryFilesCopiedUnmodified": [],
        "BinaryToolOutputsApplied": [
            record["Destination"]
            for record in overlay_records
            if str(record["Extension"]) in {".esp", ".esm", ".esl", ".pex", ".strings", ".dlstrings", ".ilstrings"}
        ],
        "TranslationFilesApplied": [record["Destination"] for record in overlay_records],
        "IntermediateOutputsMirrored": [relative_path(root, dictionary_dir)],
        "TranslationTextDictionary": dictionary_manifest,
        "TranslationDictionaryEntryCount": len(combined_dictionary),
        "BsaLooseOverrideClaims": 0,
        "Ba2LooseOverrideSidecar": "",
        "Ba2LooseOverrideClaims": 0,
        "SkippedArchiveFiles": [],
        "Warnings": overlaps,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    actual_provenance_count = write_provenance_jsonl(
        root,
        output,
        provenance_path,
        [],
        overlay_records,
        mod_name,
        context,
    )
    if actual_provenance_count != provenance_count:
        raise RuntimeError(
            f"Aggregate provenance count mismatch: expected={provenance_count} actual={actual_provenance_count}"
        )
    package_info = create_package(output, package_path, root)
    write_text_lines(
        localization_root / "package_report.md",
        [
            "# Packaged CHS Mod Report",
            "",
            f"- ModName: {mod_name}",
            f"- PackagePath: {package_info['Path']}",
            "- PackageNameSuffix: CHS",
            f"- Entries: {package_info['Entries']}",
            f"- SizeBytes: {package_info['SizeBytes']}",
            "- DeliveryMode: translation-overlay-package",
        ],
    )
    print(f"Aggregate overlay built: {output}")
    print(f"Aggregate report: {aggregate_manifest_path}")
    print(f"Packaged CHS mod: {package_info['Path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
