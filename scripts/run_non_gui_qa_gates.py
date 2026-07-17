"""Strict non-GUI gate for a single translated Mod output.

This script stitches together mechanical proofread, coverage, archive audit,
final_mod structure checks, binary/text review packet generation, and model
review contract checks. It does not translate text or write binaries.
"""

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from adapter_contract import AdapterResult
from adapter_result_io import read_adapter_result, require_translation_input_lane
from adapter_registry import (
    require_capability_script_entrypoint,
    require_script_entrypoint,
)
from capability_resolver import resolve_capability
from game_context import GameContext, game_context_metadata, game_display_label
from model_review_contract import (
    MODEL_REVIEWER_RE,
    jsonl_file_values,
    model_review_contract_issues as review_contract_issues,
    packet_content_reviewed,
    read_report_metric,
)
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root
from plugin_resource_evidence import (
    plugin_artifact_key,
    validate_plugin_post_verify_report,
    validate_plugin_report_identity,
    validate_plugin_report_output,
    validate_plugin_report_status,
)
from pex_translation_safety import SOURCE_FIELDS, normalized_pex_translation_line, pex_row_matches, pex_translation_row_protects_source, pex_translation_skip_reason, row_value
from translation_input_discovery import collect_translation_input_files, translation_input_evidence_roots
from translation_context import validated_translation_context
from workflow_lock import WorkflowLock
from project_paths import is_under, project_root, resolve_project_path
from route_translation_task import current_game_context
from project_paths import relative_path
from workflow_process import run_plugin_python as run_python_script
from used_capabilities import UsedCapabilityError, write_used_capabilities
from report_utils import markdown_cell_plain as markdown_cell, subprocess_output_lines as process_output
from file_utils import (
    read_text_utf8_sig_strict as read_text,
    sha256_file,
    sha256_file_upper as sha256,
)
from report_utils import to_int
from strict_qa_reuse import (
    load_reusable_mechanical_snapshot,
    write_reusable_mechanical_snapshot,
)


model_review_contract_issues = partial(
    review_contract_issues,
    display_labels=True,
    ignore_case=True,
    missing_claim_suffix=".",
)

@dataclass
class GateIssue:
    Severity: str
    Gate: str
    Message: str
    Evidence: str = ""


@dataclass(frozen=True)
class PluginWriteBinding:
    operation: dict[str, object]
    receipt: Path
    result: AdapterResult
    original: Path
    translation: Path
    tool_artifact: Path
    apply_report: Path


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


def _bound_project_path(root: Path, raw_path: str, label: str) -> Path:
    normalized = raw_path.replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        raise ValueError(f"{label} is not a canonical project-relative path: {raw_path!r}")
    path = resolve_project_path(root, normalized, must_exist=True)
    if not path.is_file() or not is_under(path, root):
        raise ValueError(f"{label} is not a regular file under the current workspace: {raw_path}")
    if relative_path(root, path).replace("\\", "/") != normalized:
        raise ValueError(f"{label} path is not canonical: {raw_path!r}")
    return path


def _validated_receipt_path(
    root: Path,
    raw_path: str,
    expected_sha256: str,
    label: str,
) -> Path:
    path = _bound_project_path(root, raw_path, label)
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"{label} SHA256 mismatch: {raw_path}")
    return path


def bind_plugin_write_artifacts(
    root: Path,
    workspace: Path,
    mod_name: str,
    resource_path: Path,
    final_plugin: Path,
    used_capability_payload: dict[str, object],
    game_id: str,
) -> PluginWriteBinding | None:
    resource_value = resource_path.as_posix()
    if resource_path.anchor or not resource_path.parts or ".." in resource_path.parts:
        raise ValueError(f"Final plugin resource path is not canonical: {resource_value!r}")
    operations = used_capability_payload.get("operations")
    if not isinstance(operations, list):
        raise ValueError("Used-capability payload does not contain a valid operations array")
    matches = [
        row
        for row in operations
        if isinstance(row, dict)
        and row.get("capability") == "plugin_text"
        and row.get("operation") == "write"
        and row.get("resource_path") == resource_value
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one plugin_text/write operation for final resource: "
            f"{resource_value}"
        )
    operation = matches[0]
    if operation.get("result") != "success" or operation.get("supported") is not True:
        raise ValueError(f"Bound plugin write operation is not successful: {resource_value}")
    evidence = operation.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(value, str) and value for value in evidence)
        or len({value.casefold() for value in evidence}) != len(evidence)
    ):
        raise ValueError(f"Plugin write operation has invalid evidence paths: {resource_value}")
    evidence_paths = {
        value: _bound_project_path(root, value, "Plugin write operation evidence")
        for value in evidence
    }
    receipt_rows = [
        (value, path)
        for value, path in evidence_paths.items()
        if path.name.casefold().endswith(".adapter_result.json")
    ]
    if len(receipt_rows) != 1:
        raise ValueError(
            "Plugin write operation must bind exactly one AdapterResult receipt: "
            f"{resource_value}"
        )
    receipt_value, receipt = receipt_rows[0]
    try:
        result = read_adapter_result(receipt)
    except ValueError as exc:
        raise ValueError(f"Invalid plugin AdapterResult receipt {receipt_value}: {exc}") from exc
    if (
        result.status != "success"
        or result.operation != "apply"
        or result.mod_name != mod_name
        or result.adapter_id != operation.get("adapter_id")
    ):
        raise ValueError(
            f"AdapterResult identity does not match plugin write operation: {resource_value}"
        )

    input_paths = [
        _validated_receipt_path(root, item.path, item.sha256, "AdapterResult input")
        for item in result.inputs
    ]
    original_rows = [path for path in input_paths if path.suffix.casefold() in PLUGIN_EXTENSIONS]
    translation_rows = [path for path in input_paths if path.suffix.casefold() == ".jsonl"]
    if len(input_paths) != 2 or len(original_rows) != 1 or len(translation_rows) != 1:
        raise ValueError(
            "Plugin AdapterResult must bind exactly one original plugin and one translation JSONL"
        )
    original = original_rows[0]
    translation = translation_rows[0]
    workspace_root = workspace.resolve(strict=True)
    if not is_under(original, workspace_root):
        raise ValueError(f"Original plugin is outside the current Mod workspace: {original}")
    if original.relative_to(workspace_root).as_posix() != resource_value:
        raise ValueError(
            "Original plugin path does not match final plugin resource path: "
            f"{resource_value}"
        )
    require_translation_input_lane(root, translation, mod_name)

    artifact_paths = [
        _validated_receipt_path(root, item.path, item.sha256, "AdapterResult artifact")
        for item in result.artifacts
    ]
    tool_rows = [path for path in artifact_paths if path.suffix.casefold() in PLUGIN_EXTENSIONS]
    if len(tool_rows) != 1:
        raise ValueError("Plugin AdapterResult must bind exactly one plugin tool artifact")
    tool_artifact = tool_rows[0]
    allowed_tool_roots = (
        root / "out" / mod_name / "tool_outputs",
        root / "translated" / "tool_outputs" / mod_name,
    )
    tool_relative = None
    for allowed_root in allowed_tool_roots:
        resolved_allowed = allowed_root.resolve(strict=False)
        if is_under(tool_artifact, resolved_allowed):
            tool_relative = tool_artifact.relative_to(resolved_allowed).as_posix()
            break
    if tool_relative != resource_value:
        raise ValueError(
            "Plugin tool artifact does not match the final plugin resource path: "
            f"{resource_value}"
        )
    final_resolved = final_plugin.resolve(strict=True)
    if sha256_file(final_resolved) != sha256_file(tool_artifact):
        raise ValueError(f"Final plugin does not match the bound tool artifact: {resource_value}")

    if len(result.evidence_files) != 1:
        raise ValueError("Plugin AdapterResult must bind exactly one apply report")
    apply_report_value = result.evidence_files[0]
    apply_report = _bound_project_path(root, apply_report_value, "AdapterResult apply report")
    if apply_report_value not in evidence_paths:
        raise ValueError("AdapterResult apply report is absent from operation evidence")
    report_artifacts = [
        item
        for item in result.artifacts
        if item.path == apply_report_value
    ]
    if len(report_artifacts) != 1 or report_artifacts[0].sha256 != sha256_file(apply_report):
        raise ValueError("AdapterResult apply report is not uniquely hash-bound as an artifact")
    validate_plugin_report_identity(
        apply_report,
        project_root=root,
        expected_input=original,
        expected_game=game_id,
        expected_operation="apply",
    )
    validate_plugin_report_output(
        apply_report,
        project_root=root,
        expected_output=tool_artifact,
    )
    validate_plugin_report_status(apply_report, return_code=0)
    return PluginWriteBinding(
        operation=operation,
        receipt=receipt,
        result=result,
        original=original,
        translation=translation,
        tool_artifact=tool_artifact,
        apply_report=apply_report,
    )


