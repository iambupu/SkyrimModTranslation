"""Write the shortest Codex handoff view from workflow evidence.

This script is a read-only summarizer. It does not execute tasks, translate,
rebuild final_mod, or decide QA pass/fail.
"""

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, normalize_python_script_command, project_root, relative_path, resolve_project_path


REFRESH_AFTER = [
    normalize_python_script_command("python scripts/audit_translation_readiness.py"),
    normalize_python_script_command("python scripts/write_workflow_state.py"),
    normalize_python_script_command("python scripts/test_workflow_health.py --run-strict-gate"),
    normalize_python_script_command("python scripts/write_workflow_tasks.py"),
    normalize_python_script_command("python scripts/write_codex_handoff.py"),
    normalize_python_script_command("python scripts/audit_project_completion.py"),
    normalize_python_script_command("python scripts/new_manual_game_test_plan.py"),
    normalize_python_script_command("python scripts/new_manual_game_test_results_template.py"),
    normalize_python_script_command("python scripts/audit_translation_goal_compliance.py"),
]


@dataclass
class HandoffIssue:
    severity: str
    area: str
    message: str
    evidence: str = ""


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"_invalid_json": True}
    return payload if isinstance(payload, dict) else {"_invalid_json": True}


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def low_risk_actions(row: dict[str, Any]) -> list[dict[str, Any]]:
    actions = row.get("next_actions", [])
    if not isinstance(actions, list):
        return []
    return [
        action
        for action in actions
        if isinstance(action, dict)
        and action.get("allowed", True)
        and str(action.get("risk", "")) == "low"
        and str(action.get("command", "")).strip()
    ]


def first_non_refresh_action(row: dict[str, Any]) -> dict[str, Any]:
    low_risk = low_risk_actions(row)
    for action in low_risk:
        if str(action.get("type", "")) != "refresh_state":
            return action
    return low_risk[0] if low_risk else {}


