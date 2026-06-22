"""Launch the project-local Mutagen plugin text adapter with strict path guards.

This wrapper is the only Python entry that may request ESP/ESM/ESL writeback.
The actual binary writing is done by the adapter, and outputs must stay under
out/<ModName>/tool_outputs/.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from project_paths import plugin_root, project_root


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


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
    # Keep the adapter contract narrow: source plugin from work/, translations
    # from translated/, output plugin from out/, and reports from qa/.
    if not is_under(path, parent):
        raise ValueError(f"{label} must be under {parent}: {path}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dotnet_path(root: Path, config_path: Path) -> Path:
    config = read_json(config_path)
    decoder_tools = config.get("DecoderTools")
    configured = ""
    if isinstance(decoder_tools, dict):
        configured = str(decoder_tools.get("DotNetSdkPath") or "")
    return resolve_project_path(root, configured or "tools/dotnet-sdk/dotnet.exe", must_exist=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the project-local Mutagen ESP/ESM/ESL text adapter.")
    parser.add_argument("--input-plugin-path", required=True)
    parser.add_argument("--translation-jsonl-path", required=True)
    parser.add_argument("--output-plugin-path", required=True)
    parser.add_argument("--report-path", default="qa/mutagen_plugin_text_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    source_root = plugin_root()
    adapter_project = source_root / "adapters" / "SkyrimPluginTextTool" / "SkyrimPluginTextTool.csproj"
    if not adapter_project.is_file():
        raise FileNotFoundError("missing adapters/SkyrimPluginTextTool/SkyrimPluginTextTool.csproj")

    config = resolve_project_path(root, args.config_path, must_exist=True)
    dotnet = dotnet_path(root, config)

    input_plugin = resolve_project_path(root, args.input_plugin_path, must_exist=True)
    translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
    output_plugin = resolve_project_path(root, args.output_plugin_path, must_exist=False)
    report = resolve_project_path(root, args.report_path, must_exist=False)

    require_under(input_plugin, root / "work" / "extracted_mods", "InputPluginPath")
    require_under(translation_jsonl, root / "translated", "TranslationJsonlPath")
    require_under(output_plugin, root / "out", "OutputPluginPath")
    require_under(report, root / "qa", "ReportPath")

    if input_plugin.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError("InputPluginPath must be .esp, .esm, or .esl.")
    if output_plugin.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError("OutputPluginPath must be .esp, .esm, or .esl.")

    output_plugin.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)

    # subprocess.run receives an argument list, not a shell command string, so
    # plugin paths with spaces are passed without shell expansion.
    command = [
        str(dotnet),
        "run",
        "--project",
        str(adapter_project),
        "--framework",
        "net8.0",
        "-p:TargetFrameworks=net8.0",
        "--",
        "apply",
        "--project-root",
        str(root),
        "--input-plugin",
        str(input_plugin),
        "--translation-jsonl",
        str(translation_jsonl),
        "--output-plugin",
        str(output_plugin),
        "--report",
        str(report),
    ]
    if args.dry_run:
        command.append("--dry-run")

    return subprocess.run(command, cwd=str(root), check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Mutagen plugin text tool failed: {exc}", file=sys.stderr)
        sys.exit(1)
