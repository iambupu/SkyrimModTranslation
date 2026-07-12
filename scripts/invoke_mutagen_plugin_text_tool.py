"""Launch the project-local Mutagen plugin text adapter with strict path guards.

This wrapper is the only Python entry that may request ESP/ESM/ESL writeback
or independent controlled-adapter verification. Apply writes under out/;
Verify is read-only and may inspect an existing final_mod plugin under out/.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from dotnet_adapter_cache import ensure_adapter_dll
from game_context import load_game_context, load_game_profile
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
    parser.add_argument("--mode", choices=("Apply", "Verify"), default="Apply")
    parser.add_argument("--input-plugin-path", required=True)
    parser.add_argument("--translation-jsonl-path", required=True)
    parser.add_argument("--output-plugin-path", required=True)
    parser.add_argument("--report-path", default="qa/mutagen_plugin_text_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--game", choices=("skyrim-se", "fallout4"), default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    output_plugin = resolve_project_path(root, args.output_plugin_path, must_exist=args.mode == "Verify")
    report = resolve_project_path(root, args.report_path, must_exist=False)

    require_under(output_plugin, root / "out", "OutputPluginPath")
    require_under(report, root / "qa", "ReportPath")

    if output_plugin.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError("OutputPluginPath must be .esp, .esm, or .esl.")
    if report.suffix.lower() != ".md":
        raise ValueError("ReportPath must be a Markdown (.md) file.")

    output_plugin.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "Apply" and output_plugin.exists():
        output_plugin.unlink()

    source_root = plugin_root()
    marker_exists = (root / ".skyrim-chs-workspace.json").is_file()
    marker_context = load_game_context(root) if marker_exists else load_game_profile("skyrim-se")
    if marker_exists and args.game and args.game != marker_context.game_id:
        raise ValueError(
            f"explicit game '{args.game}' conflicts with workspace marker game '{marker_context.game_id}'"
        )
    context = load_game_profile(args.game) if args.game else marker_context
    input_plugin = resolve_project_path(root, args.input_plugin_path, must_exist=True)
    translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
    config = resolve_project_path(root, args.config_path, must_exist=True)

    require_under(input_plugin, root / "work" / "extracted_mods", "InputPluginPath")
    require_under(translation_jsonl, root / "translated", "TranslationJsonlPath")
    if input_plugin.suffix.lower() not in PLUGIN_EXTENSIONS:
        raise ValueError("InputPluginPath must be .esp, .esm, or .esl.")
    if translation_jsonl.suffix.lower() != ".jsonl":
        raise ValueError("TranslationJsonlPath must be a JSONL (.jsonl) file.")

    dotnet = dotnet_path(root, config)
    adapter_dll = ensure_adapter_dll(root, source_root, dotnet, "SkyrimPluginTextTool")

    command = [
        str(dotnet),
        str(adapter_dll),
        args.mode.lower(),
        "--game",
        context.game_id,
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
    if args.dry_run and args.mode == "Apply":
        command.append("--dry-run")

    return_code = subprocess.run(command, cwd=str(root), check=False).returncode
    if args.mode == "Apply" and return_code != 0 and output_plugin.exists():
        output_plugin.unlink()
    return return_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Mutagen plugin text tool failed: {exc}", file=sys.stderr)
        sys.exit(1)
