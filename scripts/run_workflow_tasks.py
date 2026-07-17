"""Run pending tasks from qa/workflow_tasks.json with conservative locks."""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from audit_mod_scale import default_scale_config_path, load_scale_config
from project_paths import (
    is_under,
    plugin_root as default_plugin_root,
    project_root,
    relative_path,
    resolve_project_path,
    safe_file_name,
)
from workflow_agent_log import append_workflow_agent_event
from workflow_lock import ResourceLock
from workflow_task_policy import (
    GUI_RESOURCE,
    dependencies_satisfied,
    lease_minutes_for_timeout,
    mark_workflow_task_running,
    project_python_argv,
    resources_available,
    resources_conflict,
    task_can_be_started,
    task_is_serial,
    task_resources,
    update_workflow_task,
)
from report_utils import subprocess_output_lines as output_lines
from file_utils import read_json_object_if_exists_strict as read_json
from game_context import load_game_context


TASK_FILE_RESOURCE = "qa:workflow-tasks"
SHARED_LOCK_WAIT_SECONDS = 30
update_task = partial(
    update_workflow_task,
    lock_owner="run_workflow_tasks.py",
    lock_resource=TASK_FILE_RESOURCE,
    timeout_seconds=SHARED_LOCK_WAIT_SECONDS,
)
mark_task_running_if_pending = partial(
    mark_workflow_task_running,
    lock_owner="run_workflow_tasks.py",
    lock_resource=TASK_FILE_RESOURCE,
    timeout_seconds=SHARED_LOCK_WAIT_SECONDS,
)


@dataclass
class TaskResult:
    task_id: str
    status: str
    exit_code: int
    output_tail: list[str]
    finished_at: str


def task_execution_class(task: dict[str, Any]) -> str:
    text = " ".join([str(task.get("command", "")), *task_resources(task)]).casefold()
    if any(token in text for token in ("bsa", "ba2", "archive")):
        return "archive"
    if any(token in text for token in ("plugin", "pex", ".esp", ".esm", ".esl")):
        return "binary"
    return "text"


def scheduler_execution_policy(
    root: Path,
    tasks: list[dict[str, Any]],
    *,
    max_workers: int | None,
    max_binary: int | None,
    max_archive: int | None,
    timeout_seconds: int | None,
) -> dict[str, object]:
    config = load_scale_config(default_scale_config_path())
    context = load_game_context(root)
    absolute = config["absolute_limits"]
    profile_values: dict[str, list[int]] = {
        "max_parallel_tasks": [],
        "max_parallel_binary_tasks": [],
        "max_parallel_archive_tasks": [],
        "timeout_seconds": [],
    }
    reports: list[str] = []
    for mod_name in sorted({str(task.get("mod") or "") for task in tasks if str(task.get("mod") or "")}):
        if safe_file_name(mod_name) != mod_name:
            raise ValueError(f"Workflow task contains a non-canonical Mod name: {mod_name!r}")
        path = root / "qa" / f"{mod_name}.scale_execution.json"
        if not path.is_file():
            continue
        payload = read_json(path)
        effective = payload.get("effective") if payload.get("status") == "ready" else None
        if not isinstance(effective, dict):
            raise ValueError(f"Scale execution report is not ready for scheduler: {path}")
        if payload.get("game_id") != context.game_id:
            raise ValueError(f"Scale execution report game_id does not match the current workspace: {path}")
        reports.append(relative_path(root, path).replace("\\", "/"))
        for key in profile_values:
            value = effective.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                profile_values[key].append(value)

    requested = {
        "max_parallel_tasks": max_workers,
        "max_parallel_binary_tasks": max_binary,
        "max_parallel_archive_tasks": max_archive,
        "timeout_seconds": timeout_seconds,
    }
    fallbacks = {
        "max_parallel_tasks": 2,
        "max_parallel_binary_tasks": 1,
        "max_parallel_archive_tasks": 1,
        "timeout_seconds": 1800,
    }
    effective_values: dict[str, int] = {}
    overrides: dict[str, int] = {}
    for key, requested_value in requested.items():
        value = requested_value
        if value is None:
            value = min(profile_values[key]) if profile_values[key] else fallbacks[key]
        else:
            overrides[key] = value
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        cap = int(absolute[key])
        if value > cap:
            raise ValueError(f"{key}={value} exceeds absolute safety cap {cap}")
        effective_values[key] = value
    return {
        "effective": effective_values,
        "overrides": overrides,
        "scale_execution_reports": reports,
    }



def executable_pending_tasks(payload: dict[str, Any], *, include_serial: bool, include_gui: bool) -> list[dict[str, Any]]:
    now = datetime.now()
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return []
    result: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("executable") is not True or not task_can_be_started(task, now):
            continue
        if not dependencies_satisfied(payload, task):
            continue
        if not resources_available(payload, task):
            continue
        resources = task_resources(task)
        if GUI_RESOURCE in resources and not include_gui:
            continue
        if not include_serial and task.get("can_run_parallel") is not True:
            continue
        result.append(task)
    return result


def task_log_details(task: dict[str, Any], message: str = "") -> str:
    details = {
        "task_id": str(task.get("task_id", "")),
        "owner": str(task.get("claim_owner", "")),
        "resource_locks": task_resources(task),
    }
    if message:
        details["message"] = message
    return json.dumps(details, ensure_ascii=False, sort_keys=True)


def log_task_event(task: dict[str, Any], event: str, status: str, message: str = "") -> None:
    try:
        append_workflow_agent_event(
            mod_name=str(task.get("mod", "")),
            state=str(task.get("stage", "")),
            event=event,
            action=str(task.get("command", "")) or str(task.get("kind", "")),
            status=status,
            evidence=str(task.get("evidence", "")),
            details=task_log_details(task, message),
            task_id=str(task.get("task_id", "")),
            owner=str(task.get("claim_owner", "")),
            resource_locks=task_resources(task),
        )
    except Exception as exc:
        print(f"Warning: workflow agent log append failed: {exc}", file=sys.stderr)


