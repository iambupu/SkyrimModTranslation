"""Write the machine-readable workflow state from existing QA evidence.

This script does not translate, extract, write binaries, or rebuild final_mod.
It reads policy/readiness evidence and writes qa/workflow_state.json plus a
compact Markdown handoff report.
"""

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_capabilities import GUI_DESKTOP_CAPABILITY, KNOWN_AGENT_CAPABILITIES
from adapter_registry import require_adapter
from capability_resolver import resolve_capability, resolve_resource_capability
from game_context import GAME_METADATA_KEYS, game_context_metadata, game_display_label_from_metadata, game_metadata_mismatches
from game_context import GameContext
from model_review_contract import read_jsonl_objects as read_jsonl
from project_paths import (
    is_under,
    normalize_python_script_command,
    project_root,
    relative_path,
    resolve_project_path,
    resolve_workspace_or_plugin_path,
)
from route_translation_task import current_game_context
from workflow_progress import emit_from_qa_workflow_state
from workflow_refresh import core_refresh_commands
from workflow_issues import compact_issue_refs, issue_record_from_mapping, make_issue_record
from file_utils import discover_regular_files, read_json_object_or_invalid_any as read_json, sha256_file
from report_utils import markdown_cell
from report_utils import is_zero_metric as zero
from resource_model import classify_resource


STAGE_ORDER = [
    "discovered",
    "extracted",
    "routed",
    "candidates_extracted",
    "translated",
    "tool_outputs_generated",
    "final_mod_built",
    "packaged",
    "qa_pending_strict",
    "qa_passed",
    "ready_for_manual_test",
    "manual_tested",
]
READY_STATES = {"ready_for_manual_test", "manual_tested"}
BLOCKING_STATES = {"qa_failed", "blocked"}


@dataclass
class WorkflowIssue:
    severity: str
    area: str
    message: str
    evidence: str = ""


def has_files(path: Path) -> bool:
    return path.is_dir() and bool(
        discover_regular_files(path, label="Workflow state artifact directory")
    )


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default



def strict_gate_pending_value(value: Any) -> bool:
    normalized = str(value).strip().lower()
    return normalized in {"", "missing", "stale", "stale-final-mod-path"}


def strict_gate_failed_value(value: Any) -> bool:
    return not strict_gate_pending_value(value) and not zero(value)


def policy_stage(policy: dict[str, Any], state: str) -> dict[str, Any]:
    states = policy.get("states", {})
    if isinstance(states, dict) and isinstance(states.get(state), dict):
        return states[state]
    return {}


def allowed_scripts(policy: dict[str, Any], stage_policy: dict[str, Any]) -> list[str]:
    scripts: list[str] = []
    for value in policy.get("always_allowed_scripts", []):
        if isinstance(value, str) and value not in scripts:
            scripts.append(value)
    for value in policy.get("allowed_entrypoint_scripts", []):
        if isinstance(value, str) and value not in scripts:
            scripts.append(value)
    for value in stage_policy.get("allowed_scripts", []):
        if isinstance(value, str) and value not in scripts:
            scripts.append(value)
    for value in policy.get("allowed_leaf_scripts", []):
        if isinstance(value, str) and value not in scripts:
            scripts.append(value)
    return scripts


