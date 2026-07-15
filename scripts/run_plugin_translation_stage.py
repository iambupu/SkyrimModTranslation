"""Run the ESP/ESM/ESL translation stage for project-local plugin files."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from adapter_registry import require_adapter, require_script_entrypoint
from capability_resolver import CapabilityDecision, resolve_capability
from game_context import GameContext, load_game_context, load_game_profile, supported_game_ids
from project_paths import find_data_root
from project_paths import project_root
from project_paths import safe_file_name
from route_translation_task import route_for, write_report as write_route_report
from project_paths import is_under, resolve_project_path, relative_posix_path as relative_path
from model_review_contract import read_jsonl_objects
from workflow_process import run_plugin_python as run_python_script
from report_utils import markdown_cell


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
EXPERIMENTAL_WRITE_WARNING = (
    "Experimental plugin writeback produced a project-local copy; it is not a stable "
    "delivery and still requires independent in-game validation."
)


@dataclass
class PluginRow:
    Plugin: str
    Status: str
    Candidates: int
    ReviewRows: int
    TranslationMap: str
    TranslationJsonl: str
    ToolOutput: str
    Evidence: str


@dataclass
class Issue:
    Severity: str
    Plugin: str
    Message: str
    Evidence: str = ""


def resolve_plugin_text_access(
    context: GameContext,
) -> tuple[CapabilityDecision, CapabilityDecision]:
    read = resolve_capability(context, "plugin_text", "read")
    write = resolve_capability(context, "plugin_text", "write")
    if not read.supported:
        raise ValueError(read.reason)
    if not read.adapter_id:
        raise ValueError("plugin_text read capability does not declare an adapter")
    require_adapter(read.adapter_id, "extract")
    require_adapter(read.adapter_id, "verify")
    if not write.supported:
        raise ValueError(write.reason)
    if write.adapter_id != read.adapter_id:
        raise ValueError("plugin_text read/write must use the same adapter")
    require_adapter(write.adapter_id, "apply")
    return read, write


def resolve_plugin_text_entrypoints(
    context: GameContext,
) -> tuple[CapabilityDecision, CapabilityDecision, str, str, str]:
    read, write = resolve_plugin_text_access(context)
    return (
        read,
        write,
        require_script_entrypoint(read.adapter_id or "", "extract"),
        require_script_entrypoint(write.adapter_id or "", "apply"),
        require_script_entrypoint(read.adapter_id or "", "verify"),
    )


def build_export_command_args(
    *,
    plugin: Path,
    mod_name: str,
    output_path: Path,
    report_path: Path,
    game_id: str,
) -> list[str]:
    return [
        "--plugin-path",
        str(plugin),
        "--mod-name",
        mod_name,
        "--output-path",
        str(output_path),
        "--report-path",
        str(report_path),
        "--game",
        game_id,
    ]


def build_write_command_args(
    *,
    input_plugin: Path,
    translation_jsonl: Path,
    output_plugin: Path,
    report_path: Path,
    adapter_result_path: Path,
    game_id: str,
) -> list[str]:
    return [
        "--input-plugin-path",
        str(input_plugin),
        "--translation-jsonl-path",
        str(translation_jsonl),
        "--output-plugin-path",
        str(output_plugin),
        "--report-path",
        str(report_path),
        "--adapter-result-path",
        str(adapter_result_path),
        "--game",
        game_id,
    ]
def process_output(result: subprocess.CompletedProcess[str]) -> str:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return " ".join(lines[-8:])


def write_map_template(path: Path, rows: list[dict[str, Any]], context: GameContext) -> None:
    translations: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("risk", "")) != "candidate":
            continue
        translations.append(
            {
                key: row.get(key, "")
                for key in (
                    "schema_version",
                    "game_id",
                    "plugin",
                    "record_type",
                    "form_id",
                    "editor_id",
                    "field_path",
                    "subrecord_type",
                    "subrecord_index",
                    "source",
                    "risk",
                    "writeback",
                )
            }
            | {"target": ""}
        )
    template = {"schema_version": 2, "game_id": context.game_id, "translations": translations}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_reports(
    root: Path,
    mod_name: str,
    workspace: Path,
    report_path: Path,
    json_path: Path,
    plugin_rows: list[PluginRow],
    issues: list[Issue],
    context: GameContext,
) -> None:
    plugin_capability = context.capabilities.get("plugin_text")
    if plugin_capability is None:
        raise ValueError("Game profile does not declare plugin_text capability metadata.")
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    lines: list[str] = [
        "# Plugin Translation Stage Report",
        "",
        f"- game_id: {context.game_id}",
        f"- game_profile_version: {context.schema_version}",
        f"- plugin_adapter: {plugin_capability.adapter_id}",
        f"- plugin_text_capability_level: {plugin_capability.level}",
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- Plugins checked: {len(plugin_rows)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Plugins",
        "",
        "| Plugin | Status | Candidates | Review rows | Translation map | Translation JSONL | Tool output | Evidence |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for row in plugin_rows:
        lines.append(
            f"| {markdown_cell(row.Plugin)} | {row.Status} | {row.Candidates} | {row.ReviewRows} | "
            f"{markdown_cell(row.TranslationMap)} | {markdown_cell(row.TranslationJsonl)} | "
            f"{markdown_cell(row.ToolOutput)} | {markdown_cell(row.Evidence)} |"
        )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No plugin translation stage issues.")
    else:
        lines.extend(["| Severity | Plugin | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(
                f"| {issue.Severity} | {markdown_cell(issue.Plugin)} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |"
            )

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This stage never writes to mod/.",
            "- Plugin binaries are written only by the controlled Mutagen adapter into out/<ModName>/tool_outputs/.",
            "- Missing translation maps generate templates and block instead of silently copying English plugins.",
            "- Real Skyrim, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "ModName": mod_name,
                "game_id": context.game_id,
                "game_profile_version": context.schema_version,
                "plugin_adapter": plugin_capability.adapter_id,
                "plugin_text_capability_level": plugin_capability.level,
                "Workspace": relative_path(root, workspace),
                "BlockingIssues": blocking,
                "Warnings": warnings,
                "Plugins": [asdict(row) for row in plugin_rows],
                "Issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export, translate, write back, and verify project-local ESP/ESM/ESL plugin text.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", required=True)
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--json-output-path", default="")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    args = parser.parse_args()

    root = project_root()
    marker_exists = (root / ".skyrim-chs-workspace.json").is_file()
    if marker_exists:
        context = load_game_context(root)
        if args.game and args.game != context.game_id:
            raise ValueError(
                f"explicit game '{args.game}' conflicts with workspace marker game '{context.game_id}'"
            )
    else:
        context = load_game_profile(args.game or "skyrim-se")
    (
        read_capability,
        write_capability,
        export_entrypoint,
        write_entrypoint,
        verify_entrypoint,
    ) = resolve_plugin_text_entrypoints(context)
    mod_name = safe_file_name(args.mod_name)
    if not mod_name:
        raise ValueError("ModName cannot be empty.")
    workspace = resolve_project_path(root, args.workspace_path, must_exist=True)
    work_root = resolve_project_path(root, "work/extracted_mods", must_exist=False)
    mod_root = resolve_project_path(root, "mod", must_exist=False)
    if not (is_under(workspace, work_root) or is_under(workspace, mod_root)):
        raise ValueError("WorkspacePath must be under work/extracted_mods/ or mod/.")
    if not workspace.is_dir():
        raise ValueError(f"WorkspacePath must be a directory: {workspace}")
    detected_workspace = find_data_root(workspace, context=context).resolve(strict=True)
    if detected_workspace != workspace:
        workspace = detected_workspace

    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.plugin_translation_stage.md", must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path or f"qa/{mod_name}.plugin_translation_stage.json", must_exist=False)
    if not is_under(report_path, root / "qa") or not is_under(json_path, root / "qa"):
        raise ValueError("Report paths must be under qa/.")

    plugin_rows: list[PluginRow] = []
    issues: list[Issue] = []
    plugins = sorted(
        (item for item in workspace.rglob("*") if item.is_file() and item.suffix.lower() in PLUGIN_EXTENSIONS),
        key=lambda item: str(item).lower(),
    )
    if not plugins:
        write_reports(root, mod_name, workspace, report_path, json_path, plugin_rows, issues, context)
        print(f"Plugin translation stage report written to: {report_path}")
        print("No plugins found.")
        return 0

    for plugin in plugins:
        try:
            relative_plugin = plugin.resolve(strict=True).relative_to(workspace.resolve(strict=True))
        except ValueError:
            relative_plugin = Path(plugin.name)
        export_path = root / "source" / "plugin_exports" / mod_name / f"{plugin.name}_strings.jsonl"
        export_report = root / "qa" / f"{plugin.name}_export_report.md"
        glossary_match_report = root / "qa" / f"{mod_name}.{plugin.name}.external_glossary_matches.md"
        glossary_match_dir = root / "work" / "glossary_matches" / mod_name / plugin.name
        map_path = root / "work" / "plugin_translation_maps" / mod_name / f"{plugin.name}.translation_map.json"
        template_path = root / "work" / "plugin_translation_maps" / mod_name / f"{plugin.name}.translation_map.template.json"
        translation_jsonl = root / "translated" / "plugin_exports" / mod_name / f"{plugin.name}_strings.zh.jsonl"
        tool_output = root / "out" / mod_name / "tool_outputs" / relative_plugin
        tool_output_export = root / "source" / "plugin_exports" / mod_name / f"{plugin.name}_tool_output_strings.jsonl"
        tool_output_export_report = root / "qa" / f"{plugin.name}.plugin_stage_tool_output_export.md"
        adapter_verify_report = root / "qa" / f"{plugin.name}.plugin_stage_adapter_verify.md"
        verify_report = root / "qa" / f"{plugin.name}.plugin_stage_output_verification.md"
        write_report = root / "qa" / f"{plugin.name}.plugin_stage_mutagen_write.md"
        write_receipt = root / "qa" / f"{plugin.name}.plugin_stage_mutagen_write.adapter_result.json"

        try:
            route = route_for(root, plugin, context)
            write_route_report(root / "qa" / "routing_report.md", route)
        except (OSError, ValueError):
            issues.append(Issue("warning", plugin.name, "Route report could not be refreshed.", "qa/routing_report.md"))

        export = run_python_script(
            root,
            export_entrypoint,
            build_export_command_args(
                plugin=plugin,
                mod_name=mod_name,
                output_path=export_path,
                report_path=export_report,
                game_id=context.game_id,
            ),
        )
        if export.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Plugin export failed: {process_output(export)}", relative_path(root, export_report)))
            plugin_rows.append(PluginRow(plugin.name, "export_failed", 0, 0, "", "", "", relative_path(root, export_report)))
            continue

        rows = read_jsonl_objects(export_path, strict=True)
        glossary_matches = run_python_script(
            root,
            "build_external_glossary_matches.py",
            [
                "--mod-name",
                mod_name,
                "--input-path",
                str(export_path),
                "--output-dir",
                str(glossary_match_dir),
                "--report-output-path",
                str(glossary_match_report),
            ],
        )
        if glossary_matches.returncode != 0:
            issues.append(Issue("warning", plugin.name, f"External glossary match packet could not be generated: {process_output(glossary_matches)}", relative_path(root, glossary_match_report)))
        candidates = [row for row in rows if str(row.get("risk", "")) == "candidate"]
        review_rows = [row for row in rows if str(row.get("risk", "")) == "review"]
        if not candidates:
            plugin_rows.append(PluginRow(plugin.name, "no_candidates", 0, len(review_rows), "", "", "", relative_path(root, export_report)))
            continue

        if not map_path.is_file():
            write_map_template(template_path, rows, context)
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    "Translation map is missing; a template and external glossary match packet were generated for Codex/model translation.",
                    f"{relative_path(root, template_path)}; {relative_path(root, glossary_match_report)}",
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "blocked_missing_translation_map",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    "",
                    "",
                    relative_path(root, template_path),
                )
            )
            continue

        apply_result = run_python_script(
            root,
            "apply_plugin_translation_map.py",
            [
                "--export-path",
                str(export_path),
                "--translation-map-path",
                str(map_path),
                "--mod-name",
                mod_name,
                "--output-path",
                str(translation_jsonl),
                "--report-path",
                f"qa/{plugin.name}_strings.translation_map_report.md",
            ],
        )
        if apply_result.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Applying translation map failed: {process_output(apply_result)}", f"qa/{plugin.name}_strings.translation_map_report.md"))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "translation_map_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    f"qa/{plugin.name}_strings.translation_map_report.md",
                )
            )
            continue

        if tool_output.exists():
            tool_output.unlink()
        write_result = run_python_script(
            root,
            write_entrypoint,
            build_write_command_args(
                input_plugin=plugin,
                translation_jsonl=translation_jsonl,
                output_plugin=tool_output,
                report_path=write_report,
                adapter_result_path=write_receipt,
                game_id=context.game_id,
            ),
        )
        if write_result.returncode != 0:
            if tool_output.exists():
                tool_output.unlink()
            issues.append(Issue("error", plugin.name, f"Mutagen plugin writeback failed: {process_output(write_result)}", f"qa/{plugin.name}.plugin_stage_mutagen_write.md"))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "writeback_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    f"qa/{plugin.name}.plugin_stage_mutagen_write.md",
                )
            )
            continue

        output_export = run_python_script(
            root,
            export_entrypoint,
            [
                "--plugin-path",
                str(tool_output),
                "--mod-name",
                mod_name,
                "--output-path",
                str(tool_output_export),
                "--report-path",
                str(tool_output_export_report),
                "--allow-generated-plugin",
                "--game",
                context.game_id,
            ],
        )
        if output_export.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Tool output re-export failed: {process_output(output_export)}", relative_path(root, tool_output_export_report)))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "tool_output_export_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    relative_path(root, tool_output),
                    relative_path(root, tool_output_export_report),
                )
            )
            continue

        adapter_verify = run_python_script(
            root,
            verify_entrypoint,
            [
                "--mode",
                "Verify",
                "--input-plugin-path",
                str(plugin),
                "--translation-jsonl-path",
                str(translation_jsonl),
                "--output-plugin-path",
                str(tool_output),
                "--report-path",
                str(adapter_verify_report),
                "--game",
                context.game_id,
            ],
        )
        if adapter_verify.returncode != 0 or not adapter_verify_report.is_file():
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    f"Plugin adapter verification failed: {process_output(adapter_verify)}",
                    relative_path(root, adapter_verify_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "adapter_verification_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    relative_path(root, tool_output),
                    relative_path(root, adapter_verify_report),
                )
            )
            continue

        verify = run_python_script(
            root,
            "verify_plugin_output.py",
            [
                "--original-plugin-path",
                str(plugin),
                "--output-plugin-path",
                str(tool_output),
                "--translation-jsonl-path",
                str(translation_jsonl),
                "--output-export-jsonl-path",
                str(tool_output_export),
                "--report-output-path",
                str(verify_report),
                "--writeback-report-path",
                f"qa/{plugin.name}.plugin_stage_mutagen_write.md",
                "--require-translation-evidence",
                "--game",
                context.game_id,
            ],
        )
        if verify.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Plugin verification failed: {process_output(verify)}", relative_path(root, verify_report)))
            status = "verification_failed"
        else:
            if write_capability.level == "experimental_write":
                status = "experimental_tool_output_ready"
                issues.append(
                    Issue(
                        "warning",
                        plugin.name,
                        EXPERIMENTAL_WRITE_WARNING,
                        f"qa/{plugin.name}.plugin_stage_mutagen_write.adapter_result.json",
                    )
                )
            else:
                status = "translated_tool_output_ready"

        plugin_rows.append(
            PluginRow(
                plugin.name,
                status,
                len(candidates),
                len(review_rows),
                relative_path(root, map_path),
                relative_path(root, translation_jsonl),
                relative_path(root, tool_output),
                relative_path(root, verify_report),
            )
        )

    write_reports(root, mod_name, workspace, report_path, json_path, plugin_rows, issues, context)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Plugin translation stage report written to: {report_path}")
    print(f"Plugin translation stage JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
