"""Write qa/workflow_tasks.json from the current workflow state.

This adds a schedulable task layer above workflow_state.json. It does not run
commands, translate, rebuild final_mod, or decide QA pass/fail.
"""

import argparse
import hashlib
import json
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, plugin_root, project_root, relative_path, resolve_project_path
from workflow_lock import safe_lock_name


GLOBAL_RESOURCE = "global:workflow-state"
GUI_RESOURCE = "gui:desktop"
TASK_FILE_RESOURCE = "qa:workflow-tasks"
READY_STATES = {"ready_for_manual_test", "manual_tested"}
NON_PARALLEL_SCRIPTS = {
    "run_non_gui_translation_workflow.py",
    "run_translation_queue.py",
    "run_non_gui_qa_gates.py",
    "test_workflow_health.py",
    "write_translation_status.py",
    "refresh_project_handoff_reports.py",
    "write_agent_handoff.py",
    "write_workflow_state.py",
    "write_workflow_tasks.py",
    "audit_translation_readiness.py",
    "write_codex_handoff.py",
    "claim_workflow_task.py",
    "export_agent_context.py",
    "list_agent_skills.py",
    "resume_workflow.py",
    "run_workflow_tasks.py",
    "validate_agent_capabilities.py",
    "validate_claude_plugin_marketplace.py",
}
GUI_SCRIPTS = {
    "invoke_lextranslator_gui.py",
    "automate-lextranslator-gui.py",
    "invoke_xtranslator.py",
    "invoke_lextranslator.py",
}


@dataclass
class WorkflowTaskIssue:
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


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def split_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return []
    return [part.strip().strip('"').strip("'") for part in parts if part.strip()]


def command_python_runner_ok(value: str) -> bool:
    name = Path(value).name.lower()
    return name in {"python", "python.exe", "py", "py.exe"}


def command_script_path(command: str) -> Path | None:
    parts = split_command(command)
    if len(parts) < 2 or not command_python_runner_ok(parts[0]):
        return None
    script = Path(parts[1])
    if not script.is_absolute():
        script = plugin_root() / script
    return script.resolve(strict=False)


def command_script_name(command: str) -> str:
    script = command_script_path(command)
    if script is None:
        return ""
    scripts_root = (plugin_root() / "scripts").resolve(strict=False)
    if not is_under(script, scripts_root):
        return ""
    try:
        return script.relative_to(scripts_root).as_posix()
    except ValueError:
        return ""


def command_is_project_python(command: str) -> bool:
    script = command_script_path(command)
    if script is None:
        return False
    return is_under(script, (plugin_root() / "scripts").resolve(strict=False))


def classify_command(command: str) -> tuple[bool, list[str], list[str]]:
    script = command_script_name(command)
    resources: list[str] = []
    notes: list[str] = []
    parallel_safe = True
    if not command_is_project_python(command):
        return False, [GLOBAL_RESOURCE], ["command is not a project-local Python script"]
    if script in NON_PARALLEL_SCRIPTS:
        parallel_safe = False
        resources.append(GLOBAL_RESOURCE)
        notes.append(f"{script} uses global workflow reports or lock")
    if script in GUI_SCRIPTS:
        parallel_safe = False
        resources.append(GUI_RESOURCE)
        notes.append(f"{script} may use GUI automation")
    if not script:
        parallel_safe = False
        resources.append(GLOBAL_RESOURCE)
        notes.append("script could not be identified")
    return parallel_safe, resources, notes


