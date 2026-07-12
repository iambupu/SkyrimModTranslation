"""Route a project-local file to the correct Skill and tool priority.

This script is advisory but authoritative for workflow choice: it does not
translate or open GUI tools, it only classifies risk and the next handler.
"""

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from game_context import GameContext, load_game_context, load_game_profile
from new_ba2_archive_manifest import ADAPTER_PROTOCOL, resolve_controlled_adapter

WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
SPECIALIZED_ROUTE_EXTENSIONS = {
    ".esp",
    ".esm",
    ".esl",
    ".pex",
    ".psc",
    ".bsa",
    ".ba2",
    ".zip",
    ".rar",
    ".7z",
    ".swf",
    ".gfx",
    ".dll",
    ".exe",
    ".pdb",
}


@dataclass
class Route:
    path: str
    skill: str
    primary_tool: str
    auxiliary_tool: str
    output_dir: str
    risk: str
    agent_allowed: str
    notes: str
    game_id: str = "skyrim-se"
    game_display_name: str = "Skyrim Special Edition"
    status: str = "ready"
    blocked_reason: str = ""


def route_payload(route: Route) -> dict[str, str]:
    payload = asdict(route)
    # Compatibility alias for older reports/consumers. New surfaces should use
    # agent_allowed so the router is not Codex-specific.
    payload["codex_allowed"] = route.agent_allowed
    return payload


