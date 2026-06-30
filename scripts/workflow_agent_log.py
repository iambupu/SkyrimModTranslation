"""Shared append-only workflow agent logging helpers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from project_paths import is_under, project_root, relative_path, resolve_project_path
from workflow_lock import ResourceLock


AGENT_LOG_RESOURCE = "qa:workflow-agent-runs"


def _relative_evidence(root: Path, evidence: str) -> str:
    if not evidence.strip():
        return ""
    try:
        evidence_path = resolve_project_path(root, evidence, must_exist=False)
    except (OSError, ValueError):
        return ""
    return relative_path(root, evidence_path)


def append_workflow_agent_event(
    *,
    mod_name: str,
    state: str,
    event: str,
    action: str,
    status: str,
    evidence: str = "",
    details: str = "",
    task_id: str = "",
    owner: str = "",
    resource_locks: list[str] | None = None,
    log_path: str = "qa/workflow_agent_runs.jsonl",
) -> None:
    root = project_root()
    resolved_log_path = resolve_project_path(root, log_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(resolved_log_path, qa_root):
        raise ValueError("LogPath must be under qa/.")

    row: dict[str, Any] = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mod": mod_name,
        "state": state,
        "event": event,
        "action": action,
        "status": status,
        "evidence": _relative_evidence(root, evidence),
        "details": details,
    }
    if task_id:
        row["task_id"] = task_id
    if owner:
        row["owner"] = owner
    if resource_locks:
        row["resource_locks"] = resource_locks

    lock = ResourceLock(root, AGENT_LOG_RESOURCE, "workflow_agent_log.py").acquire()
    try:
        resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
        with resolved_log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        lock.release()