def task_id_for(*parts: object) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def task_from_action(
    *,
    mod_name: str,
    state: str,
    last_success: str,
    action: dict[str, Any],
    action_index: int,
    source: str,
) -> dict[str, Any]:
    command = str(action.get("command", "")).strip()
    reason = str(action.get("reason", "")).strip()
    risk = str(action.get("risk", "")).strip() or "low"
    action_type = str(action.get("type", "")).strip() or source
    evidence = str(action.get("evidence", "") or action.get("path", "")).strip()
    executable = bool(command and action.get("allowed", True) and risk == "low")
    parallel_safe, resources, notes = classify_command(command) if executable else (False, [], [])
    mod_resource = f"mod:{mod_name}" if mod_name else "mod:unknown"
    action_resources = string_list(action.get("resource_locks", []))
    resource_locks = action_resources[:] if action_resources else [mod_resource]
    for resource in resources:
        if resource not in resource_locks:
            resource_locks.append(resource)
    if action_resources:
        notes.append("resource locks provided by workflow action")
    if "can_run_parallel" in action:
        parallel_safe = bool(parallel_safe and action.get("can_run_parallel"))
    if not executable:
        notes.append("manual/model review or non-command task")
    return {
        "task_id": task_id_for(mod_name, state, source, action_index, action_type, reason, command, evidence),
        "mod": mod_name,
        "stage": state,
        "last_success_stage": last_success,
        "kind": action_type,
        "source": source,
        "status": "pending" if executable else "pending_manual",
        "reason": reason,
        "risk": risk,
        "command": command,
        "executable": executable,
        "can_run_parallel": bool(executable and parallel_safe),
        "dependencies": string_list(action.get("dependencies", [])),
        "resource_locks": resource_locks,
        "evidence": evidence,
        "claim_owner": "",
        "lease_until": "",
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "output_tail": [],
        "notes": notes,
    }


def task_for_ready_state(row: dict[str, Any]) -> dict[str, Any]:
    mod_name = str(row.get("mod", "")).strip()
    state = str(row.get("state", "")).strip()
    return {
        "task_id": task_id_for(mod_name, state, "manual_test"),
        "mod": mod_name,
        "stage": state,
        "last_success_stage": str(row.get("last_success_stage", "")).strip(),
        "kind": "manual_game_test",
        "source": "state",
        "status": "pending_manual",
        "reason": "ready_for_manual_game_test" if state == "ready_for_manual_test" else "manual_tested",
        "risk": "manual",
        "command": "",
        "executable": False,
        "can_run_parallel": False,
        "dependencies": [],
        "resource_locks": [f"mod:{mod_name}" if mod_name else "mod:unknown"],
        "evidence": str(row.get("next_command", "")).strip(),
        "claim_owner": "",
        "lease_until": "",
        "started_at": "",
        "finished_at": "",
        "exit_code": None,
        "output_tail": [],
        "notes": ["manual game testing is outside automated workflow"],
    }


def preserve_runtime_fields(tasks: list[dict[str, Any]], previous: dict[str, Any]) -> list[dict[str, Any]]:
    previous_tasks = previous.get("tasks", [])
    if not isinstance(previous_tasks, list):
        return tasks
    by_id = {str(task.get("task_id", "")): task for task in previous_tasks if isinstance(task, dict)}
    runtime_keys = {"status", "claim_owner", "lease_until", "started_at", "finished_at", "exit_code", "output_tail"}
    for task in tasks:
        prior = by_id.get(str(task.get("task_id", "")))
        if not prior:
            continue
        prior_status = str(prior.get("status", ""))
        if prior_status in {"running", "done", "failed", "blocked", "skipped"}:
            for key in runtime_keys:
                if key in prior:
                    task[key] = prior[key]
    return tasks


def build_mod_lanes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_mod: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        mod_name = str(task.get("mod", "")).strip() or "unknown"
        by_mod.setdefault(mod_name, []).append(task)
    lanes: list[dict[str, Any]] = []
    for mod_name in sorted(by_mod):
        mod_tasks = by_mod[mod_name]
        pending_executable = [task for task in mod_tasks if task.get("status") == "pending" and task.get("executable") is True]
        parallel_pending = [task for task in pending_executable if task.get("can_run_parallel") is True]
        lanes.append(
            {
                "mod": mod_name,
                "agent_lane": f"mod:{mod_name}",
                "suggested_owner": f"mod-agent:{mod_name}",
                "resource_lock": f"mod:{mod_name}",
                "task_count": len(mod_tasks),
                "pending_executable": len(pending_executable),
                "parallel_safe_pending": len(parallel_pending),
                "pending_manual": sum(1 for task in mod_tasks if task.get("status") == "pending_manual"),
                "running": sum(1 for task in mod_tasks if task.get("status") == "running"),
                "done": sum(1 for task in mod_tasks if task.get("status") == "done"),
                "failed": sum(1 for task in mod_tasks if task.get("status") == "failed"),
                "claim_filter": {"mod_name": mod_name, "parallel_only": True},
            }
        )
    return lanes


