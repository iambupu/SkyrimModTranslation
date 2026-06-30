"""Append a project-local Codex workflow agent trace row.

This script records orchestration attempts only. It does not translate, rebuild,
validate, or touch real game/mod-manager paths.
"""

import argparse

from workflow_agent_log import append_workflow_agent_event


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

    append_workflow_agent_event(
        mod_name=args.mod_name,
        state=args.state,
        event=args.event,
        action=args.action,
        status=args.status,
        evidence=args.evidence,
        details=args.details,
        log_path=args.log_path,
    )
    print(f"Workflow agent log appended: {args.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
