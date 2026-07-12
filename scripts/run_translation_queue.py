"""Process the project translation queue one Mod at a time."""

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
from project_paths import safe_file_name
from game_context import GameContext
from route_translation_task import ba2_adapter_ready, current_game_context, is_under, project_root, resolve_project_path, route_for
from workflow_lock import WorkflowLock
from workflow_trace import start_trace_run, trace_span


@dataclass
class QueueItem:
    ModName: str
    SourcePath: str
    Mode: str
    Status: str
    Script: str
    Skill: str
    Evidence: str
    Output: list[str]


@dataclass
class QueueIssue:
    Severity: str
    ModName: str
    SourcePath: str
    Message: str
    Evidence: str = ""


SUPPORTED_PREPARE_EXTENSIONS = {".zip", ".7z"}
UNSUPPORTED_ARCHIVE_EXTENSIONS = {".rar", ".bsa"}


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def run_python_script(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess:
    source_root = default_plugin_root()
    script_path = plugin_script_path(script_name)
    if not script_path.is_file():
        raise FileNotFoundError(f"missing plugin script: scripts/{script_name}")
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=str(root),
        env={
            **os.environ,
            "SKYRIM_CHS_WORKSPACE_ROOT": str(root),
            "SKYRIM_CHS_PLUGIN_ROOT": str(source_root),
            "SKYRIM_CHS_TRACE_CHILD": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def output_lines(result: subprocess.CompletedProcess) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def safe_report_stem(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in value.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned or "UnnamedMod"


def refresh_readiness(root: Path, mod_name: str = "") -> dict:
    args: list[str] = []
    if mod_name:
        args.extend(["--mod-name", mod_name])
    result = run_python_script(root, "audit_translation_readiness.py", args)
    if result.returncode != 0:
        detail = "\n".join(output_lines(result))
        raise RuntimeError(f"readiness audit failed: {detail}")
    return read_json(root / "qa" / "translation_readiness.json")


def refresh_workflow_state(root: Path) -> tuple[bool, list[str]]:
    result = run_python_script(root, "write_workflow_state.py", [])
    lines = output_lines(result)
    return result.returncode == 0, lines[-40:]


def output_status_by_mod(readiness: dict) -> dict[str, str]:
    rows = readiness.get("KnownModOutputs", [])
    if not isinstance(rows, list):
        return {}
    result: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("ModName", "")).strip()
            if name:
                result[name] = str(row.get("OverallStatus", "")).strip()
    return result


def output_rows_by_mod(readiness: dict) -> dict[str, dict]:
    rows = readiness.get("KnownModOutputs", [])
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("ModName", "")).strip()
            if name:
                result[name] = row
    return result


def select_inputs(
    readiness: dict,
    *,
    mode: str,
    include_ready: bool,
    include_prepared: bool,
    mod_name_filter: str,
    source_filter: str,
    limit: int,
) -> list[dict]:
    rows = readiness.get("ModInputs", [])
    if not isinstance(rows, list):
        return []
    statuses = output_status_by_mod(readiness)
    outputs = output_rows_by_mod(readiness)
    selected: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mod_name = str(row.get("LikelyModName", "")).strip()
        source_path = str(row.get("Path", "")).strip()
        if mod_name_filter and mod_name != mod_name_filter:
            continue
        if source_filter and source_path != source_filter:
            continue
        if not include_ready and statuses.get(mod_name) == "ready_for_manual_test":
            continue
        if mode == "prepare" and not include_prepared and bool(outputs.get(mod_name, {}).get("WorkspaceExists", False)):
            continue
        selected.append(row)
        if limit > 0 and len(selected) >= limit:
            break
    return selected


def run_prepare(
    root: Path,
    row: dict,
    force: bool,
    context: GameContext | None = None,
) -> tuple[QueueItem, QueueIssue | None]:
    mod_name = str(row.get("LikelyModName", "")).strip()
    source_path = str(row.get("Path", "")).strip()
    suffix = Path(source_path).suffix.lower()
    if suffix == ".ba2":
        archive_path = resolve_project_path(root, source_path, must_exist=True)
        context = context or current_game_context(root)
        route = route_for(root, archive_path, context)
        script = "scripts/invoke_ba2_extractor_safe.py"
        safe_mod_name = safe_file_name(mod_name)
        archive_name = safe_file_name(archive_path.stem)
        evidence = f"out/{safe_mod_name}/archive_audits/{archive_name}/manifest.json"
        if not ba2_adapter_ready(root, context):
            message = (
                f".ba2 routes to {route.skill}; controlled BA2 adapter materialization is blocked "
                "until the current Game Profile has a ready Ba2ExtractorPath and matching protocol."
            )
            return (
                QueueItem(mod_name, source_path, "prepare", "blocked", script, route.skill, evidence, [message]),
                QueueIssue("warning", mod_name, source_path, message, evidence),
            )
        args = [
            "--mod-name",
            mod_name,
            "--archive-path",
            source_path,
            "--output-dir",
            f"work/archive_extracts/{safe_mod_name}/{archive_name}",
        ]
        result = run_python_script(root, "invoke_ba2_extractor_safe.py", args)
        status = "passed" if result.returncode == 0 else "failed"
        item = QueueItem(mod_name, source_path, "prepare", status, script, route.skill, evidence, output_lines(result))
        issue = None
        if result.returncode != 0:
            message = item.Output[-1] if item.Output else f"invoke_ba2_extractor_safe.py exited with code {result.returncode}"
            issue = QueueIssue("error", mod_name, source_path, message, evidence)
        return item, issue
    if suffix in UNSUPPORTED_ARCHIVE_EXTENSIONS:
        if suffix == ".bsa":
            message = ".bsa inputs route to bsa-archive-audit for bethesda-structs audit and optional safe BSAFileExtractor wrapper; queue prepare does not extract BSA directly."
        else:
            message = f"{suffix} requires an explicit project-local extraction adapter before queue processing."
        return (
            QueueItem(
                mod_name,
                source_path,
                "prepare",
                "blocked",
                "scripts/prepare_mod_workspace.py",
                "skills/bsa-archive-audit" if suffix == ".bsa" else "skills/mod-input-preparation",
                "qa/translation_queue.md",
                [message],
            ),
            QueueIssue("warning", mod_name, source_path, message),
        )
    if suffix not in SUPPORTED_PREPARE_EXTENSIONS and Path(source_path).suffix:
        message = f"Unsupported queue input extension for prepare mode: {suffix}"
        return (
            QueueItem(mod_name, source_path, "prepare", "blocked", "scripts/prepare_mod_workspace.py", "skills/mod-input-preparation", "qa/translation_queue.md", [message]),
            QueueIssue("warning", mod_name, source_path, message),
        )

    stem = safe_report_stem(mod_name)
    args = [
        "--mod-name",
        mod_name,
        "--source-path",
        source_path,
        "--report-output-path",
        f"qa/{stem}.workflow_report.md",
        "--inventory-report-path",
        f"qa/{stem}.mod_inventory.md",
        "--archive-report-path",
        f"qa/{stem}.archive_extraction_report.md",
    ]
    if force:
        args.append("--force")
    result = run_python_script(root, "prepare_mod_workspace.py", args)
    status = "passed" if result.returncode == 0 else "failed"
    evidence = f"qa/{stem}.workflow_report.md"
    item = QueueItem(mod_name, source_path, "prepare", status, "scripts/prepare_mod_workspace.py", "skills/mod-input-preparation", evidence, output_lines(result))
    issue = None
    if result.returncode != 0:
        message = item.Output[-1] if item.Output else f"prepare_mod_workspace.py exited with code {result.returncode}"
        issue = QueueIssue("error", mod_name, source_path, message, evidence)
    return item, issue


def run_workflow(root: Path, row: dict, force: bool) -> tuple[QueueItem, QueueIssue | None]:
    mod_name = str(row.get("LikelyModName", "")).strip()
    source_path = str(row.get("Path", "")).strip()
    args = ["--mod-name", mod_name, "--source-path", source_path]
    if force:
        args.append("--force-prepare")
    result = run_python_script(root, "run_non_gui_translation_workflow.py", args)
    status = "passed" if result.returncode == 0 else "failed"
    evidence = f"qa/{mod_name}.non_gui_workflow_run.md"
    item = QueueItem(mod_name, source_path, "workflow", status, "scripts/run_non_gui_translation_workflow.py", "skills/skyrim-mod-translation-orchestrator", evidence, output_lines(result))
    issue = None
    if result.returncode != 0:
        message = item.Output[-1] if item.Output else f"run_non_gui_translation_workflow.py exited with code {result.returncode}"
        issue = QueueIssue("error", mod_name, source_path, message, evidence)
    return item, issue


def write_reports(root: Path, report_path: Path, json_path: Path, mode: str, items: list[QueueItem], issues: list[QueueIssue], refreshed_readiness: dict) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    lines = [
        "# Translation Queue Report",
        "",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Mode: {mode}",
        f"- Items processed: {len(items)}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        f"- Readiness status after queue: {refreshed_readiness.get('OverallStatus', 'unknown')}",
        "",
        "## Queue Items",
        "",
    ]
    if not items:
        lines.append("No queue items were processed.")
    else:
        lines.extend(["| ModName | Source | Mode | Status | Script | Skill | Evidence |", "|---|---|---|---|---|---|---|"])
        for item in items:
            lines.append(f"| {markdown_cell(item.ModName)} | {markdown_cell(item.SourcePath)} | {item.Mode} | {item.Status} | {item.Script} | {item.Skill} | {item.Evidence} |")

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No queue issues.")
    else:
        lines.extend(["| Severity | ModName | Source | Message | Evidence |", "|---|---|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {markdown_cell(issue.ModName)} | {markdown_cell(issue.SourcePath)} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |")

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Queue inputs come from `qa/translation_readiness.json`, which only lists project-local `mod/` inputs.",
            "- Prepare mode only extracts project-local archives into `work/extracted_mods/<ModName>/` and writes QA reports.",
            "- Workflow mode delegates to the existing non-GUI workflow and does not bypass QA gates.",
            "- This queue script does not directly edit ESP/ESM/ESL/PEX/BSA/BA2 files.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "ProjectRoot": str(root),
        "Mode": mode,
        "ItemsProcessed": len(items),
        "BlockingIssues": blocking,
        "Warnings": warnings,
        "ReadinessStatusAfterQueue": refreshed_readiness.get("OverallStatus", "unknown"),
        "Items": [asdict(item) for item in items],
        "Issues": [asdict(issue) for issue in issues],
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run project-local Skyrim translation input queue from translation_readiness.json.")
    parser.add_argument("--mode", choices=["prepare", "workflow"], default="prepare")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--include-ready", action="store_true")
    parser.add_argument("--include-prepared", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--report-output-path", default="qa/translation_queue.md")
    parser.add_argument("--json-output-path", default="qa/translation_queue.json")
    args = parser.parse_args()

    root = project_root()
    WorkflowLock(root, "run_translation_queue.py").acquire()
    start_trace_run(root)
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")
    if not is_under(json_path, qa_root):
        raise ValueError(f"JsonOutputPath must be under qa/: {args.json_output_path}")

    with trace_span(
        "state.update.readiness",
        stage="state.update",
        attributes={"script": "scripts/audit_translation_readiness.py", "mod_name": args.mod_name.strip()},
        artifacts=["qa/translation_readiness.json"],
        root=root,
    ):
        readiness = refresh_readiness(root, args.mod_name.strip())
    selected = select_inputs(
        readiness,
        mode=args.mode,
        include_ready=args.include_ready,
        include_prepared=args.include_prepared,
        mod_name_filter=args.mod_name.strip(),
        source_filter=args.source_path.strip(),
        limit=args.limit,
    )

    items: list[QueueItem] = []
    issues: list[QueueIssue] = []
    context = current_game_context(root) if args.mode == "prepare" else None
    for row in selected:
        mod_name = str(row.get("LikelyModName", "")).strip()
        source_path = str(row.get("Path", "")).strip()
        trace_name = "workflow.dispatch"
        trace_stage = "extracted" if args.mode == "prepare" else "state.update"
        with trace_span(
            trace_name,
            stage=trace_stage,
            attributes={"mode": args.mode, "mod_name": mod_name, "source_path": source_path},
            artifacts=["qa/translation_queue.md"],
            root=root,
        ) as span:
            if args.mode == "prepare":
                item, issue = run_prepare(root, row, args.force, context)
            else:
                item, issue = run_workflow(root, row, args.force)
            span.set_attribute("status", item.Status)
            span.add_artifact(item.Evidence)
            if issue is not None and issue.Severity == "error":
                span.status_on_success = "error"
                span.error(issue.Message)
        items.append(item)
        if issue is not None:
            issues.append(issue)
            if issue.Severity == "error" and not args.continue_on_error:
                break

    with trace_span(
        "state.update.readiness",
        stage="state.update",
        attributes={"script": "scripts/audit_translation_readiness.py", "mod_name": args.mod_name.strip(), "phase": "after_queue"},
        artifacts=["qa/translation_readiness.json"],
        root=root,
    ):
        refreshed = refresh_readiness(root, args.mod_name.strip())
    with trace_span(
        "state.update.workflow_state",
        stage="state.update",
        attributes={"script": "scripts/write_workflow_state.py"},
        artifacts=["qa/workflow_state.json", ".workflow/progress_card.md"],
        root=root,
    ) as span:
        state_ok, state_output = refresh_workflow_state(root)
        span.set_attribute("ok", state_ok)
        if not state_ok:
            span.status_on_success = "error"
            span.error(state_output[-1] if state_output else "write_workflow_state.py failed after queue refresh.")
    if not state_ok:
        message = state_output[-1] if state_output else "write_workflow_state.py failed after queue refresh."
        issues.append(QueueIssue("error", args.mod_name.strip() or "project", "", message, "qa/workflow_state.json"))
    write_reports(root, report_path, json_path, args.mode, items, issues, refreshed)
    print(f"Translation queue report written to: {report_path}")
    print(f"Translation queue JSON written to: {json_path}")
    print(f"Items processed: {len(items)}")
    print(f"Blocking issues: {sum(1 for issue in issues if issue.Severity == 'error')}")
    print(f"Warnings: {sum(1 for issue in issues if issue.Severity == 'warning')}")
    return 1 if any(issue.Severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
