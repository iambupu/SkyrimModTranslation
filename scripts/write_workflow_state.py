"""Write the machine-readable workflow state from existing QA evidence.

This script does not translate, extract, write binaries, or rebuild final_mod.
It reads policy/readiness evidence and writes qa/workflow_state.json plus a
compact Markdown handoff report.
"""

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import (
    is_under,
    normalize_python_script_command,
    project_root,
    relative_path,
    resolve_project_path,
    resolve_workspace_or_plugin_path,
)
from workflow_progress import emit_from_qa_workflow_state


STAGE_ORDER = [
    "discovered",
    "extracted",
    "routed",
    "candidates_extracted",
    "translated",
    "tool_outputs_generated",
    "final_mod_built",
    "packaged",
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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_text(path))
    except Exception:
        return {"_invalid_json": True}
    return payload if isinstance(payload, dict) else {"_invalid_json": True}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def zero(value: Any) -> bool:
    return str(value).strip() in {"0", "0.0"}


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


def derive_next_command(states: list[dict[str, Any]], readiness_next: str) -> str:
    for row in states:
        if state_value(row) in BLOCKING_STATES or state_blockers(row):
            command = str(row.get("next_command", "")).strip()
            if command:
                return normalize_python_script_command(command)
    for row in states:
        if state_value(row) not in READY_STATES:
            command = str(row.get("next_command", "")).strip()
            if command:
                return normalize_python_script_command(command)
    return normalize_python_script_command(readiness_next or str(states[0].get("next_command", "") if states else ""))


def command_or_policy(row: dict[str, Any], policy: dict[str, Any], state: str) -> str:
    next_action = str(row.get("NextRecommendedAction", "")).strip()
    if next_action:
        return normalize_python_script_command(next_action)
    stage = policy_stage(policy, state)
    return normalize_python_script_command(str(stage.get("next_command", "")).strip())


def agent_attempt_summary(root: Path, mod_name: str) -> tuple[int, dict[str, Any]]:
    rows = [row for row in read_jsonl(root / "qa" / "workflow_agent_runs.jsonl") if str(row.get("mod", "")).strip() == mod_name]
    return len(rows), (rows[-1] if rows else {})


def action(kind: str, reason: str, *, path: str = "", command: str = "", allowed: bool = True, risk: str = "low", evidence: str = "") -> dict[str, Any]:
    return {
        "type": kind,
        "reason": reason,
        "path": path,
        "command": normalize_python_script_command(command),
        "allowed": allowed,
        "risk": risk,
        "evidence": evidence or path,
    }