def task_resources(task: dict[str, Any]) -> list[str]:
    return string_list(task.get("resource_locks", []))


def resource_lane_key(task: dict[str, Any]) -> str:
    for resource in task_resources(task):
        if resource.startswith("file:") or resource.startswith("resource:"):
            return resource
    return ""


def build_resource_lanes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_resource: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for task in tasks:
        resource = resource_lane_key(task)
        if not resource:
            continue
        mod_name = str(task.get("mod", "")).strip() or "unknown"
        by_resource.setdefault((mod_name, resource), []).append(task)
    lanes: list[dict[str, Any]] = []
    for mod_name, resource in sorted(by_resource):
        lane_tasks = by_resource[(mod_name, resource)]
        pending_executable = [task for task in lane_tasks if task.get("status") == "pending" and task.get("executable") is True]
        parallel_pending = [task for task in pending_executable if task.get("can_run_parallel") is True]
        lanes.append(
            {
                "mod": mod_name,
                "agent_lane": resource,
                "suggested_owner": f"resource-agent:{mod_name}:{safe_lock_name(resource)}",
                "resource_lock": resource,
                "task_count": len(lane_tasks),
                "pending_executable": len(pending_executable),
                "parallel_safe_pending": len(parallel_pending),
                "pending_manual": sum(1 for task in lane_tasks if task.get("status") == "pending_manual"),
                "running": sum(1 for task in lane_tasks if task.get("status") == "running"),
                "done": sum(1 for task in lane_tasks if task.get("status") == "done"),
                "failed": sum(1 for task in lane_tasks if task.get("status") == "failed"),
                "claim_filter": {"mod_name": mod_name, "resource_lock": resource, "parallel_only": True},
            }
        )
    return lanes


def build_tasks(root: Path, state_path: Path, previous_path: Path) -> tuple[dict[str, Any], list[WorkflowTaskIssue]]:
    issues: list[WorkflowTaskIssue] = []
    state = read_json(state_path)
    if not state or state.get("_invalid_json"):
        issues.append(WorkflowTaskIssue("error", "workflow_state", "workflow state is missing or invalid", relative_path(root, state_path)))
        state = {}
    previous = read_json(previous_path)
    rows = state.get("states", [])
    if not isinstance(rows, list):
        rows = []
        issues.append(WorkflowTaskIssue("error", "workflow_state", "workflow_state.states is not an array", relative_path(root, state_path)))

    tasks: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mod_name = str(row.get("mod", "")).strip()
        if not mod_name:
            continue
        state_name = str(row.get("state", "")).strip()
        last_success = str(row.get("last_success_stage", "")).strip()
        if state_name in READY_STATES:
            tasks.append(task_for_ready_state(row))
            continue
        actions = []
        for source in ("repair_candidates", "recommended_actions"):
            values = row.get(source, [])
            if isinstance(values, list):
                for index, action in enumerate(values):
                    if isinstance(action, dict):
                        actions.append((source, index, action))
        for source, index, action in actions:
            task = task_from_action(
                mod_name=mod_name,
                state=state_name,
                last_success=last_success,
                action=action,
                action_index=index,
                source=source,
            )
            if task["command"] or task["status"] == "pending_manual":
                tasks.append(task)

    tasks = preserve_runtime_fields(tasks, previous)
    mod_lanes = build_mod_lanes(tasks)
    resource_lanes = build_resource_lanes(tasks)
    pending_executable = sum(1 for task in tasks if task.get("status") == "pending")
    pending_manual = sum(1 for task in tasks if task.get("status") == "pending_manual")
    counts = {
        "total": len(tasks),
        "pending": pending_executable,
        "pending_executable": pending_executable,
        "pending_manual": pending_manual,
        "pending_total": pending_executable + pending_manual,
        "running": sum(1 for task in tasks if task.get("status") == "running"),
        "done": sum(1 for task in tasks if task.get("status") == "done"),
        "failed": sum(1 for task in tasks if task.get("status") == "failed"),
        "parallel_safe": sum(1 for task in tasks if task.get("can_run_parallel") is True),
        "resource_lanes": len(resource_lanes),
    }
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "workflow_state_path": relative_path(root, state_path),
        "workflow_state_generated_at": str(state.get("generated_at", "")),
        "scheduler_model": {
            "facts_source": "qa/workflow_state.json",
            "task_file": "qa/workflow_tasks.json",
            "mod_locks": "work/locks/mod.<ModName>.lock",
            "global_lock": "work/.workflow.lock",
            "task_file_lock": f"work/locks/{safe_lock_name(TASK_FILE_RESOURCE)}.lock",
        },
        "parallel_policy": {
            "can_parallelize": [
                "different Mod lanes with disjoint mod:<ModName> resource locks",
                "different file/resource lanes inside one large Mod when locks are file:<ModName>:<PathOrHash> or resource:<ModName>:<Name>",
                "project-local Python leaf tasks marked can_run_parallel=true",
            ],
            "must_serialize": [
                "global status refreshes and workflow_state/readiness/health writers",
                "GUI automation through LexTranslator/xTranslator/Computer Use",
                "legacy entrypoints that hold work/.workflow.lock",
                "same file/resource lane writes, final_mod assembly, strict QA, and Mod-wide tasks",
            ],
        },
        "mod_lanes": mod_lanes,
        "resource_lanes": resource_lanes,
        "counts": counts,
        "tasks": tasks,
        "issues": [asdict(issue) for issue in issues],
    }
    return payload, issues


