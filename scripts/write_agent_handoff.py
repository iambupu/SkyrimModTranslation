"""Write an agent-neutral handoff view.

This is intentionally separate from write_codex_handoff.py so the existing
Codex hot path keeps its current cost and output contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from project_paths import is_under, project_root, resolve_project_path
from write_codex_handoff import build_handoff, markdown_cell


CHECKPOINT_NEXT_READ_SET = [
    ".workflow/progress_card.json",
    ".workflow/progress_card.md",
    "qa/agent_handoff.json",
    "qa/codex_handoff.json",
    "qa/workflow_state.json",
    "qa/workflow_tasks.json",
    "qa/translation_readiness.json",
    "qa/workflow_health.json",
]
CHECKPOINT_STALE_WATCH_PATHS = [
    "mod",
    "source",
    "translated",
    "work",
    "out",
    "qa/workflow_state.json",
    "qa/workflow_tasks.json",
    "qa/translation_readiness.json",
    "qa/workflow_health.json",
    "qa/codex_handoff.json",
]
CHECKPOINT_REPORT_REFS = [
    ".workflow/progress_card.md",
    "qa/workflow_state.md",
    "qa/workflow_tasks.md",
    "qa/translation_readiness.md",
    "qa/workflow_health.md",
    "qa/codex_handoff.md",
]


def normalize_project_ref(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or candidate.root or ".." in candidate.parts:
        return ""
    return text.strip("/")


def normalized_project_path(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()


def validate_agent_handoff_outputs(root: Path, json_path: Path, report_path: Path) -> None:
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(json_path, qa_root) or not is_under(report_path, qa_root):
        raise ValueError("Agent handoff outputs must be under qa/.")
    if normalized_project_path(root, json_path) != "qa/agent_handoff.json":
        raise ValueError("Agent handoff JSON output must be qa/agent_handoff.json.")
    if normalized_project_path(root, report_path) != "qa/agent_handoff.md":
        raise ValueError("Agent handoff report output must be qa/agent_handoff.md.")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mtime_utc(path: Path) -> str:
    try:
        value = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return ""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def path_snapshot(root: Path, rel_path: str, *, max_entries: int = 2000) -> dict[str, object]:
    normalized = normalize_project_ref(rel_path)
    if not normalized:
        return {
            "path": str(rel_path),
            "exists": False,
            "kind": "unsafe_path",
            "mtime_utc": "",
            "latest_mtime_utc": "",
            "scanned_entries": 0,
            "truncated": False,
        }
    path = root / normalized
    exists = path.exists()
    snapshot: dict[str, object] = {
        "path": normalized,
        "exists": exists,
        "kind": "missing",
        "mtime_utc": "",
        "latest_mtime_utc": "",
        "scanned_entries": 0,
        "truncated": False,
    }
    if not exists:
        return snapshot
    if path.is_file():
        current_mtime = mtime_utc(path)
        snapshot.update({"kind": "file", "mtime_utc": current_mtime, "latest_mtime_utc": current_mtime})
        return snapshot
    if not path.is_dir():
        current_mtime = mtime_utc(path)
        snapshot.update({"kind": "other", "mtime_utc": current_mtime, "latest_mtime_utc": current_mtime})
        return snapshot

    latest = mtime_utc(path)
    scanned = 0
    truncated = False
    try:
        for child in path.rglob("*"):
            scanned += 1
            child_mtime = mtime_utc(child)
            if child_mtime and child_mtime > latest:
                latest = child_mtime
            if scanned >= max_entries:
                truncated = True
                break
    except OSError:
        truncated = True
    snapshot.update(
        {
            "kind": "directory",
            "mtime_utc": mtime_utc(path),
            "latest_mtime_utc": latest,
            "scanned_entries": scanned,
            "truncated": truncated,
        }
    )
    return snapshot


def unique_strings(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip().replace("\\", "/").strip("/")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def unique_project_refs(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = normalize_project_ref(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def checkpoint_actions(payload: dict[str, object]) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        action = row.get("safe_next_action", {})
        if not isinstance(action, dict) or not str(action.get("command", "")).strip():
            continue
        actions.append(
            {
                "mod": str(row.get("mod", "")),
                "task_id": str(row.get("task_id", "")),
                "command": str(action.get("command", "")),
                "type": str(action.get("type", "")),
                "risk": str(action.get("risk", "")),
                "can_run_parallel": bool(row.get("can_run_parallel", False)),
                "resource_locks": row.get("resource_locks", []) if isinstance(row.get("resource_locks", []), list) else [],
                "must_read_evidence": unique_project_refs(row.get("must_read_evidence", []))
                if isinstance(row.get("must_read_evidence", []), list)
                else [],
            }
        )
    return actions


def evidence_refs(payload: dict[str, object]) -> list[str]:
    values: list[object] = [*CHECKPOINT_REPORT_REFS]
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        evidence = row.get("must_read_evidence", [])
        if isinstance(evidence, list):
            values.extend(evidence)
    return unique_project_refs(values)


def build_resume_checkpoint(root: Path, payload: dict[str, object]) -> dict[str, object]:
    generated_at_utc = utc_now_text()
    checkpoint_source = {
        "generated_at_utc": generated_at_utc,
        "project_state": payload.get("project_state", ""),
        "readiness": payload.get("readiness_overall_status", ""),
        "source_reports": payload.get("source_reports", {}),
        "task_summary": payload.get("task_summary", {}),
    }
    checkpoint_id = hashlib.sha256(json.dumps(checkpoint_source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    blocking_mods = [row for row in payload.get("blocking_mods", []) if isinstance(row, dict)]
    last_stages = unique_strings([row.get("last_success_stage", "") for row in blocking_mods])
    refs = evidence_refs(payload)
    return {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "generated_at_utc": generated_at_utc,
        "purpose": "low-context resume index; authoritative state remains workflow_state.json and workflow_tasks.json",
        "project_state": payload.get("project_state", ""),
        "readiness_overall_status": payload.get("readiness_overall_status", ""),
        "last_successful_stage": last_stages[0] if last_stages else "",
        "last_successful_stages": last_stages,
        "next_read_set": CHECKPOINT_NEXT_READ_SET,
        "next_actions": checkpoint_actions(payload),
        "artifact_refs": [{"path": path, "snapshot": path_snapshot(root, path)} for path in refs],
        "stale_if_newer_than": {
            "checkpoint_generated_at_utc": generated_at_utc,
            "rule": "If any watched path has latest_mtime_utc later than checkpoint_generated_at_utc, refresh readiness/state/tasks/handoff before trusting this checkpoint.",
            "watch": [path_snapshot(root, path) for path in CHECKPOINT_STALE_WATCH_PATHS],
        },
    }


def write_agent_reports(root: Path, payload: dict[str, object], json_path: Path, report_path: Path) -> None:
    payload["handoff_kind"] = "agent"
    payload["read_order"] = [
        "qa/agent_handoff.json",
        "qa/codex_handoff.json",
        "qa/workflow_state.json",
        "qa/workflow_tasks.json",
        "qa/translation_readiness.json",
    ]
    payload["plugin_source_read_order"] = [
        "config/workflow_policy.json",
        "config/agent_capabilities.example.json",
    ]
    payload["resume_checkpoint"] = build_resume_checkpoint(root, payload)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    health = payload.get("workflow_health", {}) if isinstance(payload.get("workflow_health"), dict) else {}
    task_info = payload.get("task_summary", {}) if isinstance(payload.get("task_summary"), dict) else {}
    checkpoint = payload.get("resume_checkpoint", {}) if isinstance(payload.get("resume_checkpoint"), dict) else {}
    checkpoint_actions_count = len(checkpoint.get("next_actions", [])) if isinstance(checkpoint.get("next_actions", []), list) else 0
    lines = [
        "# Agent Handoff",
        "",
        f"- Generated at: {payload.get('generated_at', '')}",
        f"- Project state: {payload.get('project_state', '')}",
        f"- Readiness: {payload.get('readiness_overall_status', '')}",
        f"- Workflow health: {health.get('verdict', '')} / Blocking: {health.get('blocking_issues', 0)}",
        f"- Pending executable tasks: {task_info.get('pending_executable', 0)}",
        f"- Parallel-safe tasks: {task_info.get('parallel_safe', 0)}",
        f"- Resume checkpoint: {checkpoint.get('checkpoint_id', '')} / Next actions: {checkpoint_actions_count}",
        "- GUI boundary: GUI, Computer Use, pywinauto, and desktop automation are Codex-only.",
        "- Plugin source files: `config/workflow_policy.json`, `config/agent_capabilities.example.json`.",
        "- Resume checkpoint is an index only; refresh the state chain if watched artifacts changed after it was generated.",
        "",
        "## Blocking Mods",
        "",
        "| Mod | State | Primary blocker | Safe next action | Task |",
        "|---|---|---|---|---|",
    ]
    for row in payload.get("blocking_mods", []):
        if not isinstance(row, dict):
            continue
        action = row.get("safe_next_action", {}) if isinstance(row.get("safe_next_action"), dict) else {}
        lines.append(
            f"| {markdown_cell(row.get('mod', ''))} | {markdown_cell(row.get('state', ''))} | "
            f"{markdown_cell(row.get('primary_blocker', ''))} | {markdown_cell(action.get('command', ''))} | "
            f"{markdown_cell(row.get('task_id', ''))} |"
        )
    lines.extend(
        [
            "",
            "## Resume Checkpoint",
            "",
            f"- Checkpoint id: `{checkpoint.get('checkpoint_id', '')}`",
            f"- Generated UTC: `{checkpoint.get('generated_at_utc', '')}`",
            f"- Last successful stage: `{checkpoint.get('last_successful_stage', '')}`",
            f"- Next read set: {', '.join(f'`{item}`' for item in checkpoint.get('next_read_set', []) if isinstance(item, str))}",
            "- Stale rule: refresh readiness/state/tasks/handoff if any watched path is newer than the checkpoint.",
            "",
            "## Adapter Rules",
            "",
            "- Codex may handle GUI-only handoffs through controlled project rules.",
            "- opencode and Claude Code are full non-GUI adapters.",
            "- Non-GUI adapters must return `blocked` with `handoff_target=codex` for GUI-only tasks.",
            "- This handoff is a read view; it does not run commands or mark QA complete.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write qa/agent_handoff.json and qa/agent_handoff.md.")
    parser.add_argument("--workflow-state-path", default="qa/workflow_state.json")
    parser.add_argument("--readiness-json-path", default="qa/translation_readiness.json")
    parser.add_argument("--workflow-health-path", default="qa/workflow_health.json")
    parser.add_argument("--workflow-tasks-path", default="qa/workflow_tasks.json")
    parser.add_argument("--json-output-path", default="qa/agent_handoff.json")
    parser.add_argument("--report-output-path", default="qa/agent_handoff.md")
    args = parser.parse_args()

    root = project_root()
    state_path = resolve_project_path(root, args.workflow_state_path, must_exist=False)
    readiness_path = resolve_project_path(root, args.readiness_json_path, must_exist=False)
    health_path = resolve_project_path(root, args.workflow_health_path, must_exist=False)
    tasks_path = resolve_project_path(root, args.workflow_tasks_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    validate_agent_handoff_outputs(root, json_path, report_path)

    payload, issues = build_handoff(root, state_path, readiness_path, health_path, tasks_path)
    write_agent_reports(root, payload, json_path, report_path)
    blocking = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    print(f"Agent handoff JSON written to: {json_path}")
    print(f"Agent handoff report written to: {report_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