def collect_final_plugins(final_mod: Path) -> list[tuple[Path, Path]]:
    final_root = final_mod.resolve(strict=True)
    plugins: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for candidate in final_mod.rglob("*"):
        if not candidate.is_file() or candidate.suffix.casefold() not in PLUGIN_EXTENSIONS:
            continue
        relative = candidate.relative_to(final_mod)
        resource_value = relative.as_posix()
        resolved = candidate.resolve(strict=True)
        if not is_under(resolved, final_root):
            raise ValueError(f"Final plugin resolves outside final_mod: {resource_value}")
        key = resource_value.casefold()
        if key in seen:
            raise ValueError(f"Final plugin path collision after case folding: {resource_value}")
        seen.add(key)
        plugins.append((relative, resolved))
    return sorted(plugins, key=lambda item: item[0].as_posix().casefold())





def json_line_property(line: str, name: str) -> str:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return ""
    value = row.get(name)
    return "" if value is None else str(value)


def model_review_current_content_issues(
    model_text: str,
    final_text_packet: Path,
    final_binary_packet: Path,
    final_text_items_path: Path,
    final_binary_items_path: Path,
) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    for packet in (final_text_packet, final_binary_packet):
        if packet.is_file() and not packet_content_reviewed(model_text, packet):
            issues.append(
                (
                    "Agent model review does not cover the current final_mod review packet content hash; rerun model review.",
                    str(packet),
                )
            )
    reviewed_files = jsonl_file_values(final_text_items_path, "File") | jsonl_file_values(final_binary_items_path, "File")
    for contract_issue in model_review_contract_issues(model_text, reviewed_files):
        issues.append((contract_issue, ""))
    return issues


def translation_context_gate_issues(root: Path, mod_name: str) -> list[GateIssue]:
    packet = root / "qa" / f"{mod_name}.translation_context_packet.md"
    expected_hash = read_report_metric(packet, "Source Items SHA256")
    if not packet.is_file() or not expected_hash:
        return [
            GateIssue(
                "error",
                "translation-context",
                "Mod translation context packet is missing or has no source hash.",
                f"qa/{mod_name}.translation_context_packet.md",
            )
        ]
    context = current_game_context(root)
    _payload, validation_issues = validated_translation_context(root, mod_name, context)
    return [
        GateIssue(
            "error",
            "translation-context",
            issue,
            f"qa/{mod_name}.translation_context.json",
        )
        for issue in validation_issues
    ]


def model_review_evidence_paths(root: Path, mod_name: str) -> list[Path]:
    return [
        root / "qa" / f"{mod_name}.final_text_review_packet.md",
        root / "qa" / f"{mod_name}.final_text_review_items.jsonl",
        root / "qa" / f"{mod_name}.final_binary_review_packet.md",
        root / "qa" / f"{mod_name}.final_binary_review_items.jsonl",
        root / "qa" / f"{mod_name}.final_review_quality.md",
        root / "qa" / f"{mod_name}.final_review_quality.json",
        root / "qa" / f"{mod_name}.translation_context_packet.md",
        root / "qa" / f"{mod_name}.translation_context.json",
    ]


def collect_model_review_gate_issues(
    root: Path,
    mod_name: str,
    translation_inputs: list[Path],
    translation_input_list: Path,
    *,
    allow_missing: bool,
) -> list[GateIssue]:
    issues: list[GateIssue] = []
    evidence_paths = model_review_evidence_paths(root, mod_name)
    (
        final_text_packet,
        final_text_items_path,
        final_binary_packet,
        final_binary_items_path,
        _final_review_quality_report,
        _final_review_quality_json,
        _translation_context_packet,
        translation_context_path,
    ) = evidence_paths
    model_review = root / "qa" / f"{mod_name}.model_review.md"
    if translation_inputs:
        context_result = run_python_script(
            root,
            "new_model_review_packet.py",
            ["--mod-name", mod_name, "--input-list-path", str(translation_input_list)],
        )
        if context_result.returncode != 0:
            add_issue(
                issues,
                "error",
                "translation-context",
                "Mod translation context packet could not be refreshed from current translation inputs.",
                f"qa/{mod_name}.translation_context_packet.md",
            )
        else:
            issues.extend(translation_context_gate_issues(root, mod_name))
    contract_refresh = run_python_script(
        root,
        "update_model_review_contract.py",
        ["--mod-name", mod_name, "--create-if-missing"],
    )
    if contract_refresh.returncode != 0:
        add_issue(
            issues,
            "error",
            "model-review",
            "Model review contract scaffold could not be refreshed.",
            f"qa/{mod_name}.model_review.md",
        )
    if not model_review.is_file():
        if not allow_missing:
            add_issue(
                issues,
                "error",
                "model-review",
                f"Agent model review report is missing. Fill qa/{mod_name}.model_review.md before writeback/final delivery.",
                f"qa/{mod_name}.model_review.md",
            )
        return issues

    model_text = read_text(model_review)
    if translation_inputs and translation_context_path.is_file():
        expected_context_hash = sha256_file(translation_context_path)
        if read_report_metric(model_review, "Mod context Content SHA256") != expected_context_hash:
            add_issue(
                issues,
                "error",
                "translation-context",
                "Agent model review does not bind the current Mod translation context content hash.",
                f"qa/{mod_name}.model_review.md",
            )
    if re.search(r"\bTODO\b", model_text, re.I):
        add_issue(issues, "error", "model-review", "Agent model review report still contains TODO placeholders.", f"qa/{mod_name}.model_review.md")
    elif MODEL_REVIEWER_RE.search(model_text) is None:
        add_issue(issues, "error", "model-review", "Model review report does not explicitly state Reviewer: Agent model.", f"qa/{mod_name}.model_review.md")
    elif not re.search(r"\bpass\b", model_text, re.I):
        add_issue(issues, "warning", "model-review", "Agent model review report does not contain an explicit pass verdict.", f"qa/{mod_name}.model_review.md")
    if final_text_packet.is_file() and f"{mod_name}.final_text_review_packet.md" not in model_text:
        add_issue(issues, "error", "model-review", "Agent model review does not mention the final_mod text review packet.", f"qa/{mod_name}.final_text_review_packet.md")
    if final_binary_packet.is_file() and f"{mod_name}.final_binary_review_packet.md" not in model_text:
        add_issue(issues, "error", "model-review", "Agent model review does not mention the final_mod binary review packet.", f"qa/{mod_name}.final_binary_review_packet.md")
    for contract_issue, evidence in model_review_current_content_issues(
        model_text,
        final_text_packet,
        final_binary_packet,
        final_text_items_path,
        final_binary_items_path,
    ):
        add_issue(issues, "error", "model-review", contract_issue, evidence or f"qa/{mod_name}.model_review.md")
    return issues


