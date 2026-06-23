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

WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
WORKSPACE_ROOT_ENV = "SKYRIM_CHS_WORKSPACE_ROOT"
PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"


@dataclass
class Route:
    path: str
    skill: str
    primary_tool: str
    auxiliary_tool: str
    output_dir: str
    risk: str
    codex_allowed: str
    notes: str


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
        path=relative,
        skill="skills/text-resource-translation",
        primary_tool="Codex Text Pipeline",
        auxiliary_tool="",
        output_dir="translated/",
        risk="Low to Medium",
        codex_allowed="Yes, for project-local text copies",
        notes="Generic project-local text asset route.",
    )


def is_resource_xml_path(relative_for_match: str, extension: str) -> bool:
    # XML under asset directories usually describes resources, not UI text.
    # Treat it as protected metadata unless a specific workflow proves otherwise.
    if extension != ".xml":
        return False
    parts = [part for part in relative_for_match.lower().split("\\") if part]
    return any(part in {"meshes", "textures", "facegendata"} for part in parts)


def route_for(root: Path, full_path: Path) -> Route:
    # Route by extension and path before acting. This keeps risky binary/PEX
    # work from being accidentally handled by the generic text pipeline.
    relative = relative_path(root, full_path)
    relative_for_match = relative.replace("/", "\\")
    lowered_relative = relative_for_match.lower()
    extension = full_path.suffix.lower()
    route = default_route(relative)

    if is_resource_xml_path(relative_for_match, extension):
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = ""
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Protected resource metadata"
        route.codex_allowed = "No automatic translation"
        route.notes = (
            "XML under Meshes, Textures, or FaceGenData is treated as resource metadata such as "
            "head, bone, mesh, texture, or tool configuration data. Do not translate text, attributes, "
            "or names automatically. final_mod validation must keep this file byte-for-byte unchanged "
            "unless a human documents a specific safe exception."
        )
    elif extension in {".esp", ".esm", ".esl"}:
        route.skill = "skills/esp-esm-esl-translation"
        route.primary_tool = "Decoder CLI/library pipeline"
        route.auxiliary_tool = "LexTranslator/xTranslator GUI fallback"
        route.output_dir = "source/plugin_exports/<ModName>/, translated/plugin_exports/<ModName>/, out/<ModName>/tool_outputs/"
        route.risk = "High"
        route.codex_allowed = "Tool-mediated project-local output only"
        route.notes = (
            "Use python scripts/export_esp_strings.py first for project-local read-only text export, then "
            "python scripts/apply_plugin_translation_map.py to create translated JSONL, then "
            "python scripts/invoke_mutagen_plugin_text_tool.py for project-local Mutagen writeback. "
            "Use configured PluginTextCliPath, MutagenCliPath, or safe SSEDump wrapper for deeper context. "
            "Raw xEdit/SSEDump must not be launched directly. Writeback still requires a controlled "
            "project-local plugin writer or tool output. Do not modify plugin binaries directly."
        )
    elif ("\\interface\\translations\\" in lowered_relative or lowered_relative.startswith("interface\\translations\\")) and extension == ".txt":
        route.skill = "skills/text-resource-translation"
        route.primary_tool = "Codex Text Pipeline"
        route.auxiliary_tool = "LexTranslator"
        route.output_dir = "translated/final_mod/<ModName>/Interface/translations/"
        route.risk = "Low"
        route.codex_allowed = "Yes, write translated copy only"
        route.notes = "Preserve key, tab separator, line count, control codes, and variables."
    elif extension == ".pex":
        route.skill = "skills/pex-visible-strings-translation"
        route.primary_tool = "Configured PexStringToolPath decoder/rewriter"
        route.auxiliary_tool = "LexTranslator/xTranslator PapyrusPex GUI fallback"
        route.output_dir = "source/pex_exports/<ModName>/, translated/lextranslator_ready/<ModName>/, out/<ModName>/tool_outputs/Scripts/"
        route.risk = "High"
        route.codex_allowed = "Only decoder/tool-exported visible strings"
        route.notes = (
            "Use python scripts/invoke_mutagen_pex_string_tool.py via configured PexStringToolPath first: "
            "Mode Export for instruction-string JSONL, Mode Apply for project-local PEX copy writeback. "
            "It may only write out/<ModName>/tool_outputs/Scripts/*.pex or "
            "translated/tool_outputs/<ModName>/Scripts/*.pex. Codex must not modify .pex directly. "
            "Unknown logic strings stay untranslated."
        )
    elif extension == ".psc":
        route.skill = "skills/pex-visible-strings-translation"
        route.primary_tool = "Codex read-only analysis"
        route.auxiliary_tool = ""
        route.output_dir = "work/psc_strings/"
        route.risk = "High"
        route.codex_allowed = "Read-only extraction only"
        route.notes = "Do not write back source code and do not compile."
    elif extension == ".bsa":
        route.skill = "skills/bsa-archive-audit"
        route.primary_tool = "bethesda-structs read-only archive audit"
        route.auxiliary_tool = "scripts/new_bsa_archive_manifest.py first; scripts/invoke_bsa_file_extractor_safe.py only when extraction is required"
        route.risk = "Medium"
        route.output_dir = "out/<ModName>/archive_audits/<ArchiveName>/"
        route.codex_allowed = "Audit only; extraction only through project safe wrapper"
        route.notes = (
            "Do not edit or repack BSA. Prefer bethesda-structs inventory and manifest evidence; "
            "if materialization is required, extract only to work/archive_extracts/<ModName>/<ArchiveName>/. "
            "Translated BSA content must become same-path loose override in final_mod by default; BSA repack is a future high-risk adapter path only after manual testing proves it is required."
        )
    elif extension == ".ba2":
        route.skill = "skills/bsa-archive-audit"
        route.primary_tool = "bethesda-structs read-only archive audit"
        route.auxiliary_tool = "future project-local Ba2ExtractorPath adapter only when explicitly configured"
        route.output_dir = "out/<ModName>/archive_audits/<ArchiveName>/"
        route.risk = "Medium"
        route.codex_allowed = "Read-only audit only; extraction only with a future controlled BA2 adapter"
        route.notes = (
            "Do not edit, extract by default, or repack BA2. Generate a read-only archive audit manifest "
            "with scripts/new_bsa_archive_manifest.py / bethesda-structs. If actual materialization is "
            "required, block until a project-local Ba2ExtractorPath adapter exists."
        )
    elif extension in {".zip", ".rar", ".7z"}:
        route.skill = "skills/mod-input-preparation"
        route.primary_tool = "Project-local decoder/extraction handoff"
        route.auxiliary_tool = ""
        route.output_dir = "work/extracted_mods/<ModName>/"
        route.risk = "Medium"
        if extension == ".zip":
            route.codex_allowed = "Read-only extraction into work/extracted_mods is required before translation"
            route.notes = (
                "Codex must not modify the archive. Extract project-local .zip to work/extracted_mods "
                "first, then scan and route the extracted working copy."
            )
        else:
            route.codex_allowed = "Extraction only when configured archive decoder exists"
            route.notes = (
                "Use configured Archive7zPath for project-local extraction. If missing, generate an "
                "extraction plan only."
            )
    elif extension in {".dll", ".exe", ".pdb"}:
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Protected binary"
        route.codex_allowed = "No automatic translation or binary editing"
        route.notes = "Protected binary/tool symbol file. Copy project-local source unchanged when needed; do not edit."
    elif "\\mcm\\" in lowered_relative or lowered_relative.startswith("mcm\\") or (
        "mcm" in full_path.name.lower()
        and extension in {".json", ".jsonl", ".ini", ".txt", ".xml", ".csv", ".md"}
    ):
        route.skill = "skills/mcm-translation"
        if extension in {".json", ".ini"}:
            route.primary_tool = "Codex Structured MCM Extractor"
            route.auxiliary_tool = "LexTranslator"
        else:
            route.primary_tool = "LexTranslator"
            route.auxiliary_tool = "xTranslator"
        route.output_dir = "source/mcm/<ModName>/, translated/final_mod/<ModName>/"
        route.risk = "Medium"
        route.codex_allowed = "Yes, extract visible MCM text only"
        route.notes = (
            "Do not translate page id, option id, state id, StorageUtil key, JsonUtil key, "
            "setting key, script name, or function name."
        )
    elif extension in {".json", ".jsonl", ".xml", ".csv", ".txt", ".md"}:
        route.skill = "skills/text-resource-translation"
        route.primary_tool = "Codex Text Pipeline"
        route.auxiliary_tool = ""
        route.output_dir = "translated/final_mod/<ModName>/"
        route.risk = "Low to Medium"
        route.codex_allowed = "Yes, preserve structure"
        route.notes = "Validate format, placeholders, keys, and row or record counts."
    else:
        route.skill = "manual-review"
        route.primary_tool = "Manual review"
        route.auxiliary_tool = ""
        route.output_dir = "qa/"
        route.risk = "Unknown"
        route.codex_allowed = "No translation until reviewed"
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
        f"- File: {route.path}",
        f"- Recommended Skill: {route.skill}",
        f"- Primary Tool: {route.primary_tool}",
        f"- Auxiliary Tool: {route.auxiliary_tool}",
        f"- Recommended Output Dir: {route.output_dir}",
        f"- Risk: {route.risk}",
        f"- Codex Allowed: {route.codex_allowed}",
        f"- Notes: {route.notes}",
    ]
    with report_path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + "\n".join(lines) + "\n")


