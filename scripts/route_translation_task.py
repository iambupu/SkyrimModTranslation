"""Route a project-local file to the correct Skill and tool priority.

This script is advisory but authoritative for workflow choice: it does not
translate or open GUI tools, it only classifies risk and the next handler.
"""

import argparse
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from adapter_registry import require_adapter
from capability_resolver import (
    CapabilityDecision,
    resolve_capability,
    resolve_resource_capability,
)
from game_context import GameContext, load_game_context, load_game_profile
from new_ba2_archive_manifest import ADAPTER_PROTOCOL, resolve_controlled_adapter
from project_paths import is_under, resolve_project_path, relative_path
from resource_model import ResourceDescriptor, classify_resource

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
    agent_allowed: str
    notes: str
    game_id: str = "skyrim-se"
    game_display_name: str = "Skyrim Special Edition"
    status: str = "ready"
    blocked_reason: str = ""
    category: str = "unknown"
    subtype: str = "unknown"
    container: str = ""
    traits: tuple[str, ...] = ()
    capability: str = ""
    effective_capability: str = "unsupported"


def route_payload(route: Route) -> dict[str, Any]:
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
    plugin_source_root = Path(__file__).resolve().parents[1]
    if root.resolve(strict=False) == plugin_source_root.resolve(strict=False) and not os.environ.get(WORKSPACE_ROOT_ENV, "").strip():
        # Repository-only validation is not a translation workspace. Preserve
        # the historical Skyrim context there without allowing an unmarked
        # external workspace to inherit a game silently.
        return load_game_profile("skyrim-se")
    raise FileNotFoundError(
        f"workspace marker is required before game-aware routing: {marker_path}. "
        "Initialize the workspace with an explicit --game value."
    )


def ba2_adapter_ready(root: Path, context: GameContext | None = None) -> bool:
    context = context or current_game_context(root)
    decision = resolve_capability(context, "archive.ba2", "read")
    if not decision.supported or not decision.adapter_id:
        return False
    try:
        require_adapter(decision.adapter_id, "extract")
    except ValueError:
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


def capability_ready(
    context: GameContext,
    capability: str,
    operation: str,
    adapter_operation: str,
) -> CapabilityDecision:
    decision = resolve_capability(context, capability, operation)
    if decision.supported and decision.adapter_id:
        require_adapter(decision.adapter_id, adapter_operation)
    return decision


def resource_capability_ready(
    context: GameContext,
    resource: ResourceDescriptor,
    operation: str,
    adapter_operation: str,
) -> CapabilityDecision:
    decision = resolve_resource_capability(context, resource, operation)
    if decision.supported and decision.adapter_id:
        require_adapter(decision.adapter_id, adapter_operation)
    return decision


def apply_loose_text_capability(
    route: Route,
    context: GameContext,
    resource: ResourceDescriptor,
) -> None:
    read = resource_capability_ready(context, resource, "read", "extract")
    write = resource_capability_ready(context, resource, "write", "apply")
    if not read.supported or not write.supported:
        blocked = write if not write.supported else read
        route.status = "blocked"
        route.risk = "Blocked"
        route.blocked_reason = blocked.reason
        route.agent_allowed = "No text translation for this Game Profile"
    route.notes = (
        f"loose_text={write.level}; adapter={write.adapter_id or read.adapter_id}. "
        + route.notes
    )








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


def profile_directory_note(resource: ResourceDescriptor, context: GameContext) -> str:
    if resource.container:
        declared_parts = [
            part.casefold()
            for part in resource.relative_path.parts[:-1]
            if context.resource_model.containers.get(part.casefold()) == resource.container
        ]
        if declared_parts:
            directory = declared_parts[-1]
            protected = " protected" if directory in context.protected_directories else ""
            return (
                f"Current game profile {context.game_id} recognized data directory "
                f"'{directory}' as a{protected} Data path."
            )
        return (
            f"Current game profile {context.game_id} classified the path container as "
            f"'{resource.container}'."
        )
    return f"This path is not a recognized Data directory in current game profile {context.game_id}."