def project_root() -> Path:
    configured = os.environ.get(WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    current = Path.cwd().expanduser().resolve(strict=False)
    for candidate in (current, *current.parents):
        if (candidate / WORKSPACE_MARKER).is_file():
            return candidate
    plugin_root = os.environ.get(PLUGIN_ROOT_ENV, "").strip()
    if plugin_root:
        return Path(plugin_root).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[1]


def current_game_context(root: Path) -> GameContext:
    marker_path = root / WORKSPACE_MARKER
    if marker_path.is_file():
        return load_game_context(root)
    return load_game_profile("skyrim-se")


def ba2_adapter_ready(root: Path, context: GameContext | None = None) -> bool:
    context = context or current_game_context(root)
    if not context.archive_materialization_enabled:
        return False
    config_path = root / "config" / "tools.local.json"
    if not config_path.is_file():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    decoder_tools = config.get("DecoderTools") if isinstance(config, dict) else None
    if not isinstance(decoder_tools, dict):
        return False
    if decoder_tools.get("Ba2ExtractorProtocol") != ADAPTER_PROTOCOL:
        return False
    value = str(decoder_tools.get("Ba2ExtractorPath") or "").strip()
    if not value:
        return False
    try:
        return resolve_controlled_adapter(root, value, must_exist=True).is_file()
    except (OSError, ValueError):
        return False


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
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def default_route(relative: str) -> Route:
    return Route(
        game_id="skyrim-se",
        game_display_name="Skyrim Special Edition",
        path=relative,
        skill="skills/text-resource-translation",
        primary_tool="Agent Text Pipeline",
        auxiliary_tool="",
        output_dir="translated/",
        risk="Low to Medium",
        status="ready",
        blocked_reason="",
        agent_allowed="Yes, for project-local text copies",
        notes="Generic project-local text asset route.",
    )


def is_resource_xml_path(relative_for_match: str, extension: str) -> bool:
    # XML under asset directories usually describes resources, not UI text.
    # Treat it as protected metadata unless a specific workflow proves otherwise.
    if extension != ".xml":
        return False
    parts = [part for part in relative_for_match.lower().split("\\") if part]
    return any(part in {"meshes", "textures", "facegendata"} for part in parts)


def profile_data_directory(relative_for_match: str, context: GameContext) -> str:
    parts = [part for part in relative_for_match.lower().split("\\") if part]
    matches = [part for part in parts if part in context.data_directories]
    return matches[-1] if matches else ""


def profile_directory_note(directory: str, context: GameContext) -> str:
    if directory:
        protected = " protected" if directory in context.protected_directories else ""
        return f"Current game profile {context.game_id} recognized data directory '{directory}' as a{protected} Data path."
    return f"This path is not a recognized Data directory in current game profile {context.game_id}."


def route_for(root: Path, full_path: Path) -> Route:
    # Route by extension and path before acting. This keeps risky binary/PEX
    # work from being accidentally handled by the generic text pipeline.
    context = current_game_context(root)
    relative = relative_path(root, full_path)
    relative_for_match = relative.replace("/", "\\")
    lowered_relative = relative_for_match.lower()
    extension = full_path.suffix.lower()
    data_directory = profile_data_directory(relative_for_match, context)
    route = default_route(relative)
    route.game_id = context.game_id
    route.game_display_name = context.display_name

    if is_resource_xml_path(relative_for_match, extension):
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = ""
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Protected resource metadata"
        route.status = "manual"
        route.agent_allowed = "No automatic translation"
        route.notes = (
            "XML under Meshes, Textures, or FaceGenData is treated as resource metadata such as "
            "head, bone, mesh, texture, or tool configuration data. Do not translate text, attributes, "
            "or names automatically. final_mod validation must keep this file byte-for-byte unchanged "
            "unless a human documents a specific safe exception."
        )
    elif data_directory in context.protected_directories and extension not in SPECIALIZED_ROUTE_EXTENSIONS:
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Profile-protected resource"
        route.status = "manual"
        route.agent_allowed = "No automatic translation or binary editing"
        route.notes = (
            f"{profile_directory_note(data_directory, context)} "
            "The active marker profile requires byte-for-byte original-copy provenance; do not translate or replace it."
        )
    elif extension in {".esp", ".esm", ".esl"}:
        route.skill = "skills/esp-esm-esl-translation"
        route.primary_tool = "Decoder CLI/library pipeline"
        route.auxiliary_tool = "Codex-only LexTranslator/xTranslator GUI fallback"
        route.output_dir = "source/plugin_exports/<ModName>/, translated/plugin_exports/<ModName>/, out/<ModName>/tool_outputs/"
        route.risk = "High"
        route.agent_allowed = "Tool-mediated project-local output only"
        route.notes = (
            f"Current game profile: {context.game_id} ({context.mutagen_release}). "
            "Use python scripts/export_esp_strings.py first for project-local read-only text export, then "
            "python scripts/apply_plugin_translation_map.py to create translated JSONL, then "
            "python scripts/invoke_mutagen_plugin_text_tool.py for project-local Mutagen writeback. "
            "Use configured PluginTextCliPath, MutagenCliPath, or safe SSEDump wrapper for deeper context. "
            "Raw xEdit/SSEDump must not be launched directly. Writeback still requires a controlled "
            "project-local plugin writer or tool output. Do not modify plugin binaries directly."
        )
    elif extension in context.string_table_extensions:
        route.skill = "localized-string-table-translation"
        route.output_dir = "source/string_tables/<ModName>/, translated/string_tables/<ModName>/, out/<ModName>/tool_outputs/"
        route.agent_allowed = "No generic text decoding; controlled tool path only"
        if context.string_tables_enabled:
            route.primary_tool = "Controlled LexTranslator/xTranslator STRINGS workflow"
            route.auxiliary_tool = "Project-local string-table export/writeback adapter when available"
            route.risk = "High"
            route.status = "tool-mediated"
            route.blocked_reason = ""
            route.notes = (
                f"{context.display_name} localized string tables must stay on the controlled STRINGS workflow. "
                "Do not generic-decode or treat them as ordinary text resources. Use the existing controlled "
                "LexTranslator/xTranslator path or a project-local string-table adapter."
            )
        else:
            route.primary_tool = "Dedicated string-table adapter"
            route.auxiliary_tool = ""
            route.risk = "Blocked"
            route.status = "blocked"
            route.blocked_reason = "missing string-table adapter"
            route.notes = (
                f"{context.display_name} localized string tables require a dedicated string-table adapter. "
                "The current pipeline cannot decode or write back this format safely, so this path is blocked."
            )
    elif ("\\interface\\translations\\" in lowered_relative or lowered_relative.startswith("interface\\translations\\")) and extension == ".txt":
        route.skill = "skills/text-resource-translation"
        route.primary_tool = "Agent Text Pipeline"
        route.auxiliary_tool = "LexTranslator"
        route.output_dir = "translated/final_mod/<ModName>/Interface/translations/"
        route.risk = "Low"
        route.agent_allowed = "Yes, write translated copy only"
        route.notes = "Preserve key, tab separator, line count, control codes, and variables."
    elif extension == ".pex":
        route.skill = "skills/pex-visible-strings-translation"
        route.primary_tool = "Configured PexStringToolPath decoder/rewriter"
        route.auxiliary_tool = "Codex-only LexTranslator/xTranslator PapyrusPex GUI fallback"
        route.output_dir = "source/pex_exports/<ModName>/, translated/lextranslator_ready/<ModName>/, out/<ModName>/tool_outputs/Scripts/"
        route.risk = "High"
        route.agent_allowed = "Only decoder/tool-exported visible strings"
        route.notes = (
            f"Current game profile: {context.game_id} ({context.pex_category}, writeback {context.pex_writeback_status}). "
            "Use python scripts/invoke_mutagen_pex_string_tool.py via configured PexStringToolPath first: "
            "Mode Export for instruction-string JSONL, Mode Apply for project-local PEX copy writeback. "
            "It may only write out/<ModName>/tool_outputs/Scripts/*.pex or "
            "translated/tool_outputs/<ModName>/Scripts/*.pex. Agent must not modify .pex directly. "
            "Unknown logic strings stay untranslated."
        )
    elif extension == ".psc":
        route.skill = "skills/pex-visible-strings-translation"
        route.primary_tool = "Agent read-only analysis"
        route.auxiliary_tool = ""
        route.output_dir = "work/psc_strings/"
        route.risk = "High"
        route.agent_allowed = "Read-only extraction only"
        route.notes = "Do not write back source code and do not compile."
    elif extension == ".bsa":
        route.skill = "skills/bsa-archive-audit"
        route.primary_tool = "bethesda-structs read-only archive audit"
        route.auxiliary_tool = "scripts/new_bsa_archive_manifest.py -> scripts/invoke_bsa_file_extractor_safe.py only when extraction is required"
        route.risk = "Medium"
        route.output_dir = "out/<ModName>/archive_audits/<ArchiveName>/"
        route.agent_allowed = "Audit only; extraction only through project safe wrapper"
        route.notes = (
            "Do not edit or repack BSA. Prefer bethesda-structs inventory and manifest evidence; "
            "if materialization is required, extract only to work/archive_extracts/<ModName>/<ArchiveName>/. "
            "Translated BSA content must become same-path loose override in final_mod by default; BSA repack is a future high-risk adapter path only after manual testing proves it is required."
        )
    elif extension == ".ba2":
        adapter_ready = ba2_adapter_ready(root, context)
        route.skill = "skills/ba2-archive-audit"
        route.primary_tool = "bethesda-structs read-only archive audit"
        route.auxiliary_tool = (
            "scripts/invoke_ba2_extractor_safe.py -> scripts/new_ba2_archive_manifest.py -> "
            "scripts/verify_ba2_extraction.py"
            if adapter_ready
            else "scripts/new_bsa_archive_manifest.py (bethesda-structs read-only inventory only)"
        )
        route.output_dir = "out/<ModName>/archive_audits/<ArchiveName>/"
        route.risk = "Medium"
        route.agent_allowed = "Read-only audit; extraction only through the configured controlled BA2 adapter"
        route.status = "ready"
        route.blocked_reason = ""
        if context.archive_materialization_enabled:
            route.notes = (
                "Do not edit or repack BA2. Use bethesda-structs for read-only inventory. If coverage confirms "
                "that materialization is required, the workflow remains blocked until the controlled adapter is ready. Materialize only "
                "through the safe BA2 wrapper into work/archive_extracts/<ModName>/<ArchiveName>/, verify the "
                "hash-backed manifest, and deliver translated content as same-path loose override."
            )
        else:
            route.notes = (
                f"The current {context.game_id} profile permits BA2 read-only inventory only. "
                "Materialization is disabled even when a BA2 adapter is configured; do not extract or repack this archive."
            )
    elif extension in {".zip", ".rar", ".7z"}:
        route.skill = "skills/mod-input-preparation"
        route.primary_tool = "Project-local decoder/extraction handoff"
        route.auxiliary_tool = ""
        route.output_dir = "work/extracted_mods/<ModName>/"
        route.risk = "Medium"
        if extension == ".zip":
            route.agent_allowed = "Read-only extraction into work/extracted_mods is required before translation"
            route.notes = (
                "Agent must not modify the archive. Extract project-local .zip to work/extracted_mods "
                "first, then scan and route the extracted working copy."
            )
        else:
            route.agent_allowed = "Extraction only when configured archive decoder exists"
            route.notes = (
                "Use configured Archive7zPath for project-local extraction. If missing, generate an "
                "extraction plan only."
            )
    elif extension in {".swf", ".gfx"}:
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Protected UI binary"
        route.status = "manual"
        route.agent_allowed = "No automatic translation or binary editing"
        route.notes = "Protected UI binary asset. Copy project-local source unchanged when needed; do not edit."
    elif extension in {".dll", ".exe", ".pdb"}:
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Protected binary"
        route.status = "manual"
        route.agent_allowed = "No automatic translation or binary editing"
        route.notes = (
            f"{profile_directory_note(data_directory, context)} "
            "Protected binary/tool symbol file. Copy project-local source unchanged when needed; do not edit."
        )
    elif "\\mcm\\" in lowered_relative or lowered_relative.startswith("mcm\\") or (
        "mcm" in full_path.name.lower()
        and extension in {".json", ".jsonl", ".ini", ".txt", ".xml", ".csv", ".md"}
    ):
        route.skill = "skills/mcm-translation"
        if extension in {".json", ".ini"}:
            route.primary_tool = "Agent Structured MCM Extractor"
            route.auxiliary_tool = "LexTranslator"
        else:
            route.primary_tool = "LexTranslator"
            route.auxiliary_tool = "xTranslator"
        route.output_dir = "source/mcm/<ModName>/, translated/final_mod/<ModName>/"
        route.risk = "Medium"
        route.agent_allowed = "Yes, extract visible MCM text only"
        route.notes = (
            "Do not translate page id, option id, state id, StorageUtil key, JsonUtil key, "
            "setting key, script name, or function name."
        )
    elif extension in {".json", ".jsonl", ".xml", ".csv", ".txt", ".md"}:
        route.skill = "skills/text-resource-translation"
        route.primary_tool = "Agent Text Pipeline"
        route.auxiliary_tool = ""
        route.output_dir = "translated/final_mod/<ModName>/"
        route.risk = "Low to Medium"
        route.agent_allowed = "Yes, preserve structure"
        route.notes = "Validate format, placeholders, keys, and row or record counts."
    else:
        route.skill = "manual-review"
        route.primary_tool = "Manual review"
        route.auxiliary_tool = ""
        route.output_dir = "qa/"
        route.risk = "Unknown"
        route.status = "manual"
        route.agent_allowed = "No translation until reviewed"
        route.notes = "No route rule matched this file type."

    return route


def write_report(report_path: Path, route: Route) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not report_path.exists():
        report_path.write_text("# Routing Report\n", encoding="utf-8")
    lines = [
        "",
        f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- Game ID: {route.game_id}",
        f"- Game Name: {route.game_display_name}",
        f"- File: {route.path}",
        f"- Recommended Skill: {route.skill}",
        f"- Primary Tool: {route.primary_tool}",
        f"- Auxiliary Tool: {route.auxiliary_tool}",
        f"- Recommended Output Dir: {route.output_dir}",
        f"- Risk: {route.risk}",
        f"- Status: {route.status}",
        f"- Blocked Reason: {route.blocked_reason or '(none)'}",
        f"- Agent Allowed: {route.agent_allowed}",
        f"- Notes: {route.notes}",
    ]
    with report_path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + "\n".join(lines) + "\n")