def run_task(root: Path, task: dict[str, Any], timeout_seconds: int) -> TaskResult:
    task_id = str(task.get("task_id", ""))
    resources = task_resources(task)
    acquired: list[ResourceLock] = []
    try:
        for resource in sorted(resources):
            acquired.append(ResourceLock(root, resource, f"run_workflow_tasks.py:{task_id}").acquire())
        argv = project_python_argv(str(task.get("command", "")))
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


def refresh_state(root: Path, timeout_seconds: int) -> tuple[list[str], bool]:
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
            return output[-60:], False
    return output[-60:], True


def write_run_report(
    root: Path,
    report_path: Path,
    results: list[TaskResult],
    refresh_output: list[str],
    policy: dict[str, object],
    *,
    refresh_passed: bool | None,
) -> None:
    lines = [
        "# Workflow Task Scheduler Run",
        "",
        f"- Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Tasks selected: {len(results)}",
        f"- Tasks completed: {sum(1 for result in results if result.status == 'done')}",
        f"- Tasks skipped: {sum(1 for result in results if result.status == 'skipped')}",
        f"- Failed tasks: {sum(1 for result in results if result.status == 'failed')}",
        f"- State refresh: {'passed' if refresh_passed is True else 'failed' if refresh_passed is False else 'not-run'}",
        f"- Execution policy: {json.dumps(policy, ensure_ascii=False, sort_keys=True)}",
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


def write_dry_run_report(root: Path, report_path: Path, tasks: list[dict[str, Any]], policy: dict[str, object]) -> None:
    lines = [
        "# Workflow Task Scheduler Dry Run",
        "",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Tasks selected: {len(tasks)}",
        f"- Execution policy: {json.dumps(policy, ensure_ascii=False, sort_keys=True)}",
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
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--max-binary-workers", type=int)
    parser.add_argument("--max-archive-workers", type=int)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-serial", action="store_true")
    parser.add_argument("--include-gui", action="store_true")
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--lease-minutes", type=int, default=0, help="Task lease duration. Defaults to timeout plus 5 minutes.")
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
    policy = scheduler_execution_policy(
        root,
        pending,
        max_workers=args.max_workers,
        max_binary=args.max_binary_workers,
        max_archive=args.max_archive_workers,
        timeout_seconds=args.timeout_seconds,
    )
    effective_policy = policy["effective"]
    if not isinstance(effective_policy, dict):
        raise ValueError("Scheduler execution policy is invalid")
    effective_timeout = int(effective_policy["timeout_seconds"])
    if not pending:
        write_run_report(root, report_path, [], [], policy, refresh_passed=None)
        print("No pending executable workflow tasks selected.")
        return 0
    if args.dry_run:
        write_dry_run_report(root, report_path, pending, policy)
        print(f"Workflow task dry-run report written to: {report_path}")
        print(f"Tasks selected: {len(pending)}")
        return 0

    results: list[TaskResult] = []
    running: dict[Future[TaskResult], dict[str, Any]] = {}
    queue = pending[:]
    max_workers = int(effective_policy["max_parallel_tasks"])
    max_binary_workers = int(effective_policy["max_parallel_binary_tasks"])
    max_archive_workers = int(effective_policy["max_parallel_archive_tasks"])
    lease_minutes = lease_minutes_for_timeout(effective_timeout, args.lease_minutes)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while queue or running:
            launched = False
            for task in queue[:]:
                resources = set(task_resources(task))
                needs_global = task_is_serial(task)
                if needs_global and running:
                    continue
                if running and any(task_is_serial(active) for active in running.values()):
                    continue
                if any(resources_conflict(resources, set(task_resources(active))) for active in running.values()):
                    continue
                execution_class = task_execution_class(task)
                active_same_class = sum(
                    1 for active in running.values() if task_execution_class(active) == execution_class
                )
                if execution_class == "binary" and active_same_class >= max_binary_workers:
                    continue
                if execution_class == "archive" and active_same_class >= max_archive_workers:
                    continue
                if len(running) >= max_workers:
                    break
                queue.remove(task)
                if not mark_task_running_if_pending(tasks_path, str(task.get("task_id", "")), lease_minutes):
                    message = "Task was already claimed, completed, or blocked by an active lease."
                    log_task_event(task, "command", "skipped", message)
                    results.append(
                        TaskResult(
                            str(task.get("task_id", "")),
                            "skipped",
                            0,
                            [message],
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        )
                    )
                    continue
                task["claim_owner"] = f"pid:{os.getpid()}"
                log_task_event(task, "command", "started")
                future = executor.submit(run_task, root, task, effective_timeout)
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
                result = future.result()
                results.append(result)
                if result.status != "skipped":
                    log_task_event(
                        task,
                        "command",
                        "passed" if result.status == "done" else "failed",
                        "\n".join(result.output_tail[-5:]),
                    )
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
    refresh_passed: bool | None = None
    if results and not args.no_refresh:
        refresh_output, refresh_passed = refresh_state(root, effective_timeout)
    write_run_report(
        root,
        report_path,
        results,
        refresh_output,
        policy,
        refresh_passed=refresh_passed,
    )
    print(f"Workflow task scheduler report written to: {report_path}")
    print(f"Tasks selected: {len(results)}")
    print(f"Tasks skipped: {sum(1 for result in results if result.status == 'skipped')}")
    failed = sum(1 for result in results if result.status == "failed")
    print(f"Failed tasks: {failed}")
    return 1 if failed or refresh_passed is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
