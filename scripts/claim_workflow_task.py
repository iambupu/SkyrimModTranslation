"""Claim, release, or complete one workflow task.

This script edits qa/workflow_tasks.json and appends claim/complete/release
events to qa/workflow_agent_runs.jsonl. It does not execute task commands or
change workflow_state.json.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

from project_paths import is_under, project_root, resolve_project_path
from workflow_agent_log import append_workflow_agent_event
from workflow_lock import ResourceLock
from workflow_task_policy import (
    dependencies_satisfied,
    lease_is_active,
    resources_available,
    resources_conflict as resources_conflict,
    task_resources,
)
from file_utils import read_json_object_if_exists_strict as read_json, write_json


TASK_FILE_RESOURCE = "qa:workflow-tasks"
TERMINAL_STATUSES = {"done", "failed", "blocked", "skipped"}
SHARED_LOCK_WAIT_SECONDS = 30



def task_matches(task: dict[str, Any], task_id: str, mod_name: str, resource_lock: str = "") -> bool:
    if task_id and str(task.get("task_id", "")) != task_id:
        return False
    if mod_name and str(task.get("mod", "")) != mod_name:
        return False
    if resource_lock and resource_lock not in task_resources(task):
        return False
    return True


def select_task(payload: dict[str, Any], task_id: str, mod_name: str, resource_lock: str, *, parallel_only: bool) -> dict[str, Any] | None:
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return None
    now = datetime.now()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if not task_matches(task, task_id, mod_name, resource_lock):
            continue
        if parallel_only and task.get("can_run_parallel") is not True:
            continue
        if not dependencies_satisfied(payload, task):
            continue
        if not resources_available(payload, task, now):
            continue
        status = str(task.get("status", ""))
        if status == "pending":
            return task
        if status == "running" and not lease_is_active(task, now):
            return task
    return None


def log_task_event(task: dict[str, Any], event: str, status: str, owner: str, details: str = "") -> None:
    payload = {
        "task_id": str(task.get("task_id", "")),
        "owner": owner,
        "resource_locks": task_resources(task),
    }
    if details:
        payload["message"] = details
    try:
        append_workflow_agent_event(
            mod_name=str(task.get("mod", "")),
            state=str(task.get("stage", "")),
            event=event,
            action=str(task.get("command", "")) or str(task.get("kind", "")),
            status=status,
            evidence=str(task.get("evidence", "")),
            details=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            task_id=str(task.get("task_id", "")),
            owner=owner,
            resource_locks=task_resources(task),
        )
    except Exception as exc:
        print(f"Warning: workflow agent log append failed: {exc}", file=sys.stderr)


def completion_log_status(status: str) -> str:
    if status == "done":
        return "passed"
    if status in {"failed", "blocked", "skipped"}:
        return status
    return "noted"


def complete_task(
    payload: dict[str, Any],
    task_id: str,
    mod_name: str,
    resource_lock: str,
    owner: str,
    status: str,
    exit_code: int,
    output_tail: str,
) -> dict[str, Any]:
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("workflow_tasks.json tasks must be an array.")
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if not task_matches(task, task_id, mod_name, resource_lock):
            continue
        if str(task.get("status", "")) != "running":
            raise ValueError("Only a running claimed task can be completed.")
        claim_owner = str(task.get("claim_owner", ""))
        if not claim_owner:
            raise ValueError("Task has no claim owner; claim it before completing.")
        if claim_owner != owner:
            raise ValueError(f"Task is claimed by another owner: {claim_owner}")
        task["status"] = status
        task["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task["exit_code"] = exit_code
        task["output_tail"] = [output_tail] if output_tail else []
        task["claim_owner"] = ""
        task["lease_until"] = ""
        return task
    raise ValueError("No matching task found to complete.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Claim, release, or complete one qa/workflow_tasks.json task.")
    parser.add_argument("--task-id", default="", help="Claim or complete a specific task id.")
    parser.add_argument("--mod-name", default="", help="Limit claim/release/complete to one Mod lane.")
    parser.add_argument("--resource-lock", default="", help="Limit claim/release/complete to one file/resource lane.")
    parser.add_argument("--owner", default="", help="Stable subagent id, for example mod-agent:<ModName>.")
    parser.add_argument("--lease-minutes", type=int, default=60)
    parser.add_argument("--tasks-json-path", default="qa/workflow_tasks.json")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--parallel-only", action="store_true", help="Only claim tasks marked can_run_parallel=true.")
    parser.add_argument("--complete", action="store_true", help="Mark a claimed task as finished.")
    parser.add_argument("--complete-status", choices=sorted(TERMINAL_STATUSES), default="done")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--output-tail", default="")
    args = parser.parse_args()

    root = project_root()
    tasks_path = resolve_project_path(root, args.tasks_json_path, must_exist=True)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(tasks_path, qa_root):
        raise ValueError("Tasks JSON path must be under qa/.")

    owner = args.owner.strip() or f"pid:{os.getpid()}"
    lock = ResourceLock(root, TASK_FILE_RESOURCE, "claim_workflow_task.py").acquire(
        timeout_seconds=SHARED_LOCK_WAIT_SECONDS
    )
    try:
        payload = read_json(tasks_path)
        task = None
        if args.complete:
            if not args.task_id.strip():
                raise ValueError("--complete requires --task-id.")
            task = complete_task(
                payload,
                args.task_id.strip(),
                args.mod_name.strip(),
                args.resource_lock.strip(),
                owner,
                args.complete_status,
                args.exit_code,
                args.output_tail.strip(),
            )
            write_json(tasks_path, payload)
            log_task_event(task, "complete", completion_log_status(args.complete_status), owner, args.output_tail.strip())
            print(f"Completed workflow task: {task.get('task_id', '')} ({args.complete_status})")
            return 0

        if args.release:
            if not args.task_id.strip():
                raise ValueError("--release requires --task-id.")
            for candidate in payload.get("tasks", []):
                if not isinstance(candidate, dict):
                    continue
                if not task_matches(candidate, args.task_id.strip(), args.mod_name.strip(), args.resource_lock.strip()):
                    continue
                task = candidate
                break
            if not task:
                raise ValueError("No matching task found to release.")
            if str(task.get("status", "")) != "running":
                raise ValueError("Only a running claimed task can be released.")
            claim_owner = str(task.get("claim_owner", ""))
            if not claim_owner:
                raise ValueError("Task has no claim owner; claim it before releasing.")
            if claim_owner != owner:
                raise ValueError(f"Task is claimed by another owner: {claim_owner}")
            task["status"] = "pending" if task.get("executable") else "pending_manual"
            task["claim_owner"] = ""
            task["lease_until"] = ""
            task["started_at"] = ""
            write_json(tasks_path, payload)
            log_task_event(task, "release", "noted", owner)
            print(f"Released workflow task: {task.get('task_id', '')}")
            return 0

        task = select_task(
            payload,
            args.task_id.strip(),
            args.mod_name.strip(),
            args.resource_lock.strip(),
            parallel_only=args.parallel_only,
        )
        if not task:
            print("No claimable workflow task found.")
            return 2
        previous_owner = str(task.get("claim_owner", ""))
        previous_lease = str(task.get("lease_until", ""))
        task["status"] = "running"
        task["claim_owner"] = owner
        task["lease_until"] = (datetime.now() + timedelta(minutes=max(1, args.lease_minutes))).strftime("%Y-%m-%d %H:%M:%S")
        task["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_json(tasks_path, payload)
        reclaim_note = ""
        if previous_owner or previous_lease:
            reclaim_note = f"reclaimed stale task from owner={previous_owner} lease_until={previous_lease}"
        log_task_event(task, "claim", "started", owner, reclaim_note)
        print(json.dumps(task, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
