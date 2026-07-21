"""Run the ESP/ESM/ESL translation stage for project-local plugin files."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from adapter_registry import require_adapter, require_script_entrypoint
from adapter_result_io import read_adapter_result
from capability_resolver import CapabilityDecision, resolve_capability, resolve_resource_capability
from game_context import GameContext, resolve_workspace_game_context, supported_game_ids
from project_paths import find_data_root
from project_paths import project_root
from project_paths import safe_file_name
from route_translation_task import route_for, write_report as write_route_report
from project_paths import is_under, resolve_project_path, relative_posix_path as relative_path
from model_review_contract import read_jsonl_objects
from plugin_resource_evidence import (
    PluginReportTraits,
    capability_attempt_evidence,
    capability_evidence,
    create_evidence_directory_under,
    discover_regular_plugin_files,
    merge_plugin_report_traits,
    materialize_master_style_manifest,
    plugin_artifact_key,
    plugin_report_error_code,
    plugin_resource_descriptor,
    read_plugin_report_traits,
    read_plugin_report_value,
    unknown_write_plugin_trait_fields,
    validate_plugin_master_style_context,
    validate_plugin_post_verify_report,
    validate_plugin_report_identity,
    validate_plugin_report_output,
    validate_plugin_report_status,
    validate_regular_evidence_path_under,
)
from plugin_master_style_manifest import (
    create_cached_sha256_resolver,
    prepare_master_style_manifest,
)
from file_utils import sha256_file
from localized_delivery import validate_composite_receipt
from workflow_process import run_plugin_python as run_python_script
from report_utils import markdown_cell
from resource_model import ResourceDescriptor


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
PLUGIN_STAGE_SCHEMA = "skyrim-mod-chs.plugin-translation-stage"
PLUGIN_STAGE_SCHEMA_VERSION = 3
EXPERIMENTAL_WRITE_WARNING = (
    "Experimental plugin writeback produced a project-local copy; it is not a stable "
    "delivery and still requires independent in-game validation."
)


def validate_current_localized_receipt(
    root: Path,
    receipt: Path,
    *,
    plugin: Path,
    game_id: str,
    mod_name: str,
) -> dict[str, Any]:
    validate_regular_evidence_path_under(
        receipt,
        root / "qa" / "localized_delivery" / mod_name,
        kind="file",
        label="Localized delivery composite receipt",
    )
    payload = validate_composite_receipt(root, receipt)
    plugin_claim = payload.get("plugin")
    expected_path = relative_path(root, plugin).replace("\\", "/")
    if (
        payload.get("operation") != "apply"
        or payload.get("game_id") != game_id
        or payload.get("mod_name") != mod_name
        or not isinstance(plugin_claim, dict)
        or str(plugin_claim.get("path", "")).replace("\\", "/").casefold()
        != expected_path.casefold()
        or str(plugin_claim.get("sha256", "")).casefold() != sha256_file(plugin).casefold()
    ):
        raise ValueError("Localized delivery composite receipt does not bind the current plugin lane")
    return payload


@dataclass
class PluginRow:
    Plugin: str
    Status: str
    Candidates: int
    ReviewRows: int
    TranslationMap: str
    TranslationJsonl: str
    ToolOutput: str
    Evidence: str
    CapabilityEvidence: list[dict[str, Any]] = field(default_factory=list)
    PluginKey: str = ""
    RelativePath: str = ""
    InputSha256: str = ""
    TranslationJsonlSha256: str = ""
    ToolOutputSha256: str = ""
    EvidenceSha256: str = ""
    ApplyReceipt: str = ""
    ApplyReceiptSha256: str = ""
    OutputExportJsonl: str = ""
    OutputExportJsonlSha256: str = ""


@dataclass
class Issue:
    Severity: str
    Plugin: str
    Message: str
    Evidence: str = ""


def resolver_evidence(row: dict[str, Any], phase: str) -> dict[str, Any]:
    result = dict(row)
    result.update(
        {
            "evidence_kind": "resolver_decision",
            "phase": phase,
            "result": "allowed" if row.get("supported") is True else "blocked",
            "return_code": None,
            "report_path": str(row.get("evidence", "")),
        }
    )
    return result


def cleanup_generated_evidence(paths: tuple[Path, ...]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def migrate_file_without_overwrite(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
    destination_root: Path,
    label: str,
) -> None:
    source = validate_regular_evidence_path_under(
        source,
        source_root,
        kind="file",
        label=label,
    )
    create_evidence_directory_under(
        destination.parent,
        destination_root,
        label=f"{label} destination directory",
    )
    if os.path.lexists(destination):
        raise ValueError(f"Migration destination already exists: {destination}")
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise ValueError(f"Migration destination already exists: {destination}") from exc
    try:
        source.unlink()
    except OSError:
        destination.unlink(missing_ok=True)
        raise


def quarantine_legacy_receipt(root: Path, mod_name: str, receipt: Path) -> Path:
    receipt = validate_regular_evidence_path_under(
        receipt,
        root / "qa",
        kind="file",
        label="Legacy AdapterResult receipt",
    )
    digest = sha256_file(receipt)[:16]
    stem = receipt.name.removesuffix(".adapter_result.json")
    destination = (
        root
        / "work"
        / "plugin_receipt_quarantine"
        / mod_name
        / f"{safe_file_name(stem)}.{digest}.unbound.json"
    )
    if os.path.lexists(destination):
        validate_regular_evidence_path_under(
            destination,
            root / "work",
            kind="file",
            label="Quarantined legacy AdapterResult receipt",
        )
        if sha256_file(destination) != sha256_file(receipt):
            raise ValueError(f"Receipt quarantine destination conflicts: {destination}")
        receipt.unlink()
        return destination
    migrate_file_without_overwrite(
        receipt,
        destination,
        source_root=root / "qa",
        destination_root=root / "work",
        label="Legacy AdapterResult receipt",
    )
    return destination


def _canonical_receipt_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    parsed = PurePosixPath(normalized)
    if (
        not normalized
        or parsed.is_absolute()
        or bool(Path(normalized).drive)
        or ".." in parsed.parts
        or parsed.as_posix() != normalized
    ):
        raise ValueError(f"AdapterResult path is not canonical: {value!r}")
    return normalized


def _require_receipt_artifact(
    root: Path,
    artifacts: tuple[Any, ...],
    expected_path: Path,
    label: str,
) -> None:
    expected_relative = relative_path(root, expected_path)
    matches = [
        artifact
        for artifact in artifacts
        if _canonical_receipt_path(artifact.path).casefold()
        == expected_relative.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Legacy AdapterResult must claim exactly one current {label}: "
            f"{expected_relative}"
        )
    actual_sha256 = sha256_file(expected_path)
    if matches[0].sha256 != actual_sha256:
        raise ValueError(
            f"Legacy AdapterResult {label} SHA256 mismatch: {expected_relative}"
        )


def validate_legacy_plugin_receipt(
    receipt_path: Path,
    *,
    root: Path,
    plugin: Path,
    tool_output: Path,
    report_path: Path,
    mod_name: str,
    game_id: str,
    adapter_id: str,
) -> None:
    receipt_path = validate_regular_evidence_path_under(
        receipt_path,
        root / "qa",
        kind="file",
        label="Legacy AdapterResult receipt",
    )
    report_path = validate_regular_evidence_path_under(
        report_path,
        root / "qa",
        kind="file",
        label="Legacy plugin apply report",
    )
    plugin = validate_regular_evidence_path_under(
        plugin,
        root,
        kind="file",
        label="Legacy receipt plugin input",
    )
    tool_output = validate_regular_evidence_path_under(
        tool_output,
        root / "out",
        kind="file",
        label="Legacy receipt tool output",
    )
    result = read_adapter_result(receipt_path)
    if result.status != "success" or result.operation != "apply":
        raise ValueError("Legacy AdapterResult is not a successful apply receipt")
    if result.adapter_id != adapter_id:
        raise ValueError("Legacy AdapterResult adapter_id does not match the current adapter")
    if result.mod_name != mod_name:
        raise ValueError("Legacy AdapterResult mod_name does not match the current Mod lane")

    _require_receipt_artifact(root, result.inputs, plugin, "plugin input")
    _require_receipt_artifact(root, result.artifacts, tool_output, "tool output")
    _require_receipt_artifact(root, result.artifacts, report_path, "apply report")
    expected_report = relative_path(root, report_path)
    evidence = [_canonical_receipt_path(value) for value in result.evidence_files]
    if sum(value.casefold() == expected_report.casefold() for value in evidence) != 1:
        raise ValueError("Legacy AdapterResult does not uniquely claim its apply report")

    validate_plugin_report_identity(
        report_path,
        project_root=root,
        expected_input=plugin,
        expected_game=game_id,
        expected_operation="apply",
    )
    validate_plugin_report_output(
        report_path,
        project_root=root,
        expected_output=tool_output,
    )
    validate_plugin_report_status(report_path, return_code=0)


def resolve_plugin_text_access(
    context: GameContext,
) -> tuple[CapabilityDecision, CapabilityDecision]:
    read = resolve_capability(context, "plugin_text", "read")
    write = resolve_capability(context, "plugin_text", "write")
    if not read.supported:
        raise ValueError(read.reason)
    if not read.adapter_id:
        raise ValueError("plugin_text read capability does not declare an adapter")
    require_adapter(read.adapter_id, "extract")
    require_adapter(read.adapter_id, "verify")
    if not write.supported:
        raise ValueError(write.reason)
    if write.adapter_id != read.adapter_id:
        raise ValueError("plugin_text read/write must use the same adapter")
    require_adapter(write.adapter_id, "apply")
    return read, write


def read_export_report_evidence(
    context: GameContext,
    resource: ResourceDescriptor,
    report_path: Path,
    *,
    root: Path,
    expected_input: Path,
    return_code: int,
    sha256_resolver: Callable[[Path], str] = sha256_file,
) -> tuple[str, PluginReportTraits]:
    if not report_path.is_file():
        raise ValueError(f"Plugin export report is missing: {report_path}")
    trait_caps = context.resource_model.trait_level_caps.get(resource.capability, {})
    if not trait_caps:
        return ("ready" if return_code == 0 else "blocked"), PluginReportTraits()
    validate_plugin_report_identity(
        report_path,
        project_root=root,
        expected_input=expected_input,
        expected_game=context.game_id,
        expected_operation="export",
    )
    status = validate_plugin_report_status(report_path, return_code=return_code)
    traits = read_plugin_report_traits(report_path)
    context_evidence = validate_plugin_master_style_context(
        report_path,
        project_root=root,
        expected_input=expected_input,
        expected_game=context.game_id,
        sha256_resolver=sha256_resolver,
    )
    if traits.light_context is not context_evidence.light_context:
        raise ValueError(
            "Plugin report light trait does not match master-style context evidence"
        )
    return status, traits


def resolve_plugin_text_entrypoints(
    context: GameContext,
) -> tuple[CapabilityDecision, CapabilityDecision, str, str, str]:
    read, write = resolve_plugin_text_access(context)
    return (
        read,
        write,
        require_script_entrypoint(read.adapter_id or "", "extract"),
        require_script_entrypoint(write.adapter_id or "", "apply"),
        require_script_entrypoint(read.adapter_id or "", "verify"),
    )


def build_export_command_args(
    *,
    plugin: Path,
    mod_name: str,
    output_path: Path,
    report_path: Path,
    game_id: str,
    master_style_manifest: Path | None = None,
) -> list[str]:
    args = [
        "--plugin-path",
        str(plugin),
        "--mod-name",
        mod_name,
        "--output-path",
        str(output_path),
        "--report-path",
        str(report_path),
        "--game",
        game_id,
    ]
    if master_style_manifest is not None:
        args.extend(["--master-style-manifest", str(master_style_manifest)])
    return args


def build_write_command_args(
    *,
    input_plugin: Path,
    translation_jsonl: Path,
    output_plugin: Path,
    report_path: Path,
    adapter_result_path: Path,
    game_id: str,
    master_style_manifest: Path | None = None,
) -> list[str]:
    args = [
        "--input-plugin-path",
        str(input_plugin),
        "--translation-jsonl-path",
        str(translation_jsonl),
        "--output-plugin-path",
        str(output_plugin),
        "--report-path",
        str(report_path),
        "--adapter-result-path",
        str(adapter_result_path),
        "--game",
        game_id,
    ]
    if master_style_manifest is not None:
        args.extend(["--master-style-manifest", str(master_style_manifest)])
    return args


def write_master_style_preflight_report(
    path: Path,
    *,
    plugin: Path,
    status: str,
    manifest: Path | None,
    reason: str,
    root: Path,
) -> None:
    create_evidence_directory_under(
        path.parent,
        root / "qa",
        label="Master-style preflight report directory",
    )
    manifest_value = relative_path(root, manifest) if manifest is not None else "<none>"
    path.write_text(
        "\n".join(
            (
                "# Plugin Master-style Preflight",
                "",
                f"- Plugin: {relative_path(root, plugin)}",
                f"- Status: {status}",
                f"- Manifest: {manifest_value}",
                f"- Reason: {reason}",
                "",
            )
        ),
        encoding="utf-8",
    )


def process_output(result: subprocess.CompletedProcess[str]) -> str:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return " ".join(lines[-8:])


def write_map_template(path: Path, rows: list[dict[str, Any]], context: GameContext) -> None:
    translations: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("risk", "")) != "candidate":
            continue
        translations.append(
            {
                key: row.get(key, "")
                for key in (
                    "schema_version",
                    "game_id",
                    "plugin",
                    "record_type",
                    "form_id",
                    "editor_id",
                    "field_path",
                    "subrecord_type",
                    "subrecord_index",
                    "occurrence_index",
                    "source",
                    "risk",
                    "writeback",
                )
            }
            | {"target": ""}
        )
    template = {"schema_version": 2, "game_id": context.game_id, "translations": translations}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_reports(
    root: Path,
    mod_name: str,
    workspace: Path,
    report_path: Path,
    json_path: Path,
    plugin_rows: list[PluginRow],
    issues: list[Issue],
    context: GameContext,
) -> None:
    plugin_capability = context.capabilities.get("plugin_text")
    if plugin_capability is None:
        raise ValueError("Game profile does not declare plugin_text capability metadata.")
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    lines: list[str] = [
        "# Plugin Translation Stage Report",
        "",
        f"- game_id: {context.game_id}",
        f"- game_profile_version: {context.schema_version}",
        f"- plugin_adapter: {plugin_capability.adapter_id}",
        f"- plugin_text_capability_level: {plugin_capability.level}",
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- Plugins checked: {len(plugin_rows)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Plugins",
        "",
        "| Plugin | Status | Candidates | Review rows | Translation map | Translation JSONL | Tool output | Evidence |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for row in plugin_rows:
        lines.append(
            f"| {markdown_cell(row.Plugin)} | {row.Status} | {row.Candidates} | {row.ReviewRows} | "
            f"{markdown_cell(row.TranslationMap)} | {markdown_cell(row.TranslationJsonl)} | "
            f"{markdown_cell(row.ToolOutput)} | {markdown_cell(row.Evidence)} |"
        )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No plugin translation stage issues.")
    else:
        lines.extend(["| Severity | Plugin | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(
                f"| {issue.Severity} | {markdown_cell(issue.Plugin)} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |"
            )

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This stage never writes to mod/.",
            "- Plugin binaries are written only by the controlled Mutagen adapter into out/<ModName>/tool_outputs/.",
            "- Missing translation maps generate templates and block instead of silently copying English plugins.",
            "- Real game installations, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "schema": PLUGIN_STAGE_SCHEMA,
                "schema_version": PLUGIN_STAGE_SCHEMA_VERSION,
                "ModName": mod_name,
                "game_id": context.game_id,
                "game_profile_version": context.schema_version,
                "plugin_adapter": plugin_capability.adapter_id,
                "plugin_text_capability_level": plugin_capability.level,
                "Workspace": relative_path(root, workspace),
                "ProjectRoot": str(root.resolve(strict=True)),
                "BlockingIssues": blocking,
                "Warnings": warnings,
                "Plugins": [asdict(row) for row in plugin_rows],
                "Issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export, translate, write back, and verify project-local ESP/ESM/ESL plugin text.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", required=True)
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--json-output-path", default="")
    parser.add_argument("--game", choices=supported_game_ids(), default="")
    args = parser.parse_args()

    root = project_root()
    context = resolve_workspace_game_context(root, args.game)
    (
        read_capability,
        write_capability,
        export_entrypoint,
        write_entrypoint,
        verify_entrypoint,
    ) = resolve_plugin_text_entrypoints(context)
    mod_name = safe_file_name(args.mod_name)
    if not mod_name:
        raise ValueError("ModName cannot be empty.")
    workspace = resolve_project_path(root, args.workspace_path, must_exist=True)
    work_root = resolve_project_path(root, "work/extracted_mods", must_exist=False)
    if not is_under(workspace, work_root):
        raise ValueError(
            "WorkspacePath must be a prepared workspace under work/extracted_mods/. "
            "Run prepare_mod_workspace.py before the plugin translation stage."
        )
    if not workspace.is_dir():
        raise ValueError(f"WorkspacePath must be a directory: {workspace}")
    detected_workspace = find_data_root(workspace, context=context).resolve(strict=True)
    if detected_workspace != workspace:
        workspace = detected_workspace

    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.plugin_translation_stage.md", must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path or f"qa/{mod_name}.plugin_translation_stage.json", must_exist=False)
    if not is_under(report_path, root / "qa") or not is_under(json_path, root / "qa"):
        raise ValueError("Report paths must be under qa/.")

    digest = sha256_file
    master_style_digest = create_cached_sha256_resolver()
    plugin_rows: list[PluginRow] = []
    plugin_identities: list[tuple[str, str, str]] = []
    issues: list[Issue] = []
    plugins = discover_regular_plugin_files(
        workspace,
        PLUGIN_EXTENSIONS,
        label="Plugin translation stage input",
    )
    if not plugins:
        write_reports(root, mod_name, workspace, report_path, json_path, plugin_rows, issues, context)
        print(f"Plugin translation stage report written to: {report_path}")
        print("No plugins found.")
        return 0

    basename_counts: dict[str, int] = {}
    for plugin in plugins:
        basename = plugin.name.casefold()
        basename_counts[basename] = basename_counts.get(basename, 0) + 1

    for plugin in plugins:
        try:
            relative_plugin = plugin.resolve(strict=True).relative_to(workspace.resolve(strict=True))
        except ValueError:
            relative_plugin = Path(plugin.name)
        artifact_key = plugin_artifact_key(mod_name, relative_plugin)
        input_master_style_manifest: Path | None = None
        output_master_style_manifest_path = (
            root
            / "work"
            / "plugin_context"
            / mod_name
            / f"{artifact_key}.output-master-styles.json"
        )
        plugin_identities.append(
            (artifact_key, relative_plugin.as_posix(), digest(plugin))
        )
        export_path = root / "source" / "plugin_exports" / mod_name / f"{artifact_key}.strings.jsonl"
        export_report = root / "qa" / f"{artifact_key}.export.md"
        glossary_match_report = root / "qa" / f"{artifact_key}.external_glossary_matches.md"
        glossary_match_dir = root / "work" / "glossary_matches" / mod_name / artifact_key
        map_path = root / "work" / "plugin_translation_maps" / mod_name / f"{artifact_key}.translation_map.json"
        template_path = root / "work" / "plugin_translation_maps" / mod_name / f"{artifact_key}.translation_map.template.json"
        translation_jsonl = root / "translated" / "plugin_exports" / mod_name / f"{artifact_key}.strings.zh.jsonl"
        tool_output = root / "out" / mod_name / "tool_outputs" / relative_plugin
        tool_output_export = root / "source" / "plugin_exports" / mod_name / f"{artifact_key}.tool-output.strings.jsonl"
        tool_output_export_report = root / "qa" / f"{artifact_key}.tool-output-export.md"
        adapter_verify_report = root / "qa" / f"{artifact_key}.adapter-verify.md"
        verify_report = root / "qa" / f"{artifact_key}.output-verification.md"
        write_report = root / "qa" / f"{artifact_key}.apply.md"
        write_receipt = root / "qa" / f"{artifact_key}.apply.adapter_result.json"
        master_style_preflight_report = (
            root / "qa" / f"{artifact_key}.master-style-preflight.md"
        )
        map_report = root / "qa" / f"{artifact_key}.translation-map.md"
        legacy_write_report = root / "qa" / f"{plugin.name}.plugin_stage_mutagen_write.md"
        legacy_write_receipt = (
            root
            / "qa"
            / f"{plugin.name}.plugin_stage_mutagen_write.adapter_result.json"
        )
        legacy_receipt_safe = False
        if os.path.lexists(legacy_write_receipt):
            try:
                validate_regular_evidence_path_under(
                    legacy_write_receipt,
                    root / "qa",
                    kind="file",
                    label="Legacy AdapterResult receipt",
                )
            except (OSError, ValueError) as exc:
                issues.append(
                    Issue(
                        "error",
                        plugin.name,
                        f"Legacy Adapter receipt path is unsafe and was not read or migrated: {exc}",
                        relative_path(root, legacy_write_receipt),
                    )
                )
            else:
                legacy_receipt_safe = True
        if legacy_receipt_safe:
            if basename_counts[plugin.name.casefold()] != 1:
                quarantined = quarantine_legacy_receipt(
                    root,
                    mod_name,
                    legacy_write_receipt,
                )
                issues.append(
                    Issue(
                        "warning",
                        plugin.name,
                        "Legacy Adapter receipt ownership is ambiguous for plugins "
                        "with the same basename; the receipt was quarantined and not bound.",
                        relative_path(root, quarantined),
                    )
                )
            else:
                try:
                    validate_legacy_plugin_receipt(
                        legacy_write_receipt,
                        root=root,
                        plugin=plugin,
                        tool_output=tool_output,
                        report_path=legacy_write_report,
                        mod_name=mod_name,
                        game_id=context.game_id,
                        adapter_id=read_capability.adapter_id or "",
                    )
                except (OSError, ValueError) as exc:
                    quarantined = quarantine_legacy_receipt(
                        root,
                        mod_name,
                        legacy_write_receipt,
                    )
                    issues.append(
                        Issue(
                            "warning",
                            plugin.name,
                            "Legacy Adapter receipt identity could not be proven; "
                            f"the receipt was quarantined and not bound: {exc}",
                            relative_path(root, quarantined),
                        )
                    )
                else:
                    legacy_write_receipt.unlink()
        cleanup_generated_evidence(
            (
                export_report,
                glossary_match_report,
                map_report,
                tool_output_export_report,
                adapter_verify_report,
                verify_report,
                write_report,
                write_receipt,
                master_style_preflight_report,
                output_master_style_manifest_path,
            )
        )
        report_traits = PluginReportTraits()
        resource = plugin_resource_descriptor(context, relative_plugin)
        capability_rows: list[dict[str, Any]] = []
        master_style_preflight_error = ""

        try:
            input_master_style_manifest = prepare_master_style_manifest(
                root=root,
                game_id=context.game_id,
                mod_name=mod_name,
                plugin=plugin,
                relative_plugin=relative_plugin,
                sha256_resolver=master_style_digest,
            )
            write_master_style_preflight_report(
                master_style_preflight_report,
                plugin=plugin,
                status=(
                    "ready"
                    if input_master_style_manifest is not None
                    else "not_required"
                ),
                manifest=input_master_style_manifest,
                reason=(
                    "Complete workspace master-style evidence is ready."
                    if input_master_style_manifest is not None
                    else "This plugin does not require workspace master files."
                ),
                root=root,
            )
        except (OSError, ValueError) as exc:
            master_style_preflight_error = str(exc)
            write_master_style_preflight_report(
                master_style_preflight_report,
                plugin=plugin,
                status="blocked",
                manifest=None,
                reason=master_style_preflight_error,
                root=root,
            )

        try:
            route = route_for(root, plugin, context)
            write_route_report(root / "qa" / "routing_report.md", route)
        except (OSError, ValueError):
            issues.append(Issue("warning", plugin.name, "Route report could not be refreshed.", "qa/routing_report.md"))

        export = run_python_script(
            root,
            export_entrypoint,
            build_export_command_args(
                plugin=plugin,
                mod_name=mod_name,
                output_path=export_path,
                report_path=export_report,
                game_id=context.game_id,
                master_style_manifest=input_master_style_manifest,
            ),
        )
        initial_read_decision = resolve_resource_capability(context, resource, "read")
        report_status = ""
        try:
            report_status, report_traits = read_export_report_evidence(
                context,
                resource,
                export_report,
                root=root,
                expected_input=plugin,
                return_code=export.returncode,
                sha256_resolver=digest,
            )
            resource = plugin_resource_descriptor(context, relative_plugin, report_traits)
            route = route_for(
                root,
                plugin,
                context,
                traits=report_traits.resource_traits(),
            )
            write_route_report(root / "qa" / "routing_report.md", route)
        except (OSError, ValueError) as exc:
            report_error_code = (
                "invalid_report_status"
                if "Status" in str(exc)
                else "invalid_report_identity"
            )
            capability_rows.append(
                capability_attempt_evidence(
                    resource,
                    initial_read_decision,
                    phase="export",
                    result="failed",
                    return_code=export.returncode,
                    error_code=report_error_code,
                    reason=str(exc),
                    report_path=relative_path(root, export_report),
                    report_sha256=(
                        digest(export_report) if export_report.is_file() else ""
                    ),
                )
            )
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    f"Plugin export trait evidence is invalid: {exc}",
                    relative_path(root, export_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "invalid_trait_evidence",
                    0,
                    0,
                    "",
                    "",
                    "",
                    relative_path(root, export_report),
                    capability_rows,
                )
            )
            continue

        inventory_decision = resolve_resource_capability(context, resource, "inventory")
        read_decision = resolve_resource_capability(context, resource, "read")
        write_decision = resolve_resource_capability(context, resource, "write")
        unknown_write_traits = unknown_write_plugin_trait_fields(context, report_traits)
        unknown_write_reason = (
            "Plugin write is blocked because adapter header traits are unknown: "
            + ", ".join(unknown_write_traits)
            if unknown_write_traits
            else ""
        )
        report_reason = ""
        if export_report.is_file():
            report_reason = read_plugin_report_value(export_report, "Reason")
        export_blocked = report_status != "ready"
        capability_rows.extend(
            [
                resolver_evidence(
                    capability_evidence(
                        resource,
                        inventory_decision,
                        report_traits=report_traits,
                        evidence=relative_path(root, export_report),
                    ),
                    "resolve_inventory",
                ),
                resolver_evidence(
                    capability_evidence(
                        resource,
                        read_decision,
                        report_traits=report_traits,
                        evidence=relative_path(root, export_report),
                    ),
                    "resolve_read",
                ),
                resolver_evidence(
                    capability_evidence(
                        resource,
                        write_decision,
                        report_traits=report_traits,
                        supported=(
                            False
                            if unknown_write_traits
                            or report_traits.contains_unsupported_light_formids is True
                            else None
                        ),
                        error_code=(
                            "plugin_trait_unknown"
                            if unknown_write_traits
                            else "experimental_limit"
                            if report_traits.contains_unsupported_light_formids is True
                            else None
                        ),
                        reason=(
                            unknown_write_reason
                            if unknown_write_traits
                            else report_reason
                            if report_traits.contains_unsupported_light_formids is True
                            else ""
                        ),
                        evidence=relative_path(root, export_report),
                    ),
                    "resolve_write",
                ),
                capability_attempt_evidence(
                    resource,
                    read_decision,
                    phase="export",
                    result="blocked" if export_blocked else "success",
                    return_code=export.returncode,
                    error_code="adapter_blocked" if export_blocked else None,
                    reason=report_reason if export_blocked else "",
                    report_path=relative_path(root, export_report),
                    report_sha256=digest(export_report),
                    report_traits=report_traits,
                ),
            ]
        )

        if report_traits.localized is True:
            export_path.unlink(missing_ok=True)
            localized_receipt = (
                root
                / "qa"
                / "localized_delivery"
                / mod_name
                / f"{safe_file_name(plugin.name)}.apply.composite.json"
            )
            try:
                if master_style_preflight_error:
                    raise ValueError(
                        "Current master-style preflight is blocked: "
                        + master_style_preflight_error
                    )
                validate_current_localized_receipt(
                    root,
                    localized_receipt,
                    plugin=plugin,
                    game_id=context.game_id,
                    mod_name=mod_name,
                )
            except (OSError, ValueError) as receipt_error:
                localized_export_entrypoint = require_script_entrypoint(
                    "bethesda-localized-delivery",
                    "extract",
                )
                localized_args = [
                    "--mode",
                    "Export",
                    "--plugin-path",
                    str(plugin),
                    "--mod-name",
                    mod_name,
                    "--game",
                    context.game_id,
                ]
                if input_master_style_manifest is not None:
                    localized_args.extend(
                        ["--master-style-manifest", str(input_master_style_manifest)]
                    )
                localized_export = run_python_script(
                    root,
                    localized_export_entrypoint,
                    localized_args,
                )
                message = (
                    "Localized plugin must use the localized_delivery composite adapter; "
                    "generic plugin writeback remains blocked. "
                    f"Current composite receipt is unavailable or invalid: {receipt_error}."
                )
                if localized_export.returncode != 0:
                    message += f" Localized candidate export also failed: {process_output(localized_export)}"
                else:
                    message += (
                        " Candidate exports are ready under source/localized_delivery; "
                        "translate them, then run invoke_bethesda_localized_delivery.py "
                        "--mode Apply with explicit experimental opt-in."
                    )
                issues.append(
                    Issue("error", plugin.name, message, relative_path(root, export_report))
                )
                plugin_rows.append(
                    PluginRow(
                        plugin.name,
                        "localized_delivery_required",
                        0,
                        0,
                        "",
                        "",
                        "",
                        relative_path(root, export_report),
                        capability_rows,
                    )
                )
            else:
                localized_row = PluginRow(
                    plugin.name,
                    "localized_delivery_ready",
                    0,
                    0,
                    "",
                    "",
                    "",
                    relative_path(root, localized_receipt),
                    capability_rows,
                )
                localized_row.EvidenceSha256 = digest(localized_receipt)
                plugin_rows.append(localized_row)
            continue

        if report_traits.contains_unsupported_light_formids is True and export_blocked:
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    report_reason or "Plugin export is blocked by unsupported light FormIDs.",
                    relative_path(root, export_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "read_only_export_blocked",
                    0,
                    0,
                    "",
                    "",
                    "",
                    relative_path(root, export_report),
                    capability_rows,
                )
            )
            continue

        if export.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Plugin export failed: {process_output(export)}", relative_path(root, export_report)))
            plugin_rows.append(PluginRow(plugin.name, "export_failed", 0, 0, "", "", "", relative_path(root, export_report), capability_rows))
            continue

        rows = read_jsonl_objects(export_path, strict=True)
        candidates = [row for row in rows if str(row.get("risk", "")) == "candidate"]
        review_rows = [row for row in rows if str(row.get("risk", "")) == "review"]
        if not write_decision.supported or unknown_write_traits:
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    (
                        unknown_write_reason
                        if unknown_write_traits
                        else "Plugin export completed read-only, but resource traits block write; "
                        "no translated Apply output was created."
                    ),
                    relative_path(root, export_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "read_only_blocked_for_write",
                    len(candidates),
                    len(review_rows),
                    "",
                    "",
                    "",
                    relative_path(root, export_report),
                    capability_rows,
                )
            )
            continue

        if not candidates:
            if master_style_preflight_error:
                write_master_style_preflight_report(
                    master_style_preflight_report,
                    plugin=plugin,
                    status="not_required",
                    manifest=None,
                    reason=(
                        "Read-only export completed with no translation candidates; "
                        "writeback master-style evidence was not required. Initial preflight: "
                        f"{master_style_preflight_error}"
                    ),
                    root=root,
                )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "no_candidates",
                    0,
                    len(review_rows),
                    "",
                    "",
                    "",
                    relative_path(root, export_report),
                    capability_rows,
                )
            )
            continue

        if master_style_preflight_error:
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    "Plugin master-style preflight blocked before translation: "
                    f"{master_style_preflight_error}",
                    relative_path(root, master_style_preflight_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "master_style_preflight_blocked",
                    len(candidates),
                    len(review_rows),
                    "",
                    "",
                    "",
                    relative_path(root, master_style_preflight_report),
                    capability_rows,
                )
            )
            continue

        glossary_matches = run_python_script(
            root,
            "build_external_glossary_matches.py",
            [
                "--mod-name",
                mod_name,
                "--input-path",
                str(export_path),
                "--output-dir",
                str(glossary_match_dir),
                "--report-output-path",
                str(glossary_match_report),
            ],
        )
        if glossary_matches.returncode != 0:
            issues.append(Issue("warning", plugin.name, f"External glossary match packet could not be generated: {process_output(glossary_matches)}", relative_path(root, glossary_match_report)))
        legacy_map_path = map_path.with_name(
            f"{plugin.name}.translation_map.json"
        )
        if not map_path.is_file() and os.path.lexists(legacy_map_path):
            if basename_counts[plugin.name.casefold()] != 1:
                message = (
                    "Legacy translation map ownership is ambiguous because multiple "
                    "plugins in this workspace share the same basename; the map was "
                    "not reused or modified."
                )
                issues.append(
                    Issue(
                        "error",
                        plugin.name,
                        message,
                        relative_path(root, legacy_map_path),
                    )
                )
                plugin_rows.append(
                    PluginRow(
                        plugin.name,
                        "blocked_ambiguous_legacy_translation_map",
                        len(candidates),
                        len(review_rows),
                        relative_path(root, map_path),
                        "",
                        "",
                        relative_path(root, legacy_map_path),
                        capability_rows,
                    )
                )
                continue
            try:
                migrate_file_without_overwrite(
                    legacy_map_path,
                    map_path,
                    source_root=map_path.parent,
                    destination_root=map_path.parent,
                    label="Legacy plugin translation map",
                )
            except (OSError, ValueError) as exc:
                issues.append(
                    Issue(
                        "error",
                        plugin.name,
                        "Legacy translation map could not be migrated without "
                        f"overwriting an existing map: {exc}",
                        relative_path(root, legacy_map_path),
                    )
                )
                plugin_rows.append(
                    PluginRow(
                        plugin.name,
                        "blocked_legacy_translation_map_migration",
                        len(candidates),
                        len(review_rows),
                        relative_path(root, map_path),
                        "",
                        "",
                        relative_path(root, legacy_map_path),
                        capability_rows,
                    )
                )
                continue

        if not map_path.is_file():
            write_map_template(template_path, rows, context)
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    "Translation map is missing; a template and external glossary match packet were generated for Codex/model translation.",
                    f"{relative_path(root, template_path)}; {relative_path(root, glossary_match_report)}",
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "blocked_missing_translation_map",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    "",
                    "",
                    relative_path(root, template_path),
                    capability_rows,
                )
            )
            continue

        apply_result = run_python_script(
            root,
            "apply_plugin_translation_map.py",
            [
                "--export-path",
                str(export_path),
                "--translation-map-path",
                str(map_path),
                "--mod-name",
                mod_name,
                "--output-path",
                str(translation_jsonl),
                "--report-path",
                str(map_report),
                "--game",
                context.game_id,
            ],
        )
        if apply_result.returncode != 0:
            issues.append(Issue("error", plugin.name, f"Applying translation map failed: {process_output(apply_result)}", relative_path(root, map_report)))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "translation_map_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    relative_path(root, map_report),
                    capability_rows,
                )
            )
            continue

        if tool_output.exists():
            tool_output.unlink()
        write_result = run_python_script(
            root,
            write_entrypoint,
            build_write_command_args(
                input_plugin=plugin,
                translation_jsonl=translation_jsonl,
                output_plugin=tool_output,
                report_path=write_report,
                adapter_result_path=write_receipt,
                game_id=context.game_id,
                master_style_manifest=input_master_style_manifest,
            ),
        )
        apply_identity_error = ""
        try:
            validate_plugin_report_identity(
                write_report,
                project_root=root,
                expected_input=plugin,
                expected_game=context.game_id,
                expected_operation="apply",
            )
            validate_plugin_report_status(
                write_report,
                return_code=write_result.returncode,
            )
        except (OSError, ValueError) as exc:
            apply_identity_error = str(exc)
        apply_adapter_error_code = (
            plugin_report_error_code(write_report) if write_report.is_file() else ""
        )
        apply_attempt = capability_attempt_evidence(
            resource,
            write_decision,
            phase="apply",
            result=(
                "success"
                if write_result.returncode == 0 and not apply_identity_error
                else "failed"
            ),
            return_code=write_result.returncode,
            error_code=(
                "invalid_report_identity"
                if apply_identity_error
                else apply_adapter_error_code or "adapter_failed"
                if write_result.returncode != 0
                else None
            ),
            reason=apply_identity_error or process_output(write_result),
            report_path=relative_path(root, write_report),
            report_sha256=(
                digest(write_report) if write_report.is_file() else ""
            ),
            report_traits=report_traits,
        )
        capability_rows.append(apply_attempt)
        if write_result.returncode != 0:
            if tool_output.exists():
                tool_output.unlink()
            issues.append(Issue("error", plugin.name, f"Mutagen plugin writeback failed: {process_output(write_result)}", relative_path(root, write_report)))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "writeback_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    relative_path(root, write_report),
                    capability_rows,
                )
            )
            continue

        if apply_identity_error:
            tool_output.unlink(missing_ok=True)
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    f"Plugin apply report identity is invalid: {apply_identity_error}",
                    relative_path(root, write_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "invalid_apply_trait_evidence",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    relative_path(root, write_report),
                    capability_rows,
                )
            )
            continue

        try:
            apply_traits = read_plugin_report_traits(write_report)
            apply_context = validate_plugin_master_style_context(
                write_report,
                project_root=root,
                expected_input=plugin,
                expected_game=context.game_id,
                sha256_resolver=digest,
            )
            output_master_style_manifest = materialize_master_style_manifest(
                apply_context,
                project_root=root,
                destination=output_master_style_manifest_path,
                expected_game=context.game_id,
                expected_plugin=tool_output.name,
            )
            if apply_traits.light_context is not apply_context.light_context:
                raise ValueError(
                    "Plugin apply light trait does not match master-style context evidence"
                )
            merged_traits = merge_plugin_report_traits(report_traits, apply_traits)
            apply_resource = plugin_resource_descriptor(context, relative_plugin, merged_traits)
            apply_decision = resolve_resource_capability(context, apply_resource, "write")
        except (OSError, ValueError) as exc:
            apply_attempt["result"] = "failed"
            apply_attempt["error_code"] = "invalid_trait_evidence"
            apply_attempt["reason"] = str(exc)
            tool_output.unlink(missing_ok=True)
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    f"Plugin apply trait evidence is invalid: {exc}",
                    relative_path(root, write_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "invalid_apply_trait_evidence",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    relative_path(root, write_report),
                    capability_rows,
                )
            )
            continue
        capability_rows.append(
            resolver_evidence(
                capability_evidence(
                    apply_resource,
                    apply_decision,
                    report_traits=apply_traits,
                    evidence=relative_path(root, write_report),
                ),
                "resolve_apply_write",
            )
        )
        if not apply_decision.supported:
            tool_output.unlink(missing_ok=True)
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    "Plugin apply report reveals resource traits that block write.",
                    relative_path(root, write_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "blocked_for_write",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    "",
                    relative_path(root, write_report),
                    capability_rows,
                )
            )
            continue

        output_export_args = [
                "--plugin-path",
                str(tool_output),
                "--mod-name",
                mod_name,
                "--output-path",
                str(tool_output_export),
                "--report-path",
                str(tool_output_export_report),
                "--allow-generated-plugin",
                "--game",
                context.game_id,
            ]
        if output_master_style_manifest is not None:
            output_export_args.extend(
                ["--master-style-manifest", str(output_master_style_manifest)]
            )
        output_export = run_python_script(
            root,
            export_entrypoint,
            output_export_args,
        )
        output_export_identity_error = ""
        output_export_status = ""
        try:
            output_export_status, _output_export_traits = read_export_report_evidence(
                context,
                apply_resource,
                tool_output_export_report,
                root=root,
                expected_input=tool_output,
                return_code=output_export.returncode,
                sha256_resolver=digest,
            )
        except (OSError, ValueError) as exc:
            output_export_identity_error = str(exc)
        output_export_blocked = (
            not output_export_identity_error and output_export_status != "ready"
        )
        output_export_reason = (
            output_export_identity_error
            or read_plugin_report_value(tool_output_export_report, "Reason")
            or process_output(output_export)
        )
        capability_rows.append(
            capability_attempt_evidence(
                apply_resource,
                resolve_resource_capability(context, apply_resource, "read"),
                phase="output_export",
                result=(
                    "failed"
                    if output_export_identity_error
                    else "blocked"
                    if output_export_blocked
                    else "success"
                ),
                return_code=output_export.returncode,
                error_code=(
                    "invalid_report_status"
                    if output_export_identity_error
                    and "Status" in output_export_identity_error
                    else "invalid_report_identity"
                    if output_export_identity_error
                    else "adapter_blocked"
                    if output_export_blocked
                    else None
                ),
                reason=output_export_reason,
                report_path=relative_path(root, tool_output_export_report),
                report_sha256=(
                    digest(tool_output_export_report)
                    if tool_output_export_report.is_file()
                    else ""
                ),
            )
        )
        if output_export_identity_error or output_export_blocked:
            issues.append(Issue("error", plugin.name, f"Tool output re-export failed: {output_export_reason}", relative_path(root, tool_output_export_report)))
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "tool_output_export_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    relative_path(root, tool_output),
                    relative_path(root, tool_output_export_report),
                    capability_rows,
                )
            )
            continue

        adapter_verify_args = [
            "--mode",
            "Verify",
            "--input-plugin-path",
            str(plugin),
            "--translation-jsonl-path",
            str(translation_jsonl),
            "--output-plugin-path",
            str(tool_output),
            "--report-path",
            str(adapter_verify_report),
            "--game",
            context.game_id,
        ]
        if input_master_style_manifest is not None:
            adapter_verify_args.extend(
                ["--master-style-manifest", str(input_master_style_manifest)]
            )
        adapter_verify = run_python_script(
            root,
            verify_entrypoint,
            adapter_verify_args,
        )
        verify_identity_error = ""
        try:
            validate_plugin_report_identity(
                adapter_verify_report,
                project_root=root,
                expected_input=plugin,
                expected_game=context.game_id,
                expected_operation="verify",
            )
            validate_plugin_report_status(
                adapter_verify_report,
                return_code=adapter_verify.returncode,
            )
        except (OSError, ValueError) as exc:
            verify_identity_error = str(exc)
        verify_adapter_error_code = (
            plugin_report_error_code(adapter_verify_report)
            if adapter_verify_report.is_file()
            else ""
        )
        verify_attempt = capability_attempt_evidence(
            apply_resource,
            resolve_resource_capability(context, apply_resource, "read"),
            phase="adapter_verify",
            result=(
                "success"
                if adapter_verify.returncode == 0 and not verify_identity_error
                else "failed"
            ),
            return_code=adapter_verify.returncode,
            error_code=(
                "invalid_report_identity"
                if verify_identity_error
                else verify_adapter_error_code or "adapter_failed"
                if adapter_verify.returncode != 0
                else None
            ),
            reason=verify_identity_error or process_output(adapter_verify),
            report_path=relative_path(root, adapter_verify_report),
            report_sha256=(
                digest(adapter_verify_report)
                if adapter_verify_report.is_file()
                else ""
            ),
        )
        capability_rows.append(verify_attempt)
        if adapter_verify.returncode != 0 or verify_identity_error:
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    f"Plugin adapter verification failed: {verify_identity_error or process_output(adapter_verify)}",
                    relative_path(root, adapter_verify_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "adapter_verification_failed",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    relative_path(root, tool_output),
                    relative_path(root, adapter_verify_report),
                    capability_rows,
                )
            )
            continue

        try:
            verify_traits = read_plugin_report_traits(adapter_verify_report)
            verify_context = validate_plugin_master_style_context(
                adapter_verify_report,
                project_root=root,
                expected_input=plugin,
                expected_game=context.game_id,
                sha256_resolver=digest,
            )
            if verify_traits.light_context is not verify_context.light_context:
                raise ValueError(
                    "Plugin verify light trait does not match master-style context evidence"
                )
            verified_traits = merge_plugin_report_traits(merged_traits, verify_traits)
            verify_resource = plugin_resource_descriptor(context, relative_plugin, verified_traits)
            verify_decision = resolve_resource_capability(context, verify_resource, "read")
        except (OSError, ValueError) as exc:
            verify_attempt["result"] = "failed"
            verify_attempt["error_code"] = "invalid_trait_evidence"
            verify_attempt["reason"] = str(exc)
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    f"Plugin verify trait evidence is invalid: {exc}",
                    relative_path(root, adapter_verify_report),
                )
            )
            plugin_rows.append(
                PluginRow(
                    plugin.name,
                    "invalid_verify_trait_evidence",
                    len(candidates),
                    len(review_rows),
                    relative_path(root, map_path),
                    relative_path(root, translation_jsonl),
                    relative_path(root, tool_output),
                    relative_path(root, adapter_verify_report),
                    capability_rows,
                )
            )
            continue
        capability_rows.append(
            resolver_evidence(
                capability_evidence(
                    verify_resource,
                    verify_decision,
                    report_traits=verify_traits,
                    evidence=relative_path(root, adapter_verify_report),
                ),
                "resolve_verify_read",
            )
        )

        verify = run_python_script(
            root,
            "verify_plugin_output.py",
            [
                "--original-plugin-path",
                str(plugin),
                "--output-plugin-path",
                str(tool_output),
                "--translation-jsonl-path",
                str(translation_jsonl),
                "--output-export-jsonl-path",
                str(tool_output_export),
                "--report-output-path",
                str(verify_report),
                "--writeback-report-path",
                relative_path(root, write_report),
                "--invariant-report-path",
                relative_path(root, adapter_verify_report),
                "--require-translation-evidence",
                "--game",
                context.game_id,
            ],
        )
        post_verify_error = ""
        if verify.returncode == 0:
            try:
                validate_plugin_post_verify_report(
                    verify_report,
                    project_root=root,
                    expected_game=context.game_id,
                    expected_adapter=verify_decision.adapter_id or "",
                    expected_original=plugin,
                    expected_output=tool_output,
                    expected_translation_jsonl=translation_jsonl,
                    expected_output_export_jsonl=tool_output_export,
                    expected_writeback_report=write_report,
                    expected_invariant_report=adapter_verify_report,
                )
            except (OSError, ValueError) as exc:
                post_verify_error = str(exc)
        post_verify_attempt = capability_attempt_evidence(
            verify_resource,
            verify_decision,
            phase="post_verify",
            evidence_kind="verification_attempt",
            result=(
                "success"
                if verify.returncode == 0 and not post_verify_error
                else "failed"
            ),
            return_code=verify.returncode,
            error_code=(
                "invalid_verification_evidence"
                if post_verify_error
                else "verification_failed"
                if verify.returncode != 0
                else None
            ),
            reason=post_verify_error or process_output(verify),
            report_path=relative_path(root, verify_report),
            report_sha256=(
                digest(verify_report) if verify_report.is_file() else ""
            ),
            report_traits=verify_traits,
        )
        capability_rows.append(post_verify_attempt)
        if verify.returncode != 0 or post_verify_error:
            issues.append(
                Issue(
                    "error",
                    plugin.name,
                    "Plugin verification failed: "
                    f"{post_verify_error or process_output(verify)}",
                    relative_path(root, verify_report),
                )
            )
            status = "verification_failed"
        else:
            if write_decision.level == "experimental_write":
                status = "experimental_tool_output_ready"
                issues.append(
                    Issue(
                        "warning",
                        plugin.name,
                        EXPERIMENTAL_WRITE_WARNING,
                        relative_path(root, write_receipt),
                    )
                )
            else:
                status = "translated_tool_output_ready"

        completed_row = PluginRow(
            plugin.name,
            status,
            len(candidates),
            len(review_rows),
            relative_path(root, map_path),
            relative_path(root, translation_jsonl),
            relative_path(root, tool_output),
            relative_path(root, verify_report),
            capability_rows,
        )
        if status in {
            "translated_tool_output_ready",
            "experimental_tool_output_ready",
        }:
            completed_row.TranslationJsonlSha256 = digest(translation_jsonl)
            completed_row.ToolOutputSha256 = digest(tool_output)
            completed_row.EvidenceSha256 = digest(verify_report)
            completed_row.ApplyReceipt = relative_path(root, write_receipt)
            completed_row.ApplyReceiptSha256 = (
                digest(write_receipt) if write_receipt.is_file() else ""
            )
            completed_row.OutputExportJsonl = relative_path(root, tool_output_export)
            completed_row.OutputExportJsonlSha256 = digest(tool_output_export)
        plugin_rows.append(completed_row)

    if len(plugin_rows) != len(plugin_identities):
        raise RuntimeError("Plugin stage did not produce exactly one status row per plugin input")
    for row, (plugin_key, relative_plugin_path, input_sha256) in zip(
        plugin_rows, plugin_identities, strict=True
    ):
        row.PluginKey = plugin_key
        row.RelativePath = relative_plugin_path
        row.InputSha256 = input_sha256

    write_reports(root, mod_name, workspace, report_path, json_path, plugin_rows, issues, context)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Plugin translation stage report written to: {report_path}")
    print(f"Plugin translation stage JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
