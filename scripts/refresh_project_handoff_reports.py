"""Refresh project-level handoff and completion reports in dependency order."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from project_paths import plugin_root as default_plugin_root
from project_paths import plugin_script_path
from project_paths import project_root
from project_paths import resolve_project_path
from workflow_lock import WorkflowLock


@dataclass
class Step:
    Name: str
    Script: str
    Status: str
    ReturnCode: int
    Output: list[str]


def run_python_script(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    source_root = default_plugin_root()
    script_path = plugin_script_path(script_name)
    if not script_path.is_file():
        raise FileNotFoundError(f"missing plugin script: scripts/{script_name}")
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=root,
        env={**os.environ, "SKYRIM_CHS_WORKSPACE_ROOT": str(root), "SKYRIM_CHS_PLUGIN_ROOT": str(source_root)},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def output_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def report_metric(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    prefix = f"- {name}:"
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


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
    parser.add_argument("--skip-strict-gate", action="store_true")
    parser.add_argument("--report-output-path", default="qa/project_handoff_refresh.md")
    parser.add_argument("--json-output-path", default="qa/project_handoff_refresh.json")
    args = parser.parse_args()

    root = project_root()
    WorkflowLock(root, "refresh_project_handoff_reports.py").acquire()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)

    ordered_scripts: list[tuple[str, str, list[str]]] = [
        ("translation-readiness", "audit_translation_readiness.py", []),
        ("workflow-state", "write_workflow_state.py", []),
        ("workflow-health", "test_workflow_health.py", [] if args.skip_strict_gate else ["--run-strict-gate"]),
        ("project-completion", "audit_project_completion.py", []),
        ("manual-game-test-plan", "new_manual_game_test_plan.py", []),
        ("manual-game-test-template", "new_manual_game_test_results_template.py", []),
        ("translation-goal-compliance", "audit_translation_goal_compliance.py", []),
    ]

    steps: list[Step] = []
    for name, script_name, script_args in ordered_scripts:
        result = run_python_script(root, script_name, script_args)
        status = "passed" if result.returncode == 0 else "failed"
        steps.append(Step(name, script_name, status, result.returncode, output_lines(result)))
        if result.returncode != 0:
            break

    blocking = write_reports(root, steps, started_at, report_path, json_path)
    print(f"Project handoff refresh report written to: {report_path}")
    print(f"Project handoff refresh JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
