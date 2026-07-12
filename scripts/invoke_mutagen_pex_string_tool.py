"""Launch the project-local Mutagen PEX visible-string adapter.

Export mode is read-only and may inspect project outputs. Apply mode may only
write a project-local PEX copy under out/ or translated/tool_outputs/.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from dotnet_adapter_cache import ensure_adapter_dll
from game_context import GameContext, load_game_context, load_game_profile
from project_paths import plugin_root, project_root


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


def require_under(path: Path, allowed_roots: list[Path], label: str) -> None:
    if not any(is_under(path, allowed) for allowed in allowed_roots):
        allowed_text = ", ".join(str(root) for root in allowed_roots)
        raise ValueError(f"{label} must be under one of: {allowed_text}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dotnet_path(root: Path, config_path: Path) -> Path:
    config = read_json(config_path)
    decoder_tools = config.get("DecoderTools")
    configured = ""
    if isinstance(decoder_tools, dict):
        configured = str(decoder_tools.get("DotNetSdkPath") or "")
    return resolve_project_path(root, configured or "tools/dotnet-sdk/dotnet.exe", must_exist=True)


def resolve_game_context(root: Path, explicit_game: str) -> GameContext:
    marker_exists = (root / ".skyrim-chs-workspace.json").is_file()
    marker_context = load_game_context(root) if marker_exists else load_game_profile("skyrim-se")
    if marker_exists and explicit_game and explicit_game != marker_context.game_id:
        raise ValueError(
            f"explicit game '{explicit_game}' conflicts with workspace marker game '{marker_context.game_id}'"
        )
    return load_game_profile(explicit_game) if explicit_game else marker_context


def validate_apply_output_path(root: Path, value: str) -> Path:
    output_pex = resolve_project_path(root, value, must_exist=False)
    require_under(output_pex, [root / "out", root / "translated" / "tool_outputs"], "OutputPexPath")
    if output_pex.suffix.lower() != ".pex":
        raise ValueError("OutputPexPath must be .pex.")
    return output_pex


def build_command(
    root: Path,
    dotnet: Path,
    adapter_dll: Path,
    args: argparse.Namespace,
    context: GameContext,
) -> list[str]:
    # Validate mode-specific paths before building the command. The adapter
    # should never receive a real game PEX path or write outside project outputs.
    input_pex = resolve_project_path(root, args.input_pex_path, must_exist=True)
    report = resolve_project_path(root, args.report_path, must_exist=False)
    if input_pex.suffix.lower() != ".pex":
        raise ValueError("InputPexPath must be .pex.")
    require_under(report, [root / "qa", root / "out"], "ReportPath")

    command = [
        str(dotnet),
        str(adapter_dll),
        args.mode.lower(),
        "--game",
        context.game_id,
        "--project-root",
        str(root),
        "--input-pex",
        str(input_pex),
        "--report",
        str(report),
    ]

    if args.mode == "Export":
        # Export is also used for final_mod re-read verification, so it may read
        # from work/, out/, or translated/tool_outputs/.
        require_under(
            input_pex,
            [root / "work" / "extracted_mods", root / "out", root / "translated" / "tool_outputs"],
            "InputPexPath for Export",
        )
        output_jsonl = resolve_project_path(root, args.output_jsonl_path, must_exist=False)
        require_under(output_jsonl, [root / "source" / "pex_exports", root / "work" / "normalized"], "OutputJsonlPath")
        if output_jsonl.suffix.lower() != ".jsonl":
            raise ValueError("OutputJsonlPath must be .jsonl.")
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--output-jsonl", str(output_jsonl)])
    elif args.mode == "Apply":
        # Apply is stricter than export: it starts from the prepared workspace
        # PEX and writes only a generated project-local output copy.
        require_under(input_pex, [root / "work" / "extracted_mods"], "InputPexPath for Apply")
        translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
        output_pex = validate_apply_output_path(root, args.output_pex_path)
        require_under(translation_jsonl, [root / "translated", root / "work" / "normalized"], "TranslationJsonlPath")
        output_pex.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--translation-jsonl", str(translation_jsonl), "--output-pex", str(output_pex)])
        if args.dry_run:
            command.append("--dry-run")
        if args.allow_experimental_writeback:
            command.append("--allow-experimental-writeback")
    else:
        require_under(input_pex, [root / "work" / "extracted_mods"], "InputPexPath for Verify")
        translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
        output_pex = validate_apply_output_path(root, args.output_pex_path)
        if not output_pex.is_file():
            raise ValueError(f"OutputPexPath for Verify must exist: {args.output_pex_path}")
        require_under(translation_jsonl, [root / "translated", root / "work" / "normalized"], "TranslationJsonlPath")
        if report.suffix.lower() != ".md":
            raise ValueError("ReportPath for Verify must be .md.")
        report.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--translation-jsonl", str(translation_jsonl), "--output-pex", str(output_pex)])

    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the project-local Mutagen PEX visible string adapter.")
    parser.add_argument("--mode", choices=("Export", "Apply", "Verify"), required=True)
    parser.add_argument("--input-pex-path", required=True)
    parser.add_argument("--translation-jsonl-path", default="")
    parser.add_argument("--output-pex-path", default="")
    parser.add_argument("--output-jsonl-path", default="")
    parser.add_argument("--report-path", default="qa/mutagen_pex_string_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--game", choices=("skyrim-se", "fallout4"), default="")
    parser.add_argument("--allow-experimental-writeback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    source_root = plugin_root()
    context = resolve_game_context(root, args.game)

    if args.mode == "Export" and not args.output_jsonl_path:
        raise ValueError("--output-jsonl-path is required for Export.")
    if args.mode in {"Apply", "Verify"} and (not args.translation_jsonl_path or not args.output_pex_path):
        raise ValueError(f"--translation-jsonl-path and --output-pex-path are required for {args.mode}.")
    if args.mode == "Export" and not context.pex_export_supported:
        raise ValueError(f"PEX export is not supported for game profile '{context.game_id}'.")

    output_pex: Path | None = None
    if args.mode == "Apply":
        if context.pex_writeback_status == "experimental" and not args.allow_experimental_writeback:
            raise ValueError(
                f"PEX writeback for '{context.game_id}' is experimental; "
                "pass --allow-experimental-writeback for an explicit project-local attempt."
            )
        output_pex = validate_apply_output_path(root, args.output_pex_path)
        output_pex.unlink(missing_ok=True)

    config = resolve_project_path(root, args.config_path, must_exist=True)
    dotnet = dotnet_path(root, config)
    adapter_dll = ensure_adapter_dll(root, source_root, dotnet, "SkyrimPexStringTool")

    command = build_command(root, dotnet, adapter_dll, args, context)
    result = subprocess.run(command, cwd=str(root), check=False)
    if result.returncode != 0 and output_pex is not None:
        output_pex.unlink(missing_ok=True)
    return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Mutagen PEX string tool failed: {exc}", file=sys.stderr)
        sys.exit(1)
