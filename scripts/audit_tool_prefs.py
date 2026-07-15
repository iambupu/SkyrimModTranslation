"""Audit local tool configuration for unsafe real-game path preferences.

This defensive check reports risky markers in config/tool preferences; it does
not launch external tools.
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from project_paths import project_root
from project_paths import is_under, resolve_project_path, relative_path


RISKY_PATTERNS = [
    "SteamLibrary",
    "steamapps",
    r"Skyrim Special Edition\Data",
    "Skyrim Special Edition/Data",
    "ModOrganizer",
    "Vortex",
    "AppData",
    r"Documents\My Games",
    "Documents/My Games",
]








def configured_tool_path(root: Path, config: dict[str, Any], property_name: str) -> Path | None:
    value = str(config.get(property_name, "")).strip()
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def candidate_pref_files(tool_name: str, exe_path: Path) -> list[Path]:
    root = exe_path.parent
    files: list[Path] = []
    if tool_name == "XTranslatorPath":
        user_prefs = root / "UserPrefs"
        if user_prefs.is_dir():
            for path in user_prefs.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".ini", ".txt", ".xml", ".json", ".config"}:
                    files.append(path)
    else:
        for path in root.iterdir() if root.is_dir() else []:
            if path.is_file() and path.suffix.lower() in {".config", ".data", ".json", ".ini", ".txt"}:
                files.append(path)
    return sorted(set(files), key=lambda item: str(item).lower())


def scan_file(path: Path) -> list[str]:
    if path.stat().st_size > 1024 * 1024:
        return [f"Skipped large tool preference file: {path}"]
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    issues = []
    for pattern in RISKY_PATTERNS:
        if re.search(re.escape(pattern), text, re.IGNORECASE):
            issues.append(f"Risky path marker '{pattern}' found in {path}")
    return issues


def write_report(report_path: Path, scanned_files: list[Path], issues: list[str], warnings: list[str], root: Path) -> None:
    lines = [
        "# Tool Preferences Audit",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Files scanned: {len(scanned_files)}",
        f"- Blocking issues: {len(issues)}",
        f"- Warnings: {len(warnings)}",
        "",
        "## Scanned Files",
        "",
    ]
    lines.extend([f"- {relative_path(root, item)}" for item in scanned_files] or ["No tool preference files were scanned."])
    lines.extend(["", "## Issues", ""])
    lines.extend([f"- {item}" for item in issues] or ["No risky path markers found."])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["No warnings."])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script reads known tool preference files only.",
            "- This script does not follow or access any path found inside those preferences.",
            "- This script does not modify tool configuration.",
            "- This script does not open real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games directories.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit GUI tool preference files for risky real-game path markers.")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--report-output-path", default="qa/tool_prefs_audit.md")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()

    root = project_root()
    config_path = resolve_project_path(root, args.config_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    issues: list[str] = []
    warnings: list[str] = []
    scanned_files: list[Path] = []
    if not config_path.is_file():
        warnings.append("config/tools.local.json is missing; tool preference audit skipped.")
    else:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            warnings.append(f"config/tools.local.json is not valid JSON; tool preference audit skipped: {exc}")
            config = {}
        for property_name in ("XTranslatorPath", "LexTranslatorPath", "EspEsmTranslatorPath"):
            exe_path = configured_tool_path(root, config, property_name)
            if exe_path is None:
                warnings.append(f"{property_name} is not configured.")
                continue
            if not exe_path.is_file():
                warnings.append(f"{property_name} does not exist: {exe_path}")
                continue
            for file_path in candidate_pref_files(property_name, exe_path):
                try:
                    scanned_files.append(file_path)
                    found = scan_file(file_path)
                    for item in found:
                        if item.startswith("Skipped large"):
                            warnings.append(item)
                        else:
                            issues.append(item)
                except OSError as exc:
                    warnings.append(f"Could not read tool preference file {file_path}: {exc}")

    write_report(report_path, scanned_files, issues, warnings, root)
    print(f"Tool preference audit written to: {report_path}")
    if issues:
        print(f"Tool preference audit found {len(issues)} risky marker(s).")
        return 0 if args.warn_only else 1
    print("Tool preference audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
