"""Run the composite localized-plugin and string-table adapter."""

from __future__ import annotations

import argparse
import json
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
from dotnet_adapter_cache import configured_dotnet_path, ensure_adapter_dll
from file_utils import create_regular_directory_under, read_json_unchecked as read_json
from file_utils import sha256_file, validate_regular_path_under
from game_context import GameContext, resolve_workspace_game_context, supported_game_ids
from localized_delivery import (
    ADAPTER_ID,
    LocalizedCoverage,
    LocalizedPublicationTransaction,
    LocalizedTableComponent,
    build_composite_receipt,
    discover_localized_tables,
    load_localized_references,
    load_table_export_ids,
    validate_composite_receipt,
    verify_localized_reference_coverage,
    write_json_atomic,
)
from plugin_master_style_manifest import prepare_master_style_manifest
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
    dotnet = configured_dotnet_path(root, read_json(config), source_root=plugin_root())
    adapter_dll = ensure_adapter_dll(root, plugin_root(), dotnet, "SkyrimPluginTextTool")
    command = [
        str(dotnet),
        str(adapter_dll),
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
    plugin_hash = sha256_file(plugin)
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


def _remove_stage_roots(root: Path, stage_roots: tuple[Path, ...]) -> None:
    resolved_root = root.resolve(strict=True)
    for stage_root in stage_roots:
        resolved = stage_root.resolve(strict=False)
        if not is_under(resolved, resolved_root):
            raise ValueError(f"Localized staging path escapes the workspace: {stage_root}")
        if resolved.exists():
            shutil.rmtree(resolved, ignore_errors=True)


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
) -> tuple[LocalizedCoverage, tuple[Path, ...]]:
    result_paths: list[Path] = []
    table_ids: dict[str, frozenset[int]] = {}
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
    coverage = verify_localized_reference_coverage(references, table_ids)
    if not coverage.passed:
        missing = "; ".join(
            f"{item['record_type']} {item['form_id']} {item['field_path']} "
            f"{item['table_type']}:{item['string_id']}"
            for item in coverage.missing
        )
        raise ValueError(f"Localized reference coverage failed: {missing}")
    return coverage, tuple(result_paths)


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
        data_root = validate_regular_path_under(
            find_data_root(lane_root, context=context),
            root,
            kind="directory",
            label="Localized Mod Data root",
        )
        if not is_under(plugin.resolve(strict=True), data_root):
            raise ValueError("Localized plugin is outside the detected Mod Data root")
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
        if args.mode in {"Apply", "Verify"} and master_style_manifest is None:
            master_style_manifest = prepare_master_style_manifest(
                root=root,
                game_id=context.game_id,
                mod_name=mod_name,
                plugin=plugin,
                relative_plugin=plugin.relative_to(data_root),
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
            )
            if args.mode == "Export":
                write_json_atomic(coverage_report, coverage.payload())
                generated.append(coverage_report)

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
                    label="Localized translation JSONL",
                )
            assert coverage is not None
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
            generated.extend((coverage_report, composite_receipt))

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
            artifacts.append(composite_receipt)
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
                    reason=str(exc),
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
                blockers=(str(exc),),
                mod_name=mod_name if failure_inputs else "",
                input_paths=failure_inputs,
            ),
        )
        if adapter_result_path is None:
            raise
        print(f"Bethesda localized delivery failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Bethesda localized delivery failed: {exc}", file=sys.stderr)
        sys.exit(1)
