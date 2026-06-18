"""Orchestrate the repeatable non-GUI translation workflow for one Mod.

The workflow is intentionally stage-based: each subprocess writes evidence, and
the final report records enough output for a later agent to resume without
re-discovering the whole project.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import packaged_mod_path
from project_paths import find_data_root
from workflow_lock import WorkflowLock


@dataclass
class Step:
    Name: str
    Status: str
    Script: str
    Evidence: str
    Output: list[str]


@dataclass
class Issue:
    Severity: str
    Step: str
    Message: str
    Evidence: str = ""


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def safe_file_name(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return cleaned.strip()


def read_report_value(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def report_metric(path: Path, name: str) -> str:
    return read_report_value(path, name)


def run_python_script(root: Path, script_name: str, args: list[str]) -> subprocess.CompletedProcess:
    script = root / "scripts" / script_name
    if not script.is_file():
        raise FileNotFoundError(f"missing script: scripts/{script_name}")
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def output_lines(result: subprocess.CompletedProcess) -> list[str]:
    lines: list[str] = []
    if result.stdout:
        lines.extend(result.stdout.splitlines())
    if result.stderr:
        lines.extend(result.stderr.splitlines())
    return lines


def add_step(steps: list[Step], name: str, status: str, script: str, evidence: str, output: list[str] | None = None) -> None:
    steps.append(Step(name, status, script, evidence, output or []))


def run_stage(
    root: Path,
    steps: list[Step],
    issues: list[Issue],
    name: str,
    script_name: str,
    args: list[str],
    evidence: str,
    *,
    required: bool = False,
) -> bool:
    # required=False lets diagnostic/reporting stages fail without hiding the
    # earlier successful work. required=True is reserved for stages whose output
    # is needed to build or validate final_mod.
    script_label = f"scripts/{script_name}"
    try:
        result = run_python_script(root, script_name, args)
        lines = output_lines(result)
        status = "passed" if result.returncode == 0 else "failed"
        add_step(steps, name, status, script_label, evidence, lines)
        if result.returncode != 0:
            message = lines[-1] if lines else f"Script exited with code {result.returncode}."
            issues.append(Issue("error", name, message, evidence))
            return not required
        return True
    except Exception as exc:
        add_step(steps, name, "failed", script_label, evidence, [str(exc)])
        issues.append(Issue("error", name, str(exc), evidence))
        return not required


def health_failure_is_readiness_only(root: Path) -> bool:
    # During this workflow the health checker may read readiness before this
    # workflow report JSON has been rewritten. Treat a pure readiness self-cycle
    # as a non-blocking post-run refresh issue; all real health problems still
    # remain blocking.
    health_path = root / "qa" / "workflow_health.json"
    if not health_path.is_file():
        return False
    try:
        payload = json.loads(health_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return False
    issues = payload.get("Issues", [])
    if not isinstance(issues, list) or not issues:
        return False
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        if str(issue.get("Severity", "")).lower() != "error":
            return False
        if str(issue.get("Area", "")).lower() != "readiness":
            return False
    return True


def refresh_handoff_reports(root: Path, mod_name: str, workspace: Path, final_mod: Path, run_strict_gate: bool) -> list[str]:
    outputs: list[str] = []
    for script_name, args in (
        ("audit_translation_readiness.py", []),
        ("write_workflow_state.py", []),
        (
            "test_workflow_health.py",
            [
                "--mod-name",
                mod_name,
                "--workspace-path",
                relative_path(root, workspace),
                "--final-mod-dir",
                relative_path(root, final_mod),
                *(["--run-strict-gate"] if run_strict_gate else []),
            ],
        ),
    ):
        result = run_python_script(root, script_name, list(args))
        outputs.extend(output_lines(result))
        if result.returncode != 0:
            outputs.append(f"{script_name} exited with code {result.returncode}.")
            break
    return outputs


def read_jsonl_rows(path: Path) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append((stripped, parsed))
    return rows


def collect_pex_translation_inputs(root: Path, mod_name: str) -> list[Path]:
    # PEX inputs may be produced by LexTranslator-ready files or normalized
    # extraction outputs. Collect both so writeback can be attempted without
    # depending on one tool's directory layout.
    candidates: list[Path] = []
    lex_dir = root / "translated" / "lextranslator_ready" / mod_name
    if lex_dir.is_dir():
        candidates.extend(sorted(lex_dir.glob("*.jsonl"), key=lambda item: item.name.lower()))

    normalized_dir = root / "work" / "normalized" / mod_name
    normalized_candidates = [
        normalized_dir / "pex_visible_strings.jsonl",
        *sorted(normalized_dir.glob("*pex*.jsonl"), key=lambda item: item.name.lower()),
    ]
    for candidate in normalized_candidates:
        if candidate.is_file() and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def pex_row_matches(row: dict, pex: Path) -> bool:
    pex_name = pex.name.lower()
    pex_stem = pex.stem.lower()
    direct_fields = ("ModName", "mod_name", "PexName", "pex_name", "OutputPex", "output_pex")
    path_fields = ("source_file", "SourceFile", "source_path", "SourcePath", "File", "file", "Path", "path")

    for field in direct_fields:
        value = str(row.get(field, "") or "").strip()
        if not value:
            continue
        value_name = Path(value.replace("\\", "/")).name.lower()
        if value_name in {pex_name, pex_stem}:
            return True

    for field in path_fields:
        value = str(row.get(field, "") or "").strip()
        if not value:
            continue
        value_path = Path(value.replace("\\", "/"))
        value_name = value_path.name.lower()
        value_stem = value_path.stem.lower()
        if value_name == pex_name or value_stem == pex_stem:
            return True

    return False


def translation_row_key(row: dict, fallback_line: str) -> str:
    source = str(row.get("Source", row.get("source", "")) or "")
    target = str(row.get("Result", row.get("target", "")) or "")
    if source or target:
        return json.dumps({"Source": source, "Target": target}, ensure_ascii=False, sort_keys=True)
    return fallback_line


def run_pex_translation_stage(root: Path, steps: list[Step], issues: list[Issue], mod_name: str, workspace: Path) -> bool:
    pex_files = sorted((item for item in workspace.rglob("*.pex") if item.is_file()), key=lambda item: str(item).lower())
    if not pex_files:
        add_step(steps, "pex-translation-stage", "skipped", "scripts/invoke_mutagen_pex_string_tool.py", "No PEX files found.")
        return True

    translation_inputs = collect_pex_translation_inputs(root, mod_name)
    if not translation_inputs:
        add_step(
            steps,
            "pex-translation-stage",
            "skipped",
            "scripts/invoke_mutagen_pex_string_tool.py",
            f"No PEX translation JSONL found for {mod_name}.",
        )
        return True

    loaded_rows: list[tuple[Path, str, dict]] = []
    for candidate in translation_inputs:
        for line, row in read_jsonl_rows(candidate):
            loaded_rows.append((candidate, line, row))

    stage_output: list[str] = []
    matched_outputs = 0
    stage_issue_count = len(issues)
    for pex in pex_files:
        matched_lines: list[str] = []
        seen_lines: set[str] = set()
        for _candidate, line, row in loaded_rows:
            key = translation_row_key(row, line)
            if pex_row_matches(row, pex) and key not in seen_lines:
                matched_lines.append(line)
                seen_lines.add(key)
        if not matched_lines:
            continue

        rel_pex = pex.resolve(strict=False).relative_to(workspace.resolve(strict=True))
        filtered = root / "work" / "normalized" / mod_name / "pex_apply" / f"{pex.stem}.translation.jsonl"
        filtered.parent.mkdir(parents=True, exist_ok=True)
        filtered.write_text("\n".join(matched_lines) + "\n", encoding="utf-8")

        output_pex = root / "out" / mod_name / "tool_outputs" / rel_pex
        write_report = root / "qa" / f"{pex.stem}.mutagen_pex_write.md"
        apply_result = run_python_script(
            root,
            "invoke_mutagen_pex_string_tool.py",
            [
                "--mode",
                "Apply",
                "--input-pex-path",
                relative_path(root, pex),
                "--translation-jsonl-path",
                relative_path(root, filtered),
                "--output-pex-path",
                relative_path(root, output_pex),
                "--report-path",
                relative_path(root, write_report),
            ],
        )
        stage_output.extend(output_lines(apply_result))
        if apply_result.returncode != 0:
            issues.append(Issue("error", "pex-translation-stage", f"PEX apply failed for {relative_path(root, pex)}.", relative_path(root, write_report)))
            continue

        verify_report = root / "qa" / f"{pex.stem}.pex_output_verification.md"
        verify_result = run_python_script(
            root,
            "verify_pex_output.py",
            [
                "--original-pex-path",
                relative_path(root, pex),
                "--output-pex-path",
                relative_path(root, output_pex),
                "--translation-jsonl-path",
                relative_path(root, filtered),
                "--report-output-path",
                relative_path(root, verify_report),
            ],
        )
        stage_output.extend(output_lines(verify_result))
        if verify_result.returncode != 0:
            issues.append(Issue("error", "pex-translation-stage", f"PEX output verification failed for {relative_path(root, output_pex)}.", relative_path(root, verify_report)))
            continue
        matched_outputs += 1

    if matched_outputs == 0 and len(issues) == stage_issue_count:
        add_step(
            steps,
            "pex-translation-stage",
            "skipped",
            "scripts/invoke_mutagen_pex_string_tool.py",
            "PEX files found, but no translation rows matched a PEX file.",
        )
        return True

    status = "passed" if len(issues) == stage_issue_count else "failed"
    add_step(
        steps,
        "pex-translation-stage",
        status,
        "scripts/invoke_mutagen_pex_string_tool.py; scripts/verify_pex_output.py",
        f"out/{mod_name}/tool_outputs",
        stage_output,
    )
    return len(issues) == stage_issue_count


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def write_reports(
    root: Path,
    report_path: Path,
    json_path: Path,
    mod_name: str,
    started_at: str,
    workspace: Path,
    final_mod: Path,
    steps: list[Step],
    issues: list[Issue],
) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    verdict = "PASS" if blocking == 0 else "BLOCKED"
    workspace_rel = relative_path(root, workspace)
    final_mod_rel = relative_path(root, final_mod)
    finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Non-GUI Translation Workflow Run",
        "",
        f"- ProjectRoot: {root}",
        f"- ModName: {mod_name}",
        f"- StartedAt: {started_at}",
        f"- FinishedAt: {finished_at}",
        f"- Workspace: {workspace_rel}",
        f"- FinalModDir: {final_mod_rel}",
        f"- Verdict: {verdict}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        "",
        "## Steps",
        "",
        "| Step | Status | Script | Evidence |",
        "|---|---|---|---|",
    ]
    for step in steps:
        lines.append(f"| {step.Name} | {step.Status} | {step.Script} | {step.Evidence} |")

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No workflow issues.")
    else:
        lines.extend(["| Severity | Step | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {issue.Step} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |")

    lines.extend(
        [
            "",
            "## Next Checkpoints",
            "",
            "- Human-readable handoff: `qa/workflow_health.md`",
            "- Machine-readable handoff: `qa/workflow_health.json`",
            f"- Strict gate evidence: `qa/{mod_name}.non_gui_qa_gates.md`",
            f"- Final output: `{final_mod_rel}`",
            f"- Packaged CHS output: `{relative_path(root, packaged_mod_path(root, mod_name))}`",
            "",
            "## Safety",
            "",
            "- This script only orchestrates project-local scripts.",
            "- This script does not translate text by dictionary replacement.",
            "- This script does not directly modify ESP/ESM/ESL/PEX/BSA/BA2 binaries.",
            "- Real Skyrim, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "ProjectRoot": str(root),
        "ModName": mod_name,
        "StartedAt": started_at,
        "FinishedAt": finished_at,
        "Workspace": workspace_rel,
        "FinalModDir": final_mod_rel,
        "Verdict": verdict,
        "BlockingIssues": blocking,
        "Warnings": warnings,
        "Steps": [asdict(step) for step in steps],
        "Issues": [asdict(issue) for issue in issues],
        "Safety": {
            "ProjectLocalOnly": True,
            "DirectBinaryEdit": False,
            "GuiAutomation": False,
        },
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the decoder-first non-GUI Skyrim translation workflow.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--source-path", default="")
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--force-prepare", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-build-final-mod", action="store_true")
    parser.add_argument("--preserve-existing-final-mod", action="store_true")
    parser.add_argument("--skip-strict-gate", action="store_true")
    parser.add_argument("--allow-missing-model-review", action="store_true")
    parser.add_argument("--report-output-path", default="")
    parser.add_argument("--json-output-path", default="")
    args = parser.parse_args()

    root = project_root()
    WorkflowLock(root, "run_non_gui_translation_workflow.py").acquire()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    steps: list[Step] = []
    issues: list[Issue] = []

    if args.skip_prepare and not args.workspace_path:
        raise ValueError("--skip-prepare requires --workspace-path.")

    mod_name = safe_file_name(args.mod_name.strip())
    workspace_value = args.workspace_path

    if not args.skip_prepare:
        prepare_args: list[str] = []
        if mod_name:
            prepare_args.extend(["--mod-name", mod_name])
        if args.source_path:
            prepare_args.extend(["--source-path", args.source_path])
        if args.force_prepare:
            prepare_args.append("--force")
        ok = run_stage(
            root,
            steps,
            issues,
            "prepare-workspace",
            "prepare_mod_workspace.py",
            prepare_args,
            "qa/workflow_report.md",
            required=True,
        )
        if not ok:
            mod_name = mod_name or "unknown"
            workspace = resolve_project_path(root, workspace_value or f"work/extracted_mods/{mod_name}", must_exist=False)
            default_final_mod = relative_path(root, default_final_mod_dir(root, mod_name))
            final_mod = resolve_project_path(root, args.final_mod_dir or default_final_mod, must_exist=False)
            report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.non_gui_workflow_run.md", must_exist=False)
            json_path = resolve_project_path(root, args.json_output_path or f"qa/{mod_name}.non_gui_workflow_run.json", must_exist=False)
            write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
            return 1
        workflow_report = root / "qa" / "workflow_report.md"
        if not mod_name:
            mod_name = safe_file_name(read_report_value(workflow_report, "ModName"))
        if not workspace_value:
            workspace_value = read_report_value(workflow_report, "Workspace")

    if not mod_name:
        raise ValueError("ModName could not be inferred.")
    if not workspace_value:
        workspace_value = f"work/extracted_mods/{mod_name}"
    final_mod_value = args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name))

    workspace = resolve_project_path(root, workspace_value, must_exist=False)
    final_mod = resolve_project_path(root, final_mod_value, must_exist=False)
    expected_final_mod = default_final_mod_dir(root, mod_name).resolve(strict=False)
    work_root = resolve_project_path(root, "work/extracted_mods", must_exist=False)
    mod_root = resolve_project_path(root, "mod", must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)

    if not is_under(workspace, work_root) and not is_under(workspace, mod_root):
        raise ValueError(f"WorkspacePath must be under work/extracted_mods/ or mod/: {workspace_value}")
    if final_mod.resolve(strict=False) != expected_final_mod:
        raise ValueError(f"FinalModDir must be out/{mod_name}/汉化产出/final_mod: {final_mod_value}")
    if not workspace.is_dir():
        raise FileNotFoundError(f"WorkspacePath does not exist: {workspace_value}")
    detected_workspace = find_data_root(workspace).resolve(strict=True)
    if detected_workspace != workspace:
        workspace = detected_workspace

    report_path = resolve_project_path(root, args.report_output_path or f"qa/{mod_name}.non_gui_workflow_run.md", must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path or f"qa/{mod_name}.non_gui_workflow_run.json", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {report_path}")
    if not is_under(json_path, qa_root):
        raise ValueError(f"JsonOutputPath must be under qa/: {json_path}")

    if not run_stage(
        root,
        steps,
        issues,
        "detect-decoder-tools",
        "detect_decoder_tools.py",
        [],
        "qa/decoder_tools_report.md",
        required=True,
    ):
        write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
        return 1

    if not run_stage(
        root,
        steps,
        issues,
        "refresh-lextranslator-dictionary-rag-index",
        "build_lextranslator_dictionary_rag_index.py",
        [],
        "qa/lextranslator_dictionary_rag_index.md",
        required=True,
    ):
        write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
        return 1

    if args.skip_prepare:
        run_stage(
            root,
            steps,
            issues,
            "inventory-workspace",
            "detect_mod_files.py",
            ["--scan-path", relative_path(root, workspace), "--report-path", "qa/mod_inventory.md"],
            "qa/mod_inventory.md",
        )

    mcm_dir = workspace / "MCM"
    if mcm_dir.is_dir():
        run_stage(
            root,
            steps,
            issues,
            "extract-mcm-visible-text",
            "extract_mcm_text.py",
            ["--input-path", relative_path(root, mcm_dir), "--mod-name", mod_name],
            f"work/normalized/{mod_name}/mcm_text_candidates.jsonl",
        )
    else:
        add_step(steps, "extract-mcm-visible-text", "skipped", "scripts/extract_mcm_text.py", "No MCM directory found.")

    if not run_stage(
        root,
        steps,
        issues,
        "plugin-translation-stage",
        "run_plugin_translation_stage.py",
        ["--mod-name", mod_name, "--workspace-path", relative_path(root, workspace)],
        f"qa/{mod_name}.plugin_translation_stage.md",
        required=True,
    ):
        write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
        return 1

    if not run_pex_translation_stage(root, steps, issues, mod_name, workspace):
        write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
        return 1

    if not args.skip_build_final_mod:
        build_args = [
            "--mod-name",
            mod_name,
            "--source-mod-dir",
            relative_path(root, workspace),
            "--output-dir",
            relative_path(root, final_mod),
        ]
        if not args.preserve_existing_final_mod:
            build_args.append("--force")
        if not run_stage(
            root,
            steps,
            issues,
            "build-final-mod",
            "build_final_mod.py",
            build_args,
            f"{relative_path(root, final_mod)}/meta/manifest.json",
            required=True,
        ):
            write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
            return 1
    else:
        add_step(steps, "build-final-mod", "skipped", "scripts/build_final_mod.py", "SkipBuildFinalMod was set.")

    if not run_stage(
        root,
        steps,
        issues,
        "validate-chs-package",
        "validate_chs_package.py",
        ["--mod-name", mod_name, "--final-mod-dir", relative_path(root, final_mod)],
        f"qa/{mod_name}.chs_package_validation.md",
        required=True,
    ):
        write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
        return 1

    run_stage(
        root,
        steps,
        issues,
        "validate-final-mod",
        "validate_final_mod.py",
        ["--final-mod-dir", relative_path(root, final_mod)],
        "qa/final_mod_validation.md",
    )

    if not args.skip_strict_gate:
        py_gate_args = [
            "--mod-name",
            mod_name,
            "--workspace-path",
            relative_path(root, workspace),
            "--final-mod-dir",
            relative_path(root, final_mod),
            "--strict-complete",
        ]
        if args.allow_missing_model_review:
            py_gate_args.append("--allow-missing-model-review")
        gate_result = run_python_script(root, "run_non_gui_qa_gates.py", py_gate_args)
        gate_output = output_lines(gate_result)
        gate_report = root / "qa" / f"{mod_name}.non_gui_qa_gates.md"
        gate_clean = (
            report_metric(gate_report, "Blocking issues") == "0"
            and report_metric(gate_report, "Warnings") == "0"
            and report_metric(gate_report, "Strict complete mode") == "True"
        )
        if gate_clean:
            add_step(steps, "strict-non-gui-qa-gates", "passed", "scripts/run_non_gui_qa_gates.py", f"qa/{mod_name}.non_gui_qa_gates.md", gate_output)
        else:
            add_step(steps, "strict-non-gui-qa-gates", "failed", "scripts/run_non_gui_qa_gates.py", f"qa/{mod_name}.non_gui_qa_gates.md", gate_output)
            message = gate_output[-1] if gate_output else "Strict gate did not generate a clean report."
            issues.append(Issue("error", "strict-non-gui-qa-gates", message, f"qa/{mod_name}.non_gui_qa_gates.md"))
    else:
        add_step(steps, "strict-non-gui-qa-gates", "skipped", "scripts/run_non_gui_qa_gates.py", "SkipStrictGate was set.")

    run_stage(
        root,
        steps,
        issues,
        "refresh-status",
        "write_translation_status.py",
        ["--mod-name", mod_name, "--workspace-path", relative_path(root, workspace)],
        "qa/status.md",
    )

    health_args = ["--mod-name", mod_name, "--workspace-path", relative_path(root, workspace), "--final-mod-dir", relative_path(root, final_mod)]
    if not args.skip_strict_gate:
        health_args.append("--run-strict-gate")
    health_result = run_python_script(root, "test_workflow_health.py", health_args)
    health_output = output_lines(health_result)
    health_evidence = "qa/workflow_state.md; qa/workflow_state.json; qa/workflow_health.md; qa/workflow_health.json"
    health_readiness_only = False
    if health_result.returncode == 0:
        add_step(steps, "workflow-health", "passed", "scripts/test_workflow_health.py", health_evidence, health_output)
    elif health_failure_is_readiness_only(root):
        health_readiness_only = True
        add_step(
            steps,
            "workflow-health",
            "passed",
            "scripts/test_workflow_health.py",
            health_evidence,
            health_output + ["Readiness-only self-reference ignored; rerun readiness after this workflow report is written."],
        )
    else:
        add_step(steps, "workflow-health", "failed", "scripts/test_workflow_health.py", health_evidence, health_output)
        message = health_output[-1] if health_output else f"Script exited with code {health_result.returncode}."
        issues.append(Issue("error", "workflow-health", message, health_evidence))

    write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
    if health_readiness_only and not any(issue.Severity == "error" for issue in issues):
        refresh_output = refresh_handoff_reports(root, mod_name, workspace, final_mod, not args.skip_strict_gate)
        if refresh_output:
            steps.append(
                Step(
                    "refresh-handoff-reports",
                    "passed" if not any("exited with code" in line for line in refresh_output) else "failed",
                    "scripts/audit_translation_readiness.py; scripts/write_workflow_state.py; scripts/test_workflow_health.py",
                    "qa/translation_readiness.md; qa/workflow_state.md; qa/workflow_health.md",
                    refresh_output,
                )
            )
            if any("exited with code" in line for line in refresh_output):
                issues.append(Issue("error", "refresh-handoff-reports", refresh_output[-1], "qa/workflow_health.md"))
            write_reports(root, report_path, json_path, mod_name, started_at, workspace, final_mod, steps, issues)
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    print(f"Non-GUI workflow report written to: {report_path}")
    print(f"Non-GUI workflow JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
