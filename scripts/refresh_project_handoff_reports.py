"""Refresh project-level handoff and completion reports in dependency order."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from model_review_contract import read_report_metric as report_metric
from project_paths import project_root
from project_paths import resolve_project_path
from workflow_lock import WorkflowLock
from workflow_refresh import report_refresh_steps
from workflow_process import run_plugin_python as run_python_script
from report_utils import subprocess_output_lines as output_lines


@dataclass
class Step:
    Name: str
    Script: str
    Status: str
    ReturnCode: int
    Output: list[str]



def write_reports(root: Path, steps: list[Step], started_at: str, report_path: Path, json_path: Path) -> int:
    blocking = sum(1 for step in steps if step.ReturnCode != 0)
    warnings = 0
    status = "PASS" if blocking == 0 else "FAIL"
    finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Project Handoff Report Refresh",
        "",
        f"- ProjectRoot: {root}",
        f"- StartedAt: {started_at}",
        f"- FinishedAt: {finished_at}",
        f"- Verdict: {status}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Steps",
        "",
        "| Step | Script | Status | Return code |",
        "|---|---|---|---:|",
    ]
    for step in steps:
        lines.append(f"| {step.Name} | scripts/{step.Script} | {step.Status} | {step.ReturnCode} |")
    lines.extend(
        [
            "",
            "## Final State",
            "",
            f"- Translation readiness: {report_metric(root / 'qa' / 'translation_readiness.md', 'Overall status')}",
            f"- Workflow state: {report_metric(root / 'qa' / 'workflow_state.md', 'Project state')}",
            f"- Workflow health blocking: {report_metric(root / 'qa' / 'workflow_health.md', 'Blocking issues')}",
            f"- Project completion blocking: {report_metric(root / 'qa' / 'project_completion_audit.md', 'Blocking issues')}",
            f"- Goal compliance blocking: {report_metric(root / 'qa' / 'translation_goal_compliance.md', 'Project-local blocking issues')}",
            "",
            "## Safety",
            "",
            "- This script only runs project-local Python report generators.",
            "- It does not translate text, edit plugin/PEX/BSA/BA2 binaries, or access real game/mod-manager paths.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "ProjectRoot": str(root),
                "StartedAt": started_at,
                "FinishedAt": finished_at,
                "Verdict": status,
                "BlockingIssues": blocking,
                "Warnings": warnings,
                "Steps": [asdict(step) for step in steps],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return blocking


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh project handoff reports in the required dependency order.")
    strict_group = parser.add_mutually_exclusive_group()
    strict_group.add_argument(
        "--run-strict-gate",
        action="store_true",
        help="Explicitly run strict non-GUI QA as part of the workflow-health step.",
    )
    strict_group.add_argument(
        "--skip-strict-gate",
        dest="run_strict_gate",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--report-output-path", default="qa/project_handoff_refresh.md")
    parser.add_argument("--json-output-path", default="qa/project_handoff_refresh.json")
    args = parser.parse_args()

    root = project_root()
    WorkflowLock(root, "refresh_project_handoff_reports.py").acquire()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)

    steps: list[Step] = []
    for refresh_step in report_refresh_steps(run_strict_gate=args.run_strict_gate):
        result = run_python_script(root, refresh_step.script, list(refresh_step.args))
        status = "passed" if result.returncode == 0 else "failed"
        steps.append(
            Step(
                refresh_step.name,
                refresh_step.script,
                status,
                result.returncode,
                output_lines(result),
            )
        )
        if result.returncode != 0:
            break

    blocking = write_reports(root, steps, started_at, report_path, json_path)
    print(f"Project handoff refresh report written to: {report_path}")
    print(f"Project handoff refresh JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
