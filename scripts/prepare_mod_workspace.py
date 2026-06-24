"""Prepare a project-local working copy from mod/ input.

Archives are extracted read-only into work/extracted_mods/<ModName>/; existing
workspaces are reused only when --force is absent and the report says so. This
script never treats a compressed archive itself as a final_mod source.
"""

import argparse
import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from detect_mod_files import write_inventory
from project_paths import find_data_root
from route_translation_task import is_under, project_root, relative_path, resolve_project_path, route_for
from workflow_trace import trace_span


BINARY_EXTENSIONS = {".esp", ".esm", ".esl", ".bsa", ".ba2", ".pex", ".dll", ".exe"}
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


@dataclass
class ExtractionPlan:
    output_dir: Path
    warnings: list[str]
    reuse_existing_workspace: bool = False


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return cleaned.strip()


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


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


def zip_member_path(member_name: str) -> Path | None:
    normalized = member_name.replace("\\", "/")
    if not normalized.strip() or normalized.startswith("/") or normalized.startswith("//"):
        return None
    if re.match(r"^[A-Za-z]:", normalized):
        return None
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return Path(*parts)


def read_tools_config(root: Path) -> dict[str, Any]:
    config_path = root / "config" / "tools.local.json"
    if not config_path.is_file():
        return {}
    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def configured_decoder_tool(root: Path, property_name: str) -> Path | None:
    config = read_tools_config(root)
    decoder_tools = config.get("DecoderTools", {})
    if not isinstance(decoder_tools, dict):
        return None
    value = str(decoder_tools.get(property_name, "") or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.is_file():
        return None
    return resolved


def unique_stale_output_dir(output_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = output_dir.with_name(f"{output_dir.name}.stale-{timestamp}")
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = output_dir.with_name(f"{base.name}-{counter}")
        counter += 1
    return candidate


def prepare_extraction_output(root: Path, safe_mod_name: str, output_dir_value: str, force: bool) -> ExtractionPlan:
    extract_root = resolve_project_path(root, "work/extracted_mods", must_exist=False)
    extract_root.mkdir(parents=True, exist_ok=True)
    extract_root = extract_root.resolve(strict=True)

    output_dir = resolve_project_path(
        root,
        output_dir_value or str(Path("work") / "extracted_mods" / safe_mod_name),
        must_exist=False,
    )
    if not is_under(output_dir, extract_root):
        raise ValueError(f"OutputDir must be under work/extracted_mods: {output_dir_value}")
    if output_dir.resolve(strict=False) == extract_root:
        raise ValueError("OutputDir must be a child directory under work/extracted_mods, not work/extracted_mods itself.")

    warnings: list[str] = []
    if output_dir.exists():
        existing_items = list(output_dir.iterdir())
        if existing_items and not force:
            raise FileExistsError(f"OutputDir already exists and is not empty. Re-run with --force to rebuild: {output_dir}")
        if force:
            if not is_under(output_dir, extract_root):
                raise ValueError(f"Refusing to remove path outside work/extracted_mods: {output_dir}")
            if existing_items:
                stale_dir = unique_stale_output_dir(output_dir)
                try:
                    output_dir.rename(stale_dir)
                    warnings.append(f"Existing OutputDir was preserved before rebuild: {relative_path(root, stale_dir)}")
                except OSError as exc:
                    warnings.append(
                        "Force rebuild could not move the existing OutputDir, likely because a file is locked. "
                        f"Reusing existing workspace without extraction: {exc.__class__.__name__}: {exc}"
                    )
                    return ExtractionPlan(output_dir.resolve(strict=True), warnings, reuse_existing_workspace=True)
            else:
                try:
                    output_dir.rmdir()
                except OSError as exc:
                    warnings.append(
                        "Force rebuild could not remove the empty OutputDir. "
                        f"Reusing existing workspace without extraction: {exc.__class__.__name__}: {exc}"
                    )
                    return ExtractionPlan(output_dir.resolve(strict=True), warnings, reuse_existing_workspace=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return ExtractionPlan(output_dir.resolve(strict=True), warnings)


def collect_extracted_files(root: Path, output_dir: Path, skipped_entries: list[str]) -> tuple[list[str], list[str]]:
    extracted_files: list[str] = []
    binary_files: list[str] = []
    for destination in sorted((item for item in output_dir.rglob("*") if item.is_file()), key=lambda item: str(item).lower()):
        resolved = destination.resolve(strict=False)
        if not is_under(resolved, output_dir):
            skipped_entries.append(f"Unsafe extracted path ignored: {destination}")
            continue
        relative_destination = relative_path(root, resolved)
        extracted_files.append(relative_destination)
        if resolved.suffix.lower() in BINARY_EXTENSIONS:
            binary_files.append(relative_destination)
    return extracted_files, binary_files


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
        "- No real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directory was accessed.",
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


def write_blocked_archive_report(root: Path, archive_path: Path, report_path: Path, message: str) -> None:
    lines = [
        "# Archive Extraction Report",
        "",
        f"- Archive: {relative_path(root, archive_path)}",
        "- OutputDir: (not created)",
        f"- Extracted at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- Extracted files: 0",
        "- Binary files copied unmodified: 0",
        "- Skipped entries: 0",
        "- Status: blocked",
        "",
        "## Blocking Reason",
        "",
        f"- {message}",
        "",
        "## Safety",
        "",
        "- Source archive was read from project mod/ sandbox.",
        "- Archive was not modified.",
        "- No workspace files were written for the blocked archive.",
        "- No real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directory was accessed.",
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def extract_zip(
    root: Path,
    archive_path: Path,
    safe_mod_name: str,
    output_dir_value: str,
    archive_report_path: Path,
    force: bool,
) -> ExtractionResult:
    # zipfile extraction is manual so every archive member can be checked for
    # traversal before it is joined to the project-local workspace path.
    plan = prepare_extraction_output(root, safe_mod_name, output_dir_value, force)
    output_dir = plan.output_dir
    extracted_files: list[str] = []
    binary_files: list[str] = []
    skipped_entries: list[str] = []
    if plan.reuse_existing_workspace:
        extracted_files, binary_files = collect_extracted_files(root, output_dir, skipped_entries)
        result = ExtractionResult(
            output_dir=output_dir,
            extracted_files=extracted_files,
            binary_files=binary_files,
            skipped_entries=skipped_entries,
            warnings=plan.warnings,
            reused_existing_workspace=True,
        )
        write_archive_report(root, archive_path, result, archive_report_path)
        return result

    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            member_path = zip_member_path(member.filename)
            if member_path is None:
                skipped_entries.append(f"Unsafe entry skipped: {member.filename}")
                continue
            destination = (output_dir / member_path).resolve(strict=False)
            if not is_under(destination, output_dir):
                skipped_entries.append(f"Unsafe destination skipped: {member.filename}")
                continue
            if member.is_dir() or member.filename.endswith(("/", "\\")):
                destination.mkdir(parents=True, exist_ok=True)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source_handle, destination.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)

            relative_destination = relative_path(root, destination)
            extracted_files.append(relative_destination)
            if destination.suffix.lower() in BINARY_EXTENSIONS:
                binary_files.append(relative_destination)

    result = ExtractionResult(output_dir=output_dir, extracted_files=extracted_files, binary_files=binary_files, skipped_entries=skipped_entries, warnings=plan.warnings)
    write_archive_report(root, archive_path, result, archive_report_path)
    return result


def py7zr_available() -> bool:
    try:
        import py7zr  # noqa: F401
    except Exception:
        return False
    return True


def extract_7z_with_py7zr(
    root: Path,
    archive_path: Path,
    safe_mod_name: str,
    output_dir_value: str,
    archive_report_path: Path,
    force: bool,
) -> ExtractionResult:
    # py7zr is preferred over a user-local 7-Zip install because it keeps the
    # workflow inside Python and follows the same project-local report path.
    import py7zr

    plan = prepare_extraction_output(root, safe_mod_name, output_dir_value, force)
    output_dir = plan.output_dir
    skipped_entries: list[str] = []
    if plan.reuse_existing_workspace:
        extracted_files, binary_files = collect_extracted_files(root, output_dir, skipped_entries)
        result = ExtractionResult(
            output_dir=output_dir,
            extracted_files=extracted_files,
            binary_files=binary_files,
            skipped_entries=skipped_entries,
            warnings=plan.warnings,
            reused_existing_workspace=True,
        )
        write_archive_report(root, archive_path, result, archive_report_path)
        return result
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        names = archive.getnames()
        for name in names:
            member_path = zip_member_path(name)
            if member_path is None:
                skipped_entries.append(f"Unsafe entry blocked before extraction: {name}")
        if skipped_entries:
            write_blocked_archive_report(root, archive_path, archive_report_path, "Unsafe 7z archive member path(s) were found; extraction stopped.")
            raise ValueError("Unsafe 7z archive member path(s) were found; extraction stopped.")
        archive.extractall(path=output_dir)

    extracted_files, binary_files = collect_extracted_files(root, output_dir, skipped_entries)
    result = ExtractionResult(output_dir=output_dir, extracted_files=extracted_files, binary_files=binary_files, skipped_entries=skipped_entries, warnings=plan.warnings)
    write_archive_report(root, archive_path, result, archive_report_path)
    return result


def list_7z_cli_members(archive7z_path: Path, archive_path: Path) -> list[str]:
    result = subprocess.run(
        [str(archive7z_path), "l", "-slt", str(archive_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"7z list exited with code {result.returncode}"
        raise RuntimeError(message)
    names: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("Path = "):
            value = line[len("Path = ") :].strip()
            if value and value != str(archive_path):
                names.append(value)
    return names


def extract_7z_with_cli(
    root: Path,
    archive7z_path: Path,
    archive_path: Path,
    safe_mod_name: str,
    output_dir_value: str,
    archive_report_path: Path,
    force: bool,
) -> ExtractionResult:
    # CLI fallback is allowed only when the path is explicitly configured. We
    # list members first and block unsafe entries before extraction starts.
    plan = prepare_extraction_output(root, safe_mod_name, output_dir_value, force)
    output_dir = plan.output_dir
    skipped_entries: list[str] = []
    if plan.reuse_existing_workspace:
        extracted_files, binary_files = collect_extracted_files(root, output_dir, skipped_entries)
        result_payload = ExtractionResult(
            output_dir=output_dir,
            extracted_files=extracted_files,
            binary_files=binary_files,
            skipped_entries=skipped_entries,
            warnings=plan.warnings,
            reused_existing_workspace=True,
        )
        write_archive_report(root, archive_path, result_payload, archive_report_path)
        return result_payload
    for name in list_7z_cli_members(archive7z_path, archive_path):
        member_path = zip_member_path(name)
        if member_path is None:
            skipped_entries.append(f"Unsafe entry blocked before extraction: {name}")
    if skipped_entries:
        write_blocked_archive_report(root, archive_path, archive_report_path, "Unsafe 7z archive member path(s) were found; extraction stopped.")
        raise ValueError("Unsafe 7z archive member path(s) were found; extraction stopped.")

    result = subprocess.run(
        [str(archive7z_path), "x", "-y", f"-o{output_dir}", str(archive_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"7z extract exited with code {result.returncode}"
        write_blocked_archive_report(root, archive_path, archive_report_path, message)
        raise RuntimeError(message)

    extracted_files, binary_files = collect_extracted_files(root, output_dir, skipped_entries)
    result_payload = ExtractionResult(output_dir=output_dir, extracted_files=extracted_files, binary_files=binary_files, skipped_entries=skipped_entries, warnings=plan.warnings)
    write_archive_report(root, archive_path, result_payload, archive_report_path)
    return result_payload


def extract_7z(
    root: Path,
    archive_path: Path,
    safe_mod_name: str,
    output_dir_value: str,
    archive_report_path: Path,
    force: bool,
) -> ExtractionResult:
    if py7zr_available():
        return extract_7z_with_py7zr(root, archive_path, safe_mod_name, output_dir_value, archive_report_path, force)
    archive7z_path = configured_decoder_tool(root, "Archive7zPath")
    if archive7z_path is not None:
        return extract_7z_with_cli(root, archive7z_path, archive_path, safe_mod_name, output_dir_value, archive_report_path, force)
    message = "No 7z extractor is available. Install Python package py7zr or configure DecoderTools.Archive7zPath in config/tools.local.json."
    write_blocked_archive_report(root, archive_path, archive_report_path, message)
    raise RuntimeError(message)


def write_workflow_report(
    root: Path,
    report_path: Path,
    mod_name: str,
    source: Path,
    workspace: Path,
    files: list[Path],
    steps: list[str],
) -> None:
    route_samples = []
    for file_path in sorted(files, key=lambda item: str(item).lower()):
        route = route_for(root, file_path)
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
            "- Zip extraction, if used, wrote only to `work/extracted_mods/`.",
            "- This script did not access real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a project-local Skyrim Mod workspace, inventory, and route report.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--inventory-report-path", default="qa/mod_inventory.md")
    parser.add_argument("--archive-report-path", default="qa/archive_extraction_report.md")
    parser.add_argument("--report-output-path", default="qa/workflow_report.md")
    args = parser.parse_args()

    root = project_root()
    source = select_source(root, args.source_path)
    mod_name = safe_file_name(args.mod_name.strip() or default_mod_name(source))
    if not mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")

    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    inventory_report_path = resolve_project_path(root, args.inventory_report_path, must_exist=False)
    archive_report_path = resolve_project_path(root, args.archive_report_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    for path_value, label in (
        (report_path, "ReportOutputPath"),
        (inventory_report_path, "InventoryReportPath"),
        (archive_report_path, "ArchiveReportPath"),
    ):
        if not is_under(path_value, qa_root):
            raise ValueError(f"{label} must stay under qa/: {path_value}")

    steps = [f"Source selected: {relative_path(root, source)}"]
    if source.is_dir():
        # Directory input may contain a wrapper folder. Keep the workspace
        # untouched and only report the detected Skyrim Data root for later
        # build/QA steps.
        with trace_span(
            "input.scan",
            stage="input_discovered",
            attributes={"mod_name": mod_name, "source_path": relative_path(root, source), "source_type": "directory"},
            root=root,
        ) as span:
            extracted_workspace = source.resolve(strict=True)
            workspace = find_data_root(extracted_workspace).resolve(strict=True)
            span.set_attribute("workspace", relative_path(root, workspace))
            if args.output_dir:
                raise ValueError("--output-dir is only valid when the source is an archive.")
            steps.append("Source is already a directory; using it as the working copy.")
            if workspace != extracted_workspace:
                steps.append(f"Detected Skyrim Data root inside source: {relative_path(root, workspace)}")
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
                if extension == ".zip":
                    extraction = extract_zip(root, source, mod_name, args.output_dir, archive_report_path, args.force)
                else:
                    extraction = extract_7z(root, source, mod_name, args.output_dir, archive_report_path, args.force)
                workspace = find_data_root(extraction.output_dir).resolve(strict=True)
                span.set_attribute("workspace", relative_path(root, workspace))
                span.set_attribute("extracted_files", len(extraction.extracted_files))
                span.set_attribute("binary_files", len(extraction.binary_files))
                span.set_attribute("reused_existing_workspace", extraction.reused_existing_workspace)
                steps.append(f"Archive extracted to: {relative_path(root, extraction.output_dir)}")
                if workspace != extraction.output_dir:
                    steps.append(f"Detected Skyrim Data root inside archive: {relative_path(root, workspace)}")
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
        write_inventory(root, workspace, inventory_report_path, files)
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
        write_workflow_report(root, report_path, mod_name, source, workspace, files, steps)

    print("Workflow prepared.")
    print(f"Workspace: {workspace}")
    print(f"Workflow report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