def evidence_required(row: dict[str, Any], safe_action: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for source in ("recommended_actions", "repair_candidates"):
        values = row.get(source, [])
        if not isinstance(values, list):
            continue
        for action in values:
            if not isinstance(action, dict):
                continue
            path = str(action.get("evidence", "") or action.get("path", "")).strip()
            if path and path not in evidence:
                evidence.append(path)
    action_evidence = str(safe_action.get("evidence", "")).strip()
    if action_evidence and action_evidence not in evidence:
        evidence.insert(0, action_evidence)
    return evidence[:8]


def task_summary(tasks_payload: dict[str, Any]) -> dict[str, Any]:
    counts = tasks_payload.get("counts", {})
    if not isinstance(counts, dict):
        counts = {}
    return {
        "generated_at": str(tasks_payload.get("generated_at", "")),
        "total": counts.get("total", 0),
        "pending_executable": counts.get("pending_executable", counts.get("pending", 0)),
        "pending_manual": counts.get("pending_manual", 0),
        "pending_total": counts.get("pending_total", counts.get("pending", 0) + counts.get("pending_manual", 0)),
        "parallel_safe": counts.get("parallel_safe", 0),
    }


def pending_task_for_action(tasks_payload: dict[str, Any], mod_name: str, command: str) -> dict[str, Any]:
    tasks = tasks_payload.get("tasks", [])
    if not isinstance(tasks, list):
        return {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("mod", "")) != mod_name:
            continue
        if str(task.get("command", "")).strip() == command.strip():
            return task
    return {}


def safe_action_with_task(row: dict[str, Any], tasks_payload: dict[str, Any], mod_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for action in low_risk_actions(row):
        if str(action.get("type", "")) == "refresh_state":
            continue
        task = pending_task_for_action(tasks_payload, mod_name, str(action.get("command", "")))
        paired.append((action, task))
    for action, task in paired:
        if task and task.get("status") == "pending" and task.get("can_run_parallel") is True:
            return action, task
    for action, task in paired:
        if task and task.get("status") == "pending":
            return action, task
    if paired:
        return paired[0]
    action = first_non_refresh_action(row)
    return action, pending_task_for_action(tasks_payload, mod_name, str(action.get("command", ""))) if action else {}


def blocking_handoffs(states: list[dict[str, Any]], tasks_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in states:
        if not isinstance(row, dict):
            continue
        blockers = row.get("blocking_checks", [])
        blockers = blockers if isinstance(blockers, list) else []
        state = str(row.get("state", "")).strip()
        if state not in {"qa_failed", "blocked", "needs_input"} and not blockers:
            continue
        mod_name = str(row.get("mod", "")).strip()
        safe_action, task = safe_action_with_task(row, tasks_payload, mod_name)
        rows.append(
            {
                "mod": mod_name,
                "state": state,
                "last_success_stage": str(row.get("last_success_stage", "")).strip(),
                "primary_blocker": str(blockers[0]) if blockers else "",
                "blocking_checks": blockers,
                "safe_next_action": safe_action,
                "task_id": str(task.get("task_id", "")),
                "can_run_parallel": bool(task.get("can_run_parallel", False)),
                "resource_locks": task.get("resource_locks", []),
                "must_read_evidence": evidence_required(row, safe_action),
                "stop_conditions": row.get("stop_conditions", []),
                "retry_count": row.get("retry_count", 0),
                "last_attempt": row.get("last_attempt", {}),
                "refresh_after": REFRESH_AFTER,
            }
        )
    return rows


def build_handoff(
    root: Path,
    state_path: Path,
    readiness_path: Path,
    health_path: Path,
    tasks_path: Path,
) -> tuple[dict[str, Any], list[HandoffIssue]]:
    issues: list[HandoffIssue] = []
    state = read_json(state_path)
    readiness = read_json(readiness_path)
    health = read_json(health_path)
    tasks = read_json(tasks_path)
    for label, path, payload in (
        ("workflow_state", state_path, state),
        ("translation_readiness", readiness_path, readiness),
        ("workflow_health", health_path, health),
        ("workflow_tasks", tasks_path, tasks),
    ):
        if not payload:
            issues.append(HandoffIssue("warning", label, f"{label} is missing", relative_path(root, path)))
        elif payload.get("_invalid_json"):
            issues.append(HandoffIssue("error", label, f"{label} is invalid JSON", relative_path(root, path)))

    state_generated = str(state.get("generated_at", "")).strip()
    tasks_state_generated = str(tasks.get("workflow_state_generated_at", "")).strip()
    if state_generated and tasks_state_generated and state_generated != tasks_state_generated:
        issues.append(
            HandoffIssue(
                "warning",
                "stale_state_artifact",
                "workflow_tasks was generated from an older workflow_state.json; refresh the state chain before treating handoff as ready.",
                "qa/workflow_tasks.json",
            )
        )

    states = state.get("states", [])
    states = states if isinstance(states, list) else []
    blocking = blocking_handoffs(states, tasks)
    safe_commands = [
        row["safe_next_action"]
        for row in blocking
        if isinstance(row.get("safe_next_action"), dict) and str(row["safe_next_action"].get("command", "")).strip()
    ]
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "read_order": [
            "qa/codex_handoff.json",
            "qa/workflow_state.json",
            "qa/workflow_tasks.json",
            "qa/translation_readiness.json",
            "config/workflow_policy.json",
        ],
        "project_state": str(state.get("project_state", "")),
        "readiness_overall_status": str(readiness.get("OverallStatus", "")),
        "workflow_health": {
            "verdict": str(health.get("Verdict", "")),
            "blocking_issues": health.get("BlockingIssues", 0),
            "warnings": health.get("Warnings", 0),
        },
        "state_summary": state.get("state_summary", {}),
        "task_summary": task_summary(tasks),
        "source_reports": {
            "workflow_state_generated_at": state_generated,
            "translation_readiness_checked_at": str(readiness.get("CheckedAt", "")),
            "workflow_health_generated_at": str(health.get("GeneratedAt", health.get("generated_at", ""))),
            "workflow_tasks_generated_at": str(tasks.get("generated_at", "")),
            "workflow_tasks_state_generated_at": tasks_state_generated,
        },
        "blocking_mods": blocking,
        "safe_next_actions": safe_commands,
        "refresh_after_any_action": REFRESH_AFTER,
        "issues": [asdict(issue) for issue in issues],
    }
    return payload, issues


def write_reports(root: Path, payload: dict[str, Any], json_path: Path, report_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    health = payload.get("workflow_health", {}) if isinstance(payload.get("workflow_health"), dict) else {}
    summary = payload.get("state_summary", {}) if isinstance(payload.get("state_summary"), dict) else {}
    task_info = payload.get("task_summary", {}) if isinstance(payload.get("task_summary"), dict) else {}
    lines = [
        "# Codex Handoff",
        "",
        f"- Generated at: {payload.get('generated_at', '')}",
        f"- Project state: {payload.get('project_state', '')}",
        f"- Readiness: {payload.get('readiness_overall_status', '')}",
        f"- Workflow health: {health.get('verdict', '')} / Blocking: {health.get('blocking_issues', 0)}",
        f"- Blocking Mods: {', '.join(summary.get('blocking_mods', [])) if isinstance(summary.get('blocking_mods'), list) else ''}",
        f"- Pending executable tasks: {task_info.get('pending_executable', 0)}",
        f"- Pending manual/model tasks: {task_info.get('pending_manual', 0)}",
        f"- Pending total tasks: {task_info.get('pending_total', 0)}",
        f"- Parallel-safe tasks: {task_info.get('parallel_safe', 0)}",
        "- Manual validation boundary: project-local static QA can be ready, but real game/MO2/Vortex validation is still player-operated evidence.",
        "",
        "## Blocking Mods",
        "",
        "| Mod | State | Primary blocker | Safe next action | Evidence |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        action = row.get("safe_next_action", {}) if isinstance(row.get("safe_next_action"), dict) else {}
        evidence = ", ".join(row.get("must_read_evidence", [])) if isinstance(row.get("must_read_evidence"), list) else ""
        lines.append(
            f"| {markdown_cell(row.get('mod', ''))} | {markdown_cell(row.get('state', ''))} | "
            f"{markdown_cell(row.get('primary_blocker', ''))} | {markdown_cell(action.get('command', ''))} | "
            f"{markdown_cell(evidence)} |"
        )
    lines.extend(
        [
            "",
            "## Required Refresh",
            "",
        ]
    )
    for command in payload.get("refresh_after_any_action", []):
        lines.append(f"- `{command}`")
    lines.extend(["", "## Issues", ""])
    issues = payload.get("issues", [])
    if not issues:
        lines.append("No handoff issues.")
    else:
        lines.extend(["| Severity | Area | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(
                    f"| {markdown_cell(issue.get('severity', ''))} | {markdown_cell(issue.get('area', ''))} | "
                    f"{markdown_cell(issue.get('message', ''))} | {markdown_cell(issue.get('evidence', ''))} |"
                )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- `workflow_state.json` remains the source of truth.",
            "- This handoff is a compact read view; it does not run commands or mark QA complete.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write qa/codex_handoff.json and qa/codex_handoff.md.")
    parser.add_argument("--workflow-state-path", default="qa/workflow_state.json")
    parser.add_argument("--readiness-json-path", default="qa/translation_readiness.json")
    parser.add_argument("--workflow-health-path", default="qa/workflow_health.json")
    parser.add_argument("--workflow-tasks-path", default="qa/workflow_tasks.json")
    parser.add_argument("--json-output-path", default="qa/codex_handoff.json")
    parser.add_argument("--report-output-path", default="qa/codex_handoff.md")
    args = parser.parse_args()

    root = project_root()
    state_path = resolve_project_path(root, args.workflow_state_path, must_exist=False)
    readiness_path = resolve_project_path(root, args.readiness_json_path, must_exist=False)
    health_path = resolve_project_path(root, args.workflow_health_path, must_exist=False)
    tasks_path = resolve_project_path(root, args.workflow_tasks_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(json_path, qa_root) or not is_under(report_path, qa_root):
        raise ValueError("Codex handoff outputs must be under qa/.")

    payload, issues = build_handoff(root, state_path, readiness_path, health_path, tasks_path)
    write_reports(root, payload, json_path, report_path)
    blocking = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    print(f"Codex handoff JSON written to: {json_path}")
    print(f"Codex handoff report written to: {report_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
