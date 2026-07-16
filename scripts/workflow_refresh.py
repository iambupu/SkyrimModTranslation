"""Canonical workflow refresh definitions.

Ordinary state refreshes must stay cheap and must never imply strict QA.  The
strict gate is an explicit operation because it may block on unfinished work.
"""

from __future__ import annotations

from dataclasses import dataclass

from project_paths import normalize_python_script_command


@dataclass(frozen=True)
class RefreshStep:
    name: str
    script: str
    args: tuple[str, ...] = ()


CORE_REFRESH_STEPS = (
    RefreshStep("translation-readiness", "audit_translation_readiness.py"),
    RefreshStep("workflow-state", "write_workflow_state.py"),
    RefreshStep("workflow-tasks", "write_workflow_tasks.py"),
    RefreshStep("codex-handoff", "write_codex_handoff.py"),
)

REPORT_REFRESH_STEPS = (
    RefreshStep("translation-readiness", "audit_translation_readiness.py"),
    RefreshStep("workflow-state", "write_workflow_state.py"),
    RefreshStep("workflow-health", "test_workflow_health.py"),
    RefreshStep("workflow-tasks", "write_workflow_tasks.py"),
    RefreshStep("codex-handoff", "write_codex_handoff.py"),
    RefreshStep("project-completion", "audit_project_completion.py"),
    RefreshStep("manual-game-test-plan", "new_manual_game_test_plan.py"),
    RefreshStep("manual-game-test-template", "new_manual_game_test_results_template.py"),
    RefreshStep("translation-goal-compliance", "audit_translation_goal_compliance.py"),
)


def core_refresh_commands() -> list[str]:
    return [
        normalize_python_script_command(f"python scripts/{step.script}")
        for step in CORE_REFRESH_STEPS
    ]


def report_refresh_steps(*, run_strict_gate: bool = False) -> list[RefreshStep]:
    steps: list[RefreshStep] = []
    for step in REPORT_REFRESH_STEPS:
        if step.name == "workflow-health" and run_strict_gate:
            steps.append(RefreshStep(step.name, step.script, ("--run-strict-gate",)))
        else:
            steps.append(step)
    return steps
