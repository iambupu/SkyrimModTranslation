"""Run the composite localized-plugin and string-table adapter."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    prepare_adapter_result_path,
    read_adapter_result,
    write_adapter_result_if_requested,
)
from capability_resolver import CapabilityDecision, resolve_capability
from file_utils import (
    create_regular_directory_under,
    discover_regular_tree,
    write_jsonl_sorted,
)
from file_utils import sha256_file, validate_regular_path_under
from game_context import GameContext, resolve_workspace_game_context, supported_game_ids
from managed_tool_resolver import (
    adapter_uses_managed_binding,
    leased_payload_path,
    load_workspace_tool_config,
)
from localized_delivery import (
    ADAPTER_ID,
    LocalizedCoverage,
    LocalizedPublicationTransaction,
    LocalizedTableComponent,
    build_localized_review_rows,
    build_composite_receipt,
    discover_localized_tables,
    load_localized_references,
    load_table_export_ids,
    load_table_translation_ids,
    validate_composite_receipt,
    verify_localized_reference_coverage,
    write_json_atomic,
)
from plugin_master_style_manifest import prepare_master_style_manifest
from plugin_resource_evidence import discover_regular_plugin_files
from project_paths import (
    find_data_root,
    is_under,
    plugin_root,
    project_root,
    relative_path,
    require_under_any,
    resolve_project_path,
    safe_file_name,
)
from workflow_lock import ResourceLock


MODE_OPERATION = {
    "Inventory": "inventory",
    "Export": "extract",
    "Apply": "apply",
    "Verify": "verify",
}
CAPABILITY_OPERATION = {
    "Inventory": "inventory",
    "Export": "read",
    "Apply": "write",
    "Verify": "read",
}
PLUGIN_EXTENSIONS = frozenset({".esp", ".esm", ".esl"})
EXPERIMENTAL_WARNING = (
    "Experimental localized delivery was used; xEdit and in-game validation remain required."
)


def _option_text(options: Mapping[str, object], name: str, label: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} option {name!r} must be non-empty text")
    return value.strip()


def _resolve_plugin_lane(root: Path, plugin: Path) -> tuple[str, Path]:
    resolved = plugin.resolve(strict=True)
    for base in (
        root / "work" / "extracted_mods",
        root / "work" / "archive_extracts",
    ):
        base_resolved = base.resolve(strict=False)
        if not is_under(resolved, base_resolved):
            continue
        relative = resolved.relative_to(base_resolved)
        if len(relative.parts) >= 2:
            return relative.parts[0], base_resolved / relative.parts[0]
    raise ValueError(
        "Localized plugin must identify a Mod lane under work/extracted_mods "
        f"or work/archive_extracts: {plugin}"
    )


def _require_unique_localized_plugin_stem(data_root: Path, plugin: Path) -> None:
    collisions = [
        candidate
        for candidate in discover_regular_plugin_files(
            data_root,
            PLUGIN_EXTENSIONS,
            label="Localized plugin lane",
        )
        if candidate.stem.casefold() == plugin.stem.casefold()
    ]
    if len(collisions) > 1:
        names = ", ".join(
            sorted(
                (relative_path(data_root, candidate) for candidate in collisions),
                key=str.casefold,
            )
        )
        raise ValueError(
            "Localized string-table basename collision; process cannot safely "
            f"distinguish these plugins: {names}"
        )


def _decision_payload(decision: CapabilityDecision) -> dict[str, object]:
    return {
        "level": decision.level,
        "adapter_id": decision.adapter_id,
        "supported": decision.supported,
        "operation": decision.operation,
    }


def _write_report(
    path: Path,
    *,
    root: Path,
    mode: str,
    status: str,
    game_id: str,
    mod_name: str,
    plugin: Path,
    capability_level: str,
    reference_count: int,
    components: tuple[LocalizedTableComponent, ...],
    coverage: LocalizedCoverage | None,
    reason: str = "",
) -> None:
    lines = [
        "# Bethesda Localized Delivery Report",
        "",
        f"- game_id: {game_id}",
        f"- mod_name: {mod_name}",
        f"- adapter_id: {ADAPTER_ID}",
        f"- operation: {MODE_OPERATION[mode]}",
        f"- capability_level: {capability_level}",
        f"- status: {status}",
        f"- plugin: {relative_path(root, plugin).replace(chr(92), '/')}",
        f"- plugin_sha256: {sha256_file(plugin)}",
        f"- localized_reference_count: {reference_count}",
        f"- table_components: {len(components)}",
    ]
    if coverage is not None:
        lines.extend(
            (
                f"- reference_coverage: {'passed' if coverage.passed else 'blocked'}",
                f"- resolved_references: {coverage.resolved_count}/{coverage.reference_count}",
                f"- missing_references: {len(coverage.missing)}",
            )
        )
    if reason:
        lines.append(f"- reason: {reason.replace(chr(10), ' ').replace(chr(13), ' ')}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _run_plugin_inventory(
    *,
    root: Path,
    context: GameContext,
    plugin: Path,
    output_jsonl: Path,
    report: Path,
    config: Path,
    master_style_manifest: Path | None,
) -> None:
    plugin_capability = context.require_capability("plugin_text")
    mutagen_release = _option_text(
        plugin_capability.options,
        "mutagen_release",
        "plugin_text",
    )
    plugin_hash = sha256_file(plugin)
    tool_config = load_workspace_tool_config(root, config)
    with leased_payload_path(
        root,
        tool_config,
        "MutagenCliPath",
        command="run localized plugin inventory",
    ) as adapter_resolution, leased_payload_path(
        root,
        tool_config,
        "DotNetSdkPath",
        command="run localized plugin inventory",
        managed_only=adapter_uses_managed_binding(
            root,
            tool_config,
            "MutagenCliPath",
        ),
    ) as dotnet_resolution:
        if dotnet_resolution.path is None or adapter_resolution.path is None:
            raise FileNotFoundError("managed localized plugin adapter binding is unavailable")
        command = [
            str(dotnet_resolution.path),
            str(adapter_resolution.path),
            "localized-inventory",
            "--game",
            context.game_id,
            "--mutagen-release",
            mutagen_release,
            "--capability-level",
            "read_only",
            "--project-root",
            str(root),
            "--input-plugin",
            str(plugin),
            "--output-jsonl",
            str(output_jsonl),
            "--report",
            str(report),
        ]
        if master_style_manifest is not None:
            command.extend(("--master-style-manifest", str(master_style_manifest)))
        result = subprocess.run(command, cwd=str(root), check=False)
    if sha256_file(plugin) != plugin_hash:
        raise RuntimeError("Localized plugin changed during read-only inventory")
    if result.returncode != 0 or not output_jsonl.is_file() or not report.is_file():
        raise RuntimeError(f"Localized plugin inventory failed with exit code {result.returncode}")


def _component_result_path(component: LocalizedTableComponent, mode: str) -> Path:
    return {
        "Export": component.apply_result.with_name(
            component.apply_result.name.replace(".apply.", ".extract.")
        ),
        "Apply": component.apply_result,
        "Verify": component.verify_result,
    }[mode]


def _component_report(component: LocalizedTableComponent, mode: str) -> Path:
    result_path = _component_result_path(component, mode)
    suffix = ".adapter-result.json"
    if not result_path.name.endswith(suffix):
        raise ValueError(f"Unexpected component AdapterResult name: {result_path.name}")
    return result_path.with_name(result_path.name[: -len(suffix)] + ".md")


def _run_string_component(
    *,
    root: Path,
    game_id: str,
    mod_name: str,
    mode: str,
    component: LocalizedTableComponent,
    config: Path,
    experimental: bool,
) -> Path:
    result_path = _component_result_path(component, mode)
    command = [
        sys.executable,
        str(plugin_root() / "scripts" / "invoke_bethesda_string_table_tool.py"),
        "--mode",
        mode,
        "--input-table-path",
        str(component.source_path),
        "--report-path",
        str(_component_report(component, mode)),
        "--config-path",
        str(config),
        "--game",
        game_id,
        "--adapter-result-path",
        str(result_path),
    ]
    if mode == "Export":
        command.extend(("--output-jsonl-path", str(component.export_jsonl)))
    else:
        command.extend(
            (
                "--translation-jsonl-path",
                str(component.translation_jsonl),
                "--output-table-path",
                str(component.output_path),
            )
        )
        if mode == "Verify":
            command.extend(("--apply-adapter-result-path", str(component.apply_result)))
        elif experimental:
            command.append("--allow-experimental-writeback")
    result = subprocess.run(command, cwd=str(root), check=False)
    if result.returncode != 0 or not result_path.is_file():
        raise RuntimeError(
            f"Localized {component.table_type} component {mode} failed with "
            f"exit code {result.returncode}"
        )
    receipt = read_adapter_result(result_path)
    if (
        receipt.status != "success"
        or receipt.operation != MODE_OPERATION[mode]
        or receipt.adapter_id != "bethesda-string-tables"
        or receipt.mod_name != mod_name
    ):
        raise ValueError(f"Localized {component.table_type} component receipt is invalid")
    return result_path


def _staged_components(
    *,
    root: Path,
    mod_name: str,
    mode: str,
    components: tuple[LocalizedTableComponent, ...],
) -> tuple[tuple[LocalizedTableComponent, ...], tuple[Path, ...]]:
    token = uuid4().hex
    output_root = (
        root
        / "out"
        / mod_name
        / "tool_outputs"
        / f".localized-staging-{token}"
    )
    evidence_root = (
        root
        / "qa"
        / "localized_delivery"
        / mod_name
        / f".staging-{token}"
    )
    staged: list[LocalizedTableComponent] = []
    for component in components:
        if mode == "Apply":
            staged.append(
                replace(
                    component,
                    output_path=output_root / "Strings" / component.output_path.name,
                    apply_result=evidence_root / "components" / component.apply_result.name,
                    verify_result=evidence_root / "components" / component.verify_result.name,
                )
            )
        elif mode == "Verify":
            staged.append(
                replace(
                    component,
                    verify_result=evidence_root / "components" / component.verify_result.name,
                )
            )
        else:
            raise ValueError(f"Unsupported transactional component mode: {mode}")
    roots = (evidence_root,) if mode == "Verify" else (output_root, evidence_root)
    return tuple(staged), roots


def _snapshot_translation_components(
    *,
    root: Path,
    mod_name: str,
    plugin: Path,
    components: tuple[LocalizedTableComponent, ...],
) -> tuple[LocalizedTableComponent, ...]:
    snapshot_root = (
        root
        / "translated"
        / "localized_snapshots"
        / mod_name
        / safe_file_name(plugin.name)
    )
    create_regular_directory_under(
        snapshot_root,
        root / "translated",
        label="Localized translation snapshot directory",
    )
    snapshots: list[LocalizedTableComponent] = []
    for component in components:
        source = validate_regular_path_under(
            component.translation_jsonl,
            root / "translated",
            kind="file",
            label="Localized translation JSONL",
        )
        source_hash = sha256_file(source)
        destination = snapshot_root / f"{component.table_type}.{source_hash}.jsonl"
        if os.path.lexists(destination):
            snapshot = validate_regular_path_under(
                destination,
                root / "translated",
                kind="file",
                label="Localized translation snapshot",
            )
            if sha256_file(snapshot) != source_hash:
                raise ValueError(
                    "Localized translation snapshot hash conflicts with its identity"
                )
        else:
            temporary = destination.with_name(
                f".{destination.name}.{uuid4().hex}.tmp"
            )
            shutil.copy2(source, temporary)
            snapshot = validate_regular_path_under(
                temporary,
                root / "translated",
                kind="file",
                label="Temporary localized translation snapshot",
            )
            if sha256_file(source) != source_hash or sha256_file(snapshot) != source_hash:
                snapshot.unlink(missing_ok=True)
                raise ValueError(
                    "Localized translation JSONL changed while creating its snapshot"
                )
            os.replace(snapshot, destination)
            snapshot = validate_regular_path_under(
                destination,
                root / "translated",
                kind="file",
                label="Localized translation snapshot",
            )
        snapshots.append(replace(component, translation_jsonl=snapshot))
    return tuple(snapshots)


def _validate_translation_snapshots(
    components: tuple[LocalizedTableComponent, ...],
) -> None:
    for component in components:
        identity = component.translation_jsonl.name.removesuffix(".jsonl")
        _, separator, expected_hash = identity.rpartition(".")
        if (
            not separator
            or len(expected_hash) != 64
            or sha256_file(component.translation_jsonl).casefold()
            != expected_hash.casefold()
        ):
            raise ValueError(
                "Localized translation snapshot changed after coverage was computed"
            )


def _capture_localized_evidence_inputs(
    paths: tuple[Path, ...],
) -> dict[Path, str]:
    return {
        path.resolve(strict=True): sha256_file(path)
        for path in paths
    }


def _validate_localized_evidence_inputs(bindings: Mapping[Path, str]) -> None:
    for path, expected_hash in bindings.items():
        if sha256_file(path) != expected_hash:
            raise RuntimeError(
                f"Localized evidence input changed after coverage: {path}"
            )


def _remove_stage_roots(root: Path, stage_roots: tuple[Path, ...]) -> None:
    lexical_root = Path(os.path.abspath(root))
    for stage_root in stage_roots:
        lexical_stage = Path(os.path.abspath(stage_root))
        try:
            relative = lexical_stage.relative_to(lexical_root)
        except ValueError:
            raise ValueError(f"Localized staging path escapes the workspace: {stage_root}")
        parts = relative.parts
        output_stage = (
            len(parts) == 4
            and parts[0].casefold() == "out"
            and parts[2].casefold() == "tool_outputs"
            and parts[3].startswith(".localized-staging-")
            and len(parts[3]) > len(".localized-staging-")
        )
        evidence_stage = (
            len(parts) == 4
            and parts[0].casefold() == "qa"
            and parts[1].casefold() == "localized_delivery"
            and parts[3].startswith(".staging-")
            and len(parts[3]) > len(".staging-")
        )
        if not (output_stage or evidence_stage):
            raise ValueError(
                f"Localized staging path has an unexpected identity: {stage_root}"
            )
        if not os.path.lexists(lexical_stage):
            continue
        validate_regular_path_under(
            lexical_stage,
            lexical_root,
            kind="directory",
            label="Localized staging cleanup",
        )
        discover_regular_tree(
            lexical_stage,
            label="Localized staging cleanup",
        )
        shutil.rmtree(lexical_stage)


def _prepare_components(
    *,
    root: Path,
    context: GameContext,
    mod_name: str,
    data_root: Path,
    plugin: Path,
    references_path: Path,
) -> tuple[tuple[object, ...], tuple[LocalizedTableComponent, ...], str, str]:
    references = load_localized_references(
        references_path,
        game_id=context.game_id,
        plugin_name=plugin.name,
    )
    string_capability = context.require_capability("string_tables")
    source_language = _option_text(
        string_capability.options,
        "source_language",
        "string_tables",
    )
    target_language = _option_text(
        string_capability.options,
        "target_language",
        "string_tables",
    )
    components = discover_localized_tables(
        data_root=data_root,
        plugin_path=plugin,
        source_language=source_language,
        target_language=target_language,
        mod_name=mod_name,
        root=root,
        required_types={reference.table_type for reference in references},
    )
    return references, components, source_language, target_language


def _export_and_cover(
    *,
    root: Path,
    context: GameContext,
    mod_name: str,
    plugin: Path,
    references,
    components: tuple[LocalizedTableComponent, ...],
    source_language: str,
    config: Path,
    require_translations: bool,
) -> tuple[LocalizedCoverage, tuple[Path, ...]]:
    result_paths: list[Path] = []
    table_ids: dict[str, frozenset[int]] = {}
    translated_ids: dict[str, frozenset[int]] = {}
    for component in components:
        result_paths.append(
            _run_string_component(
                root=root,
                game_id=context.game_id,
                mod_name=mod_name,
                mode="Export",
                component=component,
                config=config,
                experimental=False,
            )
        )
        table_ids[component.table_type] = load_table_export_ids(
            component.export_jsonl,
            root=root,
            game_id=context.game_id,
            plugin_basename=plugin.stem,
            table_type=component.table_type,
            source_language=source_language,
            source_table=component.source_path,
        )
        if require_translations:
            validate_regular_path_under(
                component.translation_jsonl,
                root / "translated",
                kind="file",
                label="Localized translation JSONL",
            )
            translated_ids[component.table_type] = load_table_translation_ids(
                component.translation_jsonl,
                root=root,
                game_id=context.game_id,
                plugin_basename=plugin.stem,
                table_type=component.table_type,
                source_language=source_language,
                source_table=component.source_path,
            )
    coverage = verify_localized_reference_coverage(
        references,
        table_ids,
        translated_ids if require_translations else None,
    )
    if not coverage.passed:
        missing = "; ".join(
            f"{item['record_type']} {item['form_id']} {item['field_path']} "
            f"{item['table_type']}:{item['string_id']}"
            for item in coverage.missing
        )
        raise ValueError(f"Localized reference coverage failed: {missing}")
    return coverage, tuple(result_paths)


def _write_referenced_review_input(
    *,
    root: Path,
    destination: Path,
    components: tuple[LocalizedTableComponent, ...],
    coverage: LocalizedCoverage,
) -> Path:
    rows = build_localized_review_rows(
        root=root,
        source_paths={
            component.table_type: component.source_path
            for component in components
        },
        translation_paths={
            component.table_type: component.translation_jsonl
            for component in components
        },
        coverage=coverage,
    )
    create_regular_directory_under(
        destination.parent,
        root / "translated",
        label="Localized referenced review directory",
    )
    write_jsonl_sorted(destination, rows)
    return validate_regular_path_under(
        destination,
        root / "translated",
        kind="file",
        label="Localized referenced review input",
    )


def _translated_target_light_state(
    references,
    coverage: LocalizedCoverage,
) -> bool | None:
    translated_ids = {
        table_type: set(values)
        for table_type, values in coverage.translated_ids.items()
    }
    state: bool | None = False
    for reference in references:
        if reference.string_id not in translated_ids.get(reference.table_type, set()):
            continue
        style = reference.master_style.casefold()
        if style == "unknown":
            state = None
        elif style == "light":
            if state is not None:
                state = True
        elif style != "full":
            raise ValueError(
                f"Unsupported localized target master style: {reference.master_style}"
            )
    return state


def _acquire_localized_lane_lock(root: Path, mod_name: str) -> ResourceLock:
    return ResourceLock(
        root,
        f"mod:{mod_name}",
        "invoke_bethesda_localized_delivery.py",
    ).acquire()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run localized plugin and string-table joint delivery."
    )
    parser.add_argument("--mode", choices=tuple(MODE_OPERATION), required=True)
    parser.add_argument("--plugin-path", required=True)
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--master-style-manifest", default="")
    parser.add_argument("--adapter-result-path", default="")
    parser.add_argument("--allow-experimental-writeback", action="store_true")
    args = parser.parse_args()

    root = project_root()
    adapter_result_path = prepare_adapter_result_path(root, args.adapter_result_path)
    operation = MODE_OPERATION[args.mode]
    decision: CapabilityDecision | None = None
    context: GameContext | None = None
    report: Path | None = None
    generated: list[Path] = []
    plugin: Path | None = None
    mod_name = ""
    components: tuple[LocalizedTableComponent, ...] = ()
    references = ()
    coverage: LocalizedCoverage | None = None
    lane_lock: ResourceLock | None = None
    lane_lock_attempted = False

    try:
        context = resolve_workspace_game_context(root, args.game)
        decision = resolve_capability(
            context,
            "localized_delivery",
            CAPABILITY_OPERATION[args.mode],
        )
        if not decision.supported:
            write_adapter_result_if_requested(
                adapter_result_path,
                lambda: build_result(
                    root=root,
                    status="blocked",
                    error_code=decision.error_code or "capability_unsupported",
                    operation=operation,
                    adapter_id=decision.adapter_id or ADAPTER_ID,
                    blockers=(decision.reason,),
                ),
            )
            return 2
        if decision.adapter_id != ADAPTER_ID:
            raise ValueError("localized_delivery does not resolve to the composite adapter")
        require_adapter(ADAPTER_ID, operation)
        if (
            args.mode == "Apply"
            and decision.level == "experimental_write"
            and not args.allow_experimental_writeback
        ):
            raise ValueError(
                "Experimental localized delivery requires --allow-experimental-writeback"
            )

        plugin = validate_regular_path_under(
            resolve_project_path(root, args.plugin_path, must_exist=True),
            root,
            kind="file",
            label="Localized plugin",
        )
        lane_name, lane_root = _resolve_plugin_lane(root, plugin)
        mod_name = args.mod_name.strip() or lane_name
        if safe_file_name(mod_name) != mod_name or mod_name != lane_name:
            raise ValueError("--mod-name must exactly match the localized plugin Mod lane")
        lane_lock_attempted = True
        lane_lock = _acquire_localized_lane_lock(root, mod_name)
        data_root = validate_regular_path_under(
            find_data_root(lane_root, context=context),
            root,
            kind="directory",
            label="Localized Mod Data root",
        )
        if not is_under(plugin.resolve(strict=True), data_root):
            raise ValueError("Localized plugin is outside the detected Mod Data root")
        _require_unique_localized_plugin_stem(data_root, plugin)
        config = validate_regular_path_under(
            resolve_project_path(root, args.config_path, must_exist=True),
            root,
            kind="file",
            label="ConfigPath",
        )
        master_style_manifest = None
        if args.master_style_manifest:
            master_style_manifest = resolve_project_path(
                root,
                args.master_style_manifest,
                must_exist=True,
            )
            require_under_any(
                master_style_manifest,
                [root / "work" / "plugin_context"],
                "MasterStyleManifest",
            )
            master_style_manifest = validate_regular_path_under(
                master_style_manifest,
                root / "work" / "plugin_context",
                kind="file",
                label="MasterStyleManifest",
            )
        stem = safe_file_name(plugin.name)
        references_path = (
            root / "source" / "localized_delivery" / mod_name / f"{stem}.references.jsonl"
        )
        plugin_inventory_report = (
            root / "qa" / "localized_delivery" / mod_name / f"{stem}.inventory.md"
        )
        report = root / "qa" / "localized_delivery" / mod_name / f"{stem}.{args.mode.lower()}.md"
        inventory_manifest = (
            root / "qa" / "localized_delivery" / mod_name / f"{stem}.inventory.json"
        )
        coverage_report = (
            root / "qa" / "localized_delivery" / mod_name / f"{stem}.coverage.json"
        )
        composite_receipt = (
            root
            / "qa"
            / "localized_delivery"
            / mod_name
            / f"{stem}.{operation}.composite.json"
        )
        referenced_review_input = (
            root
            / "translated"
            / mod_name
            / "localized_delivery"
            / f"{stem}.referenced-translations.jsonl"
        )
        generated.extend((references_path, plugin_inventory_report, report, inventory_manifest))
        for path in generated:
            create_regular_directory_under(
                path.parent,
                root,
                label="Localized generated artifact directory",
            )
            if path.exists():
                validate_regular_path_under(
                    path,
                    root,
                    kind="file",
                    label="Localized generated artifact",
                ).unlink()

        _run_plugin_inventory(
            root=root,
            context=context,
            plugin=plugin,
            output_jsonl=references_path,
            report=plugin_inventory_report,
            config=config,
            master_style_manifest=master_style_manifest,
        )
        references, components, source_language, target_language = _prepare_components(
            root=root,
            context=context,
            mod_name=mod_name,
            data_root=data_root,
            plugin=plugin,
            references_path=references_path,
        )
        if args.mode in {"Apply", "Verify"}:
            components = _snapshot_translation_components(
                root=root,
                mod_name=mod_name,
                plugin=plugin,
                components=components,
            )

        component_results: tuple[Path, ...] = ()
        verification_results: tuple[Path, ...] = ()
        transactional_report_written = False
        if args.mode != "Inventory":
            coverage, _ = _export_and_cover(
                root=root,
                context=context,
                mod_name=mod_name,
                plugin=plugin,
                references=references,
                components=components,
                source_language=source_language,
                config=config,
                require_translations=args.mode in {"Apply", "Verify"},
            )
            if args.mode == "Export":
                write_json_atomic(coverage_report, coverage.payload())
                generated.append(coverage_report)
            elif _translated_target_light_state(references, coverage) is None:
                translated_ids = {
                    table_type: set(values)
                    for table_type, values in coverage.translated_ids.items()
                }
                target_masters: set[str] = set()
                for reference in references:
                    if reference.string_id not in translated_ids.get(
                        reference.table_type, set()
                    ):
                        continue
                    if reference.master_style.casefold() != "unknown":
                        continue
                    if (
                        reference.master_style_evidence
                        != "unresolved:unseparated-master-order"
                    ):
                        raise ValueError(
                            "Unknown localized target has invalid master-style evidence"
                        )
                    if not reference.owner_mod_key.strip():
                        raise ValueError(
                            "Unknown localized target is missing owner_mod_key"
                        )
                    target_masters.add(reference.owner_mod_key)
                if not target_masters:
                    raise ValueError(
                        "Localized translated targets have unknown ownership without "
                        "an unresolved target row"
                    )
                master_style_manifest = prepare_master_style_manifest(
                    root=root,
                    game_id=context.game_id,
                    mod_name=mod_name,
                    plugin=plugin,
                    plugin_root=lane_root,
                    relative_plugin=plugin.relative_to(data_root),
                    required_masters=tuple(
                        sorted(target_masters, key=str.casefold)
                    ),
                )
                if master_style_manifest is None:
                    raise ValueError(
                        "Target-scoped master-style evidence did not resolve "
                        "localized translated targets"
                    )
                _run_plugin_inventory(
                    root=root,
                    context=context,
                    plugin=plugin,
                    output_jsonl=references_path,
                    report=plugin_inventory_report,
                    config=config,
                    master_style_manifest=master_style_manifest,
                )
                references = load_localized_references(
                    references_path,
                    game_id=context.game_id,
                    plugin_name=plugin.name,
                )
                coverage, _ = _export_and_cover(
                    root=root,
                    context=context,
                    mod_name=mod_name,
                    plugin=plugin,
                    references=references,
                    components=components,
                    source_language=source_language,
                    config=config,
                    require_translations=True,
                )
                if _translated_target_light_state(references, coverage) is None:
                    raise ValueError(
                        "Target-scoped master-style evidence did not resolve "
                        "localized translated targets"
                    )

        localized_evidence_inputs: dict[Path, str] = {}
        if args.mode in {"Apply", "Verify"}:
            semantic_inputs = [plugin, references_path]
            for component in components:
                semantic_inputs.extend(
                    (
                        component.source_path,
                        component.export_jsonl,
                        component.translation_jsonl,
                    )
                )
            localized_evidence_inputs = _capture_localized_evidence_inputs(
                tuple(semantic_inputs)
            )

        inventory_payload = {
            "schema_version": 1,
            "game_id": context.game_id,
            "mod_name": mod_name,
            "plugin": {
                "path": relative_path(root, plugin).replace("\\", "/"),
                "sha256": sha256_file(plugin),
                "file_name": plugin.name,
                "localized": True,
            },
            "source_language": source_language,
            "target_language": target_language,
            "references": {
                "path": relative_path(root, references_path).replace("\\", "/"),
                "sha256": sha256_file(references_path),
                "count": len(references),
            },
            "source_tables": [
                {
                    "table_type": component.table_type,
                    "path": relative_path(root, component.source_path).replace("\\", "/"),
                    "sha256": sha256_file(component.source_path),
                }
                for component in components
            ],
        }
        write_json_atomic(inventory_manifest, inventory_payload)

        string_decision = resolve_capability(
            context,
            "string_tables",
            "write" if args.mode in {"Apply", "Verify"} else "read",
        )
        localized_write_decision = decision
        if args.mode in {"Apply", "Verify"}:
            localized_write_decision = resolve_capability(
                context,
                "localized_delivery",
                "write",
            )
            if (
                not localized_write_decision.supported
                or localized_write_decision.adapter_id != ADAPTER_ID
            ):
                raise ValueError(localized_write_decision.reason)
            if not string_decision.supported:
                raise ValueError(string_decision.reason)
            for component in components:
                if not component.translation_jsonl.is_file():
                    raise FileNotFoundError(
                        "Localized translation JSONL is missing: "
                        f"{component.translation_jsonl}"
                    )
                validate_regular_path_under(
                    component.translation_jsonl,
                    root / "translated",
                    kind="file",
                    label="Localized translation snapshot",
                )
            assert coverage is not None
            _validate_translation_snapshots(components)
            _validate_localized_evidence_inputs(localized_evidence_inputs)
            staged_components, stage_roots = _staged_components(
                root=root,
                mod_name=mod_name,
                mode=args.mode,
                components=components,
            )
            try:
                for staged in staged_components:
                    if args.mode == "Apply":
                        _run_string_component(
                            root=root,
                            game_id=context.game_id,
                            mod_name=mod_name,
                            mode="Apply",
                            component=staged,
                            config=config,
                            experimental=string_decision.level == "experimental_write",
                        )
                    _run_string_component(
                        root=root,
                        game_id=context.game_id,
                        mod_name=mod_name,
                        mode="Verify",
                        component=staged,
                        config=config,
                        experimental=string_decision.level == "experimental_write",
                    )

                mode_result_paths: list[Path] = []
                verification_result_paths: list[Path] = []
                with LocalizedPublicationTransaction(root, mod_name) as transaction:
                    for component, staged in zip(
                        components,
                        staged_components,
                        strict=True,
                    ):
                        if args.mode == "Apply":
                            staged_output_hash = sha256_file(staged.output_path)
                            transaction.publish(staged.output_path, component.output_path)
                            transaction.protect(_component_report(component, "Apply"))
                            transaction.protect(component.apply_result)
                            mode_result_paths.append(
                                _run_string_component(
                                    root=root,
                                    game_id=context.game_id,
                                    mod_name=mod_name,
                                    mode="Apply",
                                    component=component,
                                    config=config,
                                    experimental=string_decision.level
                                    == "experimental_write",
                                )
                            )
                            if sha256_file(component.output_path) != staged_output_hash:
                                raise RuntimeError(
                                    "Localized output differs from its verified staged output"
                                )
                            transaction.protect(_component_report(component, "Verify"))
                            transaction.protect(component.verify_result)
                            verification_result_paths.append(
                                _run_string_component(
                                    root=root,
                                    game_id=context.game_id,
                                    mod_name=mod_name,
                                    mode="Verify",
                                    component=component,
                                    config=config,
                                    experimental=string_decision.level
                                    == "experimental_write",
                                )
                            )
                        else:
                            transaction.protect(_component_report(component, "Verify"))
                            transaction.protect(component.verify_result)
                            mode_result_paths.append(
                                _run_string_component(
                                    root=root,
                                    game_id=context.game_id,
                                    mod_name=mod_name,
                                    mode="Verify",
                                    component=component,
                                    config=config,
                                    experimental=string_decision.level
                                    == "experimental_write",
                                )
                            )

                    component_results = tuple(mode_result_paths)
                    verification_results = tuple(verification_result_paths)
                    _validate_translation_snapshots(components)
                    _validate_localized_evidence_inputs(localized_evidence_inputs)
                    transaction.protect(referenced_review_input)
                    _write_referenced_review_input(
                        root=root,
                        destination=referenced_review_input,
                        components=components,
                        coverage=coverage,
                    )
                    transaction.protect(coverage_report)
                    write_json_atomic(coverage_report, coverage.payload())
                    payload = build_composite_receipt(
                        root=root,
                        operation=operation,
                        game_id=context.game_id,
                        mod_name=mod_name,
                        capability_level=localized_write_decision.level,
                        plugin_path=plugin,
                        references_path=references_path,
                        references=references,
                        source_language=source_language,
                        target_language=target_language,
                        components=components,
                        component_result_paths=component_results,
                        verification_result_paths=verification_results,
                        coverage=coverage,
                        coverage_report=coverage_report,
                        review_input=referenced_review_input,
                        evidence_input_hashes=localized_evidence_inputs,
                        capability_decisions={
                            "localized_delivery": _decision_payload(
                                localized_write_decision
                            ),
                            "string_tables": _decision_payload(string_decision),
                        },
                        master_style_context=master_style_manifest,
                    )
                    transaction.protect(composite_receipt)
                    write_json_atomic(composite_receipt, payload)
                    validate_composite_receipt(root, composite_receipt)
                    transaction.protect(report)
                    _write_report(
                        report,
                        root=root,
                        mode=args.mode,
                        status="ready",
                        game_id=context.game_id,
                        mod_name=mod_name,
                        plugin=plugin,
                        capability_level=localized_write_decision.level,
                        reference_count=len(references),
                        components=components,
                        coverage=coverage,
                    )
                    transaction.commit()
                    transactional_report_written = True
            finally:
                _remove_stage_roots(root, stage_roots)

            generated.extend(component.output_path for component in components)
            generated.extend(component_results)
            generated.extend(verification_results)
            generated.extend(
                (coverage_report, composite_receipt, referenced_review_input)
            )

        if not transactional_report_written:
            _write_report(
                report,
                root=root,
                mode=args.mode,
                status="ready",
                game_id=context.game_id,
                mod_name=mod_name,
                plugin=plugin,
                capability_level=decision.level,
                reference_count=len(references),
                components=components,
                coverage=coverage,
            )
        artifacts = [references_path, inventory_manifest, report]
        artifacts.extend(component.export_jsonl for component in components if component.export_jsonl.is_file())
        if coverage_report.is_file():
            artifacts.append(coverage_report)
        if args.mode in {"Apply", "Verify"}:
            artifacts.extend(component.output_path for component in components)
            artifacts.extend((composite_receipt, referenced_review_input))
        inputs = [plugin]
        inputs.extend(component.source_path for component in components)
        if args.mode in {"Apply", "Verify"}:
            inputs.extend(component.translation_jsonl for component in components)
        warnings = (
            (EXPERIMENTAL_WARNING,)
            if decision.level == "experimental_write" and args.mode in {"Apply", "Verify"}
            else ()
        )
        write_adapter_result_if_requested(
            adapter_result_path,
            lambda: build_result(
                root=root,
                status="success",
                error_code=None,
                operation=operation,
                adapter_id=ADAPTER_ID,
                artifact_paths=artifacts,
                evidence_paths=(report, plugin_inventory_report),
                warnings=warnings,
                mod_name=mod_name,
                input_paths=inputs,
            ),
        )
        return 0
    except Exception as exc:
        failure_reason = str(exc)
        if lane_lock_attempted and lane_lock is None:
            if adapter_result_path is None:
                raise
            print(f"Bethesda localized delivery failed: {failure_reason}", file=sys.stderr)
            return 1
        failure_report = report
        if report is not None and args.mode in {"Apply", "Verify"}:
            failure_report = report.with_name(f"{report.stem}.failed{report.suffix}")
        if report is not None and plugin is not None and plugin.is_file():
            try:
                _write_report(
                    failure_report,
                    root=root,
                    mode=args.mode,
                    status="blocked",
                    game_id=context.game_id if context is not None else args.game,
                    mod_name=mod_name,
                    plugin=plugin,
                    capability_level=decision.level if decision is not None else "unsupported",
                    reference_count=len(references),
                    components=components,
                    coverage=coverage,
                    reason=failure_reason,
                )
            except Exception:
                pass
        evidence = (
            (failure_report,)
            if failure_report is not None and failure_report.is_file()
            else ()
        )
        failure_inputs = (
            (plugin,)
            if plugin is not None and plugin.is_file() and mod_name
            else ()
        )
        write_adapter_result_if_requested(
            adapter_result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code="adapter_failed",
                operation=operation,
                adapter_id=ADAPTER_ID,
                evidence_paths=evidence,
                blockers=(failure_reason,),
                mod_name=mod_name if failure_inputs else "",
                input_paths=failure_inputs,
            ),
        )
        if adapter_result_path is None:
            raise
        print(f"Bethesda localized delivery failed: {failure_reason}", file=sys.stderr)
        return 1
    finally:
        if lane_lock is not None:
            lane_lock.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Bethesda localized delivery failed: {exc}", file=sys.stderr)
        sys.exit(1)
