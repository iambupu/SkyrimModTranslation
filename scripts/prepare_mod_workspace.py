"""Prepare a project-local working copy from mod/ input.

Archives and directory inputs are materialized into
work/extracted_mods/<ModName>/ under the enforced scale policy. L2-L4 may reuse
hash-matching shards; this script never treats a compressed archive itself as a
final_mod source.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from audit_mod_scale import (
    assess_source,
    resolve_scale_config_path,
    write_scale_assessment,
)
from detect_mod_files import write_inventory
from game_context import GameContext
from mod_materialization import materialize_source
from mod_scale_policy import resolve_scale_execution_policy, write_scale_execution_report
from project_paths import find_data_root, safe_file_name
from project_paths import is_under, project_root, relative_path, resolve_project_path
from route_translation_task import current_game_context, route_for
from workflow_trace import trace_span
from report_utils import markdown_cell_plain as markdown_cell


HANDOFF_EXTENSIONS = {".rar", ".bsa", ".ba2"}
PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
COMMON_DATA_DIRS = {"interface", "scripts", "skse", "meshes", "textures", "sound", "seq", "mcm"}


@dataclass
class ExtractionResult:
    output_dir: Path
    extracted_files: list[str]
    binary_files: list[str]
    skipped_entries: list[str]
    warnings: list[str]
    reused_existing_workspace: bool = False


def select_source(root: Path, source_path: str) -> Path:
    mod_root = resolve_project_path(root, "mod", must_exist=True)
    if source_path.strip():
        source = resolve_project_path(root, source_path, must_exist=True)
    else:
        candidates = sorted((item for item in mod_root.iterdir() if item.name != ".gitkeep"), key=lambda item: item.name.lower())
        if not candidates:
            raise FileNotFoundError("No Mod source found under mod/.")
        if len(candidates) > 1:
            candidate_list = ", ".join(relative_path(root, item) for item in candidates)
            raise ValueError(f"Multiple Mod sources found under mod/. Pass --source-path explicitly: {candidate_list}")
        source = candidates[0]

    if not is_under(source, mod_root):
        raise ValueError(f"SourcePath must be under mod/: {relative_path(root, source)}")
    return source


def default_mod_name(source: Path) -> str:
    return source.name if source.is_dir() else source.stem


def write_archive_report(root: Path, archive_path: Path, result: ExtractionResult, report_path: Path) -> None:
    lines = [
        "# Archive Extraction Report",
        "",
        f"- Archive: {relative_path(root, archive_path)}",
        f"- OutputDir: {relative_path(root, result.output_dir)}",
        f"- Extracted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Extracted files: {len(result.extracted_files)}",
        f"- Binary files copied unmodified: {len(result.binary_files)}",
        f"- Skipped entries: {len(result.skipped_entries)}",
        f"- Warnings: {len(result.warnings)}",
        f"- Reused existing workspace: {result.reused_existing_workspace}",
        "",
        "## Safety",
        "",
        "- Source archive was read from project mod/ sandbox.",
        "- Archive was not modified.",
        "- Output is a derived working copy under work/extracted_mods.",
        "- Binary entries were extracted unmodified for workflow analysis and final assembly only.",
        "- No real game installation, MO2, Vortex, Steam, AppData, or Documents/My Games directory was accessed.",
        "",
        "## Extracted Files",
        "",
    ]
    lines.extend(f"- {item}" for item in result.extracted_files)
    lines.extend(["", "## Skipped Entries", ""])
    if result.skipped_entries:
        lines.extend(f"- {item}" for item in result.skipped_entries)
    else:
        lines.append("No entries were skipped.")
    lines.extend(["", "## Warnings", ""])
    if result.warnings:
        lines.extend(f"- {item}" for item in result.warnings)
    else:
        lines.append("No warnings.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_directory_report(root: Path, source_path: Path, result: ExtractionResult, report_path: Path) -> None:
    lines = [
        "# Input Preparation Report",
        "",
        f"- Directory source: {relative_path(root, source_path)}",
        f"- OutputDir: {relative_path(root, result.output_dir)}",
        f"- Prepared at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Copied files: {len(result.extracted_files)}",
        f"- Binary files copied unmodified: {len(result.binary_files)}",
        f"- Skipped entries: {len(result.skipped_entries)}",
        f"- Warnings: {len(result.warnings)}",
        f"- Reused existing workspace: {result.reused_existing_workspace}",
        "",
        "## Safety",
        "",
        "- Source directory was read from the project mod/ sandbox.",
        "- Source files were not modified.",
        "- Output is a derived working copy under work/extracted_mods.",
        "- Binary files were copied byte-for-byte for later controlled processing.",
        "",
        "## Copied Files",
        "",
    ]
    lines.extend(f"- {item}" for item in result.extracted_files)
    lines.extend(["", "## Warnings", ""])
    if result.warnings:
        lines.extend(f"- {item}" for item in result.warnings)
    else:
        lines.append("No warnings.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_workflow_report(
    root: Path,
    report_path: Path,
    mod_name: str,
    source: Path,
    workspace: Path,
    files: list[Path],
    steps: list[str],
    context: GameContext | None = None,
) -> None:
    context = context or current_game_context(root)
    route_samples = []
    for file_path in sorted(files, key=lambda item: str(item).lower()):
        route = route_for(root, file_path, context)
        if route.skill != "manual-review":
            route_samples.append(route)

    lines = [
        "# Workflow Report",
        "",
        f"- ModName: {mod_name}",
        f"- Source: {relative_path(root, source)}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- Prepared at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Files in workspace: {len(files)}",
        "",
        "## Steps",
        "",
    ]
    lines.extend(f"- {step}" for step in steps)
    lines.extend(
        [
            "",
            "## Routed Files",
            "",
            "| File | Skill | Tool | Risk |",
            "|---|---|---|---|",
        ]
    )
    for route in route_samples:
        lines.append(f"| {markdown_cell(route.path)} | {route.skill} | {route.primary_tool} | {route.risk} |")

    lines.extend(
        [
            "",
            "## Recommended Next Steps",
            "",
            "1. Translate `Interface/translations/*.txt` first when present.",
            "2. Run file-type QA before overlaying translated files.",
            "3. Use LexTranslator/xTranslator GUI automation only after `config/tools.local.json` validates and decoder-first paths cannot complete the writeback.",
            "4. Assemble final_mod from this workspace, not directly from the compressed archive.",
            "",
            "## Safety",
            "",
            "- Source was restricted to project `mod/`.",
            "- Directory preparation and archive extraction, if used, wrote only to `work/extracted_mods/`.",
            "- This script did not access real game installation, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a project-local Bethesda Mod workspace, inventory, and route report.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--inventory-report-path", default="qa/mod_inventory.md")
    parser.add_argument("--archive-report-path", default="qa/archive_extraction_report.md")
    parser.add_argument("--report-output-path", default="qa/workflow_report.md")
    parser.add_argument("--scale-config-path", default="")
    parser.add_argument("--scale-report-path", default="")
    parser.add_argument("--scale-execution-report-path", default="")
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-file-bytes", type=int)
    parser.add_argument("--max-total-bytes", type=int)
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--max-parallel-tasks", type=int)
    parser.add_argument("--max-parallel-binary-tasks", type=int)
    parser.add_argument("--max-parallel-archive-tasks", type=int)
    parser.add_argument("--checkpoint-every-files", type=int)
    parser.add_argument("--translation-batch-rows", type=int)
    parser.add_argument("--extract-mode", choices=("full", "filtered", "selective", "selective-sharded"))
    parser.add_argument("--package-mode", choices=("complete", "translation-overlay"))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    root = project_root()
    source = select_source(root, args.source_path)
    mod_name = safe_file_name(args.mod_name.strip() or default_mod_name(source))
    if not mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")
    context = current_game_context(root)

    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    inventory_report_path = resolve_project_path(root, args.inventory_report_path, must_exist=False)
    archive_report_path = resolve_project_path(root, args.archive_report_path, must_exist=False)
    scale_report_value = args.scale_report_path or f"qa/{mod_name}.scale_assessment.json"
    scale_report_path = resolve_project_path(root, scale_report_value, must_exist=False)
    scale_execution_value = args.scale_execution_report_path or f"qa/{mod_name}.scale_execution.json"
    scale_execution_path = resolve_project_path(root, scale_execution_value, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    for path_value, label in (
        (report_path, "ReportOutputPath"),
        (inventory_report_path, "InventoryReportPath"),
        (archive_report_path, "ArchiveReportPath"),
        (scale_report_path, "ScaleReportPath"),
        (scale_execution_path, "ScaleExecutionReportPath"),
    ):
        if not is_under(path_value, qa_root):
            raise ValueError(f"{label} must stay under qa/: {path_value}")

    steps = [f"Source selected: {relative_path(root, source)}"]
    scale_policy = None
    scale_config_path = None
    scale_assessment_ready = False
    try:
        scale_config_path = resolve_scale_config_path(root, args.scale_config_path)
        scale_payload = assess_source(
            root,
            source,
            mod_name,
            context,
            scale_config_path,
        )
        write_scale_assessment(scale_report_path, scale_payload)
        scale_assessment_ready = True
        steps.append(
            "Scale assessment written before materialization: "
            f"{relative_path(root, scale_report_path)} "
            f"({scale_payload['scale_level']}-{scale_payload['risk_level']})"
        )
    except Exception as exc:
        scale_report_path.unlink(missing_ok=True)
        scale_execution_path.unlink(missing_ok=True)
        failure_message = f"Scale assessment failed; bounded materialization is blocked: {type(exc).__name__}: {exc}"
        steps.append(failure_message)
        write_scale_execution_report(
            scale_execution_path,
            {
                "schema_version": 1,
                "report_type": "mod-scale-execution",
                "mod_name": mod_name,
                "game_id": context.game_id,
                "status": "blocked",
                "error": failure_message,
            },
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            "\n".join(
                [
                    "# Workflow Report",
                    "",
                    f"- ModName: {mod_name}",
                    f"- Source: {relative_path(root, source)}",
                    "- Status: blocked",
                    "",
                    "## Steps",
                    "",
                    f"- {failure_message}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        raise ValueError(failure_message) from exc
    if scale_assessment_ready and scale_config_path is not None:
        output_value = args.output_dir or str(Path("work") / "extracted_mods" / mod_name)
        materialization_output = resolve_project_path(root, output_value, must_exist=False)
        overrides = {
            "max_files": args.max_files,
            "max_file_bytes": args.max_file_bytes,
            "max_total_bytes": args.max_total_bytes,
            "timeout_seconds": args.timeout_seconds,
            "max_parallel_tasks": args.max_parallel_tasks,
            "max_parallel_binary_tasks": args.max_parallel_binary_tasks,
            "max_parallel_archive_tasks": args.max_parallel_archive_tasks,
            "checkpoint_every_files": args.checkpoint_every_files,
            "translation_batch_rows": args.translation_batch_rows,
            "extract_mode": args.extract_mode,
            "package_mode": args.package_mode,
        }
        try:
            scale_policy, execution_report = resolve_scale_execution_policy(
                root=root,
                mod_name=mod_name,
                assessment_path=scale_report_path,
                config_path=scale_config_path,
                output_path=materialization_output,
                overrides=overrides,
                expected_game_id=context.game_id,
            )
        except Exception as exc:
            failure_report = {
                "schema_version": 1,
                "report_type": "mod-scale-execution",
                "mod_name": mod_name,
                "status": "blocked",
                "assessment_path": relative_path(root, scale_report_path).replace("\\", "/"),
                "requested_overrides": {key: value for key, value in overrides.items() if value is not None},
                "error": f"{type(exc).__name__}: {exc}",
            }
            write_scale_execution_report(scale_execution_path, failure_report)
            raise
        write_scale_execution_report(scale_execution_path, execution_report)
        steps.append(
            "Scale execution policy enforced: "
            f"{relative_path(root, scale_execution_path)} "
            f"({scale_policy.extract_mode}, {scale_policy.package_mode})"
        )
    resume_materialization = bool(
        args.resume or scale_policy.scale_level in {"L2", "L3", "L4"}
    )
    if source.is_dir():
        # Directory input may contain a wrapper folder. Materialize it into the
        # same derived workspace root used for archives so downstream coverage
        # and QA gates see one canonical workspace shape.
        with trace_span(
            "input.scan",
            stage="input_discovered",
            attributes={"mod_name": mod_name, "source_path": relative_path(root, source), "source_type": "directory"},
            root=root,
        ) as span:
            bounded = materialize_source(
                root=root,
                mod_name=mod_name,
                source=source,
                output_dir=materialization_output,
                context=context,
                policy=scale_policy,
                force=args.force,
                resume=resume_materialization,
            )
            directory_copy = ExtractionResult(
                output_dir=bounded.output_dir,
                extracted_files=bounded.extracted_files,
                binary_files=bounded.binary_files,
                skipped_entries=bounded.skipped_entries,
                warnings=bounded.warnings,
                reused_existing_workspace=bounded.reused_files > 0 and bounded.materialized_files == 0,
            )
            steps.append(
                f"Materialization checkpoint: reused {bounded.reused_files}, wrote {bounded.materialized_files} files."
            )
            workspace = find_data_root(directory_copy.output_dir).resolve(strict=True)
            span.set_attribute("workspace", relative_path(root, workspace))
            span.set_attribute("copied_files", len(directory_copy.extracted_files))
            span.set_attribute("binary_files", len(directory_copy.binary_files))
            span.set_attribute("reused_existing_workspace", directory_copy.reused_existing_workspace)
            steps.append(f"Directory source copied to: {relative_path(root, directory_copy.output_dir)}")
            if directory_copy.reused_existing_workspace:
                steps.append("Existing directory workspace reused.")
            if workspace != directory_copy.output_dir:
                steps.append(f"Detected game Data root inside source: {relative_path(root, workspace)}")
            steps.append(f"Copied files: {len(directory_copy.extracted_files)}")
            steps.append(f"Binary files copied unmodified: {len(directory_copy.binary_files)}")
            write_directory_report(root, source, directory_copy, archive_report_path)
            steps.append(
                "Directory preparation report written to: "
                f"{relative_path(root, archive_report_path)}"
            )
    else:
        extension = source.suffix.lower()
        if extension in {".zip", ".7z"}:
            with trace_span(
                "archive.extract",
                stage="extracted",
                attributes={"mod_name": mod_name, "source_path": relative_path(root, source), "archive_type": extension},
                artifacts=[relative_path(root, archive_report_path)],
                root=root,
            ) as span:
                bounded = materialize_source(
                    root=root,
                    mod_name=mod_name,
                    source=source,
                    output_dir=materialization_output,
                    context=context,
                    policy=scale_policy,
                    force=args.force,
                    resume=resume_materialization,
                )
                extraction = ExtractionResult(
                    output_dir=bounded.output_dir,
                    extracted_files=bounded.extracted_files,
                    binary_files=bounded.binary_files,
                    skipped_entries=bounded.skipped_entries,
                    warnings=bounded.warnings,
                    reused_existing_workspace=bounded.reused_files > 0 and bounded.materialized_files == 0,
                )
                write_archive_report(root, source, extraction, archive_report_path)
                steps.append(
                    f"Materialization checkpoint: reused {bounded.reused_files}, wrote {bounded.materialized_files} files."
                )
                workspace = find_data_root(extraction.output_dir).resolve(strict=True)
                span.set_attribute("workspace", relative_path(root, workspace))
                span.set_attribute("extracted_files", len(extraction.extracted_files))
                span.set_attribute("binary_files", len(extraction.binary_files))
                span.set_attribute("reused_existing_workspace", extraction.reused_existing_workspace)
                steps.append(f"Archive extracted to: {relative_path(root, extraction.output_dir)}")
                if workspace != extraction.output_dir:
                    steps.append(f"Detected game Data root inside archive: {relative_path(root, workspace)}")
                steps.append(f"Extraction report written to: {relative_path(root, archive_report_path)}")
                steps.append(f"Extracted files: {len(extraction.extracted_files)}")
                steps.append(f"Binary files copied unmodified: {len(extraction.binary_files)}")
                steps.append("Archive extracted before inventory and routing.")
        elif extension in HANDOFF_EXTENSIONS:
            raise ValueError(f"{extension} is not extracted automatically. Create an explicit project-local extraction flow before translation.")
        else:
            raise ValueError(f"Unsupported source file type: {extension}")

    with trace_span(
        "input.scan",
        stage="input_discovered",
        attributes={"mod_name": mod_name, "workspace": relative_path(root, workspace)},
        artifacts=[relative_path(root, inventory_report_path)],
        root=root,
    ) as span:
        files = [item for item in workspace.rglob("*") if item.is_file()]
        write_inventory(root, workspace, inventory_report_path, files, context)
        span.set_attribute("file_count", len(files))
        steps.append(f"Mod inventory written to: {inventory_report_path}")
        steps.append(f"Files scanned: {len(files)}")
    with trace_span(
        "file.route",
        stage="routed",
        attributes={"mod_name": mod_name, "workspace": relative_path(root, workspace), "file_count": len(files)},
        artifacts=[relative_path(root, report_path)],
        root=root,
    ):
        write_workflow_report(root, report_path, mod_name, source, workspace, files, steps, context)

    print("Workflow prepared.")
    print(f"Workspace: {workspace}")
    print(f"Workflow report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
