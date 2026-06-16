import argparse
import importlib.util
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ToolStatus:
    Tool: str
    Property: str
    Role: str
    RecommendedUse: str
    PathType: str
    RequiresSafeWrapper: bool
    Path: str
    Exists: bool
    Status: str


TOOL_SPECS = [
    {
        "Property": "PluginTextCliPath",
        "Name": "Plugin text decoder/importer",
        "Role": "ESP/ESM/ESL",
        "Use": "Export player-visible plugin strings and optionally import translated strings into a project-local plugin copy.",
        "PathType": "Leaf",
    },
    {
        "Property": "XEditPath",
        "Name": "xEdit CLI/script runner",
        "Role": "ESP/ESM/ESL",
        "Use": "Run only through a project wrapper that supplies project-local data/master paths; raw xEdit/SSEDump can auto-detect real game paths.",
        "PathType": "Leaf",
        "RequiresSafeWrapper": True,
    },
    {
        "Property": "SafeSseDumpWrapperPath",
        "Name": "Safe SSEDump wrapper",
        "Role": "ESP/ESM/ESL wrapper",
        "Use": "Project-local SSEDump wrapper that refuses external Data paths and blocks missing masters before launch.",
        "PathType": "Leaf",
    },
    {
        "Property": "DotNetSdkPath",
        "Name": "Project-local .NET SDK",
        "Role": "Build prerequisite",
        "Use": "Build project-approved Mutagen CLI adapters without relying on system-wide SDK installation.",
        "PathType": "Leaf",
    },
    {
        "Property": "MutagenSourceDir",
        "Name": "Mutagen source tree",
        "Role": "ESP/ESM/ESL source",
        "Use": "Source for building a project-approved Mutagen CLI adapter; not a runnable decoder by itself.",
        "PathType": "Container",
    },
    {
        "Property": "MutagenCliPath",
        "Name": "Mutagen-based CLI adapter",
        "Role": "ESP/ESM/ESL",
        "Use": "Structured Bethesda plugin parsing/export/import through a project-approved adapter.",
        "PathType": "Leaf",
    },
    {
        "Property": "PexStringToolPath",
        "Name": "PEX string decoder/rewriter",
        "Role": "PEX",
        "Use": "Extract and, if explicitly supported, rewrite visible strings in a project-local PEX copy.",
        "PathType": "Leaf",
    },
    {
        "Property": "ChampollionSourceDir",
        "Name": "Champollion source tree",
        "Role": "PEX source",
        "Use": "Source for building a PEX decompiler; not a runnable decompiler until built.",
        "PathType": "Container",
    },
    {
        "Property": "PexDecompilerPath",
        "Name": "PEX decompiler",
        "Role": "PEX/PSC review",
        "Use": "Read-only decompile or string context review; not for automatic source rewrite/compile.",
        "PathType": "Leaf",
    },
    {
        "Property": "BsaExtractorPath",
        "Name": "BSA extractor",
        "Role": "BSA",
        "Use": "Extract project-local BSA archives into work/extracted_mods for text discovery.",
        "PathType": "Leaf",
    },
    {
        "Property": "Ba2ExtractorPath",
        "Name": "BA2 extractor",
        "Role": "BA2",
        "Use": "Extract project-local BA2 archives into work/extracted_mods for text discovery.",
        "PathType": "Leaf",
    },
    {
        "Property": "python-package:py7zr",
        "Name": "Python py7zr package",
        "Role": "7z",
        "Use": "Extract project-local .7z archives in Python without GUI tools.",
        "PathType": "PythonPackage",
    },
    {
        "Property": "Archive7zPath",
        "Name": "7z extractor",
        "Role": "7z/RAR",
        "Use": "Extract project-local archives when explicitly configured; never modify archives.",
        "PathType": "Leaf",
    },
]


RISKY_PATH_PATTERNS = [
    "SteamLibrary",
    "steamapps",
    r"Skyrim Special Edition\\Data",
    "ModOrganizer",
    "Vortex",
    "AppData",
    r"Documents\\My Games",
]


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


