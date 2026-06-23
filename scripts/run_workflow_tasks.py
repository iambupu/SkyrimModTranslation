"""Run pending tasks from qa/workflow_tasks.json with conservative locks."""

import argparse
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, plugin_root as default_plugin_root, project_root, relative_path, resolve_project_path
from workflow_lock import ResourceLock


TASK_FILE_RESOURCE = "qa:workflow-tasks"
GLOBAL_RESOURCE = "global:workflow-state"
GUI_RESOURCE = "gui:desktop"


@dataclass
class TaskResult:
    task_id: str
    status: str
    exit_code: int
    output_tail: list[str]
    finished_at: str


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


def output_lines(result: subprocess.CompletedProcess) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def split_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=False)
    return [part.strip().strip('"').strip("'") for part in parts if part.strip()]


def project_python_argv(root: Path, command: str) -> list[str]:
    source_root = default_plugin_root()
    parts = split_command(command)
    if len(parts) < 2:
        raise ValueError(f"Task command is too short: {command}")
    runner = parts[0].lower()
    if runner not in {"python", "python.exe", "py"}:
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


def task_resources(task: dict[str, Any]) -> list[str]:
    resources = task.get("resource_locks", [])
    if not isinstance(resources, list):
        return []
    return [str(resource) for resource in resources if str(resource).strip()]


def executable_pending_tasks(payload: dict[str, Any], *, include_serial: bool, include_gui: bool) -> list[dict[str, Any]]:
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return []
    result: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("status") != "pending" or task.get("executable") is not True:
            continue
        resources = task_resources(task)
        if GUI_RESOURCE in resources and not include_gui:
            continue
        if not include_serial and task.get("can_run_parallel") is not True:
            continue
        result.append(task)
    return result


def update_task(tasks_path: Path, task_id: str, **fields: Any) -> None:
    root = project_root()
    lock = ResourceLock(root, TASK_FILE_RESOURCE, "run_workflow_tasks.py").acquire()
    try:
        payload = read_json(tasks_path)
        for task in payload.get("tasks", []):
            if isinstance(task, dict) and str(task.get("task_id", "")) == task_id:
                task.update(fields)
                break
        write_json(tasks_path, payload)
    finally:
        lock.release()