def stage_index(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return -1


def highest_stage(stages: list[str]) -> str:
    ordered = [stage for stage in stages if stage in STAGE_ORDER]
    if not ordered:
        return ""
    return max(ordered, key=stage_index)


def state_value(row: dict[str, Any]) -> str:
    return str(row.get("state", "")).strip()


def state_blockers(row: dict[str, Any]) -> list[str]:
    value = row.get("blocking_checks", [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def state_summary(states: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [row for row in states if state_value(row) in READY_STATES]
    blocking = [row for row in states if state_value(row) in BLOCKING_STATES or state_blockers(row)]
    in_progress = [
        row
        for row in states
        if row not in ready and row not in blocking and state_value(row) not in {"manual_tested"}
    ]
    return {
        "total": len(states),
        "ready": len(ready),
        "blocking": len(blocking),
        "in_progress": len(in_progress),
        "blocking_mods": [str(row.get("mod", "")) for row in blocking if str(row.get("mod", "")).strip()],
        "in_progress_mods": [str(row.get("mod", "")) for row in in_progress if str(row.get("mod", "")).strip()],
    }


def derive_project_state(states: list[dict[str, Any]], readiness_state: str) -> str:
    if not states:
        return "needs_input"
    values = [state_value(row) for row in states]
    if all(value == "needs_input" for value in values):
        return "needs_input"
    if any(value == "qa_failed" for value in values):
        return "qa_failed"
    if any(value == "blocked" or state_blockers(row) for value, row in zip(values, states)):
        return "blocked"
    if all(value == "manual_tested" for value in values):
        return "manual_tested"
    if all(value in READY_STATES for value in values):
        return "ready_for_manual_test"
    in_progress = [value for value in values if value not in READY_STATES]
    return highest_stage(in_progress) or readiness_state or values[0] or "needs_input"


def recommended_action_text(row: dict[str, Any], policy: dict[str, Any], state: str) -> str:
    next_action = str(row.get("NextRecommendedAction", "")).strip()
    if next_action:
        return normalize_python_script_command(next_action)
    stage = policy_stage(policy, state)
    return normalize_python_script_command(str(stage.get("recommended_command", "")).strip())


def recommended_stage_action(
    row: dict[str, Any],
    policy: dict[str, Any],
    state: str,
) -> dict[str, Any] | None:
    recommended = recommended_action_text(row, policy, state)
    if not recommended:
        return None
    if recommended.casefold().startswith("python "):
        return action(
            "run_command",
            "policy_recommended_action",
            command=recommended,
            risk="low",
        )
    return action(
        "manual_action",
        recommended,
        allowed=False,
        risk="manual",
    )


def agent_attempt_summary(root: Path, mod_name: str) -> tuple[int, dict[str, Any]]:
    rows = [row for row in read_jsonl(root / "qa" / "workflow_agent_runs.jsonl") if str(row.get("mod", "")).strip() == mod_name]
    completed_attempts = [
        row
        for row in rows
        if str(row.get("event", "")).strip() in {"command", "complete"}
        and str(row.get("status", "")).strip() in {"passed", "failed", "blocked"}
    ]
    last_attempts = [
        row
        for row in rows
        if str(row.get("event", "")).strip()
        in {"command", "complete", "smt_command"}
        and (
            str(row.get("status", "")).strip()
            in {"passed", "failed", "blocked"}
            or (
                str(row.get("event", "")).strip() == "smt_command"
                and str(row.get("status", "")).strip() == "skipped"
            )
        )
    ]
    retry_count = max(0, len(completed_attempts) - 1)
    return retry_count, (last_attempts[-1] if last_attempts else {})


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def action(
    kind: str,
    reason: str,
    *,
    path: str = "",
    command: str = "",
    allowed: bool = True,
    risk: str = "low",
    evidence: str = "",
    resource_locks: list[str] | None = None,
    dependencies: list[str] | None = None,
    can_run_parallel: bool | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": kind,
        "reason": reason,
        "path": path,
        "command": normalize_python_script_command(command),
        "allowed": allowed,
        "risk": risk,
        "evidence": evidence or path,
    }
    if resource_locks:
        result["resource_locks"] = resource_locks
    if dependencies:
        result["dependencies"] = dependencies
    if can_run_parallel is not None:
        result["can_run_parallel"] = can_run_parallel
    return result


def capability_request_for_command(command: str) -> tuple[str, str, str] | None:
    normalized = command.replace("\\", "/").casefold()
    inventory_mode = any(
        token in normalized
        for token in ('--mode inventory', '--mode "inventory"', '--mode=inventory')
    )
    verify_mode = any(token in normalized for token in ('--mode verify', '--mode "verify"', '--mode=verify'))
    export_mode = any(token in normalized for token in ('--mode export', '--mode "export"', '--mode=export'))
    if "invoke_bethesda_localized_delivery.py" in normalized:
        if inventory_mode:
            return "localized_delivery", "inventory", "inventory"
        if export_mode:
            return "localized_delivery", "read", "extract"
        return "localized_delivery", "write", "verify" if verify_mode else "apply"
    if "invoke_bethesda_string_table_tool.py" in normalized:
        if inventory_mode:
            return "string_tables", "inventory", "inventory"
        if export_mode:
            return "string_tables", "read", "extract"
        return "string_tables", "write", "verify" if verify_mode else "apply"
    if "invoke_mutagen_plugin_text_tool.py" in normalized:
        return "plugin_text", "write", "verify" if verify_mode else "apply"
    if "invoke_mutagen_pex_string_tool.py" in normalized:
        if export_mode:
            return "pex", "read", "extract"
        return "pex", "write", "verify" if verify_mode else "apply"
    if "invoke_bsa_file_extractor_safe.py" in normalized:
        return "archive.bsa", "read", "extract"
    if "invoke_ba2_extractor_safe.py" in normalized:
        return "archive.ba2", "read", "extract"
    if "new_bsa_archive_manifest.py" in normalized:
        return "archive.bsa", "inventory", "inventory"
    if "new_ba2_archive_manifest.py" in normalized:
        return "archive.ba2", "inventory", "inventory"
    return None


def add_capability_metadata(
    entry: dict[str, Any],
    context: GameContext,
) -> str:
    explicit_capability = str(entry.get("capability", "")).strip()
    explicit_operation = str(entry.get("operation", "")).strip()
    if explicit_capability or explicit_operation:
        if not explicit_capability or not explicit_operation:
            entry["allowed"] = False
            entry["error_code"] = "profile_error"
            return "capability:invalid-declaration:profile_error"
        adapter_operation = {
            "inventory": "inventory",
            "read": "extract",
            "write": "apply",
            "strict_complete": "",
        }.get(explicit_operation, "")
        request = (explicit_capability, explicit_operation, adapter_operation)
    else:
        request = capability_request_for_command(str(entry.get("command", "")))
    if request is None:
        return ""
    capability, operation, adapter_operation = request
    resource_path = str(entry.get("resource_path", "")).strip()
    try:
        if resource_path:
            resource = classify_resource(
                context,
                Path(resource_path),
                traits=frozenset(string_list(entry.get("resource_traits", []))),
            )
            if resource.capability != capability:
                raise ValueError(
                    f"Resource {resource_path!r} resolves capability {resource.capability!r}, "
                    f"not {capability!r}"
                )
            decision = resolve_resource_capability(context, resource, operation)
            entry.update(
                {
                    "resource_path": resource.relative_path.as_posix(),
                    "resource_category": resource.category,
                    "resource_subtype": resource.subtype,
                    "resource_container": resource.container,
                    "resource_traits": sorted(resource.traits),
                }
            )
        else:
            decision = resolve_capability(context, capability, operation)
    except ValueError:
        entry.update(
            {
                "capability": capability,
                "operation": operation,
                "adapter_id": "",
                "capability_level": "unsupported",
                "effective_level": "unsupported",
                "strict_complete_allowed": False,
                "supported": False,
                "allowed": False,
                "error_code": "profile_error",
            }
        )
        return f"capability:{capability}:{operation}:profile_error"
    entry.update(
        {
            "capability": capability,
            "operation": operation,
            "adapter_id": decision.adapter_id or "",
            "capability_level": decision.level,
            "effective_level": decision.level,
            "strict_complete_allowed": decision.strict_complete_allowed,
            "supported": decision.supported,
            "capability_reason": decision.reason,
        }
    )
    if not decision.supported or not decision.adapter_id:
        error_code = decision.error_code or "capability_unsupported"
        entry["allowed"] = False
        entry["error_code"] = error_code
        return f"capability:{capability}:{operation}:{error_code}"
    if adapter_operation:
        try:
            require_adapter(decision.adapter_id, adapter_operation)
        except ValueError:
            entry["allowed"] = False
            entry["error_code"] = "adapter_missing"
            return f"capability:{capability}:{operation}:adapter_missing"
    return ""


def next_actions_from_actions(
    row: dict[str, Any],
    context: GameContext,
) -> tuple[list[dict[str, Any]], list[str]]:
    actions: list[dict[str, Any]] = []
    blockers: list[str] = []
    refresh_after = core_refresh_commands()
    for source in ("repair_candidates", "recommended_actions"):
        values = row.get(source, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command", "")).strip()
            reason = str(item.get("reason", "")).strip()
            evidence = str(item.get("evidence", "") or item.get("path", "")).strip()
            if not command and not reason and not evidence:
                continue
            entry: dict[str, Any] = {
                "type": str(item.get("type", "")).strip(),
                "source": source,
                "command": normalize_python_script_command(command),
                "risk": str(item.get("risk", "")).strip() or "low",
                "reason": reason,
                "evidence": evidence,
                "allowed": bool(item.get("allowed", True)),
                "refresh_after": refresh_after,
            }
            for key in (
                "capability",
                "operation",
                "resource_path",
                "resource_category",
                "resource_subtype",
                "resource_container",
            ):
                if key in item:
                    entry[key] = str(item.get(key, "")).strip()
            if "resource_traits" in item:
                entry["resource_traits"] = string_list(item.get("resource_traits", []))
            resource_locks = string_list(item.get("resource_locks", []))
            dependencies = string_list(item.get("dependencies", []))
            if resource_locks:
                entry["resource_locks"] = resource_locks
            if dependencies:
                entry["dependencies"] = dependencies
            if "can_run_parallel" in item:
                entry["can_run_parallel"] = bool(item.get("can_run_parallel"))
            required_agent_capability = str(
                item.get("required_agent_capability", "")
            ).strip()
            normalized_command = entry["command"].replace("\\", "/").casefold()
            if not required_agent_capability and (
                "gui:desktop" in resource_locks
                or any(
                    script_name in normalized_command
                    for script_name in {
                    "automate-lextranslator-gui.py",
                    "invoke_lextranslator.py",
                    "invoke_lextranslator_gui.py",
                    "invoke_xtranslator.py",
                    }
                )
            ):
                required_agent_capability = GUI_DESKTOP_CAPABILITY
            if required_agent_capability:
                entry["required_agent_capability"] = required_agent_capability
                if required_agent_capability not in KNOWN_AGENT_CAPABILITIES:
                    entry["allowed"] = False
                    entry["error_code"] = "agent_capability_unknown"
                    blockers.append(
                        f"agent_capability:{required_agent_capability}:unknown"
                    )
            blocker = add_capability_metadata(entry, context)
            if blocker:
                blockers.append(blocker)
            actions.append(entry)
    return actions, sorted(set(blockers))


def stop_conditions_for_state(state: str, blockers: list[str]) -> list[str]:
    stops = [
        "unsafe_path",
        "manual_game_test_required",
        "gui_save_blocked",
        "unverified_plugin_output",
        "unverified_pex_output",
        "bsa_repack_required_without_adapter",
        "ba2_extraction_required_without_adapter",
    ]
    if state == "qa_failed":
        stops.extend(["semantic_quality_requires_model_review", "high_risk_binary_writeback_requires_adapter"])
    if any("model_review" in blocker for blocker in blockers):
        stops.append("model_review_required")
    if any("final_review_quality" in blocker for blocker in blockers):
        stops.append("semantic_quality_requires_model_review")
    if any(blocker.startswith("capability:") for blocker in blockers):
        stops.append("capability_or_adapter_unavailable")
    return sorted(set(stops))


def orchestration_fields(
    root: Path,
    mod_name: str,
    state: str,
    last_success: str,
    blockers: list[str],
    row: dict[str, Any],
    workspace: Path | None = None,
    final_mod: Path | None = None,
) -> dict[str, Any]:
    retry_count, last_attempt = agent_attempt_summary(root, mod_name)
    recommended_actions: list[dict[str, Any]] = []
    repair_candidates: list[dict[str, Any]] = []

    if state in {"qa_failed", "blocked"} or blockers:
        recommended_actions.extend(
            [
                action("inspect_report", "strict_gate", path=f"qa/{mod_name}.non_gui_qa_gates.md", risk="low"),
                action("inspect_report", "final_mod_validation", path="qa/final_mod_validation.md", risk="low"),
                action("inspect_report", "final_review_quality", path=f"qa/{mod_name}.final_review_quality.md", risk="semantic"),
                action("inspect_report", "model_review", path=f"qa/{mod_name}.model_review.md", risk="semantic"),
            ]
        )

    if final_mod and final_mod.is_dir() and not (final_mod / "meta" / "provenance.jsonl").is_file():
        repair_candidates.append(
            action(
                "repair_candidate",
                "provenance_missing",
                command=f'python scripts/build_final_mod.py --mod-name "{mod_name}" --source-mod-dir "{relative_path(root, workspace or (root / f"work/extracted_mods/{mod_name}"))}" --force',
                evidence="qa/final_mod_validation.md",
                risk="low",
            )
        )

    if "chs_package_missing" in blockers and workspace:
        repair_candidates.append(
            action(
                "repair_candidate",
                "chs_package_missing",
                command=f'python scripts/build_final_mod.py --mod-name "{mod_name}" --source-mod-dir "{relative_path(root, workspace)}" --force',
                evidence=f"out/{mod_name}/汉化产出/package_report.md",
                risk="low",
            )
        )

    if "strict_gate_not_clean" in blockers:
        repair_candidates.append(
            action(
                "verify_after_repair",
                "strict_gate_not_clean",
                command=f'python scripts/run_non_gui_qa_gates.py --mod-name "{mod_name}" --workspace-path "work/extracted_mods/{mod_name}" --final-mod-dir "out/{mod_name}/汉化产出/final_mod" --strict-complete --reuse-mechanical-evidence',
                evidence=f"qa/{mod_name}.non_gui_qa_gates.md",
                risk="low",
            )
        )

    if "final_review_quality_not_passed" in blockers:
        repair_candidates.append(
            action(
                "needs_model_judgment",
                "final_review_quality_not_passed",
                path=f"qa/{mod_name}.final_review_quality.md",
                allowed=True,
                risk="semantic",
                evidence=f"qa/{mod_name}.final_review_quality.md",
            )
        )

    if str(row.get("PackageValidationStatus", "")) not in {"", "passed"} or not zero(row.get("PackageValidationBlockingIssues", "")):
        repair_candidates.append(
            action(
                "repair_candidate",
                "package_validation_not_clean",
                command=f'python scripts/validate_chs_package.py --mod-name "{mod_name}"',
                evidence=f"qa/{mod_name}.chs_package_validation.md",
                risk="low",
            )
        )

    recommended_actions.append(
        action(
            "refresh_state",
            "refresh_translation_readiness_after_any_action",
            command="python scripts/audit_translation_readiness.py",
            risk="low",
        )
    )
    recommended_actions.append(
        action(
            "refresh_state",
            "refresh_workflow_state_after_readiness",
            command="python scripts/write_workflow_state.py",
            risk="low",
        )
    )

    return {
        "recommended_actions": recommended_actions,
        "repair_candidates": repair_candidates,
        "stop_conditions": stop_conditions_for_state(state, blockers),
        "retry_count": retry_count,
        "last_attempt": last_attempt,
    }


def manual_tested_mods(root: Path) -> set[str]:
    payload = read_json(root / "qa" / "manual_game_test_results_validation.json")
    if str(payload.get("Status", "")) != "passed" or not zero(payload.get("BlockingIssues", "")):
        return set()
    rows = payload.get("Rows", [])
    if not isinstance(rows, list):
        return set()
    mods: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("ValidationStatus", "")) == "passed":
            mod_name = str(row.get("ModName", "")).strip()
            if mod_name:
                mods.add(mod_name)
    return mods


def infer_output_state(
    root: Path,
    policy: dict[str, Any],
    row: dict[str, Any],
    tested_mods: set[str],
    context: GameContext,
    readiness_blockers: list[str],
) -> dict[str, Any]:
    mod_name = str(row.get("ModName", "")).strip()
    workspace = root / str(row.get("Workspace", ""))
    final_mod = root / str(row.get("FinalModDir", ""))
    package_path = root / str(row.get("PackagedModPath", ""))
    tool_outputs = root / "out" / mod_name / "tool_outputs"
    translated_tool_outputs = root / "translated" / "tool_outputs" / mod_name
    translated_root = root / "translated"
    source_root = root / "source"
    normalized_root = root / "work" / "normalized"

    successful: list[str] = []
    blockers = list(readiness_blockers)
    evidence: dict[str, Any] = {
        "workspace": str(row.get("Workspace", "")),
        "final_mod": str(row.get("FinalModDir", "")),
        "package": str(row.get("PackagedModPath", "")),
        "overall_status": str(row.get("OverallStatus", "")),
        "model_review": str(row.get("ModelReviewStatus", "")),
        "package_validation": str(row.get("PackageValidationStatus", "")),
        "translation_dictionary_entries": str(row.get("TranslationDictionaryEntries", "")),
        "used_capabilities": str(row.get("UsedCapabilitiesPath", "")),
        "used_capabilities_status": str(row.get("UsedCapabilitiesStatus", "")),
        "localized_delivery_status": str(
            row.get("StringTableDeliveryStatus", "not_used")
        ),
        "plugin_stage": str(row.get("PluginStagePath", "")),
        "plugin_stage_status": str(row.get("PluginStageStatus", "")),
        "plugin_stage_blocking": str(row.get("PluginStageBlockingIssues", "")),
    }

    if workspace.is_dir():
        successful.append("extracted")

    if (root / "qa" / "routing_report.md").is_file() or workspace.is_dir():
        successful.append("routed")

    if has_files(source_root / "plugin_exports" / mod_name) or has_files(source_root / "pex_exports" / mod_name) or has_files(normalized_root / mod_name):
        successful.append("candidates_extracted")

    if (
        has_files(translated_root / "plugin_exports" / mod_name)
        or has_files(translated_root / "lextranslator_ready" / mod_name)
        or has_files(translated_root / "final_mod" / mod_name)
        or to_int(row.get("TranslationDictionaryEntries", 0)) > 0
    ):
        successful.append("translated")

    if has_files(tool_outputs) or has_files(translated_tool_outputs):
        successful.append("tool_outputs_generated")

    if final_mod.is_dir():
        successful.append("final_mod_built")

    package_status = str(row.get("PackageValidationStatus", ""))
    package_validation_clean = package_status == "passed" and zero(row.get("PackageValidationBlockingIssues", ""))
    if package_path.is_file() and final_mod.is_dir() and package_validation_clean:
        successful.append("packaged")

    strict_gate_blocking = str(row.get("StrictGateBlockingIssues", "")).strip()
    strict_gate_warnings = str(row.get("StrictGateWarnings", "")).strip()
    strict_gate_pending = strict_gate_pending_value(strict_gate_blocking) or strict_gate_pending_value(strict_gate_warnings)
    strict_gate_failed = strict_gate_failed_value(strict_gate_blocking) or strict_gate_failed_value(strict_gate_warnings)
    readiness_state = str(row.get("OverallStatus", ""))
    blockers_sorted = sorted(set(item for item in blockers if item))
    if readiness_state == "ready_for_manual_test" and not blockers_sorted:
        successful.append("qa_passed")
        successful.append("ready_for_manual_test")
    elif strict_gate_pending and not strict_gate_failed and blockers_sorted == ["strict_gate_not_clean"] and final_mod.is_dir() and package_path.is_file():
        readiness_state = "qa_pending_strict"
        successful.append("qa_pending_strict")
    elif final_mod.is_dir() and blockers_sorted:
        readiness_state = "qa_failed"

    if mod_name in tested_mods:
        successful.append("manual_tested")
        readiness_state = "manual_tested"

    last_success = highest_stage(successful)
    state = readiness_state if readiness_state in {"qa_failed", "qa_pending_strict", "ready_for_manual_test", "manual_tested"} else last_success
    if not state:
        state = "blocked" if blockers else "discovered"

    stage_for_policy = state if state in STAGE_ORDER else last_success or "discovered"
    stage_policy = policy_stage(policy, stage_for_policy)

    result = {
        "mod": mod_name,
        "state": state,
        "last_success_stage": last_success,
        "blocking_checks": blockers_sorted,
        "allowed_scripts": allowed_scripts(policy, stage_policy),
        "required_files": stage_policy.get("required_files", []),
        "evidence": evidence,
    }
    result.update(orchestration_fields(root, mod_name, state, last_success, blockers_sorted, row, workspace, final_mod))
    preferred = recommended_stage_action(row, policy, state)
    if preferred is not None:
        result["recommended_actions"].insert(0, preferred)
    next_actions, capability_blockers = next_actions_from_actions(result, context)
    result["next_actions"] = next_actions
    if capability_blockers:
        result["blocking_checks"] = sorted(set([*result["blocking_checks"], *capability_blockers]))
        if result["state"] not in {"qa_failed", "blocked"}:
            result["state"] = "blocked"
        result["stop_conditions"] = stop_conditions_for_state(
            result["state"], result["blocking_checks"]
        )
    return result


def infer_input_state(
    root: Path,
    policy: dict[str, Any],
    row: dict[str, Any],
    context: GameContext,
) -> dict[str, Any]:
    state = "discovered"
    stage_policy = policy_stage(policy, state)
    mod_name = str(row.get("LikelyModName", "")).strip()
    retry_count, last_attempt = agent_attempt_summary(root, mod_name)
    result = {
        "mod": mod_name,
        "state": state,
        "last_success_stage": state,
        "blocking_checks": [],
        "allowed_scripts": allowed_scripts(policy, stage_policy),
        "required_files": stage_policy.get("required_files", []),
        "evidence": {
            "input": str(row.get("Path", "")),
            "kind": str(row.get("Kind", "")),
            "route_skill": str(row.get("RouteSkill", "")),
            "primary_tool": str(row.get("PrimaryTool", "")),
            "risk": str(row.get("Risk", "")),
        },
        "recommended_actions": [
            recommended_stage_action(row, policy, state)
            or action("needs_input", "prepare_discovered_input", allowed=False, risk="manual"),
            action("refresh_state", "refresh_translation_readiness_after_any_action", command="python scripts/audit_translation_readiness.py", risk="low"),
            action("refresh_state", "refresh_workflow_state_after_readiness", command="python scripts/write_workflow_state.py", risk="low"),
        ],
        "repair_candidates": [],
        "stop_conditions": stop_conditions_for_state(state, []),
        "retry_count": retry_count,
        "last_attempt": last_attempt,
    }
    next_actions, capability_blockers = next_actions_from_actions(result, context)
    result["next_actions"] = next_actions
    if capability_blockers:
        result["blocking_checks"] = capability_blockers
        result["state"] = "blocked"
        result["stop_conditions"] = stop_conditions_for_state(
            result["state"], result["blocking_checks"]
        )
    return result


def build_state(root: Path, policy_path: Path, readiness_path: Path) -> tuple[dict[str, Any], list[WorkflowIssue]]:
    issues: list[WorkflowIssue] = []
    context = current_game_context(root)
    policy = read_json(policy_path)
    readiness = read_json(readiness_path)
    if not policy or policy.get("_invalid_json"):
        issues.append(WorkflowIssue("error", "policy", "workflow policy is missing or invalid JSON", relative_path(root, policy_path)))
        policy = {}
    if not readiness or readiness.get("_invalid_json"):
        issues.append(WorkflowIssue("warning", "readiness", "translation readiness is missing or invalid; state will be partial", relative_path(root, readiness_path)))
        readiness = {}
    readiness_mismatches = game_metadata_mismatches(readiness, context) if readiness else []
    if readiness_mismatches:
        issues.append(
            WorkflowIssue(
                "error",
                "game_identity_mismatch",
                f"translation readiness game metadata mismatch: {'; '.join(readiness_mismatches)}",
                relative_path(root, readiness_path),
            )
        )

    tested_mods = manual_tested_mods(root)
    output_rows = [row for row in readiness.get("KnownModOutputs", []) if isinstance(row, dict)]
    input_rows = [row for row in readiness.get("ModInputs", []) if isinstance(row, dict)]
    readiness_issue_records = [
        issue_record_from_mapping(raw, default_reporter="translation_readiness")
        for raw in readiness.get("Issues", [])
        if isinstance(raw, dict)
    ]

    def blockers_for_mod(mod_name: str) -> list[str]:
        return sorted(
            {
                str(record.get("code", ""))
                for record in readiness_issue_records
                if str(record.get("severity", "")) == "error"
                and (
                    not str(record.get("mod_name", "")).strip()
                    or str(record.get("mod_name", "")).casefold() == mod_name.casefold()
                )
                and str(record.get("code", "")).strip()
            }
        )

    states: list[dict[str, Any]] = []
    output_mods = {str(row.get("ModName", "")).strip() for row in output_rows}
    for row in output_rows:
        mod_name = str(row.get("ModName", "")).strip()
        states.append(
            infer_output_state(
                root,
                policy,
                row,
                tested_mods,
                context,
                blockers_for_mod(mod_name),
            )
        )
    for row in input_rows:
        mod_name = str(row.get("LikelyModName", "")).strip()
        if mod_name and mod_name not in output_mods:
            states.append(infer_input_state(root, policy, row, context))

    if not states:
        states.append(
            {
                "mod": "",
                "state": "needs_input",
                "last_success_stage": "",
                "blocking_checks": ["no_actionable_mod_input"],
                "allowed_scripts": allowed_scripts(policy, {}),
                "required_files": ["mod/<input>"],
                "evidence": {},
                "recommended_actions": [
                    action("needs_input", "place_project_local_mod_input_under_mod", risk="manual", allowed=False)
                ],
                "repair_candidates": [],
                "stop_conditions": ["no_actionable_mod_input"],
                "retry_count": 0,
                "last_attempt": {},
                "next_actions": [],
            }
        )

    if readiness_mismatches:
        for row in states:
            blockers = string_list(row.get("blocking_checks", []))
            row["blocking_checks"] = sorted(set(blockers + ["game_identity_mismatch"]))
            row["state"] = "blocked"
            row["stop_conditions"] = sorted(
                set(string_list(row.get("stop_conditions", [])) + ["game_identity_mismatch"])
            )

    state_issue_records = [
        issue_record_from_mapping(asdict(issue), default_reporter="workflow_state")
        for issue in issues
    ]
    all_issue_records = [*readiness_issue_records, *state_issue_records]
    for row in states:
        mod_name = str(row.get("mod", "")).strip()
        matching = [
            record
            for record in readiness_issue_records
            if str(record.get("severity", "")) == "error"
            and (
                not str(record.get("mod_name", "")).strip()
                or str(record.get("mod_name", "")).casefold() == mod_name.casefold()
            )
        ]
        matched_codes = {str(record.get("code", "")) for record in matching}
        for code in state_blockers(row):
            if code in matched_codes:
                continue
            artifact = f"qa/workflow_state.json#{mod_name or 'project'}:{code}"
            record = make_issue_record(
                code=code,
                mod_name=mod_name,
                affected_artifact=artifact,
                severity="error",
                message="",
                evidence_paths=[artifact],
                reported_by=["workflow_state"],
            )
            matching.append(record)
            all_issue_records.append(record)
        row["blocking_issues"] = compact_issue_refs(matching)

    readiness_state = str(readiness.get("OverallStatus", "") or "")
    project_state = derive_project_state(states, readiness_state)
    summary = state_summary(states)

    payload = {
        **game_context_metadata(context),
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "policy_path": relative_path(root, policy_path),
        "policy_sha256": sha256_file(policy_path) if policy_path.is_file() else "",
        "project_state": project_state,
        "readiness_overall_status": readiness_state,
        "state_summary": summary,
        "states": states,
        "issues": compact_issue_refs(all_issue_records),
    }
    return payload, issues


def validate_state_shape(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in (*GAME_METADATA_KEYS, "schema_version", "generated_at", "policy_path", "policy_sha256", "states"):
        if key not in payload:
            errors.append(f"missing top-level key: {key}")
    states = payload.get("states")
    if not isinstance(states, list) or not states:
        errors.append("states must be a non-empty array")
        return errors
    required = {
        "mod",
        "state",
        "last_success_stage",
        "blocking_checks",
        "blocking_issues",
        "allowed_scripts",
        "required_files",
        "evidence",
        "recommended_actions",
        "repair_candidates",
        "stop_conditions",
        "retry_count",
        "last_attempt",
        "next_actions",
    }
    for index, row in enumerate(states):
        if not isinstance(row, dict):
            errors.append(f"states[{index}] is not an object")
            continue
        missing = sorted(required - set(row))
        if missing:
            errors.append(f"states[{index}] missing keys: {', '.join(missing)}")
    return errors


def validate_state_schema_contract(payload: dict[str, Any], schema_path: Path) -> list[str]:
    """Validate the generated top-level state against the checked-in schema."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"workflow state schema could not be read: {exc}"]
    if not isinstance(schema, dict):
        return ["workflow state schema root must be an object"]
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return ["workflow state schema must declare object properties and required keys"]

    errors: list[str] = []
    for key in required:
        if isinstance(key, str) and key not in payload:
            errors.append(f"schema required key missing: {key}")
    if schema.get("additionalProperties") is False:
        for key in sorted(set(payload) - set(properties)):
            errors.append(f"schema does not declare generated key: {key}")

    type_checks = {
        "array": list,
        "boolean": bool,
        "integer": int,
        "object": dict,
        "string": str,
    }
    for key, value in payload.items():
        definition = properties.get(key)
        if not isinstance(definition, dict):
            continue
        expected_name = definition.get("type")
        expected_type = type_checks.get(expected_name)
        if expected_type is None:
            continue
        if expected_name == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, expected_type)
        if not valid:
            errors.append(f"schema type mismatch for {key}: expected {expected_name}")
    return errors



def write_reports(root: Path, payload: dict[str, Any], json_path: Path, report_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Workflow State",
        "",
        f"- game_id: {payload.get('game_id', '')}",
        f"- Game: {game_display_label_from_metadata(payload)}",
        f"- Support level: {payload.get('support_level', '')}",
        f"- Generated at: {payload['generated_at']}",
        f"- Project state: {payload.get('project_state', '')}",
        f"- Readiness overall status: {payload.get('readiness_overall_status', '')}",
        f"- Policy: {payload.get('policy_path', '')}",
        "",
        "## State Summary",
        "",
    ]
    summary = payload.get("state_summary", {})
    if isinstance(summary, dict):
        lines.extend(
            [
                f"- Total Mod states: {summary.get('total', 0)}",
                f"- Ready states: {summary.get('ready', 0)}",
                f"- Blocking states: {summary.get('blocking', 0)}",
                f"- In-progress states: {summary.get('in_progress', 0)}",
                f"- Blocking Mods: {', '.join(summary.get('blocking_mods', [])) if isinstance(summary.get('blocking_mods'), list) else ''}",
                f"- In-progress Mods: {', '.join(summary.get('in_progress_mods', [])) if isinstance(summary.get('in_progress_mods'), list) else ''}",
                "",
            ]
        )
    lines.extend(
        [
        "## Mod States",
        "",
        "| Mod | State | Last success stage | Blocking checks | Retry count |",
        "|---|---|---|---|---:|",
        ]
    )
    for row in payload.get("states", []):
        blockers = ", ".join(row.get("blocking_checks", [])) if isinstance(row, dict) else ""
        lines.append(
            f"| {markdown_cell(row.get('mod', ''))} | {markdown_cell(row.get('state', ''))} | "
            f"{markdown_cell(row.get('last_success_stage', ''))} | {markdown_cell(blockers or 'none')} | "
            f"{row.get('retry_count', 0)} |"
        )
    lines.extend(["", "## Agent Orchestration", ""])
    lines.extend(["| Mod | Recommended actions | Repair candidates | Stop conditions | Last attempt |", "|---|---:|---:|---|---|"])
    for row in payload.get("states", []):
        if not isinstance(row, dict):
            continue
        last_attempt = row.get("last_attempt") if isinstance(row.get("last_attempt"), dict) else {}
        last_summary = ""
        if last_attempt:
            last_summary = f"{last_attempt.get('timestamp', '')} {last_attempt.get('event', '')}/{last_attempt.get('status', '')}"
        lines.append(
            f"| {markdown_cell(row.get('mod', ''))} | {len(row.get('recommended_actions', []))} | "
            f"{len(row.get('repair_candidates', []))} | {markdown_cell(', '.join(row.get('stop_conditions', [])))} | "
            f"{markdown_cell(last_summary or 'none')} |"
        )
    lines.extend(["", "## Issues", ""])
    issues = payload.get("issues", [])
    if not issues:
        lines.append("No workflow state issues.")
    else:
        lines.extend(["| Issue ID | Blocking code |", "|---|---|"])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            lines.append(
                f"| {markdown_cell(issue.get('issue_id', ''))} | {markdown_cell(issue.get('code', ''))} |"
            )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This report is generated from project-local QA/readiness evidence.",
            "- This script does not translate, extract, write plugin/PEX binaries, rebuild final_mod, or access real game/mod-manager paths.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write qa/workflow_state.json from policy and readiness evidence.")
    parser.add_argument("--policy-path", default="config/workflow_policy.json")
    parser.add_argument("--readiness-json-path", default="qa/translation_readiness.json")
    parser.add_argument("--json-output-path", default="qa/workflow_state.json")
    parser.add_argument("--report-output-path", default="qa/workflow_state.md")
    args = parser.parse_args()

    root = project_root()
    policy_path = resolve_workspace_or_plugin_path(root, args.policy_path, must_exist=False)
    readiness_path = resolve_project_path(root, args.readiness_json_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(json_path, qa_root) or not is_under(report_path, qa_root):
        raise ValueError("Workflow state outputs must be under qa/.")

    payload, issues = build_state(root, policy_path, readiness_path)
    shape_errors = validate_state_shape(payload)
    schema_path = resolve_workspace_or_plugin_path(root, "config/workflow_state.schema.json", must_exist=True)
    shape_errors.extend(validate_state_schema_contract(payload, schema_path))
    for error in shape_errors:
        issues.append(WorkflowIssue("error", "schema", error, "config/workflow_state.schema.json"))
    payload["issues"] = [asdict(issue) for issue in issues]
    write_reports(root, payload, json_path, report_path)
    progress_warning = ""
    try:
        emit_from_qa_workflow_state(root, payload)
    except Exception as exc:
        progress_warning = str(exc)

    blocking = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    print(f"Workflow state JSON written to: {json_path}")
    print(f"Workflow state report written to: {report_path}")
    if progress_warning:
        print(f"Progress card warning: {progress_warning}")
        warnings += 1
    else:
        print("Progress card written to: .workflow/progress_card.md")
    print(f"Project state: {payload.get('project_state', '')}")
    if progress_warning:
        blocking += 1
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