def validate_tasks(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ["tasks must be an array"]
    required = {
        "task_id",
        "mod",
        "stage",
        "kind",
        "status",
        "command",
        "executable",
        "can_run_parallel",
        "resource_locks",
        "evidence",
    }
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] is not an object")
            continue
        missing = sorted(required - set(task))
        if missing:
            errors.append(f"tasks[{index}] missing keys: {', '.join(missing)}")
        task_id = str(task.get("task_id", ""))
        if task_id in seen:
            errors.append(f"duplicate task_id: {task_id}")
        seen.add(task_id)
        if not isinstance(task.get("resource_locks"), list):
            errors.append(f"tasks[{index}].resource_locks must be an array")
        elif not all(isinstance(resource, str) and resource.strip() for resource in task.get("resource_locks", [])):
            errors.append(f"tasks[{index}].resource_locks must contain non-empty strings")
        if "dependencies" in task and not isinstance(task.get("dependencies"), list):
            errors.append(f"tasks[{index}].dependencies must be an array")
    return errors


def write_reports(root: Path, payload: dict[str, Any], json_path: Path, report_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counts = payload.get("counts", {}) if isinstance(payload.get("counts"), dict) else {}
    lines = [
        "# Workflow Tasks",
        "",
        f"- Generated at: {payload.get('generated_at', '')}",
        f"- Workflow state: {payload.get('workflow_state_path', '')}",
        f"- Total tasks: {counts.get('total', 0)}",
        f"- Pending executable: {counts.get('pending_executable', counts.get('pending', 0))}",
        f"- Pending manual/model: {counts.get('pending_manual', 0)}",
        f"- Pending total: {counts.get('pending_total', counts.get('pending', 0) + counts.get('pending_manual', 0))}",
        f"- Parallel-safe executable: {counts.get('parallel_safe', 0)}",
        f"- Resource lanes: {counts.get('resource_lanes', 0)}",
        f"- Compatibility pending field: {counts.get('pending', 0)} (same as pending_executable)",
        "",
        "## Parallel Policy",
        "",
        "- Different Mod lanes may run in parallel only when their `mod:<ModName>` resource locks do not overlap.",
        "- One large Mod may also fan out into file/resource lanes, for example `file:<ModName>:<PathOrHash>` or `resource:<ModName>:<Name>`.",
        "- A dedicated Mod agent may repeatedly claim tasks with `--mod-name <ModName>` and process that Mod lane serially.",
        "- A resource-lane agent may claim tasks with both `--mod-name <ModName>` and `--resource-lock <Lock>`.",
        "- Global status refreshes, GUI automation, and legacy entrypoints that hold `work/.workflow.lock` must be serialized.",
        "- `qa/workflow_state.json` remains the source of truth; this file is only a schedulable view.",
        "",
        "## Mod Lanes",
        "",
        "| Mod | Suggested owner | Resource lock | Pending executable | Parallel-safe pending | Running | Done | Failed |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for lane in payload.get("mod_lanes", []):
        if not isinstance(lane, dict):
            continue
        lines.append(
            f"| {markdown_cell(lane.get('mod', ''))} | {markdown_cell(lane.get('suggested_owner', ''))} | "
            f"{markdown_cell(lane.get('resource_lock', ''))} | {markdown_cell(lane.get('pending_executable', 0))} | "
            f"{markdown_cell(lane.get('parallel_safe_pending', 0))} | {markdown_cell(lane.get('running', 0))} | "
            f"{markdown_cell(lane.get('done', 0))} | {markdown_cell(lane.get('failed', 0))} |"
        )
    lines.extend(
        [
        "",
        "## Resource Lanes",
        "",
        "| Mod | Suggested owner | Resource lock | Pending executable | Parallel-safe pending | Running | Done | Failed |",
        "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    resource_lanes = payload.get("resource_lanes", [])
    if isinstance(resource_lanes, list) and resource_lanes:
        for lane in resource_lanes:
            if not isinstance(lane, dict):
                continue
            lines.append(
                f"| {markdown_cell(lane.get('mod', ''))} | {markdown_cell(lane.get('suggested_owner', ''))} | "
                f"{markdown_cell(lane.get('resource_lock', ''))} | {markdown_cell(lane.get('pending_executable', 0))} | "
                f"{markdown_cell(lane.get('parallel_safe_pending', 0))} | {markdown_cell(lane.get('running', 0))} | "
                f"{markdown_cell(lane.get('done', 0))} | {markdown_cell(lane.get('failed', 0))} |"
            )
    else:
        lines.append("| none |  |  | 0 | 0 | 0 | 0 | 0 |")
    lines.extend(
        [
        "",
        "## Tasks",
        "",
        "| Task | Mod | Stage | Kind | Status | Parallel | Risk | Resource locks | Evidence | Command |",
        "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for task in payload.get("tasks", []):
        if not isinstance(task, dict):
            continue
        lines.append(
            f"| {markdown_cell(task.get('task_id', ''))} | {markdown_cell(task.get('mod', ''))} | "
            f"{markdown_cell(task.get('stage', ''))} | {markdown_cell(task.get('kind', ''))} | "
            f"{markdown_cell(task.get('status', ''))} | {markdown_cell(task.get('can_run_parallel', False))} | "
            f"{markdown_cell(task.get('risk', ''))} | {markdown_cell(', '.join(task.get('resource_locks', [])))} | "
            f"{markdown_cell(task.get('evidence', ''))} | {markdown_cell(task.get('command', ''))} |"
        )
    lines.extend(["", "## Issues", ""])
    issues = payload.get("issues", [])
    if not issues:
        lines.append("No workflow task issues.")
    else:
        lines.extend(["| Severity | Area | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(
                    f"| {markdown_cell(issue.get('severity', ''))} | {markdown_cell(issue.get('area', ''))} | "
                    f"{markdown_cell(issue.get('message', ''))} | {markdown_cell(issue.get('evidence', ''))} |"
                )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write qa/workflow_tasks.json from qa/workflow_state.json.")
    parser.add_argument("--workflow-state-path", default="qa/workflow_state.json")
    parser.add_argument("--json-output-path", default="qa/workflow_tasks.json")
    parser.add_argument("--report-output-path", default="qa/workflow_tasks.md")
    args = parser.parse_args()

    root = project_root()
    state_path = resolve_project_path(root, args.workflow_state_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(json_path, qa_root) or not is_under(report_path, qa_root):
        raise ValueError("Workflow task outputs must be under qa/.")

    payload, issues = build_tasks(root, state_path, json_path)
    for error in validate_tasks(payload):
        issues.append(WorkflowTaskIssue("error", "schema", error, "qa/workflow_tasks.json"))
    payload["issues"] = [asdict(issue) for issue in issues]
    write_reports(root, payload, json_path, report_path)
    blocking = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    print(f"Workflow tasks JSON written to: {json_path}")
    print(f"Workflow tasks report written to: {report_path}")
    print(f"Pending executable tasks: {payload.get('counts', {}).get('pending_executable', payload.get('counts', {}).get('pending', 0))}")
    print(f"Pending manual/model tasks: {payload.get('counts', {}).get('pending_manual', 0)}")
    print(f"Pending total tasks: {payload.get('counts', {}).get('pending_total', 0)}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