def run_task(root: Path, tasks_path: Path, task: dict[str, Any], timeout_seconds: int) -> TaskResult:
    task_id = str(task.get("task_id", ""))
    resources = task_resources(task)
    acquired: list[ResourceLock] = []
    try:
        for resource in sorted(resources):
            acquired.append(ResourceLock(root, resource, f"run_workflow_tasks.py:{task_id}").acquire())
        argv = project_python_argv(root, str(task.get("command", "")))
        update_task(
            tasks_path,
            task_id,
            status="running",
            claim_owner=f"pid:{os.getpid()}",
            lease_until="",
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        result = subprocess.run(
            argv,
            cwd=str(root),
            env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(default_plugin_root())},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
        status = "done" if result.returncode == 0 else "failed"
        lines = output_lines(result)[-40:]
        return TaskResult(task_id, status, result.returncode, lines, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except subprocess.TimeoutExpired as exc:
        lines = []
        if exc.stdout:
            lines.extend(str(exc.stdout).splitlines())
        if exc.stderr:
            lines.extend(str(exc.stderr).splitlines())
        lines.append(f"Task timed out after {timeout_seconds} seconds.")
        return TaskResult(task_id, "failed", 124, lines[-40:], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:
        return TaskResult(task_id, "failed", 1, [str(exc)], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        for lock in reversed(acquired):
            lock.release()


def refresh_state(root: Path, timeout_seconds: int) -> list[str]:
    source_root = default_plugin_root()
    commands = [
        [sys.executable, str(source_root / "scripts" / "audit_translation_readiness.py")],
        [sys.executable, str(source_root / "scripts" / "write_workflow_state.py")],
        [sys.executable, str(source_root / "scripts" / "write_workflow_tasks.py")],
        [sys.executable, str(source_root / "scripts" / "write_codex_handoff.py")],
    ]
    output: list[str] = []
    for argv in commands:
        result = subprocess.run(
            argv,
            cwd=str(root),
            env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
        output.extend(output_lines(result)[-20:])
        if result.returncode != 0:
            output.append(f"{Path(argv[1]).name} exited with code {result.returncode}")
            break
    return output[-60:]


def write_run_report(root: Path, report_path: Path, results: list[TaskResult], refresh_output: list[str]) -> None:
    lines = [
        "# Workflow Task Scheduler Run",
        "",
        f"- Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Tasks executed: {len(results)}",
        f"- Failed tasks: {sum(1 for result in results if result.status != 'done')}",
        "",
        "## Results",
        "",
        "| Task | Status | Exit code |",
        "|---|---|---:|",
    ]
    for result in results:
        lines.append(f"| {result.task_id} | {result.status} | {result.exit_code} |")
    lines.extend(["", "## Refresh Output", ""])
    if refresh_output:
        lines.extend(f"- {line}" for line in refresh_output)
    else:
        lines.append("No refresh was run.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_dry_run_report(root: Path, report_path: Path, tasks: list[dict[str, Any]]) -> None:
    lines = [
        "# Workflow Task Scheduler Dry Run",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Tasks selected: {len(tasks)}",
        "",
        "| Task | Mod | Parallel | Resource locks | Command |",
        "|---|---|---|---|---|",
    ]
    for task in tasks:
        resources = ", ".join(task_resources(task))
        command = str(task.get("command", "")).replace("|", "\\|")
        lines.append(
            f"| {task.get('task_id', '')} | {task.get('mod', '')} | "
            f"{task.get('can_run_parallel', False)} | {resources} | {command} |"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pending qa/workflow_tasks.json tasks.")
    parser.add_argument("--tasks-json-path", default="qa/workflow_tasks.json")
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-serial", action="store_true")
    parser.add_argument("--include-gui", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-output-path", default="qa/workflow_task_scheduler_run.md")
    args = parser.parse_args()

    root = project_root()
    tasks_path = resolve_project_path(root, args.tasks_json_path, must_exist=True)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(tasks_path, qa_root) or not is_under(report_path, qa_root):
        raise ValueError("Task scheduler files must be under qa/.")

    payload = read_json(tasks_path)
    pending = executable_pending_tasks(payload, include_serial=args.include_serial, include_gui=args.include_gui)
    if args.limit > 0:
        pending = pending[: args.limit]
    if not pending:
        write_run_report(root, report_path, [], [])
        print("No pending executable workflow tasks selected.")
        return 0
    if args.dry_run:
        write_dry_run_report(root, report_path, pending)
        print(f"Workflow task dry-run report written to: {report_path}")
        print(f"Tasks selected: {len(pending)}")
        return 0

    results: list[TaskResult] = []
    running: dict[Future[TaskResult], dict[str, Any]] = {}
    held_resources: set[str] = set()
    queue = pending[:]
    max_workers = max(1, args.max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while queue or running:
            launched = False
            for task in queue[:]:
                resources = set(task_resources(task))
                needs_global = GLOBAL_RESOURCE in resources or GUI_RESOURCE in resources or task.get("can_run_parallel") is not True
                if resources & held_resources:
                    continue
                if needs_global and running:
                    continue
                if running and any(
                    GLOBAL_RESOURCE in set(task_resources(active)) or GUI_RESOURCE in set(task_resources(active)) or active.get("can_run_parallel") is not True
                    for active in running.values()
                ):
                    continue
                if len(running) >= max_workers:
                    break
                queue.remove(task)
                held_resources.update(resources)
                future = executor.submit(run_task, root, tasks_path, task, args.timeout_seconds)
                running[future] = task
                launched = True
            if not running:
                break
            if not launched and running:
                done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
            else:
                done, _ = wait(running.keys(), timeout=0)
            for future in list(done):
                task = running.pop(future)
                for resource in task_resources(task):
                    held_resources.discard(resource)
                result = future.result()
                results.append(result)
                update_task(
                    tasks_path,
                    result.task_id,
                    status=result.status,
                    finished_at=result.finished_at,
                    exit_code=result.exit_code,
                    output_tail=result.output_tail,
                    claim_owner="",
                    lease_until="",
                )

    refresh_output: list[str] = []
    if results and not args.no_refresh:
        refresh_output = refresh_state(root, args.timeout_seconds)
    write_run_report(root, report_path, results, refresh_output)
    print(f"Workflow task scheduler report written to: {report_path}")
    print(f"Tasks executed: {len(results)}")
    failed = sum(1 for result in results if result.status != "done")
    print(f"Failed tasks: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
