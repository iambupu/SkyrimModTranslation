"""Resume one low-risk workflow task safely.

This is the Codex-friendly single-entry recovery path. It selects one pending
low-risk task, logs the attempt, runs the project-local Python command, and
refreshes the handoff state.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, plugin_root as default_plugin_root, project_root, resolve_project_path
from workflow_lock import ResourceLock


GUI_RESOURCE = "gui:desktop"
GLOBAL_RESOURCE = "global:workflow-state"
TASK_FILE_RESOURCE = "qa:workflow-tasks"
REFRESH_COMMANDS = [
    ["audit_translation_readiness.py"],
    ["write_workflow_state.py"],
    ["write_workflow_tasks.py"],
    ["write_codex_handoff.py"],
]


@dataclass
class ResumeResult:
    task_id: str
    mod: str
    command: str
    status: str
    exit_code: int
    evidence: str
    output_tail: list[str]
    refresh_output_tail: list[str]


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must contain an object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def split_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=False)
    return [part.strip().strip('"').strip("'") for part in parts if part.strip()]


def project_python_argv(root: Path, command: str) -> list[str]:
    source_root = default_plugin_root()
    parts = split_command(command)
    if len(parts) < 2:
        raise ValueError(f"Task command is too short: {command}")
    if parts[0].lower() not in {"python", "python.exe", "py"}:
        raise ValueError(f"Task command must start with python/py: {command}")
    script = Path(parts[1])
    if not script.is_absolute():
        script = source_root / script
    script = script.resolve(strict=False)
    scripts_root = (source_root / "scripts").resolve(strict=True)
    if not is_under(script, scripts_root):
        raise ValueError(f"Task script is outside scripts/: {parts[1]}")
    if script.suffix.lower() != ".py":
        raise ValueError(f"Task script is not a Python file: {parts[1]}")
    return [sys.executable, str(script), *parts[2:]]


def output_lines(result: subprocess.CompletedProcess) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def task_resources(task: dict[str, Any]) -> list[str]:
    resources = task.get("resource_locks", [])
    if not isinstance(resources, list):
        return []
    return [str(resource) for resource in resources if str(resource).strip()]


def update_task(tasks_path: Path, task_id: str, **fields: Any) -> None:
    root = project_root()
    lock = ResourceLock(root, TASK_FILE_RESOURCE, "resume_workflow.py").acquire()
    try:
        payload = read_json(tasks_path)
        for task in payload.get("tasks", []):
            if isinstance(task, dict) and str(task.get("task_id", "")) == task_id:
                task.update(fields)
                break
        write_json(tasks_path, payload)
    finally:
        lock.release()


def eligible_task(task: dict[str, Any], mod_name: str, task_id: str, include_serial: bool) -> bool:
    if task_id and str(task.get("task_id", "")) != task_id:
        return False
    if mod_name and str(task.get("mod", "")) != mod_name:
        return False
    if task.get("status") != "pending" or task.get("executable") is not True:
        return False
    if str(task.get("risk", "")) != "low":
        return False
    resources = task_resources(task)
    if GUI_RESOURCE in resources:
        return False
    if not include_serial and task.get("can_run_parallel") is not True:
        return False
    return bool(str(task.get("command", "")).strip())


def choose_task(tasks_payload: dict[str, Any], mod_name: str, task_id: str, include_serial: bool) -> dict[str, Any]:
    tasks = tasks_payload.get("tasks", [])
    if not isinstance(tasks, list):
        return {}
    eligible = [task for task in tasks if isinstance(task, dict) and eligible_task(task, mod_name, task_id, include_serial)]
    if not eligible:
        return {}
    reason_priority = {
        "chs_package_missing": 0,
        "provenance_missing": 1,
        "package_validation_not_clean": 2,
        "strict_gate_not_clean": 3,
        "refresh_translation_readiness_after_any_action": 8,
        "refresh_workflow_state_after_readiness": 9,
    }
    eligible.sort(
        key=lambda task: (
            task.get("can_run_parallel") is not True,
            reason_priority.get(str(task.get("reason", "")), 5),
            str(task.get("mod", "")),
            str(task.get("task_id", "")),
        )
    )
    return eligible[0]


def log_agent(root: Path, *, mod: str, state: str, event: str, action: str, status: str, evidence: str = "", details: str = "") -> None:
    source_root = default_plugin_root()
    args = [
        sys.executable,
        str(source_root / "scripts" / "log_workflow_agent_run.py"),
        "--mod-name",
        mod or "project",
        "--state",
        state or "unknown",
        "--event",
        event,
        "--action",
        action,
        "--status",
        status,
    ]
    if evidence:
        args.extend(["--evidence", evidence])
    if details:
        args.extend(["--details", details])
    subprocess.run(
        args,
        cwd=str(root),
        env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
        capture_output=True,
        text=True,
        check=False,
    )


def refresh_handoff(root: Path, timeout_seconds: int) -> list[str]:
    source_root = default_plugin_root()
    output: list[str] = []
    for script_args in REFRESH_COMMANDS:
        argv = [sys.executable, str(source_root / "scripts" / script_args[0]), *script_args[1:]]
        result = subprocess.run(
            argv,
            cwd=str(root),
            env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        output.extend(output_lines(result)[-20:])
        if result.returncode != 0:
            output.append(f"{script_args[0]} exited with code {result.returncode}")
            break
    return output[-60:]


def write_report(root: Path, report_path: Path, json_path: Path, result: ResumeResult | None, dry_run_task: dict[str, Any] | None) -> None:
    lines = [
        "# Resume Workflow",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if dry_run_task is not None:
        lines.extend(
            [
                "- Mode: dry-run",
                f"- Selected task: {dry_run_task.get('task_id', '')}",
                f"- Mod: {dry_run_task.get('mod', '')}",
                f"- Command: {dry_run_task.get('command', '')}",
            ]
        )
        payload = {"Mode": "dry-run", "Task": dry_run_task}
    elif result is not None:
        lines.extend(
            [
                "- Mode: execute",
                f"- Task: {result.task_id}",
                f"- Mod: {result.mod}",
                f"- Status: {result.status}",
                f"- Exit code: {result.exit_code}",
                f"- Evidence: {result.evidence}",
                "",
                "## Output Tail",
                "",
            ]
        )
        lines.extend(f"- {line}" for line in result.output_tail)
        lines.extend(["", "## Refresh Output", ""])
        lines.extend(f"- {line}" for line in result.refresh_output_tail)
        payload = asdict(result)
    else:
        lines.extend(["- Mode: no-op", "- No eligible safe task was found."])
        payload = {"Mode": "no-op", "Message": "No eligible safe task was found."}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume one safe project-local workflow task.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--mode", choices=["safe"], default="safe")
    parser.add_argument("--include-serial", action="store_true", help="Allow low-risk serial tasks that hold global workflow locks.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--tasks-json-path", default="qa/workflow_tasks.json")
    parser.add_argument("--report-output-path", default="qa/resume_workflow_run.md")
    parser.add_argument("--json-output-path", default="qa/resume_workflow_run.json")
    args = parser.parse_args()

    root = project_root()
    tasks_path = resolve_project_path(root, args.tasks_json_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(tasks_path, qa_root) or not is_under(report_path, qa_root) or not is_under(json_path, qa_root):
        raise ValueError("Resume workflow files must be under qa/.")
    if not tasks_path.is_file():
        refresh_handoff(root, args.timeout_seconds)
    tasks_payload = read_json(tasks_path)
    task = choose_task(tasks_payload, args.mod_name.strip(), args.task_id.strip(), args.include_serial)
    if not task:
        write_report(root, report_path, json_path, None, None)
        print("No eligible safe workflow task found.")
        return 2
    if args.dry_run:
        write_report(root, report_path, json_path, None, task)
        print(f"Resume workflow dry-run report written to: {report_path}")
        print(f"Selected task: {task.get('task_id', '')}")
        return 0

    resources = task_resources(task)
    acquired: list[ResourceLock] = []
    task_id = str(task.get("task_id", ""))
    mod = str(task.get("mod", ""))
    state = str(task.get("stage", ""))
    command = str(task.get("command", ""))
    evidence = str(task.get("evidence", ""))
    try:
        for resource in sorted(resources):
            acquired.append(ResourceLock(root, resource, f"resume_workflow.py:{task_id}").acquire())
        update_task(
            tasks_path,
            task_id,
            status="running",
            claim_owner=f"pid:{os.getpid()}",
            lease_until="",
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        log_agent(root, mod=mod, state=state, event="command", action=command, status="started", evidence=evidence, details=f"task_id={task_id}")
        try:
            result = subprocess.run(
                project_python_argv(root, command),
                cwd=str(root),
                env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(default_plugin_root())},
                capture_output=True,
                text=True,
                check=False,
                timeout=args.timeout_seconds,
            )
            exit_code = result.returncode
            output_tail = output_lines(result)[-40:]
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            output_tail = []
            if exc.stdout:
                output_tail.extend(str(exc.stdout).splitlines())
            if exc.stderr:
                output_tail.extend(str(exc.stderr).splitlines())
            output_tail.append(f"Task timed out after {args.timeout_seconds} seconds.")
            output_tail = output_tail[-40:]
        except Exception as exc:
            exit_code = 1
            output_tail = [str(exc)]
        status = "passed" if exit_code == 0 else "failed"
        task_status = "done" if exit_code == 0 else "failed"
        update_task(
            tasks_path,
            task_id,
            status=task_status,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            exit_code=exit_code,
            output_tail=output_tail,
            claim_owner="",
            lease_until="",
        )
        log_agent(root, mod=mod, state=state, event="command", action=command, status=status, evidence=evidence, details=f"task_id={task_id}")
        refresh_output = refresh_handoff(root, args.timeout_seconds)
        resume_result = ResumeResult(task_id, mod, command, status, exit_code, evidence, output_tail, refresh_output)
        write_report(root, report_path, json_path, resume_result, None)
        print(f"Resume workflow report written to: {report_path}")
        print(f"Task status: {status}")
        return 0 if exit_code == 0 else 1
    finally:
        for lock in reversed(acquired):
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