def print_text(route: Route, report_path: Path) -> None:
    print(f"File: {route.path}")
    print(f"Recommended Skill: {route.skill}")
    print(f"Primary Tool: {route.primary_tool}")
    print(f"Auxiliary Tool: {route.auxiliary_tool}")
    print(f"Recommended Output Dir: {route.output_dir}")
    print(f"Risk: {route.risk}")
    print(f"Codex Allowed: {route.codex_allowed}")
    print(f"Notes: {route.notes}")
    print(f"Routing report updated: {report_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Route a project-local Skyrim Mod file to the correct translation skill and tool path.")
    parser.add_argument("path", nargs="?", help="Project-local file path to route.")
    parser.add_argument("--file-path", dest="file_path", default="", help="Project-local file path to route.")
    parser.add_argument("--report-output-path", default="qa/routing_report.md")
    parser.add_argument("--as-json", action="store_true")
    args = parser.parse_args()

    value = args.file_path or args.path
    if not value:
        raise ValueError("Pass a file path as a positional argument or --file-path.")

    root = project_root()
    target = resolve_project_path(root, value, must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    route = route_for(root, target)
    write_report(report_path, route)
    if args.as_json:
        print(json.dumps(asdict(route), ensure_ascii=False, indent=2))
    else:
        print_text(route, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
