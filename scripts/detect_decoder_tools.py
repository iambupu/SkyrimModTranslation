"""Detect configured decoder and adapter availability without running workflow stages.

The detector reports readiness and unsafe raw-tool situations. A tool existing
on disk is not always "ready": xEdit/SSEDump style tools may require a safe
project wrapper before they can be used.
"""

import argparse
import importlib.util
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from project_paths import plugin_root, project_root, risky_marker
from new_ba2_archive_manifest import resolve_controlled_adapter
from project_paths import is_under, resolve_project_path
from report_utils import markdown_cell_plain as markdown_cell


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
        "Property": "python-package:bethesda_structs",
        "Name": "bethesda-structs archive parser",
        "Role": "BSA/BA2 audit",
        "Use": "Read project-local BSA/BA2 archives for manifest/audit evidence; parser-only, not an archive writer.",
        "PathType": "PythonPackage",
    },
    {
        "Property": "BsaFileExtractorPath",
        "Name": "BSAFileExtractor adapter",
        "Role": "BSA extractor",
        "Use": "First-stage BSA extraction candidate; must be wrapped so input/output stay project-local.",
        "PathType": "Leaf",
    },
    {
        "Property": "BsaExtractorPath",
        "Name": "BSA extractor",
        "Role": "BSA",
        "Use": "Generic project-local BSA extractor path; prefer BSAFileExtractor adapter when configured.",
        "PathType": "Leaf",
    },
    {
        "Property": "Ba2ExtractorPath",
        "Name": "Controlled BA2 extractor adapter",
        "Role": "BA2",
        "Use": "Use only through scripts/invoke_ba2_extractor_safe.py with the declared safe wrapper/adapter protocol.",
        "PathType": "Leaf",
        "ProtocolProperty": "Ba2ExtractorProtocol",
        "RequiredProtocol": "skyrim-mod-chs.ba2-extractor.v1",
        "ControlledPathOnly": True,
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


DOTNET_DEPENDENT_PROPERTIES = {"MutagenCliPath", "PexStringToolPath"}






def resolve_configured_tool_path(root: Path, value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        plugin_candidate = plugin_root() / candidate
        if candidate.parts and candidate.parts[0] == "scripts" and plugin_candidate.exists():
            candidate = plugin_candidate
        else:
            candidate = root / candidate
    return str(candidate.resolve(strict=False))


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
    controlled_path_error = False
    if spec["Property"] == "Ba2ExtractorPath" and str(value or "").strip():
        try:
            controlled = resolve_controlled_adapter(root, str(value), must_exist=True)
            full_path = str(controlled)
            exists = controlled.is_file()
        except (OSError, ValueError):
            controlled_path_error = True

    status = "missing-path"
    if full_path:
        status = "ready" if exists else "path-not-found"
    requires_safe_wrapper = bool(spec.get("RequiresSafeWrapper", False))
    protocol_property = str(spec.get("ProtocolProperty") or "")
    required_protocol = str(spec.get("RequiredProtocol") or "")
    configured_protocol = str(decoder_config.get(protocol_property) or "") if decoder_config and protocol_property else ""
    path_obj = Path(full_path) if full_path else None
    is_project_or_plugin_path = bool(path_obj and (is_under(path_obj, root) or is_under(path_obj, plugin_root())))
    if exists and required_protocol and configured_protocol != required_protocol:
        status = "requires-safe-adapter-protocol"
    elif controlled_path_error:
        status = "invalid-controlled-adapter-path"
    elif exists and spec.get("ControlledPathOnly") and path_obj and not is_project_or_plugin_path:
        status = "outside-controlled-adapter-roots"
    elif exists and requires_safe_wrapper:
        status = "requires-safe-wrapper"
    if risky_marker(full_path) and not is_project_or_plugin_path:
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


def apply_dependency_gates(tools: list[ToolStatus]) -> list[str]:
    dotnet_ready = any(tool.Property == "DotNetSdkPath" and tool.Status == "ready" for tool in tools)
    if dotnet_ready:
        return []

    warnings: list[str] = []
    for tool in tools:
        if tool.Property not in DOTNET_DEPENDENT_PROPERTIES or tool.Status != "ready":
            continue
        tool.Status = "missing-build-prerequisite"
        warnings.append(f"{tool.Tool} exists but requires a ready project-local .NET SDK before it can be used.")
    return warnings



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
        f"- Archive audit tools ready: {ready_by_role['ArchiveAudit']}",
        f"- Archive extractor tools ready: {ready_by_role['ArchiveExtractor']}",
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
    pex_missing_build_prerequisite = any(tool.Property == "PexStringToolPath" and tool.Status == "missing-build-prerequisite" for tool in tools)
    if ready_by_role["Pex"] > 0:
        lines.append(
            "- PEX: use configured decoder CLI for extraction/context first; writeback only if the tool explicitly supports project-local PEX rewriting."
        )
    elif pex_missing_build_prerequisite:
        lines.append(
            "- PEX: configured decoder exists, but the project-local .NET SDK build prerequisite is missing or not ready."
        )
    else:
        lines.append(
            "- PEX: no decoder CLI configured; prefer Interface/translations and MCM text files, otherwise tool-exported visible strings remain required."
        )
    if ready_by_role["ArchiveAudit"] > 0:
        lines.append("- BSA/BA2: use bethesda-structs first for read-only archive manifest/audit evidence.")
    else:
        lines.append("- BSA/BA2: bethesda-structs is missing; archive content audit cannot be proven.")
    if any(tool.Property == "BsaFileExtractorPath" and tool.Status == "ready" for tool in tools):
        lines.append("- BSA: extraction, when required, must use scripts/invoke_bsa_file_extractor_safe.py and output only to work/archive_extracts/.")
    else:
        lines.append("- BSA: no safe BSAFileExtractor wrapper is configured; generate audit/blocked evidence instead of extracting.")
    lines.append("- BSA delivery: translated archive content should become same-path loose override in final_mod; BSA repacking is not a default tool path.")
    if any(tool.Property == "Ba2ExtractorPath" and tool.Status == "ready" for tool in tools):
        lines.append(
            "- BA2: the configured adapter declares the safe wrapper/adapter protocol; invoke it only through "
            "scripts/invoke_ba2_extractor_safe.py and verify receipt-backed evidence."
        )
    else:
        lines.append(
            "- BA2: no workspace/plugin-local adapter with the safe wrapper/adapter protocol is ready; keep extraction blocked."
        )
    lines.append("- BA2 delivery: use verified same-path loose overrides only; BA2 repacking is not allowed.")

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
    warnings.extend(apply_dependency_gates(tools))
    for tool in tools:
        if tool.Status == "unsafe-path-marker":
            errors.append(f"{tool.Tool} points to a path with a forbidden game/mod-manager marker: {tool.Path}")
        elif tool.Status != "ready":
            warnings.append(f"{tool.Tool} is not configured: {tool.Status}")

    ready_by_role = {
        "Plugin": count_ready(tools, lambda item: item.Role == "ESP/ESM/ESL"),
        "Pex": count_ready(tools, lambda item: item.Role in {"PEX", "PEX/PSC review"}),
        "ArchiveAudit": count_ready(tools, lambda item: "audit" in item.Role and bool(re.search(r"BSA|BA2", item.Role))),
        "ArchiveExtractor": count_ready(tools, lambda item: bool(re.search(r"BSA extractor|^BSA$|^BA2$|7z|RAR", item.Role))),
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
        print(f"Archive audit tools ready: {ready_by_role['ArchiveAudit']}")
        print(f"Archive extractor tools ready: {ready_by_role['ArchiveExtractor']}")
        if errors:
            print(f"Decoder tool detection failed with {len(errors)} error(s).")
            return 1
        print("Decoder tool detection completed with no blocking errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
