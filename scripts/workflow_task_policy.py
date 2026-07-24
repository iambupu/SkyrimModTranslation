"""Shared workflow-task scheduling, dependency, lease, and resource rules."""

from __future__ import annotations

import os
import shlex
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from project_paths import is_under, plugin_root
from file_utils import read_json_object_if_exists_strict, write_json
from project_paths import project_root
from workflow_lock import ResourceLock


GLOBAL_RESOURCE = "global:workflow-state"
GUI_RESOURCE = "gui:desktop"


def update_workflow_task(
    tasks_path: Path,
    task_id: str,
    *,
    lock_owner: str,
    lock_resource: str,
    timeout_seconds: int,
    **fields: Any,
) -> None:
    root = project_root()
    lock = ResourceLock(root, lock_resource, lock_owner).acquire(timeout_seconds=timeout_seconds)
    try:
        payload = read_json_object_if_exists_strict(tasks_path)
        for task in payload.get("tasks", []):
            if isinstance(task, dict) and str(task.get("task_id", "")) == task_id:
                task.update(fields)
                break
        write_json(tasks_path, payload)
    finally:
        lock.release()


def mark_workflow_task_running(
    tasks_path: Path,
    task_id: str,
    lease_minutes: int,
    *,
    lock_owner: str,
    lock_resource: str,
    timeout_seconds: int,
    reset_completion_fields: bool = False,
) -> bool:
    root = project_root()
    lock = ResourceLock(root, lock_resource, lock_owner).acquire(timeout_seconds=timeout_seconds)
    try:
        payload = read_json_object_if_exists_strict(tasks_path)
        now = datetime.now()
        for task in payload.get("tasks", []):
            if not isinstance(task, dict) or str(task.get("task_id", "")) != task_id:
                continue
            if (
                not task_can_be_started(task, now)
                or not dependencies_satisfied(payload, task)
                or not resources_available(payload, task)
            ):
                return False
            task["status"] = "running"
            task["claim_owner"] = f"pid:{os.getpid()}"
            task["lease_until"] = (now + timedelta(minutes=max(1, lease_minutes))).strftime("%Y-%m-%d %H:%M:%S")
            task["started_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            if reset_completion_fields:
                task["finished_at"] = ""
                task["exit_code"] = None
                task["output_tail"] = []
            write_json(tasks_path, payload)
            return True
        return False
    finally:
        lock.release()


def split_task_command(command: str, *, strict: bool = True) -> list[str]:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        if strict:
            raise
        return []
    return [part.strip().strip('"').strip("'") for part in parts if part.strip()]


def python_runner_ok(value: str) -> bool:
    return Path(value).name.lower() in {"python", "python.exe", "py", "py.exe"}


def project_python_argv(
    command: str,
    *,
    python_executable: str | Path | None = None,
) -> list[str]:
    source_root = plugin_root()
    parts = split_task_command(command)
    if len(parts) < 2:
        raise ValueError(f"Task command is too short: {command}")
    if not python_runner_ok(parts[0]):
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
    return [str(python_executable or sys.executable), str(script), *parts[2:]]


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def task_resources(task: dict[str, Any]) -> list[str]:
    resources = task.get("resource_locks", [])
    if not isinstance(resources, list):
        return []
    return [str(resource) for resource in resources if str(resource).strip()]


def _mod_lock_name(resource: str) -> str:
    if not resource.startswith("mod:"):
        return ""
    return resource.split(":", 1)[1].strip()


def _scoped_resource_mod(resource: str) -> str:
    if not (resource.startswith("file:") or resource.startswith("resource:")):
        return ""
    parts = resource.split(":", 2)
    return parts[1].strip() if len(parts) >= 3 else ""


def resources_conflict(left: set[str], right: set[str]) -> bool:
    if left & right:
        return True
    for left_resource in left:
        left_mod = _mod_lock_name(left_resource)
        left_scoped_mod = _scoped_resource_mod(left_resource)
        for right_resource in right:
            right_mod = _mod_lock_name(right_resource)
            right_scoped_mod = _scoped_resource_mod(right_resource)
            if left_mod and right_scoped_mod and left_mod == right_scoped_mod:
                return True
            if right_mod and left_scoped_mod and right_mod == left_scoped_mod:
                return True
    return False


def lease_is_active(task: dict[str, Any], now: datetime) -> bool:
    if str(task.get("status", "")) != "running":
        return False
    lease_until = parse_time(str(task.get("lease_until", "")))
    return bool(lease_until and lease_until >= now)


def task_can_be_started(task: dict[str, Any], now: datetime) -> bool:
    status = str(task.get("status", ""))
    return status == "pending" or (status == "running" and not lease_is_active(task, now))


def lease_minutes_for_timeout(timeout_seconds: int, requested_minutes: int) -> int:
    if requested_minutes > 0:
        return requested_minutes
    timeout_minutes = max(1, (max(1, timeout_seconds) + 59) // 60)
    return timeout_minutes + 5


def task_is_serial(task: dict[str, Any]) -> bool:
    resources = set(task_resources(task))
    return (
        task.get("can_run_parallel") is not True
        or GLOBAL_RESOURCE in resources
        or GUI_RESOURCE in resources
    )


def active_running_tasks(
    payload: dict[str, Any],
    *,
    ignore_task_id: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current_time = now or datetime.now()
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return []
    return [
        task
        for task in tasks
        if isinstance(task, dict)
        and (not ignore_task_id or str(task.get("task_id", "")) != ignore_task_id)
        and lease_is_active(task, current_time)
    ]


def resources_available(
    payload: dict[str, Any],
    task: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    active = active_running_tasks(
        payload,
        ignore_task_id=str(task.get("task_id", "")),
        now=now,
    )
    if not active:
        return True
    if task_is_serial(task) or any(task_is_serial(candidate) for candidate in active):
        return False
    resources = set(task_resources(task))
    return not any(
        resources_conflict(resources, set(task_resources(candidate)))
        for candidate in active
    )


def dependencies_satisfied(payload: dict[str, Any], task: dict[str, Any]) -> bool:
    dependencies = task.get("dependencies", [])
    if not dependencies:
        return True
    if not isinstance(dependencies, list):
        return False
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return False
    status_by_id = {
        str(candidate.get("task_id", "")): str(candidate.get("status", ""))
        for candidate in tasks
        if isinstance(candidate, dict)
    }
    return all(status_by_id.get(str(dependency), "") == "done" for dependency in dependencies)