def resolve_configured_tool_path(root: Path, value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    return str(candidate.resolve(strict=False))


def has_risky_marker(path: str) -> bool:
    if not path:
        return False
    return any(re.search(re.escape(pattern), path, re.IGNORECASE) for pattern in RISKY_PATH_PATTERNS)


def path_exists(path: str, path_type: str) -> bool:
    if path_type == "PythonPackage":
        return importlib.util.find_spec(path) is not None
    if not path:
        return False
    candidate = Path(path)
    return candidate.is_file() if path_type == "Leaf" else candidate.is_dir()


def tool_status(root: Path, decoder_config: dict[str, Any] | None, spec: dict[str, Any]) -> ToolStatus:
    if spec["PathType"] == "PythonPackage":
        package_name = spec["Property"].split(":", 1)[1]
        exists = path_exists(package_name, spec["PathType"])
        return ToolStatus(
            Tool=spec["Name"],
            Property=spec["Property"],
            Role=spec["Role"],
            RecommendedUse=spec["Use"],
            PathType=spec["PathType"],
            RequiresSafeWrapper=False,
            Path=package_name,
            Exists=exists,
            Status="ready" if exists else "missing-package",
        )

    value = decoder_config.get(spec["Property"], "") if decoder_config else ""
    full_path = resolve_configured_tool_path(root, value)
    exists = path_exists(full_path, spec["PathType"]) if full_path else False

    status = "missing-path"
    if full_path:
        status = "ready" if exists else "path-not-found"
    requires_safe_wrapper = bool(spec.get("RequiresSafeWrapper", False))
    if exists and requires_safe_wrapper:
        status = "requires-safe-wrapper"
    if has_risky_marker(full_path):
        status = "unsafe-path-marker"

    return ToolStatus(
        Tool=spec["Name"],
        Property=spec["Property"],
        Role=spec["Role"],
        RecommendedUse=spec["Use"],
        PathType=spec["PathType"],
        RequiresSafeWrapper=requires_safe_wrapper,
        Path=full_path,
        Exists=exists,
        Status=status,
    )


def count_ready(tools: list[ToolStatus], predicate) -> int:
    return sum(1 for tool in tools if tool.Status == "ready" and predicate(tool))


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_report(
    report_path: Path,
    config_path: str,
    decoder_first: bool,
    allow_gui_fallback: bool,
    tools: list[ToolStatus],
    ready_by_role: dict[str, int],
    errors: list[str],
    warnings: list[str],
) -> None:
    lines = [
        "# Decoder Tools Report",
        "",
        f"- Config: {config_path}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- DecoderFirst: {decoder_first}",
        f"- AllowGuiFallback: {allow_gui_fallback}",
        f"- Plugin decoder tools ready: {ready_by_role['Plugin']}",
        f"- PEX decoder tools ready: {ready_by_role['Pex']}",
        f"- Archive decoder tools ready: {ready_by_role['Archive']}",
        f"- Decoder source trees ready: {ready_by_role['Source']}",
        f"- Safe wrapper tools ready: {ready_by_role['Wrapper']}",
        f"- Build prerequisites ready: {ready_by_role['BuildPrerequisite']}",
        "",
        "## Tool Paths",
        "",
        "| Tool | Role | Exists | Status | Path |",
        "|---|---|---:|---|---|",
    ]
    for tool in tools:
        lines.append(
            f"| {markdown_cell(tool.Tool)} | {markdown_cell(tool.Role)} | {tool.Exists} | {tool.Status} | {markdown_cell(tool.Path)} |"
        )

    lines.extend(["", "## Recommended Routing", ""])
    lines.append("- Text, MCM, Interface, JSON, XML, CSV, TXT: use Codex structured text pipeline.")
    if ready_by_role["Plugin"] > 0:
        lines.append("- ESP/ESM/ESL: use configured decoder CLI first; GUI tools are fallback.")
    elif ready_by_role["Wrapper"] > 0:
        lines.append(
            "- ESP/ESM/ESL: safe read-only wrapper is configured; it can run only when required masters are present in the project sandbox. No project-local plugin writeback CLI is configured yet."
        )
    else:
        lines.append(
            "- ESP/ESM/ESL: no decoder CLI configured; prepare text exports only or fall back to LexTranslator/xTranslator GUI when necessary."
        )
    lines.append("- Mutagen adapter build: requires project-local .NET SDK plus Mutagen source tree.")
    if ready_by_role["Pex"] > 0:
        lines.append(
            "- PEX: use configured decoder CLI for extraction/context first; writeback only if the tool explicitly supports project-local PEX rewriting."
        )
    else:
        lines.append(
            "- PEX: no decoder CLI configured; prefer Interface/translations and MCM text files, otherwise tool-exported visible strings remain required."
        )
    if ready_by_role["Archive"] > 0:
        lines.append("- BSA/BA2/7z/RAR: use configured project-local extractor flow.")
    else:
        lines.append("- BSA/BA2/7z/RAR: no extractor configured; only ZIP is handled by existing project script.")

    lines.extend(["", "## Errors", ""])
    lines.extend([f"- {item}" for item in errors] or ["No blocking errors."])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["No warnings."])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script does not launch tools.",
            "- This script does not decode, extract, import, write back, or install files.",
            "- Tool input/output must still be project-local when used by later scripts.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect configured project-local decoder tools without launching them.")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--report-output-path", default="qa/decoder_tools_report.md")
    parser.add_argument("--as-json", action="store_true")
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
        warnings.append(f"Config not found: {args.config_path}. Decoder tools are treated as unconfigured.")
    else:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            errors.append(f"Config is not valid JSON: {exc}")

    decoder_first = bool(config.get("DecoderFirst", True)) if config else True
    allow_gui_fallback = bool(config.get("AllowGuiFallback", True)) if config else True
    decoder_config = config.get("DecoderTools", {}) if config and isinstance(config.get("DecoderTools", {}), dict) else {}

    tools = [tool_status(root, decoder_config, spec) for spec in TOOL_SPECS]
    for tool in tools:
        if tool.Status == "unsafe-path-marker":
            errors.append(f"{tool.Tool} points to a path with a forbidden game/mod-manager marker: {tool.Path}")
        elif tool.Status != "ready":
            warnings.append(f"{tool.Tool} is not configured: {tool.Status}")

    ready_by_role = {
        "Plugin": count_ready(tools, lambda item: item.Role == "ESP/ESM/ESL"),
        "Pex": count_ready(tools, lambda item: item.Role in {"PEX", "PEX/PSC review"}),
        "Archive": count_ready(tools, lambda item: bool(re.search(r"BSA|BA2|7z|RAR", item.Role))),
        "Source": count_ready(tools, lambda item: "source" in item.Role),
        "Wrapper": count_ready(tools, lambda item: "wrapper" in item.Role),
        "BuildPrerequisite": count_ready(tools, lambda item: item.Role == "Build prerequisite"),
    }

    write_report(report_path, args.config_path, decoder_first, allow_gui_fallback, tools, ready_by_role, errors, warnings)

    if args.as_json:
        print(
            json.dumps(
                {
                    "DecoderFirst": decoder_first,
                    "AllowGuiFallback": allow_gui_fallback,
                    "ReadyByRole": ready_by_role,
                    "Tools": [asdict(tool) for tool in tools],
                    "Errors": errors,
                    "Warnings": warnings,
                    "Report": str(report_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Decoder tools report written to: {report_path}")
        print(f"Plugin decoder tools ready: {ready_by_role['Plugin']}")
        print(f"PEX decoder tools ready: {ready_by_role['Pex']}")
        print(f"Archive decoder tools ready: {ready_by_role['Archive']}")
        if errors:
            print(f"Decoder tool detection failed with {len(errors)} error(s).")
            return 1
        print("Decoder tool detection completed with no blocking errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