def print_text(route: Route, report_path: Path) -> None:
    print(f"Game ID: {route.game_id}")
    print(f"Game Name: {route.game_display_name}")
    print(f"File: {route.path}")
    print(f"Recommended Skill: {route.skill}")
    print(f"Primary Tool: {route.primary_tool}")
    print(f"Auxiliary Tool: {route.auxiliary_tool}")
    print(f"Recommended Output Dir: {route.output_dir}")
    print(f"Risk: {route.risk}")
    print(f"Status: {route.status}")
    print(f"Blocked Reason: {route.blocked_reason or '(none)'}")
    print(f"Agent Allowed: {route.agent_allowed}")
    print(f"Notes: {route.notes}")
    print(f"Routing report updated: {report_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route a project-local Skyrim Mod file to the correct translation skill and tool path.")
    parser.add_argument("path", nargs="?", help="Project-local file path to route.")
    parser.add_argument("--file-path", "--input-path", dest="file_path", default="", help="Project-local file path to route.")
    parser.add_argument("--report-output-path", default="qa/routing_report.md")
    parser.add_argument("--as-json", action="store_true")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    value = args.file_path or args.path
    if not value:
        raise ValueError("Pass a file path as a positional argument, --file-path, or --input-path.")

    root = project_root()
    target = resolve_project_path(root, value, must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    route = route_for(root, target)
    write_report(report_path, route)
    if args.as_json:
        print(json.dumps(route_payload(route), ensure_ascii=False, indent=2))
    else:
        print_text(route, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
