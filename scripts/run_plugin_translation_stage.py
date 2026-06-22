"""Run the ESP/ESM/ESL translation stage for project-local plugin files."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import find_data_root
from project_paths import plugin_root as default_plugin_root
from project_paths import plugin_script_path
from project_paths import project_root


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True))).replace("\\", "/")
    except ValueError:
        return str(value).replace("\\", "/")


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return cleaned.strip()


def run_python_script(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    source_root = default_plugin_root()
    script = plugin_script_path(script_name)
    if not script.is_file():
        raise FileNotFoundError(f"missing plugin script: scripts/{script_name}")
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(root),
        env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
        capture_output=True,
        text=True,
        check=False,
    )


def process_output(result: subprocess.CompletedProcess[str]) -> str:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return " ".join(lines[-8:])


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_map_template(path: Path, rows: list[dict[str, Any]]) -> None:
    template: dict[str, str] = {}
    for row in rows:
        if str(row.get("risk", "")) != "candidate":
            continue
        source = "" if row.get("source") is None else str(row.get("source"))
        if source and source not in template:
            template[source] = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_reports(
    root: Path,
    mod_name: str,
    workspace: Path,
    report_path: Path,
    json_path: Path,
    plugin_rows: list[PluginRow],
    issues: list[Issue],
) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    lines: list[str] = [
        "# Plugin Translation Stage Report",
        "",
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
    args = parser.parse_args()

    root = project_root()
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
    detected_workspace = find_data_root(workspace).resolve(strict=True)
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
        write_reports(root, mod_name, workspace, report_path, json_path, plugin_rows, issues)
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
        verify_report = root / "qa" / f"{plugin.name}.plugin_stage_output_verification.md"

        route = run_python_script(root, "route_translation_task.py", ["--file-path", str(plugin)])
        if route.returncode != 0:
            issues.append(Issue("warning", plugin.name, "Route report could not be refreshed.", "qa/routing_report.md"))

        export = run_python_script(
            root,
            "export_esp_strings.py",
            [
                "--plugin-path",
                str(plugin),
                "--mod-name",
                mod_name,
                "--output-path",
                str(export_path),
                "--report-path",
                str(export_report),
            ],
        )
        if export.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Plugin export failed: {process_output(export)}", relative_path(root, export_report)))
            plugin_rows.append(PluginRow(plugin.name, "export_failed", 0, 0, "", "", "", relative_path(root, export_report)))
            continue

        rows = read_jsonl_rows(export_path)
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
            write_map_template(template_path, rows)
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

        write_result = run_python_script(
            root,
            "invoke_mutagen_plugin_text_tool.py",
            [
                "--input-plugin-path",
                str(plugin),
                "--translation-jsonl-path",
                str(translation_jsonl),
                "--output-plugin-path",
                str(tool_output),
                "--report-path",
                f"qa/{plugin.name}.plugin_stage_mutagen_write.md",
            ],
        )
        if write_result.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Mutagen plugin writeback failed: {process_output(write_result)}", f"qa/{plugin.name}.plugin_stage_mutagen_write.md"))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "writeback_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    relative_path(root, tool_output),
                    f"qa/{plugin.name}.plugin_stage_mutagen_write.md",
                )
            )
            continue

        output_export = run_python_script(
            root,
            "export_esp_strings.py",
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
                "--warn-only",
            ],
        )
        if verify.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Plugin verification failed: {process_output(verify)}", relative_path(root, verify_report)))
            status = "verification_failed"
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

    write_reports(root, mod_name, workspace, report_path, json_path, plugin_rows, issues)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Plugin translation stage report written to: {report_path}")
    print(f"Plugin translation stage JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
