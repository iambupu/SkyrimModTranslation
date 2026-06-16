import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def build_command(root: Path, dotnet: Path, adapter_project: Path, args: argparse.Namespace) -> list[str]:
    input_pex = resolve_project_path(root, args.input_pex_path, must_exist=True)
    report = resolve_project_path(root, args.report_path, must_exist=False)
    if input_pex.suffix.lower() != ".pex":
        raise ValueError("InputPexPath must be .pex.")
    require_under(report, [root / "qa", root / "out"], "ReportPath")

    command = [
        str(dotnet),
        "run",
        "--project",
        str(adapter_project),
        "--framework",
        "net8.0",
        "-p:TargetFrameworks=net8.0",
        "--",
        args.mode.lower(),
        "--project-root",
        str(root),
        "--input-pex",
        str(input_pex),
        "--report",
        str(report),
    ]

    if args.mode == "Export":
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
    else:
        require_under(input_pex, [root / "work" / "extracted_mods"], "InputPexPath for Apply")
        translation_jsonl = resolve_project_path(root, args.translation_jsonl_path, must_exist=True)
        output_pex = resolve_project_path(root, args.output_pex_path, must_exist=False)
        require_under(translation_jsonl, [root / "translated", root / "work" / "normalized"], "TranslationJsonlPath")
        require_under(output_pex, [root / "out", root / "translated" / "tool_outputs"], "OutputPexPath")
        if output_pex.suffix.lower() != ".pex":
            raise ValueError("OutputPexPath must be .pex.")
        output_pex.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--translation-jsonl", str(translation_jsonl), "--output-pex", str(output_pex)])
        if args.dry_run:
            command.append("--dry-run")

    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the project-local Mutagen PEX visible string adapter.")
    parser.add_argument("--mode", choices=("Export", "Apply"), required=True)
    parser.add_argument("--input-pex-path", required=True)
    parser.add_argument("--translation-jsonl-path", default="")
    parser.add_argument("--output-pex-path", default="")
    parser.add_argument("--output-jsonl-path", default="")
    parser.add_argument("--report-path", default="qa/mutagen_pex_string_tool_report.md")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = project_root()
    adapter_project = root / "tools" / "adapters" / "SkyrimPexStringTool" / "SkyrimPexStringTool.csproj"
    if not adapter_project.is_file():
        raise FileNotFoundError("missing tools/adapters/SkyrimPexStringTool/SkyrimPexStringTool.csproj")
    config = resolve_project_path(root, args.config_path, must_exist=True)
    dotnet = dotnet_path(root, config)

    if args.mode == "Export" and not args.output_jsonl_path:
        raise ValueError("--output-jsonl-path is required for Export.")
    if args.mode == "Apply" and (not args.translation_jsonl_path or not args.output_pex_path):
        raise ValueError("--translation-jsonl-path and --output-pex-path are required for Apply.")

    command = build_command(root, dotnet, adapter_project, args)
    result = subprocess.run(command, cwd=str(root), check=False)
    return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Mutagen PEX string tool failed: {exc}", file=sys.stderr)
        sys.exit(1)