def count_candidate_rows_strict(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return None
    count = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(row, dict) or not isinstance(row.get("risk"), str):
            return None
        if row["risk"] == "candidate":
            count += 1
    return count



def add_issue(issues: list[GateIssue], severity: str, gate: str, message: str, evidence: str = "") -> None:
    issues.append(GateIssue(severity, gate, message, evidence))


def coverage_is_complete(missing: int, blocking: int) -> bool:
    return missing == 0 and blocking == 0


def collect_used_capability_gate_issues(
    root: Path,
    mod_name: str,
    final_mod: Path,
    *,
    strict_complete: bool,
) -> tuple[list[GateIssue], dict[str, object]]:
    output = root / "qa" / f"{mod_name}.used_capabilities.json"
    try:
        write_used_capabilities(root, mod_name, final_mod, output)
        payload = json.loads(output.read_text(encoding="utf-8-sig"))
    except UsedCapabilityError as exc:
        return (
            [
                GateIssue(
                    "error",
                    f"used-capability-{exc.error_code.replace('_', '-')}",
                    str(exc),
                    relative_path(root, output),
                )
            ],
            {},
        )
    except (OSError, json.JSONDecodeError) as exc:
        return (
            [
                GateIssue(
                    "error",
                    "used-capability-verification-failed",
                    f"Unable to read generated used-capability evidence: {exc}",
                    relative_path(root, output),
                )
            ],
            {},
        )
    rows = payload.get("operations", [])
    if not isinstance(rows, list):
        return (
            [
                GateIssue(
                    "error",
                    "used-capability-verification-failed",
                    "Used-capability evidence has an invalid operations array.",
                    relative_path(root, output),
                )
            ],
            payload,
        )
    issues: list[GateIssue] = []
    required_resource_fields = {
        "resource_category",
        "resource_subtype",
        "resource_container",
        "resource_traits",
        "capability",
        "operation",
        "effective_level",
        "strict_complete_allowed",
        "supported",
        "error_code",
        "reason",
    }
    for row in rows:
        if not isinstance(row, dict):
            issues.append(
                GateIssue(
                    "error",
                    "used-capability-verification-failed",
                    "Used-capability evidence contains a non-object row.",
                    relative_path(root, output),
                )
            )
            continue
        missing_fields = sorted(required_resource_fields - set(row))
        if missing_fields:
            issues.append(
                GateIssue(
                    "error",
                    "used-capability-verification-failed",
                    "Used-capability evidence is missing resource decision fields: "
                    + ", ".join(missing_fields),
                    relative_path(root, output),
                )
            )
            continue
        if (
            row.get("supported") is not True
            or str(row.get("capability", "")) != str(row.get("name", ""))
            or str(row.get("effective_level", "")) != str(row.get("level", ""))
        ):
            issues.append(
                GateIssue(
                    "error",
                    "used-capability-verification-failed",
                    "Used-capability resource decision is inconsistent or unsupported.",
                    relative_path(root, output),
                )
            )
            continue
        if str(row.get("result", "")) != "success":
            issues.append(
                GateIssue(
                    "error",
                    "used-capability-adapter-failed",
                    f"Capability {row.get('name', '')}/{row.get('operation', '')} did not succeed.",
                    relative_path(root, output),
                )
            )
            continue
        if (
            strict_complete
            and row.get("participates_in_final_delivery") is True
            and row.get("strict_complete_allowed") is not True
        ):
            issues.append(
                GateIssue(
                    "error",
                    "used-capability-experimental-restriction",
                    f"Capability {row.get('name', '')}/{row.get('operation', '')} "
                    f"at level {row.get('effective_level', '')} participated in final delivery but is "
                    "not eligible for strict completion.",
                    relative_path(root, output),
                )
            )
    return issues, payload


def clean_report_passed(path: Path, pattern: str) -> bool:
    return path.is_file() and re.search(pattern, read_text(path)) is not None


def get_plugin_candidate_count(
    root: Path,
    mod_name: str,
    plugin_path: Path,
    plugin_name: str,
    game_id: str,
) -> int | None:
    export_path = root / "source" / "plugin_exports" / mod_name / f"{plugin_name}.strict_candidate_probe.jsonl"
    report_path = root / "qa" / f"{plugin_name}.strict_candidate_probe.md"
    for stale in (export_path, report_path):
        if stale.exists():
            stale.unlink()
    result = run_python_script(
        root,
        "export_esp_strings.py",
        [
            "--plugin-path",
            str(plugin_path),
            "--mod-name",
            mod_name,
            "--output-path",
            relative_path(root, export_path),
            "--report-path",
            relative_path(root, report_path),
            "--allow-generated-plugin",
            "--game",
            game_id,
        ],
    )
    if result.returncode != 0 or not export_path.is_file() or not report_path.is_file():
        return None
    return count_candidate_rows_strict(export_path)


def get_pex_candidate_count(
    root: Path,
    mod_name: str,
    pex_path: Path,
    pex_base_name: str,
    extract_entrypoint: str,
    game_id: str,
) -> int | None:
    output_path = root / "source" / "pex_exports" / mod_name / f"{pex_base_name}.strict_final_mod.pex_strings.jsonl"
    report_path = root / "qa" / f"{pex_base_name}.strict_pex_export_report.md"
    for stale in (output_path, report_path):
        if stale.exists():
            stale.unlink()
    result = run_python_script(
        root,
        extract_entrypoint,
        [
            "--mode",
            "Export",
            "--game",
            game_id,
            "--input-pex-path",
            str(pex_path),
            "--output-jsonl-path",
            relative_path(root, output_path),
            "--report-path",
            relative_path(root, report_path),
        ],
    )
    if result.returncode != 0 or not output_path.is_file() or not report_path.is_file():
        return None
    return count_candidate_rows_strict(output_path)


def collect_translation_inputs(root: Path, mod_name: str) -> list[Path]:
    return collect_translation_input_files(root, mod_name, suffixes={".jsonl"}, include_derived_pex_apply=True)


def write_translation_input_list(root: Path, mod_name: str, inputs: list[Path]) -> Path:
    gate_dir = root / "work" / "gates" / mod_name
    gate_dir.mkdir(parents=True, exist_ok=True)
    list_path = gate_dir / "translation_inputs.txt"
    if inputs:
        list_path.write_text("\n".join(str(item) for item in inputs) + "\n", encoding="utf-8")
    elif list_path.exists():
        list_path.write_text("", encoding="utf-8")
    return list_path


def report_success_metrics(root: Path, mod_name: str, workspace: Path, final_mod: Path, report_path: Path, strict_complete: bool, issues: list[GateIssue], notes: list[str], metrics: dict[str, object], translation_inputs: list[Path], context: GameContext | None = None) -> None:
    context = context or current_game_context(root)
    if strict_complete:
        # Release readiness is stricter than normal QA: any warning indicates
        # unreviewed uncertainty and becomes blocking for completion claims.
        warnings = [issue for issue in issues if issue.Severity == "warning"]
        for warning in warnings:
            add_issue(
                issues,
                "error",
                "strict-complete",
                f"Strict complete mode treats warning as blocking: {warning.Gate} - {warning.Message}",
                warning.Evidence,
            )

    blocking_count = sum(1 for issue in issues if issue.Severity == "error")
    warning_count = sum(1 for issue in issues if issue.Severity == "warning")
    lines: list[str] = [
        "# Non-GUI QA Gate Report",
        "",
        f"- Game: {game_display_label(context)}",
        f"- Support level: {context.support_level}",
        *[f"- {key}: {value}" for key, value in game_context_metadata(context).items()],
        f"- ModName: {mod_name}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Workspace: {relative_path(root, workspace)}",
        f"- FinalModDir: {relative_path(root, final_mod)}",
        f"- Translation inputs: {len(translation_inputs)}",
        f"- Localized string tables: {metrics.get('localized_string_tables', 0)}",
        f"- Coverage audited candidates: {metrics.get('coverage_audited', 'not_run')}",
        f"- Coverage missing: {metrics.get('coverage_missing', 'not_run')}",
        f"- Coverage unverified: {metrics.get('coverage_unverified', 'not_run')}",
        f"- Coverage explicit blockers: {metrics.get('coverage_blocking', 'not_run')}",
        f"- PEX delivery rows: {metrics.get('pex_delivery_rows', 'not_run')}",
        f"- PEX delivery blocking issues: {metrics.get('pex_delivery_blocking', 'not_run')}",
        f"- PEX delivery warnings: {metrics.get('pex_delivery_warnings', 'not_run')}",
        f"- Archive files checked: {metrics.get('archive_files_checked', 'not_run')}",
        f"- Archives missing evidence: {metrics.get('archive_missing_evidence', 'not_run')}",
        f"- Archives invalid evidence: {metrics.get('archive_invalid_evidence', 'not_run')}",
        f"- Archive translatable files: {metrics.get('archive_translatable_files', 'not_run')}",
        f"- Archive loose overrides present: {metrics.get('archive_loose_overrides_present', 'not_run')}",
        f"- Archive loose override exemptions: {metrics.get('archive_loose_override_exemptions', 'not_run')}",
        f"- Archive loose overrides missing: {metrics.get('archive_loose_overrides_missing', 'not_run')}",
        f"- Archive loose override exemption issues: {metrics.get('archive_loose_override_exemption_issues', 'not_run')}",
        f"- Interface runtime files checked: {metrics.get('interface_runtime_files_checked', 'not_run')}",
        f"- Interface runtime warnings: {metrics.get('interface_runtime_warnings', 'not_run')}",
        f"- Final text files checked: {metrics.get('final_text_files_checked', 'not_run')}",
        f"- Final text structure warnings: {metrics.get('final_text_warnings', 'not_run')}",
        f"- Final text review items: {metrics.get('final_text_review_items', 'not_run')}",
        f"- Final text protected review items: {metrics.get('final_text_protected_review_items', 'not_run')}",
        f"- Final binary review items: {metrics.get('final_binary_review_items', 'not_run')}",
        f"- Final binary manual review items: {metrics.get('final_binary_manual_review_items', 'not_run')}",
        f"- Final binary protected review items: {metrics.get('final_binary_protected_review_items', 'not_run')}",
        f"- Final binary export failures: {metrics.get('final_binary_export_failures', 'not_run')}",
        f"- Final review quality rows: {metrics.get('final_review_quality_rows', 'not_run')}",
        f"- Final review quality blocking issues: {metrics.get('final_review_quality_blocking', 'not_run')}",
        f"- Final review quality warnings: {metrics.get('final_review_quality_warnings', 'not_run')}",
        f"- Final plugins checked: {metrics.get('final_plugins_checked', 0)}",
        f"- Final PEX files checked: {metrics.get('final_pex_files_checked', 0)}",
        f"- Strict complete mode: {bool(strict_complete)}",
        f"- WarningPolicyBlocksCompletion: {bool(strict_complete)}",
        f"- Blocking issues: {blocking_count}",
        f"- Warnings: {warning_count}",
        "",
        "## Verdict",
        "",
        "PASS: Non-GUI QA gates have no blocking issues." if blocking_count == 0 else "FAIL: Non-GUI QA gates have blocking issues.",
        "",
        "## Issues",
        "",
    ]
    if not issues:
        lines.append("No gate issues.")
    else:
        lines.extend(["| Severity | Gate | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {issue.Gate} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |")

    lines.extend(["", "## Notes", ""])
    if not notes:
        lines.append("No additional notes.")
    else:
        lines.extend(f"- {note}" for note in notes)

    lines.extend(
        [
            "",
            "## Required Evidence",
            "",
            "- Decoder detection: `qa/decoder_tools_report.md`",
            f"- Mechanical proofread: `qa/{mod_name}.translation_proofread.md`",
            f"- Agent model review: `qa/{mod_name}.model_review.md`",
            f"- Non-GUI coverage: `out/{mod_name}/qa/non_gui_translation_coverage.md`",
            f"- PEX delivery audit: `qa/{mod_name}.pex_delivery_post_build.md`",
            f"- Archive coverage: `qa/{mod_name}.archive_coverage.md`",
            f"- final_mod Interface runtime audit: `qa/{mod_name}.final_interface_runtime.md`",
            f"- final_mod text structure: `qa/{mod_name}.final_text_structure.md`",
            f"- final_mod text model review packet: `qa/{mod_name}.final_text_review_packet.md`",
            f"- final_mod binary model review packet: `qa/{mod_name}.final_binary_review_packet.md`",
            f"- final_mod final review quality audit: `qa/{mod_name}.final_review_quality.md`",
            "- final_mod validation: `qa/final_mod_validation.md`",
            "- Plugin verification reports: `qa/*.gate-plugin-output-verification.md`",
            f"- PEX verification and re-read reports: `qa/{mod_name}.<Script>.pex_output_verification.md`, `qa/*.gate_pex_export_report.md`",
            "",
            "## Safety",
            "",
            "- This gate script does not translate text.",
            "- This gate script does not write plugin or PEX binaries.",
            "- This gate script reads only project-local inputs and writes QA/work/source reports.",
            "- Real game, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run strict non-GUI QA gates for the current workspace Game Profile.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--allow-missing-model-review", action="store_true")
    parser.add_argument("--strict-complete", action="store_true")
    parser.add_argument(
        "--reuse-mechanical-evidence",
        action="store_true",
        help="Reuse a content-bound clean mechanical pass and refresh only the model-review gate; fall back to a full run when stale.",
    )
    args = parser.parse_args()
    if args.reuse_mechanical_evidence and not args.strict_complete:
        parser.error("--reuse-mechanical-evidence requires --strict-complete")

    root = project_root()
    context = current_game_context(root)
    WorkflowLock(root, "run_non_gui_qa_gates.py").acquire()
    mod_name = args.mod_name
    workspace = resolve_project_path(root, args.workspace_path or f"work/extracted_mods/{mod_name}", must_exist=True)
    workspace = find_data_root(workspace, context=context).resolve(strict=True)
    final_mod = resolve_project_path(root, args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name)), must_exist=True)
    expected_final_mod = default_final_mod_dir(root, mod_name).resolve(strict=False)
    if final_mod.resolve(strict=False) != expected_final_mod:
        raise ValueError(f"FinalModDir must be out/{mod_name}/汉化产出/final_mod: {args.final_mod_dir}")
    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.non_gui_qa_gates.md", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")

    translation_inputs = collect_translation_inputs(root, mod_name)
    translation_input_list = write_translation_input_list(root, mod_name, translation_inputs)
    snapshot_path = root / "qa" / f"{mod_name}.strict_mechanical_snapshot.json"
    review_evidence = model_review_evidence_paths(root, mod_name)
    if args.reuse_mechanical_evidence:
        snapshot, reuse_reason = load_reusable_mechanical_snapshot(
            root=root,
            snapshot_path=snapshot_path,
            mod_name=mod_name,
            workspace=workspace,
            final_mod=final_mod,
            translation_inputs=translation_inputs,
            evidence_paths=review_evidence,
            game_metadata=game_context_metadata(context),
            source_root=context.plugin_root,
        )
        if snapshot is not None:
            issues = collect_model_review_gate_issues(
                root,
                mod_name,
                translation_inputs,
                translation_input_list,
                allow_missing=args.allow_missing_model_review,
            )
            metrics = dict(snapshot["Metrics"])
            notes = list(snapshot["Notes"])
            notes.append("Reused content-bound strict mechanical evidence; only the agent model-review contract was refreshed.")
            report_success_metrics(
                root,
                mod_name,
                workspace,
                final_mod,
                report_path,
                True,
                issues,
                notes,
                metrics,
                translation_inputs,
                context,
            )
            blocking_count = sum(1 for issue in issues if issue.Severity == "error")
            warning_count = sum(1 for issue in issues if issue.Severity == "warning")
            print(f"Reused strict mechanical evidence from: {snapshot_path}")
            print(f"Non-GUI QA gate report written to: {report_path}")
            print(f"Blocking issues: {blocking_count}")
            print(f"Warnings: {warning_count}")
            print("WarningPolicyBlocksCompletion: True")
            return 1 if blocking_count else 0
        print(f"Strict mechanical evidence was not reusable ({reuse_reason}); running full QA gates.")

    issues: list[GateIssue] = []
    notes: list[str] = []
    localized_string_tables = sorted(
        item
        for item in workspace.rglob("*")
        if item.is_file() and item.suffix.lower() in context.string_table_extensions
    )
    if localized_string_tables and not context.capability_at_least(
        "string_tables", "read_only"
    ):
        add_issue(
            issues,
            "error",
            "localized-strings",
            f"Localized STRINGS delivery is unsupported and blocked for {context.display_name}.",
            ", ".join(relative_path(root, item) for item in localized_string_tables[:8]),
        )
    metrics: dict[str, object] = {
        "used_capabilities": "not_run",
        "used_capability_blocking": "not_run",
        "localized_string_tables": len(localized_string_tables),
        "coverage_audited": "not_run",
        "coverage_missing": "not_run",
        "coverage_unverified": "not_run",
        "coverage_blocking": "not_run",
        "pex_delivery_rows": "not_run",
        "pex_delivery_blocking": "not_run",
        "pex_delivery_warnings": "not_run",
        "archive_files_checked": "not_run",
        "archive_missing_evidence": "not_run",
        "archive_invalid_evidence": "not_run",
        "archive_translatable_files": "not_run",
        "archive_loose_overrides_present": "not_run",
        "archive_loose_override_exemptions": "not_run",
        "archive_loose_overrides_missing": "not_run",
        "archive_loose_override_exemption_issues": "not_run",
        "interface_runtime_files_checked": "not_run",
        "interface_runtime_warnings": "not_run",
        "final_text_files_checked": "not_run",
        "final_text_warnings": "not_run",
        "final_text_review_items": "not_run",
        "final_text_protected_review_items": "not_run",
        "final_binary_review_items": "not_run",
        "final_binary_manual_review_items": "not_run",
        "final_binary_protected_review_items": "not_run",
        "final_binary_export_failures": "not_run",
        "final_review_quality_rows": "not_run",
        "final_review_quality_warnings": "not_run",
        "final_review_quality_blocking": "not_run",
        "final_plugins_checked": 0,
        "final_pex_files_checked": 0,
    }

    used_capability_issues, used_capability_payload = collect_used_capability_gate_issues(
        root,
        mod_name,
        final_mod,
        strict_complete=args.strict_complete,
    )
    issues.extend(used_capability_issues)
    used_capability_rows = used_capability_payload.get("operations", [])
    metrics["used_capabilities"] = len(used_capability_rows) if isinstance(used_capability_rows, list) else "invalid"
    metrics["used_capability_blocking"] = len(used_capability_issues)

    # Proofread only translation intermediates that feed writeback. Generated
    # review packets are handled later as final delivery evidence.
    # Some Mods expose only binary or structured final review items. Zero
    # standalone text candidates is suspicious, but not automatically fatal
    # until the later final_mod packets are inspected.
    coverage_found_no_candidates = False

    decoder = run_python_script(root, "detect_decoder_tools.py", [])
    if decoder.returncode != 0:
        add_issue(issues, "error", "decoder-tools", "Decoder tool detection failed.", "qa/decoder_tools_report.md")

    pex_delivery = run_python_script(
        root,
        "audit_pex_delivery.py",
        [
            "--mod-name",
            mod_name,
            "--workspace-path",
            str(workspace),
            "--final-mod-dir",
            str(final_mod),
            "--phase",
            "post-build",
        ],
    )
    pex_delivery_report = root / "qa" / f"{mod_name}.pex_delivery_post_build.md"
    if not pex_delivery_report.is_file():
        add_issue(issues, "error", "pex-delivery", "PEX delivery audit did not produce a report.", f"qa/{mod_name}.pex_delivery_post_build.md")
    else:
        metrics["pex_delivery_rows"] = read_report_metric(pex_delivery_report, "Rows checked") or "not_run"
        metrics["pex_delivery_blocking"] = read_report_metric(pex_delivery_report, "Blocking issues") or "not_run"
        metrics["pex_delivery_warnings"] = read_report_metric(pex_delivery_report, "Warnings") or "not_run"
        delivery_blocking = to_int(str(metrics["pex_delivery_blocking"]), 0)
        delivery_warnings = to_int(str(metrics["pex_delivery_warnings"]), 0)
        if delivery_blocking > 0:
            add_issue(issues, "error", "pex-delivery", f"PEX delivery audit has {delivery_blocking} blocking issue(s).", f"qa/{mod_name}.pex_delivery_post_build.md")
        if delivery_warnings > 0:
            add_issue(issues, "warning", "pex-delivery", f"PEX delivery audit has {delivery_warnings} warning(s).", f"qa/{mod_name}.pex_delivery_post_build.md")
        if pex_delivery.returncode != 0 and delivery_blocking == 0:
            add_issue(issues, "error", "pex-delivery", "PEX delivery audit failed to run cleanly.", f"qa/{mod_name}.pex_delivery_post_build.md")

    if translation_inputs:
        proofread = run_python_script(
            root,
            "proofread_translation.py",
            [
                "--input-list-path",
                str(translation_input_list),
                "--report-output-path",
                f"qa/{mod_name}.translation_proofread.md",
                "--issues-jsonl-path",
                f"qa/{mod_name}.translation_proofread_issues.jsonl",
                "--warn-only",
            ],
        )
        proofread_report = root / "qa" / f"{mod_name}.translation_proofread.md"
        if proofread.returncode != 0:
            add_issue(issues, "error", "mechanical-proofread", "Mechanical proofread script failed to run.", f"qa/{mod_name}.translation_proofread.md")
        else:
            blocking = to_int(read_report_metric(proofread_report, "Blocking issues"), 0)
            warnings = to_int(read_report_metric(proofread_report, "Warnings"), 0)
            if blocking > 0:
                add_issue(issues, "error", "mechanical-proofread", f"Mechanical proofread has {blocking} blocking issue(s).", f"qa/{mod_name}.translation_proofread.md")
            if warnings > 0:
                add_issue(issues, "warning", "mechanical-proofread", f"Mechanical proofread has {warnings} warning(s).", f"qa/{mod_name}.translation_proofread.md")
    else:
        severity = "error" if args.strict_complete else "warning"
        evidence = "; ".join(translation_input_evidence_roots(root, mod_name, include_derived_pex_apply=True))
        add_issue(issues, severity, "mechanical-proofread", "No translation JSONL inputs were found for proofread.", evidence)

    extraction = run_python_script(
        root,
        "extract_non_gui_candidates.py",
        ["--mod-name", mod_name, "--workspace-dir", str(workspace)],
    )
    if extraction.returncode != 0:
        add_issue(issues, "error", "coverage", "Non-GUI candidate extraction failed.", f"out/{mod_name}/qa/non_gui_extraction_report.md")
    else:
        coverage = run_python_script(
            root,
            "audit_non_gui_coverage.py",
            ["--mod-name", mod_name, "--final-mod-dir", str(final_mod)],
        )
        coverage_report = root / "out" / mod_name / "qa" / "non_gui_translation_coverage.md"
        if coverage.returncode != 0:
            add_issue(issues, "error", "coverage", "Non-GUI coverage audit failed to run.", f"out/{mod_name}/qa/non_gui_translation_coverage.md")
        else:
            metrics["coverage_audited"] = read_report_metric(coverage_report, "Audited candidates") or "not_run"
            metrics["coverage_missing"] = read_report_metric(coverage_report, "Missing") or "not_run"
            metrics["coverage_unverified"] = read_report_metric(coverage_report, "Unverified") or "not_run"
            metrics["coverage_blocking"] = read_report_metric(coverage_report, "Blocking") or "not_run"
            missing = to_int(str(metrics["coverage_missing"]), 0)
            unverified = to_int(str(metrics["coverage_unverified"]), 0)
            blocking = to_int(str(metrics["coverage_blocking"]), 0)
            audited = to_int(str(metrics["coverage_audited"]), 0)
            if missing > 0:
                add_issue(issues, "error", "coverage", f"Non-GUI coverage audit found {missing} missing candidate(s).", f"out/{mod_name}/qa/non_gui_remaining_gaps.jsonl")
            if blocking > 0:
                add_issue(issues, "error", "coverage", f"Non-GUI coverage audit found {blocking} explicit blocking candidate(s).", f"out/{mod_name}/qa/non_gui_coverage_all.jsonl")
            if unverified > 0:
                notes.append(f"Non-GUI coverage retained {unverified} non-blocking unverified candidate(s) for model or manual review.")
            if audited == 0:
                coverage_found_no_candidates = True

    archive_args = ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod)]
    if args.strict_complete:
        archive_args.append("--strict-complete")
    archive = run_python_script(root, "audit_archive_coverage.py", archive_args)
    archive_report = root / "qa" / f"{mod_name}.archive_coverage.md"
    if archive.returncode != 0:
        add_issue(issues, "error", "archive-coverage", "Archive coverage audit failed or found blocking issue(s).", f"qa/{mod_name}.archive_coverage.md")
    else:
        metrics["archive_files_checked"] = read_report_metric(archive_report, "Archive files checked") or "not_run"
        metrics["archive_missing_evidence"] = read_report_metric(archive_report, "Archives missing evidence") or "not_run"
        metrics["archive_invalid_evidence"] = read_report_metric(archive_report, "Archives invalid evidence") or "not_run"
        metrics["archive_translatable_files"] = read_report_metric(archive_report, "Archive translatable files") or "not_run"
        metrics["archive_loose_overrides_present"] = read_report_metric(archive_report, "Archive loose overrides present") or "not_run"
        metrics["archive_loose_override_exemptions"] = read_report_metric(archive_report, "Archive loose override exemptions") or "not_run"
        metrics["archive_loose_overrides_missing"] = read_report_metric(archive_report, "Archive loose overrides missing") or "not_run"
        metrics["archive_loose_override_exemption_issues"] = read_report_metric(archive_report, "Archive loose override exemption issues") or "not_run"
        archive_warnings = to_int(read_report_metric(archive_report, "Warnings"), 0)
        if archive_warnings > 0:
            add_issue(issues, "warning", "archive-coverage", f"Archive coverage audit has {archive_warnings} warning(s).", f"qa/{mod_name}.archive_coverage.md")

    interface_runtime = run_python_script(
        root,
        "audit_final_interface_translations.py",
        ["--mod-name", mod_name, "--final-mod-dir", str(final_mod)],
    )
    interface_runtime_report = root / "qa" / f"{mod_name}.final_interface_runtime.md"
    if interface_runtime.returncode != 0:
        add_issue(
            issues,
            "error",
            "interface-runtime",
            "final_mod Interface translation runtime audit failed.",
            f"qa/{mod_name}.final_interface_runtime.md",
        )
    else:
        metrics["interface_runtime_files_checked"] = read_report_metric(interface_runtime_report, "Interface translation files checked") or "not_run"
        metrics["interface_runtime_warnings"] = read_report_metric(interface_runtime_report, "Warnings") or "not_run"
        runtime_warnings = to_int(str(metrics["interface_runtime_warnings"]), 0)
        if runtime_warnings > 0:
            add_issue(
                issues,
                "warning",
                "interface-runtime",
                f"final_mod Interface translation runtime audit has {runtime_warnings} warning(s).",
                f"qa/{mod_name}.final_interface_runtime.md",
            )

    final_text = run_python_script(
        root,
        "validate_final_text_structure.py",
        ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod)],
    )
    final_text_report = root / "qa" / f"{mod_name}.final_text_structure.md"
    final_text_clean = (
        final_text_report.is_file()
        and read_report_metric(final_text_report, "Blocking issues") == "0"
        and read_report_metric(final_text_report, "Warnings") == "0"
    )
    if final_text.returncode != 0 and not final_text_clean:
        add_issue(issues, "error", "final-text-structure", "final_mod text structure validation failed.", f"qa/{mod_name}.final_text_structure.md")
    else:
        metrics["final_text_files_checked"] = read_report_metric(final_text_report, "Files checked") or "not_run"
        metrics["final_text_warnings"] = read_report_metric(final_text_report, "Warnings") or "not_run"
        warnings = to_int(str(metrics["final_text_warnings"]), 0)
        if warnings > 0:
            add_issue(issues, "warning", "final-text-structure", f"final_mod text structure validation has {warnings} warning(s).", f"qa/{mod_name}.final_text_structure.md")

    final_text_review = run_python_script(
        root,
        "new_final_text_review_packet.py",
        ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod)],
    )
    final_text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    if final_text_review.returncode != 0:
        add_issue(issues, "error", "final-text-model-review", "final_mod text model review packet generation failed.", f"qa/{mod_name}.final_text_review_packet.md")
    else:
        metrics["final_text_review_items"] = read_report_metric(final_text_packet, "Review items") or "not_run"
        metrics["final_text_protected_review_items"] = read_report_metric(final_text_packet, "Protected review items") or "not_run"
        protected = to_int(str(metrics["final_text_protected_review_items"]), 0)
        if protected > 0:
            add_issue(issues, "warning", "final-text-model-review", f"final_mod text model review packet has {protected} protected-review item(s).", f"qa/{mod_name}.final_text_review_packet.md")

    # Binary review packet generation re-reads delivered ESP/PEX where possible.
    # This is the last chance to catch protected strings or decoder failures in
    # the actual files that will be packaged.
    final_binary_review = run_python_script(
        root,
        "new_final_binary_review_packet.py",
        ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod), "--reuse-current-if-unchanged"],
    )
    final_binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    if final_binary_review.returncode != 0:
        add_issue(issues, "error", "final-binary-model-review", "final_mod binary model review packet generation failed.", f"qa/{mod_name}.final_binary_review_packet.md")
    else:
        metrics["final_binary_review_items"] = read_report_metric(final_binary_packet, "Review items") or "not_run"
        metrics["final_binary_manual_review_items"] = read_report_metric(final_binary_packet, "Manual review items") or "not_run"
        metrics["final_binary_protected_review_items"] = read_report_metric(final_binary_packet, "Protected review items") or "not_run"
        metrics["final_binary_export_failures"] = read_report_metric(final_binary_packet, "Export failures") or "not_run"
        protected = to_int(str(metrics["final_binary_protected_review_items"]), 0)
        failures = to_int(str(metrics["final_binary_export_failures"]), 0)
        if protected > 0:
            add_issue(issues, "error", "final-binary-model-review", f"final_mod binary model review packet has {protected} protected-review item(s).", f"qa/{mod_name}.final_binary_review_packet.md")
        if failures > 0:
            add_issue(issues, "error", "final-binary-model-review", f"final_mod binary model review packet has {failures} export failure(s).", f"qa/{mod_name}.final_binary_review_packet.md")

    final_review_quality = run_python_script(root, "audit_final_review_quality.py", ["--mod-name", mod_name])
    final_review_quality_report = root / "qa" / f"{mod_name}.final_review_quality.md"
    if not final_review_quality_report.is_file():
        add_issue(issues, "error", "final-review-quality", "final_mod final review quality audit did not produce a report.", f"qa/{mod_name}.final_review_quality.md")
    else:
        metrics["final_review_quality_rows"] = read_report_metric(final_review_quality_report, "Rows checked") or "not_run"
        metrics["final_review_quality_blocking"] = read_report_metric(final_review_quality_report, "Blocking issues") or "not_run"
        metrics["final_review_quality_warnings"] = read_report_metric(final_review_quality_report, "Warnings") or "not_run"
        quality_blocking = to_int(str(metrics["final_review_quality_blocking"]), 0)
        quality_warnings = to_int(str(metrics["final_review_quality_warnings"]), 0)
        if quality_blocking > 0:
            add_issue(issues, "error", "final-review-quality", f"final_mod final review quality audit has {quality_blocking} blocking issue(s).", f"qa/{mod_name}.final_review_quality.md")
        if quality_warnings > 0:
            add_issue(issues, "warning", "final-review-quality", f"final_mod final review quality audit has {quality_warnings} warning(s).", f"qa/{mod_name}.final_review_quality.md")
        if final_review_quality.returncode != 0 and quality_blocking == 0 and quality_warnings == 0:
            add_issue(issues, "error", "final-review-quality", "final_mod final review quality audit failed to run cleanly.", f"qa/{mod_name}.final_review_quality.md")

    if coverage_found_no_candidates:
        final_text_items = to_int(str(metrics["final_text_review_items"]), 0)
        final_binary_items = to_int(str(metrics["final_binary_review_items"]), 0)
        if final_text_items > 0 or final_binary_items > 0:
            notes.append(
                "Non-GUI text-resource coverage had no standalone text candidates; "
                f"final_mod review packets cover {final_text_items} text item(s) and {final_binary_items} binary item(s)."
            )
        else:
            severity = "error" if args.strict_complete else "warning"
            add_issue(issues, severity, "coverage", "Non-GUI coverage audit found no translation candidates.", f"out/{mod_name}/qa/non_gui_translation_coverage.md")

    # Model review is a semantic gate over final_mod, not over an early draft
    # translation table. Packet content hashes keep it tied to current inputs.
    issues.extend(
        collect_model_review_gate_issues(
            root,
            mod_name,
            translation_inputs,
            translation_input_list,
            allow_missing=args.allow_missing_model_review,
        )
    )

    try:
        final_plugins = collect_final_plugins(final_mod)
    except (OSError, ValueError) as exc:
        final_plugins = []
        add_issue(
            issues,
            "error",
            "plugin-output",
            f"Final plugin inventory is unsafe or invalid: {exc}",
            relative_path(root, final_mod),
        )
    metrics["final_plugins_checked"] = len(final_plugins)
    plugin_extract_entrypoint = ""
    plugin_verify_entrypoint = ""
    if final_plugins:
        plugin_read = resolve_capability(context, "plugin_text", "read")
        if plugin_read.supported and plugin_read.adapter_id:
            try:
                plugin_extract_entrypoint = require_script_entrypoint(
                    plugin_read.adapter_id, "extract"
                )
                plugin_verify_entrypoint = require_script_entrypoint(
                    plugin_read.adapter_id, "verify"
                )
            except ValueError as exc:
                add_issue(issues, "error", "plugin-output", str(exc), "config/game_profiles")
        else:
            add_issue(
                issues,
                "error",
                "plugin-output",
                plugin_read.reason,
                "config/game_profiles",
            )
    for relative_plugin, plugin in final_plugins:
        if not plugin_extract_entrypoint or not plugin_verify_entrypoint:
            continue
        artifact_key = plugin_artifact_key(mod_name, relative_plugin)
        try:
            binding = bind_plugin_write_artifacts(
                root,
                workspace,
                mod_name,
                relative_plugin,
                plugin,
                used_capability_payload,
                context.game_id,
            )
        except (OSError, ValueError) as exc:
            add_issue(
                issues,
                "error",
                "plugin-output",
                f"Strict plugin artifact binding failed for {relative_plugin.as_posix()}: {exc}",
                relative_path(root, plugin),
            )
            continue
        if binding is None:
            original = workspace / relative_plugin
            if not original.is_file():
                add_issue(
                    issues,
                    "error",
                    "plugin-output",
                    "Original plugin not found for unchanged final output: "
                    f"{relative_plugin.as_posix()}",
                    relative_path(root, plugin),
                )
                continue
            if args.strict_complete:
                candidate_count = get_plugin_candidate_count(
                    root,
                    mod_name,
                    plugin,
                    artifact_key,
                    context.game_id,
                )
                if candidate_count is None:
                    add_issue(issues, "error", "plugin-output", f"No plugin write operation exists for {relative_plugin.as_posix()}, and candidate export could not be verified.", f"translated/plugin_exports/{mod_name}")
                elif candidate_count > 0:
                    add_issue(issues, "error", "plugin-output", f"No plugin translation JSONL found for {relative_plugin.as_posix()}; {candidate_count} candidate row(s) need coverage.", f"translated/plugin_exports/{mod_name}")
                else:
                    notes.append(f"Plugin has no exported candidate rows and no translation JSONL was required: {relative_plugin.as_posix()}")
            else:
                add_issue(issues, "warning", "plugin-output", f"No plugin write operation exists for {relative_plugin.as_posix()}; verification skipped.", f"translated/plugin_exports/{mod_name}")
            continue
        original = binding.original
        translation = binding.translation
        writeback_report = binding.apply_report
        verify_report = f"qa/{artifact_key}.gate-plugin-output-verification.md"
        output_export = f"source/plugin_exports/{mod_name}/{artifact_key}.gate-final-mod.strings.jsonl"
        output_export_report = f"qa/{artifact_key}.gate-final-mod-export.md"
        exported = run_python_script(
            root,
            plugin_extract_entrypoint,
            [
                "--plugin-path",
                str(plugin),
                "--mod-name",
                mod_name,
                "--output-path",
                output_export,
                "--report-path",
                output_export_report,
                "--allow-generated-plugin",
                "--game",
                context.game_id,
            ],
        )
        if exported.returncode != 0 or not (root / output_export).is_file():
            add_issue(
                issues,
                "error",
                "plugin-output",
                f"Final plugin could not be exported by the production exporter: {relative_plugin.as_posix()}",
                output_export_report,
            )
            continue
        try:
            validate_plugin_report_identity(
                root / output_export_report,
                project_root=root,
                expected_input=plugin,
                expected_game=context.game_id,
                expected_operation="export",
            )
            validate_plugin_report_status(
                root / output_export_report,
                return_code=exported.returncode,
            )
        except (OSError, ValueError) as exc:
            add_issue(
                issues,
                "error",
                "plugin-output",
                f"Final plugin export report is invalid for {relative_plugin.as_posix()}: {exc}",
                output_export_report,
            )
            continue
        invariant_report = f"qa/{artifact_key}.gate-plugin-binary-invariant.md"
        if args.strict_complete:
            invariant = run_python_script(
                root,
                plugin_verify_entrypoint,
                [
                    "--mode",
                    "Verify",
                    "--input-plugin-path",
                    str(original),
                    "--translation-jsonl-path",
                    str(translation),
                    "--output-plugin-path",
                    str(plugin),
                    "--report-path",
                    invariant_report,
                    "--game",
                    context.game_id,
                ],
            )
            invariant_path = root / invariant_report
            if invariant.returncode != 0 or not invariant_path.is_file():
                invariant_detail = " ".join(process_output(invariant)[:4]).strip()
                add_issue(
                    issues,
                    "error",
                    "plugin-output",
                    f"Strict plugin verification requires a fresh controlled invariant report for {relative_plugin.as_posix()}."
                    + (f" {invariant_detail}" if invariant_detail else ""),
                    invariant_report,
                )
                continue
            try:
                validate_plugin_report_identity(
                    invariant_path,
                    project_root=root,
                    expected_input=original,
                    expected_game=context.game_id,
                    expected_operation="verify",
                )
                validate_plugin_report_output(
                    invariant_path,
                    project_root=root,
                    expected_output=plugin,
                )
                validate_plugin_report_status(
                    invariant_path,
                    return_code=invariant.returncode,
                )
            except (OSError, ValueError) as exc:
                add_issue(
                    issues,
                    "error",
                    "plugin-output",
                    f"Strict plugin invariant report is invalid for {relative_plugin.as_posix()}: {exc}",
                    invariant_report,
                )
                continue
        verify_args = [
            "--original-plugin-path",
            str(original),
            "--output-plugin-path",
            str(plugin),
            "--translation-jsonl-path",
            str(translation),
            "--output-export-jsonl-path",
            output_export,
            "--report-output-path",
            verify_report,
            "--game",
            context.game_id,
        ]
        if writeback_report.is_file():
            verify_args.extend(["--writeback-report-path", str(writeback_report)])
        if args.strict_complete:
            verify_args.extend(["--invariant-report-path", invariant_report])
            verify_args.append("--require-translation-evidence")
        else:
            verify_args.append("--warn-only")
        verify_path = root / verify_report
        if verify_path.exists():
            verify_path.unlink()
        verify = run_python_script(
            root,
            "verify_plugin_output.py",
            verify_args,
        )
        if verify.returncode != 0:
            verify_detail = " ".join(process_output(verify)[:4]).strip()
            add_issue(
                issues,
                "error",
                "plugin-output",
                f"Plugin verification failed to run for {relative_plugin.as_posix()}." + (f" {verify_detail}" if verify_detail else ""),
                verify_report,
            )
        elif args.strict_complete:
            try:
                validate_plugin_post_verify_report(
                    verify_path,
                    project_root=root,
                    expected_game=context.game_id,
                    expected_adapter=binding.result.adapter_id,
                    expected_original=original,
                    expected_output=plugin,
                    expected_translation_jsonl=translation,
                    expected_output_export_jsonl=root / output_export,
                    expected_writeback_report=writeback_report,
                    expected_invariant_report=root / invariant_report,
                )
            except (OSError, ValueError) as exc:
                add_issue(
                    issues,
                    "error",
                    "plugin-output",
                    f"Plugin verification evidence is invalid for {relative_plugin.as_posix()}: {exc}",
                    verify_report,
                )
        elif not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "plugin-output", f"Plugin verification did not pass cleanly for {relative_plugin.as_posix()}.", verify_report)

    coverage_complete = coverage_is_complete(
        to_int(str(metrics.get("coverage_missing")), -1),
        to_int(str(metrics.get("coverage_blocking")), -1),
    )
    final_pex_files = sorted(item for item in final_mod.rglob("*") if item.is_file() and item.suffix.lower() == ".pex")
    metrics["final_pex_files_checked"] = len(final_pex_files)
    pex_extract_entrypoint = ""
    if final_pex_files:
        try:
            _pex_read, pex_extract_entrypoint = require_capability_script_entrypoint(
                context,
                "pex",
                "read",
                "extract",
            )
        except ValueError as exc:
            add_issue(issues, "error", "pex-output", str(exc), "config/game_profiles")
    for pex in final_pex_files:
        if not pex_extract_entrypoint:
            continue
        rel_pex = relative_path(final_mod, pex)
        original = workspace / rel_pex
        if not original.is_file():
            add_issue(issues, "error", "pex-output", f"Original PEX not found for final output: {rel_pex}", relative_path(root, pex))
            continue

        matched_lines: list[str] = []
        skipped_pex_rows = 0
        candidate_rows: list[tuple[str, dict | None]] = []
        for candidate in translation_inputs:
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if json_line_property(line, "ModName").lower() == pex.name.lower():
                        candidate_rows.append((line, None))
                    continue
                if isinstance(row, dict) and pex_row_matches(row, pex):
                    candidate_rows.append((line, row))
        protected_sources = {
            row_value(row, *SOURCE_FIELDS)
            for _line, row in candidate_rows
            if row is not None
            and row_value(row, *SOURCE_FIELDS).strip()
            and pex_translation_row_protects_source(row)
        }
        for line, row in candidate_rows:
            if row is not None:
                source = row_value(row, *SOURCE_FIELDS)
                if source in protected_sources or pex_translation_skip_reason(row):
                    skipped_pex_rows += 1
                    continue
            matched_lines.append(normalized_pex_translation_line(row, pex, line) if row is not None else line)
        if skipped_pex_rows:
            notes.append(f"Skipped protected or non-writable PEX row(s) for {rel_pex}: {skipped_pex_rows}")

        if not matched_lines:
            if sha256(original) != sha256(pex):
                evidence = "; ".join(translation_input_evidence_roots(root, mod_name, include_derived_pex_apply=True))
                add_issue(
                    issues,
                    "error",
                    "pex-output",
                    f"Final PEX differs from original but no matching translation JSONL rows were found: {rel_pex}",
                    evidence,
                )
            elif args.strict_complete:
                if coverage_complete:
                    notes.append(f"PEX unchanged and accepted because non-GUI coverage is complete: {rel_pex}")
                    continue
                candidate_count = get_pex_candidate_count(
                    root,
                    mod_name,
                    pex,
                    pex.stem,
                    pex_extract_entrypoint,
                    context.game_id,
                )
                if candidate_count is None:
                    add_issue(issues, "error", "pex-output", f"PEX unchanged and no translation rows found, but candidate export could not be verified: {rel_pex}", f"qa/{pex.stem}.strict_pex_export_report.md")
                elif candidate_count > 0 and not coverage_complete:
                    add_issue(issues, "error", "pex-output", f"PEX unchanged and no translation rows found, but {candidate_count} candidate row(s) were exported: {rel_pex}", f"source/pex_exports/{mod_name}/{pex.stem}.strict_final_mod.pex_strings.jsonl")
                else:
                    notes.append(f"PEX unchanged and no exported candidate rows found: {rel_pex}")
            else:
                notes.append(f"PEX unchanged and no translation rows found: {rel_pex}")
            continue

        filtered = root / "work" / "gates" / mod_name / f"{pex.stem}.translation.jsonl"
        filtered.parent.mkdir(parents=True, exist_ok=True)
        filtered.write_text("\n".join(matched_lines) + "\n", encoding="utf-8")
        verify_report = f"qa/{mod_name}.{pex.stem}.pex_output_verification.md"
        verify = run_python_script(
            root,
            "verify_pex_output.py",
            [
                "--original-pex-path",
                str(original),
                "--output-pex-path",
                str(pex),
                "--translation-jsonl-path",
                str(filtered),
                "--report-output-path",
                verify_report,
                "--game",
                context.game_id,
                "--warn-only",
            ],
        )
        verify_path = root / verify_report
        if verify.returncode != 0 and not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "pex-output", f"PEX verification failed to run for {rel_pex}.", verify_report)
        elif not clean_report_passed(verify_path, r"No blocking issues\."):
            add_issue(issues, "error", "pex-output", f"PEX verification did not pass cleanly for {rel_pex}.", verify_report)

        export_report = f"qa/{pex.stem}.gate_pex_export_report.md"
        export_jsonl = f"source/pex_exports/{mod_name}/{pex.stem}.gate_final_mod.pex_strings.jsonl"
        export = run_python_script(
            root,
            pex_extract_entrypoint,
            [
                "--mode",
                "Export",
                "--game",
                context.game_id,
                "--input-pex-path",
                str(pex),
                "--output-jsonl-path",
                export_jsonl,
                "--report-path",
                export_report,
            ],
        )
        if export.returncode != 0:
            add_issue(issues, "error", "pex-output", f"Final PEX could not be re-read by its configured adapter: {rel_pex}", export_report)

    final_validation = run_python_script(root, "validate_final_mod.py", ["--final-mod-dir", str(final_mod)])
    final_validation_report = root / "qa" / "final_mod_validation.md"
    if final_validation.returncode != 0:
        add_issue(issues, "error", "final-mod", "final_mod validation failed.", "qa/final_mod_validation.md")
    else:
        text = read_text(final_validation_report)
        if "No blocking errors." not in text:
            add_issue(issues, "error", "final-mod", "final_mod validation did not report clean blocking status.", "qa/final_mod_validation.md")
        sidecar_overlay_zero = re.search(r"Language sidecar overlays:\s*0", text) is not None
        original_sidecar_warning_only = sidecar_overlay_zero and "- Language sidecar file exists in final_mod;" in text
        if "No warnings." not in text and not original_sidecar_warning_only:
            add_issue(issues, "warning", "final-mod", "final_mod validation reported warning(s).", "qa/final_mod_validation.md")
        if not re.search(r"Delivery mode:\s*(?:direct-replacement-final-mod|translation-overlay-package)", text):
            add_issue(issues, "error", "final-mod", "final_mod is not confirmed as a supported delivery mode.", "qa/final_mod_validation.md")
        if not re.search(r"Language sidecar overlays:\s*0", text):
            add_issue(issues, "error", "final-mod", "final_mod contains language sidecar overlay(s), which is not direct replacement delivery.", "qa/final_mod_validation.md")

    if args.strict_complete:
        mechanical_issues = [issue for issue in issues if issue.Gate != "model-review"]
        if mechanical_issues:
            snapshot_path.unlink(missing_ok=True)
        else:
            try:
                write_reusable_mechanical_snapshot(
                    root=root,
                    snapshot_path=snapshot_path,
                    mod_name=mod_name,
                    workspace=workspace,
                    final_mod=final_mod,
                    translation_inputs=translation_inputs,
                    evidence_paths=review_evidence,
                    game_metadata=game_context_metadata(context),
                    metrics=metrics,
                    notes=notes,
                    source_root=context.plugin_root,
                )
            except (OSError, ValueError) as exc:
                snapshot_path.unlink(missing_ok=True)
                notes.append(f"Strict mechanical evidence was not cached: {exc}")

    report_success_metrics(
        root,
        mod_name,
        workspace,
        final_mod,
        report_path,
        args.strict_complete,
        issues,
        notes,
        metrics,
        translation_inputs,
        context,
    )
    blocking_count = sum(1 for issue in issues if issue.Severity == "error")
    warning_count = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Non-GUI QA gate report written to: {report_path}")
    print(f"Blocking issues: {blocking_count}")
    print(f"Warnings: {warning_count}")
    print(f"WarningPolicyBlocksCompletion: {bool(args.strict_complete)}")
    return 1 if blocking_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