def next_actions_from_actions(row: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    refresh_after = [
        normalize_python_script_command("python scripts/audit_translation_readiness.py"),
        normalize_python_script_command("python scripts/write_workflow_state.py"),
        normalize_python_script_command("python scripts/write_workflow_tasks.py"),
        normalize_python_script_command("python scripts/write_codex_handoff.py"),
    ]
    for source in ("repair_candidates", "recommended_actions"):
        values = row.get(source, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command", "")).strip()
            if not command:
                continue
            actions.append(
                {
                    "type": str(item.get("type", "")).strip(),
                    "source": source,
                    "command": normalize_python_script_command(command),
                    "risk": str(item.get("risk", "")).strip() or "low",
                    "reason": str(item.get("reason", "")).strip(),
                    "evidence": str(item.get("evidence", "") or item.get("path", "")).strip(),
                    "allowed": bool(item.get("allowed", True)),
                    "refresh_after": refresh_after,
                }
            )
    return actions


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
                command=f'python scripts/run_non_gui_qa_gates.py --mod-name "{mod_name}" --workspace-path "work/extracted_mods/{mod_name}" --final-mod-dir "out/{mod_name}/汉化产出/final_mod" --strict-complete',
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


def infer_output_state(root: Path, policy: dict[str, Any], row: dict[str, Any], tested_mods: set[str]) -> dict[str, Any]:
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
    blockers: list[str] = []
    evidence: dict[str, Any] = {
        "workspace": str(row.get("Workspace", "")),
        "final_mod": str(row.get("FinalModDir", "")),
        "package": str(row.get("PackagedModPath", "")),
        "overall_status": str(row.get("OverallStatus", "")),
        "model_review": str(row.get("ModelReviewStatus", "")),
        "package_validation": str(row.get("PackageValidationStatus", "")),
        "translation_dictionary_entries": str(row.get("TranslationDictionaryEntries", "")),
    }

    if workspace.is_dir():
        successful.append("extracted")
    else:
        blockers.append("workspace_missing")

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
        if not (final_mod / "meta" / "provenance.jsonl").is_file():
            blockers.append("provenance_missing")
    else:
        blockers.append("final_mod_missing")

    package_status = str(row.get("PackageValidationStatus", ""))
    package_validation_clean = package_status == "passed" and zero(row.get("PackageValidationBlockingIssues", ""))
    if not package_path.is_file():
        blockers.append("chs_package_missing")
    elif final_mod.is_dir() and package_validation_clean:
        successful.append("packaged")

    if package_status and package_status != "passed":
        blockers.append(f"package_validation_{package_status}")
    if not zero(row.get("PackageValidationBlockingIssues", "")):
        blockers.append("package_validation_blocking")

    if not zero(row.get("StrictGateBlockingIssues", "")) or not zero(row.get("StrictGateWarnings", "")):
        blockers.append("strict_gate_not_clean")
    if not zero(row.get("CoverageMissing", "")):
        blockers.append("coverage_missing")
    if not zero(row.get("CoverageUnverified", "")):
        blockers.append("coverage_unverified")
    if not zero(row.get("FinalTextProtectedItems", "")):
        blockers.append("final_text_protected_items")
    if not zero(row.get("FinalBinaryProtectedItems", "")):
        blockers.append("final_binary_protected_items")
    if not zero(row.get("FinalBinaryExportFailures", "")):
        blockers.append("final_binary_export_failures")
    if str(row.get("FinalReviewQualityStatus", "")) not in {"", "passed"}:
        blockers.append("final_review_quality_not_passed")
    if str(row.get("ModelReviewStatus", "")) not in {"", "passed"}:
        blockers.append(f"model_review_{row.get('ModelReviewStatus', '')}")

    readiness_state = str(row.get("OverallStatus", ""))
    if readiness_state == "ready_for_manual_test" and not blockers:
        successful.append("qa_passed")
        successful.append("ready_for_manual_test")
    elif final_mod.is_dir() and blockers:
        readiness_state = "qa_failed"

    if mod_name in tested_mods:
        successful.append("manual_tested")
        readiness_state = "manual_tested"

    last_success = highest_stage(successful)
    state = readiness_state if readiness_state in {"qa_failed", "ready_for_manual_test", "manual_tested"} else last_success
    if not state:
        state = "blocked" if blockers else "discovered"

    stage_for_policy = state if state in STAGE_ORDER else last_success or "discovered"
    stage_policy = policy_stage(policy, stage_for_policy)

    blockers_sorted = sorted(set(item for item in blockers if item))
    result = {
        "mod": mod_name,
        "state": state,
        "last_success_stage": last_success,
        "blocking_checks": blockers_sorted,
        "next_command": command_or_policy(row, policy, state),
        "allowed_scripts": allowed_scripts(policy, stage_policy),
        "required_files": stage_policy.get("required_files", []),
        "evidence": evidence,
    }
    result.update(orchestration_fields(root, mod_name, state, last_success, blockers_sorted, row, workspace, final_mod))
    result["next_actions"] = next_actions_from_actions(result)
    return result


def infer_input_state(root: Path, policy: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    state = "discovered"
    stage_policy = policy_stage(policy, state)
    mod_name = str(row.get("LikelyModName", "")).strip()
    retry_count, last_attempt = agent_attempt_summary(root, mod_name)
    result = {
        "mod": mod_name,
        "state": state,
        "last_success_stage": state,
        "blocking_checks": [],
        "next_command": str(row.get("RecommendedCommand", "")).strip() or str(stage_policy.get("next_command", "")),
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
            action("run_command", "prepare_discovered_input", command=str(row.get("RecommendedCommand", "")).strip(), risk="low"),
            action("refresh_state", "refresh_translation_readiness_after_any_action", command="python scripts/audit_translation_readiness.py", risk="low"),
            action("refresh_state", "refresh_workflow_state_after_readiness", command="python scripts/write_workflow_state.py", risk="low"),
        ],
        "repair_candidates": [],
        "stop_conditions": stop_conditions_for_state(state, []),
        "retry_count": retry_count,
        "last_attempt": last_attempt,
    }
    result["next_actions"] = next_actions_from_actions(result)
    return result


def build_state(root: Path, policy_path: Path, readiness_path: Path) -> tuple[dict[str, Any], list[WorkflowIssue]]:
    issues: list[WorkflowIssue] = []
    policy = read_json(policy_path)
    readiness = read_json(readiness_path)
    if not policy or policy.get("_invalid_json"):
        issues.append(WorkflowIssue("error", "policy", "workflow policy is missing or invalid JSON", relative_path(root, policy_path)))
        policy = {}
    if not readiness or readiness.get("_invalid_json"):
        issues.append(WorkflowIssue("warning", "readiness", "translation readiness is missing or invalid; state will be partial", relative_path(root, readiness_path)))
        readiness = {}

    tested_mods = manual_tested_mods(root)
    output_rows = [row for row in readiness.get("KnownModOutputs", []) if isinstance(row, dict)]
    input_rows = [row for row in readiness.get("ModInputs", []) if isinstance(row, dict)]

    states: list[dict[str, Any]] = []
    output_mods = {str(row.get("ModName", "")).strip() for row in output_rows}
    for row in output_rows:
        states.append(infer_output_state(root, policy, row, tested_mods))
    for row in input_rows:
        mod_name = str(row.get("LikelyModName", "")).strip()
        if mod_name and mod_name not in output_mods:
            states.append(infer_input_state(root, policy, row))

    if not states:
        states.append(
            {
                "mod": "",
                "state": "needs_input",
                "last_success_stage": "",
                "blocking_checks": ["no_actionable_mod_input"],
                "next_command": "Place a sandboxed Mod archive or directory under mod/.",
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

    readiness_state = str(readiness.get("OverallStatus", "") or "")
    readiness_next = str(readiness.get("NextRecommendedAction", "") or "")
    project_state = derive_project_state(states, readiness_state)
    summary = state_summary(states)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "policy_path": relative_path(root, policy_path),
        "policy_sha256": sha256_file(policy_path) if policy_path.is_file() else "",
        "project_state": project_state,
        "readiness_overall_status": readiness_state,
        "state_summary": summary,
        "next_command": derive_next_command(states, readiness_next),
        "states": states,
        "issues": [asdict(issue) for issue in issues],
    }
    return payload, issues


def validate_state_shape(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("schema_version", "generated_at", "policy_path", "policy_sha256", "states"):
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
        "next_command",
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


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_reports(root: Path, payload: dict[str, Any], json_path: Path, report_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Workflow State",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Project state: {payload.get('project_state', '')}",
        f"- Readiness overall status: {payload.get('readiness_overall_status', '')}",
        f"- Policy: {payload.get('policy_path', '')}",
        f"- Next command: {payload.get('next_command', '')}",
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
        "| Mod | State | Last success stage | Blocking checks | Retry count | Next command |",
        "|---|---|---|---|---:|---|",
        ]
    )
    for row in payload.get("states", []):
        blockers = ", ".join(row.get("blocking_checks", [])) if isinstance(row, dict) else ""
        lines.append(
            f"| {markdown_cell(row.get('mod', ''))} | {markdown_cell(row.get('state', ''))} | "
            f"{markdown_cell(row.get('last_success_stage', ''))} | {markdown_cell(blockers or 'none')} | "
            f"{row.get('retry_count', 0)} | "
            f"{markdown_cell(row.get('next_command', ''))} |"
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
        lines.extend(["| Severity | Area | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            lines.append(
                f"| {markdown_cell(issue.get('severity', ''))} | {markdown_cell(issue.get('area', ''))} | "
                f"{markdown_cell(issue.get('message', ''))} | {markdown_cell(issue.get('evidence', ''))} |"
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
