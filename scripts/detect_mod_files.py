"""Inventory files under the project-local mod/ sandbox."""

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path

from game_context import GameContext
from project_paths import is_under, project_root, relative_path, resolve_project_path
from route_translation_task import ba2_adapter_ready, current_game_context, route_for
from report_utils import markdown_cell_plain as markdown_cell


def is_interface_translation(root: Path, file_path: Path) -> bool:
    rel = relative_path(root, file_path).replace("/", "\\").lower()
    return "\\interface\\translations\\" in rel and file_path.suffix.lower() == ".txt"


def extension_label(path: Path) -> str:
    return path.suffix.lower() or "(none)"


def write_inventory(
    root: Path,
    scan_root: Path,
    report_path: Path,
    files: list[Path],
    context: GameContext | None = None,
) -> None:
    context = context or current_game_context(root)
    ext_counts = Counter(file_path.suffix.lower() for file_path in files)
    interface_files = [file_path for file_path in files if is_interface_translation(root, file_path)]
    file_routes = [
        (file_path, route_for(root, file_path, context))
        for file_path in sorted(files, key=lambda item: str(item).casefold())
    ]
    mcm_files = [file_path for file_path, route in file_routes if route.container == "mcm"]
    ba2_route = route_for(root, scan_root / "dummy.ba2", context)
    if not context.can_materialize_archive(".ba2"):
        ba2_adapter_status = "inventory-only"
    else:
        ba2_adapter_status = "ready" if ba2_adapter_ready(root, context) else "blocked"

    lines = [
        "# Mod Inventory",
        "",
        f"- GameId: {context.game_id}",
        f"- GameName: {context.display_name}",
        f"- Scan root: {scan_root}",
        f"- Scanned at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Files scanned: {len(files)}",
        f"- BA2 materialization adapter: {ba2_adapter_status}",
        f"- BA2 route: {ba2_route.skill}",
        "- Scope: read-only scan of current project path",
        "",
        "## Counts",
        "",
        "| Type | Count | Recommended Skill | Recommended Tool |",
        "|---|---:|---|---|",
    ]
    for group in context.resource_model.extension_groups:
        for extension in sorted(group.extensions):
            dummy = scan_root / f"dummy{extension}"
            route = route_for(root, dummy, context)
            lines.append(
                f"| {extension} | {ext_counts.get(extension, 0)} | "
                f"{route.skill} | {route.primary_tool} |"
            )

    lines.append(
        f"| `Interface/translations/*.txt` | {len(interface_files)} | skills/text-resource-translation | Agent Text Pipeline |"
    )
    lines.append(f"| `MCM related` | {len(mcm_files)} | skills/mcm-translation | Agent Structured MCM Extractor |")
    lines.extend(
        [
            "",
            "## File Routes",
            "",
            "| File | Extension | Category | Subtype | Container | Traits | Capability | Effective Capability | Recommended Skill | Recommended Tool | Risk |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )

    for file_path, route in file_routes:
        traits = ", ".join(route.traits) or "(none)"
        lines.append(
            f"| {markdown_cell(relative_path(root, file_path))} | {extension_label(file_path)} | "
            f"{route.category} | {route.subtype} | {route.container or '(none)'} | {traits} | "
            f"{route.capability or '(none)'} | {route.effective_capability} | {route.skill} | "
            f"{route.primary_tool} | {route.risk} |"
        )

    lines.extend(
        [
            "",
            "## Safety Notes",
            "",
            "- This script does not open plugin binaries.",
            "- This script does not call LexTranslator or xTranslator.",
            "- This script does not modify any file under mod/ or work/.",
            "- Project-local `.zip` archives should be extracted read-only to `work/extracted_mods/<ModName>/` before translation and final_mod assembly; `.bsa` routes to `bsa-archive-audit`, `.ba2` routes to `ba2-archive-audit` and materializes only when its controlled adapter is ready, `.7z` uses py7zr or Archive7zPath, and `.rar` requires handoff.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a project-local Bethesda Mod workspace and write a profile-aware route inventory.")
    parser.add_argument("--scan-path", default="mod")
    parser.add_argument("--report-path", default="qa/mod_inventory.md")
    args = parser.parse_args()

    root = project_root()
    scan_root = resolve_project_path(root, args.scan_path, must_exist=True)
    if not scan_root.is_dir():
        raise ValueError(f"ScanPath must be a project-local directory: {args.scan_path}")
    report_path = resolve_project_path(root, args.report_path, must_exist=False)
    if not is_under(report_path, root):
        raise ValueError(f"ReportPath must stay inside the project: {args.report_path}")

    files = [item for item in scan_root.rglob("*") if item.is_file()]
    write_inventory(root, scan_root, report_path, files)
    print(f"Mod inventory written to: {report_path}")
    print(f"Files scanned: {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