def _normalized_traits(
    traits: Iterable[str],
    evidence: Mapping[str, Any] | None,
) -> frozenset[str]:
    values = set(traits)
    if evidence is not None:
        raw_traits = evidence.get("traits", ())
        if isinstance(raw_traits, (str, bytes)) or not isinstance(
            raw_traits,
            (list, tuple, set, frozenset),
        ):
            raise ValueError("Route evidence traits must be a collection of strings")
        values.update(raw_traits)
    if not all(isinstance(trait, str) and trait.strip() for trait in values):
        raise ValueError("Route traits must contain only non-empty strings")
    return frozenset(trait.strip().casefold() for trait in values)


def _effective_capability_level(
    inventory: CapabilityDecision,
    read: CapabilityDecision,
    write: CapabilityDecision,
) -> str:
    for decision in (write, read, inventory):
        if decision.supported:
            return decision.level
    return inventory.level


def route_for(
    root: Path,
    full_path: Path,
    context: GameContext | None = None,
    *,
    traits: Iterable[str] = (),
    evidence: Mapping[str, Any] | None = None,
    descriptor: ResourceDescriptor | None = None,
) -> Route:
    context = context or current_game_context(root)
    relative = relative_path(root, full_path)
    relative_resource_path = Path(relative)
    supplied_traits = _normalized_traits(traits, evidence)
    if descriptor is None:
        resource = classify_resource(
            context,
            relative_resource_path,
            traits=supplied_traits,
        )
    else:
        if not isinstance(descriptor, ResourceDescriptor):
            raise TypeError("descriptor must be a ResourceDescriptor")
        if descriptor.relative_path != relative_resource_path:
            raise ValueError(
                "descriptor.relative_path must match full_path relative to root: "
                f"{descriptor.relative_path!s} != {relative_resource_path!s}"
            )
        expected_extension = descriptor.relative_path.suffix.casefold()
        if descriptor.extension != expected_extension:
            raise ValueError(
                "descriptor.extension must match descriptor.relative_path suffix: "
                f"{descriptor.extension!r} != {expected_extension!r}"
            )
        resource = replace(
            descriptor,
            traits=frozenset((*descriptor.traits, *supplied_traits)),
        )
    inventory = resolve_resource_capability(context, resource, "inventory")
    read = resolve_resource_capability(context, resource, "read")
    write = resolve_resource_capability(context, resource, "write")

    relative_for_match = relative.replace("/", "\\")
    lowered_relative = relative_for_match.casefold()
    route = default_route(relative)
    route.game_id = context.game_id
    route.game_display_name = context.display_name
    route.category = resource.category
    route.subtype = resource.subtype
    route.container = resource.container
    route.traits = tuple(sorted(resource.traits))
    route.capability = resource.capability
    route.effective_capability = _effective_capability_level(inventory, read, write)

    if resource.container == "protected":
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Profile-protected resource"
        route.status = "manual"
        route.agent_allowed = "No automatic translation or binary editing"
        route.notes = (
            f"{profile_directory_note(resource, context)} "
            "The active marker profile requires byte-for-byte original-copy provenance; do not translate or replace it."
        )
    elif resource.container == "f4se":
        route.skill = "manual-review"
        route.status = "manual"
        route.output_dir = "qa/"
        route.agent_allowed = (
            "No automatic extraction or translation; confirm player-visible values manually"
        )
        if resource.extension in {".json", ".ini", ".toml"}:
            route.primary_tool = "Structured configuration manual review"
            route.auxiliary_tool = ""
            route.risk = "Manual review"
            route.notes = (
                "F4SE configuration values are not generic text candidates. Preserve keys, paths, "
                "protocols, identifiers, and structure; record a structured manual confirmation before "
                "any player-visible value is translated."
            )
        else:
            route.primary_tool = "Copy unchanged"
            route.auxiliary_tool = "final_mod provenance validation"
            route.risk = "F4SE resource"
            route.notes = (
                f"{profile_directory_note(resource, context)} "
                "Files under F4SE stay outside automatic plugin, Papyrus, and loose-text handling. "
                "Copy the project-local source unchanged when it is required in final_mod."
            )
    elif resource.category == "plugin":
        route.skill = "skills/esp-esm-esl-translation"
        route.risk = "High"
        route.primary_tool = "Decoder CLI/library pipeline"
        adapter_trait_note = (
            "Header traits for flagged .esp/.esm resources must be supplied by adapter enrichment; "
            "without that evidence the router treats them as ordinary plugins."
        )
        if write.supported:
            route.auxiliary_tool = "Codex-only LexTranslator/xTranslator GUI fallback"
            route.output_dir = (
                "source/plugin_exports/<ModName>/, translated/plugin_exports/<ModName>/, "
                "out/<ModName>/tool_outputs/"
            )
            route.agent_allowed = "Tool-mediated project-local output only"
            route.notes = (
                f"plugin_text={write.level}; traits={','.join(route.traits) or '(none)'}. "
                "Use python scripts/export_esp_strings.py first for project-local read-only text export, then "
                "python scripts/apply_plugin_translation_map.py to create translated JSONL, then "
                "python scripts/invoke_mutagen_plugin_text_tool.py for controlled project-local writeback. "
                "Do not modify plugin binaries directly. "
                + adapter_trait_note
            )
        elif read.supported:
            route.primary_tool = "Decoder CLI/library read-only export"
            route.auxiliary_tool = ""
            route.output_dir = "source/plugin_exports/<ModName>/"
            route.status = "tool-mediated"
            route.agent_allowed = "Read-only tool-mediated export only; no Apply or writeback"
            route.notes = (
                f"plugin_text={read.level}; traits={','.join(route.traits) or '(none)'}. "
                "Export visible text and evidence only. No Apply artifact or plugin writeback may be generated. "
                + adapter_trait_note
            )
        else:
            route.status = "blocked"
            route.risk = "Blocked"
            route.blocked_reason = read.reason
            route.output_dir = "qa/routing_report.md"
            route.agent_allowed = "Inventory only; no text export or writeback"
            route.notes = adapter_trait_note
    elif resource.category == "string_table":
        route.output_dir = "source/string_tables/<ModName>/, translated/string_tables/<ModName>/, out/<ModName>/tool_outputs/"
        route.agent_allowed = "No generic text decoding; controlled tool path only"
        if read.supported:
            route.skill = "skills/xtranslator-gui-automation"
            route.primary_tool = "Codex-only xTranslator STRINGS workflow"
            route.auxiliary_tool = "Codex-only LexTranslator fallback when routed"
            route.risk = "High"
            route.status = "tool-mediated"
            route.blocked_reason = ""
            route.notes = (
                f"{context.display_name} localized string tables must stay on the controlled STRINGS workflow. "
                "Do not generic-decode or treat them as ordinary text resources. Use the existing controlled "
                "Codex-only xTranslator Skill; non-Codex adapters must hand this task back to Codex."
            )
        else:
            route.skill = "manual-review"
            route.primary_tool = "Dedicated string-table adapter"
            route.auxiliary_tool = ""
            route.risk = "Blocked"
            route.status = "blocked"
            route.blocked_reason = "missing string-table adapter"
            route.notes = (
                f"{context.display_name} localized string tables require a dedicated string-table adapter. "
                "The current pipeline cannot decode or write back this format safely, so this path is blocked."
            )
    elif (
        "\\interface\\translations\\" in lowered_relative
        or lowered_relative.startswith("interface\\translations\\")
    ) and resource.extension == ".txt":
        route.skill = "skills/text-resource-translation"
        route.primary_tool = "Agent Text Pipeline"
        route.auxiliary_tool = "LexTranslator"
        route.output_dir = "translated/final_mod/<ModName>/Interface/translations/"
        route.risk = "Low"
        route.agent_allowed = "Yes, write translated copy only"
        route.notes = "Preserve key, tab separator, line count, control codes, and variables."
        apply_loose_text_capability(route, context, resource)
    elif resource.subtype == "papyrus.binary":
        if read.supported and read.adapter_id:
            require_adapter(read.adapter_id, "extract")
        if write.supported and write.adapter_id:
            require_adapter(write.adapter_id, "apply")
        route.skill = "skills/pex-visible-strings-translation"
        route.primary_tool = "Configured PexStringToolPath decoder/rewriter"
        route.auxiliary_tool = "Codex-only LexTranslator/xTranslator PapyrusPex GUI fallback"
        route.output_dir = "source/pex_exports/<ModName>/, translated/lextranslator_ready/<ModName>/, out/<ModName>/tool_outputs/Scripts/"
        route.risk = "High"
        route.agent_allowed = "Only decoder/tool-exported visible strings"
        if not read.supported:
            route.status = "blocked"
            route.risk = "Blocked"
            route.blocked_reason = read.reason
            route.agent_allowed = "No PEX extraction or writeback"
        route.notes = (
            f"pex={read.level}; adapter={read.adapter_id}; write_supported={write.supported}. "
            "Use python scripts/invoke_mutagen_pex_string_tool.py via configured PexStringToolPath first: "
            "Mode Export for instruction-string JSONL, Mode Apply for project-local PEX copy writeback. "
            "It may only write out/<ModName>/tool_outputs/Scripts/*.pex or "
            "translated/tool_outputs/<ModName>/Scripts/*.pex. Agent must not modify .pex directly. "
            "Unknown logic strings stay untranslated."
        )
    elif resource.subtype == "papyrus.source":
        route.skill = "skills/pex-visible-strings-translation"
        route.primary_tool = "Agent read-only analysis"
        route.auxiliary_tool = ""
        route.output_dir = "work/psc_strings/"
        route.risk = "High"
        route.agent_allowed = "Read-only extraction only"
        route.notes = "Do not write back source code and do not compile."
    elif resource.category == "archive" and not inventory.supported:
        route.skill = "manual-review"
        route.primary_tool = "Unsupported archive for current Game Profile"
        route.auxiliary_tool = ""
        route.output_dir = "qa/routing_report.md"
        route.risk = "Blocked"
        route.status = "blocked"
        route.blocked_reason = (
            f"archive format {resource.extension} is not declared for inventory by Game Profile "
            f"{context.game_id}: {inventory.reason}"
        )
        route.agent_allowed = "No extraction or materialization"
        route.notes = (
            f"The current Game Profile {context.game_id} does not declare usable {resource.extension} inventory capability. "
            "Do not infer compatibility from another Bethesda game; update and validate the Game Profile first."
        )
    elif resource.subtype == "archive.bsa":
        inventory = resource_capability_ready(context, resource, "inventory", "inventory")
        materialize = resource_capability_ready(context, resource, "read", "extract")
        route.skill = "skills/bsa-archive-audit"
        route.primary_tool = "bethesda-structs read-only archive audit"
        route.auxiliary_tool = "scripts/new_bsa_archive_manifest.py -> scripts/invoke_bsa_file_extractor_safe.py only when extraction is required"
        route.risk = "Medium"
        route.output_dir = "out/<ModName>/archive_audits/<ArchiveName>/"
        route.agent_allowed = "Audit only; extraction only through project safe wrapper"
        route.notes = (
            f"archive.bsa={inventory.level}; adapter={inventory.adapter_id}; "
            f"materialization_supported={materialize.supported}. "
            "Do not edit or repack BSA. Prefer bethesda-structs inventory and manifest evidence; "
            "if materialization is required, extract only to work/archive_extracts/<ModName>/<ArchiveName>/. "
            "Translated BSA content must become same-path loose override in final_mod by default; BSA repack is a future high-risk adapter path only after manual testing proves it is required."
        )
    elif resource.subtype == "archive.ba2":
        inventory = resource_capability_ready(context, resource, "inventory", "inventory")
        materialize = resource_capability_ready(context, resource, "read", "extract")
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
        if materialize.supported:
            route.notes = (
                f"archive.ba2={inventory.level}; adapter={inventory.adapter_id}; materialization_supported=True. "
                "Do not edit or repack BA2. Use bethesda-structs for read-only inventory. If coverage confirms "
                "that materialization is required, the workflow remains blocked until the controlled adapter is ready. Materialize only "
                "through the safe BA2 wrapper into work/archive_extracts/<ModName>/<ArchiveName>/, verify the "
                "hash-backed manifest, and deliver translated content as same-path loose override."
            )
        else:
            route.notes = (
                f"archive.ba2={inventory.level}; adapter={inventory.adapter_id}; "
                "inventory-only (inventory only). "
                "Materialization is disabled even when a BA2 adapter is configured; do not extract or repack this archive."
            )
    elif resource.category == "package":
        route.skill = "skills/mod-input-preparation"
        route.primary_tool = "Project-local decoder/extraction handoff"
        route.auxiliary_tool = ""
        route.output_dir = "work/extracted_mods/<ModName>/"
        route.risk = "Medium"
        if resource.extension == ".zip":
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
    elif resource.category == "interface":
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = "Protected UI binary"
        route.status = "manual"
        route.agent_allowed = "No automatic translation or binary editing"
        route.notes = "Protected UI binary asset. Copy project-local source unchanged when needed; do not edit."
    elif resource.category == "protected_binary" and (
        bool(resource.container) or resource.extension in {".dll", ".exe"}
    ):
        route.skill = "manual-review"
        route.primary_tool = "Copy unchanged"
        route.auxiliary_tool = "final_mod provenance validation"
        route.output_dir = "out/<ModName>/汉化产出/final_mod/ unchanged copy"
        route.risk = (
            "Profile-protected resource"
            if resource.container == "protected"
            else "Protected binary"
        )
        route.status = "manual"
        route.agent_allowed = "No automatic translation or binary editing"
        route.notes = (
            f"{profile_directory_note(resource, context)} "
            "Protected binary/tool symbol file. Copy project-local source unchanged when needed; do not edit."
        )
    elif resource.container == "mcm" or (
        "mcm" in full_path.name.casefold() and resource.category == "loose_text"
    ):
        route.skill = "skills/mcm-translation"
        route.risk = "Medium"
        route.output_dir = "source/mcm/<ModName>/, translated/final_mod/<ModName>/"
        auto_text_route = True
        if resource.extension in {".json", ".ini"}:
            route.primary_tool = "Agent Structured MCM Extractor"
            route.auxiliary_tool = "Codex-only LexTranslator fallback"
            route.agent_allowed = "Yes, extract and translate confirmed visible MCM values"
            route.notes = (
                "Use the profile-aware structured MCM extractor. LexTranslator is a Codex-only "
                "fallback, not the primary path. Preserve keys, paths, protocol values, and identifiers."
            )
        elif resource.extension == ".txt":
            route.primary_tool = "Agent Text Pipeline"
            route.auxiliary_tool = ""
            route.agent_allowed = "Yes, translate visible MCM text while preserving structure"
            route.notes = (
                "Preserve keys, line structure, placeholders, paths, protocol values, and identifiers."
            )
        elif resource.extension == ".toml":
            auto_text_route = False
            route.primary_tool = "Structured TOML manual review"
            route.auxiliary_tool = ""
            route.output_dir = "qa/mcm_review.md"
            route.risk = "Manual review"
            route.status = "manual"
            route.agent_allowed = "No automatic translation or writeback; manual review required"
            route.notes = (
                "The current workflow has no safe MCM TOML writeback implementation. Preserve the file "
                "and record any player-visible value for manual review."
            )
        else:
            auto_text_route = False
            route.primary_tool = "Manual MCM format review"
            route.auxiliary_tool = ""
            route.output_dir = "qa/mcm_review.md"
            route.risk = "Manual review"
            route.status = "manual"
            route.agent_allowed = "No translation until the MCM format is reviewed"
            route.notes = "No safe automatic MCM handler is declared for this file format."
        if auto_text_route:
            apply_loose_text_capability(route, context, resource)
    elif resource.subtype == "config_text":
        route.skill = "manual-review"
        route.primary_tool = "Full-line comment extraction and manual value review"
        route.auxiliary_tool = ""
        route.output_dir = "qa/"
        route.risk = "Manual review"
        route.status = "manual"
        route.agent_allowed = "No automatic key, path, protocol, or value translation"
        route.notes = (
            "Full-line INI/TOML comments may be extracted as read-only translation candidates. "
            "Review values manually and preserve keys, paths, protocols, identifiers, and structure."
        )
    elif resource.category == "loose_text":
        route.skill = "skills/text-resource-translation"
        route.primary_tool = "Agent Text Pipeline"
        route.auxiliary_tool = ""
        route.output_dir = "translated/final_mod/<ModName>/"
        route.risk = "Low to Medium"
        route.agent_allowed = "Yes, preserve structure"
        route.notes = "Validate format, placeholders, keys, and row or record counts."
        apply_loose_text_capability(route, context, resource)
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
        f"- Category: {route.category}",
        f"- Subtype: {route.subtype}",
        f"- Container: {route.container or '(none)'}",
        f"- Traits: {', '.join(route.traits) or '(none)'}",
        f"- Capability: {route.capability or '(none)'}",
        f"- Effective Capability: {route.effective_capability}",
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
    print(f"Category: {route.category}")
    print(f"Subtype: {route.subtype}")
    print(f"Container: {route.container or '(none)'}")
    print(f"Traits: {', '.join(route.traits) or '(none)'}")
    print(f"Capability: {route.capability or '(none)'}")
    print(f"Effective Capability: {route.effective_capability}")
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
    parser = argparse.ArgumentParser(description="Route a project-local Bethesda Mod file through the current Game Profile.")
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
