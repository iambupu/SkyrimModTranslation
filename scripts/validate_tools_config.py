"""Validate local tool configuration without launching external tools."""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from project_paths import bool_config, is_under, project_root, resolve_project_path, risky_marker
from report_utils import markdown_cell_plain as markdown_cell


GUI_TOOL_SPECS = [
    ("LexTranslatorPath", "LexTranslator", True),
    ("XTranslatorPath", "xTranslator", True),
    ("EspEsmTranslatorPath", "ESP-ESM Translator", False),
]


@dataclass
class ToolStatus:
    tool: str
    property_name: str
    path: str
    exists: bool
    status: str






def resolve_configured_path(root: Path, value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if text.startswith("请填写"):
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    return str(candidate.resolve(strict=False))


def tool_status(root: Path, config: dict[str, Any], property_name: str, display_name: str) -> ToolStatus:
    path = resolve_configured_path(root, config.get(property_name, ""))
    if not path:
        return ToolStatus(display_name, property_name, "", False, "missing-path")
    marker = risky_marker(path)
    if marker and not is_under(Path(path), root):
        return ToolStatus(display_name, property_name, path, Path(path).is_file(), f"unsafe-path-marker:{marker}")
    exists = Path(path).is_file()
    return ToolStatus(display_name, property_name, path, exists, "ready" if exists else "path-not-found")



def write_report(
    report_path: Path,
    config_path: Path,
    config: dict[str, Any] | None,
    statuses: list[ToolStatus],
    errors: list[str],
    warnings: list[str],
) -> None:
    allow_launch = bool_config(config or {}, "AllowLaunchGuiTools", False)
    decoder_first = bool_config(config or {}, "DecoderFirst", True)
    require_project_local_io = bool_config(config or {}, "RequireProjectLocalInputOutput", True)
    lines = [
        "# Tools Config Validation",
        "",
        f"- Config: {config_path}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- DecoderFirst: {decoder_first}",
        f"- AllowLaunchGuiTools: {allow_launch}",
        f"- RequireProjectLocalInputOutput: {require_project_local_io}",
        f"- Blocking issues: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        "",
        "## Tool Paths",
        "",
        "| Tool | Property | Exists | Status | Path |",
        "|---|---|---:|---|---|",
    ]
    for status in statuses:
        lines.append(
            f"| {markdown_cell(status.tool)} | {markdown_cell(status.property_name)} | {status.exists} | {markdown_cell(status.status)} | {markdown_cell(status.path)} |"
        )
    lines.extend(["", "## Errors", ""])
    lines.extend([f"- {item}" for item in errors] or ["No blocking errors."])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["No warnings."])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This validation does not launch GUI tools.",
            "- This validation does not follow tool preference paths.",
            "- This validation does not access real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
            "- External GUI executable paths are only checked for existence; all tool input and output must remain project-local.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate local GUI tool configuration without launching tools.")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--report-output-path", default="qa/tools_config_validation.md")
    args = parser.parse_args()

    root = project_root()
    config_path = resolve_project_path(root, args.config_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    errors: list[str] = []
    warnings: list[str] = []
    config: dict[str, Any] | None = None
    if not config_path.is_file():
        errors.append("Missing config/tools.local.json")
    else:
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(parsed, dict):
                errors.append("config/tools.local.json must contain a JSON object")
            else:
                config = parsed
        except json.JSONDecodeError as exc:
            errors.append(f"config/tools.local.json is not valid JSON: {exc}")

    statuses: list[ToolStatus] = []
    if config is not None:
        allow_launch = bool_config(config, "AllowLaunchGuiTools", False)
        decoder_first = bool_config(config, "DecoderFirst", True)
        require_project_local_io = bool_config(config, "RequireProjectLocalInputOutput", True)
        if not decoder_first:
            warnings.append("DecoderFirst is false; current workflow expects decoder-first before GUI fallback.")
        if not require_project_local_io:
            errors.append("RequireProjectLocalInputOutput must remain true.")

        for property_name, display_name, required_when_gui_enabled in GUI_TOOL_SPECS:
            status = tool_status(root, config, property_name, display_name)
            statuses.append(status)
            if status.status.startswith("unsafe-path-marker"):
                errors.append(f"{display_name} path contains a forbidden marker: {status.status}")
            elif allow_launch and required_when_gui_enabled and not status.exists:
                errors.append(f"{display_name} is required because AllowLaunchGuiTools=true, but status is {status.status}.")
            elif not status.exists:
                warnings.append(f"{display_name} path is not ready; that optional tool remains unavailable until configured.")

        expected_bools = [
            "NeverTouchRealGameDirectory",
            "NeverTouchRealModManagerDirectory",
            "NeverTouchSteamGameDirectory",
            "NeverTouchAppDataGameConfig",
            "NeverTouchDocumentsMyGames",
        ]
        for name in expected_bools:
            if not bool_config(config, name, True):
                errors.append(f"{name} must remain true.")

    write_report(report_path, config_path, config, statuses, errors, warnings)
    print(f"Tools config validation written to: {report_path}")
    if errors:
        print(f"Validation failed with {len(errors)} error(s).")
        return 1
    print("Tools config validation completed with no blocking errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
