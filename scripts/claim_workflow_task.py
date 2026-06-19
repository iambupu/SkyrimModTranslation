"""Claim or release one pending workflow task.

This script only edits qa/workflow_tasks.json. It does not execute task commands
or change workflow_state.json.
"""

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from project_paths import is_under, project_root, resolve_project_path
from workflow_lock import ResourceLock


TASK_FILE_RESOURCE = "qa:workflow-tasks"


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


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def select_task(payload: dict[str, Any], task_id: str, mod_name: str) -> dict[str, Any] | None:
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    now = datetime.now()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task_id and str(task.get("task_id", "")) != task_id:
            continue
        if mod_name and str(task.get("mod", "")) != mod_name:
            continue
        status = str(task.get("status", ""))
        if status == "pending":
            return task
        if status == "running":
            lease_until = parse_time(str(task.get("lease_until", "")))
            if lease_until and lease_until < now:
                return task
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Claim one pending qa/workflow_tasks.json task.")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--owner", default="")
    parser.add_argument("--lease-minutes", type=int, default=60)
    parser.add_argument("--tasks-json-path", default="qa/workflow_tasks.json")
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()

    root = project_root()
    tasks_path = resolve_project_path(root, args.tasks_json_path, must_exist=True)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(tasks_path, qa_root):
        raise ValueError("Tasks JSON path must be under qa/.")

    owner = args.owner.strip() or f"pid:{os.getpid()}"
    lock = ResourceLock(root, TASK_FILE_RESOURCE, "claim_workflow_task.py").acquire()
    try:
        payload = read_json(tasks_path)
        task = None
        if args.release:
            for candidate in payload.get("tasks", []):
                if not isinstance(candidate, dict):
                    continue
                if args.task_id and str(candidate.get("task_id", "")) != args.task_id:
                    continue
                if args.mod_name and str(candidate.get("mod", "")) != args.mod_name:
                    continue
                task = candidate
                break
            if not task:
                raise ValueError("No matching task found to release.")
            task["status"] = "pending" if task.get("executable") else "pending_manual"
            task["claim_owner"] = ""
            task["lease_until"] = ""
            write_json(tasks_path, payload)
            print(f"Released workflow task: {task.get('task_id', '')}")
            return 0

        task = select_task(payload, args.task_id.strip(), args.mod_name.strip())
        if not task:
            print("No claimable workflow task found.")
            return 2
        task["status"] = "running"
        task["claim_owner"] = owner
        task["lease_until"] = (datetime.now() + timedelta(minutes=max(1, args.lease_minutes))).strftime("%Y-%m-%d %H:%M:%S")
        task["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_json(tasks_path, payload)
        print(json.dumps(task, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
