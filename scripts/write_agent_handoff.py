"""Write an agent-neutral handoff view.

This is intentionally separate from write_codex_handoff.py so the existing
Codex hot path keeps its current cost and output contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
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


def mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def path_snapshot(root: Path, rel_path: str, *, max_entries: int | None = None) -> dict[str, object]:
    normalized = normalize_project_ref(rel_path)
    if not normalized:
        return {
            "path": str(rel_path),
            "exists": False,
            "kind": "unsafe_path",
            "mtime_utc": "",
            "latest_mtime_utc": "",
            "mtime_ns": 0,
            "latest_mtime_ns": 0,
            "size": 0,
            "fingerprint": "",
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
        "mtime_ns": 0,
        "latest_mtime_ns": 0,
        "size": 0,
        "fingerprint": "",
        "scanned_entries": 0,
        "truncated": False,
    }
    if not exists:
        return snapshot
    if path.is_file():
        current_mtime = mtime_utc(path)
        current_mtime_ns = mtime_ns(path)
        try:
            current_size = path.stat().st_size
        except OSError:
            current_size = 0
        snapshot.update(
            {
                "kind": "file",
                "mtime_utc": current_mtime,
                "latest_mtime_utc": current_mtime,
                "mtime_ns": current_mtime_ns,
                "latest_mtime_ns": current_mtime_ns,
                "size": current_size,
                "fingerprint": hashlib.sha256(
                    f"file:{current_size}:{current_mtime_ns}".encode("utf-8")
                ).hexdigest(),
            }
        )
        return snapshot
    if not path.is_dir():
        current_mtime = mtime_utc(path)
        current_mtime_ns = mtime_ns(path)
        try:
            current_size = path.stat().st_size
        except OSError:
            current_size = 0
        snapshot.update(
            {
                "kind": "other",
                "mtime_utc": current_mtime,
                "latest_mtime_utc": current_mtime,
                "mtime_ns": current_mtime_ns,
                "latest_mtime_ns": current_mtime_ns,
                "size": current_size,
                "fingerprint": hashlib.sha256(
                    f"other:{current_size}:{current_mtime_ns}".encode("utf-8")
                ).hexdigest(),
            }
        )
        return snapshot

    try:
        root_stat = path.stat()
    except OSError:
        snapshot.update({"kind": "directory", "truncated": True})
        return snapshot
    root_mtime_utc = (
        datetime.fromtimestamp(root_stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    latest = root_mtime_utc
    latest_ns = root_stat.st_mtime_ns
    scanned = 0
    truncated = False
    fingerprint_value = 0
    try:
        for child in path.rglob("*"):
            scanned += 1
            try:
                child_stat = child.stat()
                child_mtime_ns = child_stat.st_mtime_ns
                child_size = child_stat.st_size
                child_kind = "directory" if child.is_dir() else "file" if child.is_file() else "other"
                child_relative = child.relative_to(path).as_posix()
            except OSError:
                truncated = True
                break
            child_mtime = (
                datetime.fromtimestamp(child_stat.st_mtime, timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            if child_mtime and child_mtime > latest:
                latest = child_mtime
            if child_mtime_ns > latest_ns:
                latest_ns = child_mtime_ns
            fingerprint_row = f"{child_relative}\0{child_kind}\0{child_size}\0{child_mtime_ns}"
            fingerprint_value ^= int.from_bytes(hashlib.sha256(fingerprint_row.encode("utf-8")).digest(), "big")
            if max_entries is not None and scanned >= max_entries:
                truncated = True
                break
    except OSError:
        truncated = True
    snapshot.update(
        {
            "kind": "directory",
            "mtime_utc": root_mtime_utc,
            "latest_mtime_utc": latest,
            "mtime_ns": root_stat.st_mtime_ns,
            "latest_mtime_ns": latest_ns,
            "size": root_stat.st_size,
            "fingerprint": f"{fingerprint_value:064x}",
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
    refs = evidence_refs(payload)
    artifact_snapshots = [{"path": path, "snapshot": path_snapshot(root, path)} for path in refs]
    watch_snapshots = [path_snapshot(root, path) for path in CHECKPOINT_STALE_WATCH_PATHS]
    generated_at_epoch_ns = time.time_ns()
    generated_at_utc = utc_now_text()
    checkpoint_source = {
        "generated_at_utc": generated_at_utc,
        "generated_at_epoch_ns": generated_at_epoch_ns,
        "project_state": payload.get("project_state", ""),
        "readiness": payload.get("readiness_overall_status", ""),
        "source_reports": payload.get("source_reports", {}),
        "task_summary": payload.get("task_summary", {}),
    }
    checkpoint_id = hashlib.sha256(json.dumps(checkpoint_source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    blocking_mods = [row for row in payload.get("blocking_mods", []) if isinstance(row, dict)]
    last_stages = unique_strings([row.get("last_success_stage", "") for row in blocking_mods])
    return {
        "schema_version": 2,
        "checkpoint_id": checkpoint_id,
        "generated_at_utc": generated_at_utc,
        "generated_at_epoch_ns": generated_at_epoch_ns,
        "purpose": "low-context resume index; authoritative state remains workflow_state.json and workflow_tasks.json",
        "project_state": payload.get("project_state", ""),
        "readiness_overall_status": payload.get("readiness_overall_status", ""),
        "last_successful_stage": last_stages[0] if last_stages else "",
        "last_successful_stages": last_stages,
        "next_read_set": CHECKPOINT_NEXT_READ_SET,
        "next_actions": checkpoint_actions(payload),
        "artifact_refs": artifact_snapshots,
        "stale_if_newer_than": {
            "checkpoint_generated_at_utc": generated_at_utc,
            "checkpoint_generated_at_epoch_ns": generated_at_epoch_ns,
            "rule": "Compare stored and current watched-path snapshots before trusting this checkpoint.",
            "watch": watch_snapshots,
        },
    }


def evaluate_resume_checkpoint(root: Path, checkpoint: dict[str, object]) -> dict[str, object]:
    reasons: list[dict[str, object]] = []
    if checkpoint.get("schema_version") != 2:
        reasons.append({"path": "", "reason": "unsupported_checkpoint_schema"})
    stale_rule = checkpoint.get("stale_if_newer_than", {})
    if not isinstance(stale_rule, dict):
        stale_rule = {}
    watch = stale_rule.get("watch", [])
    if not isinstance(watch, list) or not watch:
        reasons.append({"path": "", "reason": "missing_watch_snapshots"})
        watch = []

    actual_watch_paths = [
        normalize_project_ref(row.get("path", ""))
        for row in watch
        if isinstance(row, dict) and normalize_project_ref(row.get("path", ""))
    ]
    expected_watch_paths = set(CHECKPOINT_STALE_WATCH_PATHS)
    for path in sorted(expected_watch_paths - set(actual_watch_paths)):
        reasons.append({"path": path, "reason": "missing_required_watch_path"})
    for path in sorted(set(actual_watch_paths) - expected_watch_paths):
        reasons.append({"path": path, "reason": "unexpected_watch_path"})
    if len(actual_watch_paths) != len(set(actual_watch_paths)):
        reasons.append({"path": "", "reason": "duplicate_watch_path"})

    generated_ns = checkpoint.get("generated_at_epoch_ns", stale_rule.get("checkpoint_generated_at_epoch_ns", 0))
    try:
        generated_epoch_ns = int(generated_ns)
    except (TypeError, ValueError):
        generated_epoch_ns = 0
    if generated_epoch_ns <= 0:
        reasons.append({"path": "", "reason": "missing_generated_at_epoch_ns"})

    for stored in watch:
        if not isinstance(stored, dict):
            reasons.append({"path": "", "reason": "invalid_watch_snapshot"})
            continue
        path = normalize_project_ref(stored.get("path", ""))
        if not path:
            reasons.append({"path": str(stored.get("path", "")), "reason": "unsafe_watch_path"})
            continue
        if bool(stored.get("truncated", False)):
            reasons.append({"path": path, "reason": "stored_snapshot_truncated"})
            continue

        current = path_snapshot(root, path)
        if bool(current.get("truncated", False)):
            reasons.append({"path": path, "reason": "current_snapshot_truncated"})
            continue

        changed_fields: list[str] = []
        for field in ("exists", "kind", "latest_mtime_ns", "scanned_entries", "size", "fingerprint"):
            if stored.get(field) != current.get(field):
                changed_fields.append(field)
        if changed_fields:
            reasons.append(
                {
                    "path": path,
                    "reason": "snapshot_changed",
                    "changed_fields": changed_fields,
                    "stored": {field: stored.get(field) for field in changed_fields},
                    "current": {field: current.get(field) for field in changed_fields},
                }
            )

    return {
        "checkpoint_id": str(checkpoint.get("checkpoint_id", "")),
        "checked_at_utc": utc_now_text(),
        "fresh": not reasons,
        "status": "fresh" if not reasons else "stale",
        "generated_at_epoch_ns": generated_epoch_ns,
        "watch_count": len(watch),
        "reasons": reasons,
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
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="Check the existing resume checkpoint against current watched paths without rewriting reports.",
    )
    args = parser.parse_args()

    root = project_root()
    state_path = resolve_project_path(root, args.workflow_state_path, must_exist=False)
    readiness_path = resolve_project_path(root, args.readiness_json_path, must_exist=False)
    health_path = resolve_project_path(root, args.workflow_health_path, must_exist=False)
    tasks_path = resolve_project_path(root, args.workflow_tasks_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    validate_agent_handoff_outputs(root, json_path, report_path)

    if args.check_freshness:
        if not json_path.is_file():
            print(f"ERROR: agent handoff does not exist: {json_path}")
            return 1
        try:
            existing_payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read agent handoff: {exc}")
            return 1
        checkpoint = existing_payload.get("resume_checkpoint", {}) if isinstance(existing_payload, dict) else {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        result = evaluate_resume_checkpoint(root, checkpoint)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["fresh"] else 2

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
