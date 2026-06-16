"""Append a project-local Codex workflow agent trace row.

This script records orchestration attempts only. It does not translate, rebuild,
validate, or touch real game/mod-manager paths.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from project_paths import is_under, project_root, relative_path, resolve_project_path


def append_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a workflow agent orchestration event to qa/workflow_agent_runs.jsonl.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--event", required=True, help="inspect, command, repair, refresh, blocked, or note")
    parser.add_argument("--action", required=True)
    parser.add_argument("--status", required=True, help="started, passed, failed, skipped, blocked, or noted")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--details", default="")
    parser.add_argument("--log-path", default="qa/workflow_agent_runs.jsonl")
    args = parser.parse_args()

    root = project_root()
    log_path = resolve_project_path(root, args.log_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(log_path, qa_root):
        raise ValueError("LogPath must be under qa/.")

    evidence = ""
    if args.evidence.strip():
        evidence_path = resolve_project_path(root, args.evidence, must_exist=False)
        evidence = relative_path(root, evidence_path)

    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mod": args.mod_name,
        "state": args.state,
        "event": args.event,
        "action": args.action,
        "status": args.status,
        "evidence": evidence,
        "details": args.details,
    }
    append_jsonl(log_path, row)
    print(f"Workflow agent log appended: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
