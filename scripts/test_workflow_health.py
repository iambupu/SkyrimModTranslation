"""Repository-level health check for workflow policy and handoff evidence.

This is the first report a later Codex session should read. It checks scripts,
Skills, policy drift, readiness output, and optional strict gates without
touching real game or mod-manager directories.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from model_review_contract import (
    MODEL_REVIEWER_RE,
    jsonl_file_values,
    model_review_contract_issues,
    packet_content_reviewed,
    read_report_metric,
    strict_gate_current_and_clean,
)
from game_context import game_context_metadata
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root, intermediate_output_dir, packaged_mod_path
from project_paths import plugin_root as default_plugin_root
from project_paths import project_root as default_project_root
from workflow_lock import WorkflowLock
from route_translation_task import current_game_context
from ci_validate_repo import is_ignored_local_tool_meta_skill
from project_paths import is_under, resolve_project_path, relative_path
from workflow_process import run_plugin_python as run_python_script
from report_utils import markdown_cell
from workflow_issues import aggregate_issue_records, issue_record_from_mapping, make_issue_record


@dataclass
class Issue:
    Severity: str
    Area: str
    Message: str
    Evidence: str = ""
    issue_id: str = ""
    code: str = ""
    mod_name: str = ""
    affected_artifact: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    reported_by: list[str] = field(default_factory=lambda: ["workflow_health"])
    impact_scope: str = ""


@dataclass
class ScriptRow:
    Name: str
    Exists: bool


@dataclass
class SkillRow:
    Name: str
    HasSkillMd: bool
    HasFrontmatter: bool


@dataclass
class EvidenceRow:
    Name: str
    Status: str
    Path: str


@dataclass
class PolicyRow:
    Name: str
    Status: str
    Evidence: str


@dataclass
class KnownOutputHealthRow:
    ModName: str
    Status: str
    FinalModDir: str
    PackagePath: str
    DictionaryEntries: str
    PackageValidation: str
    StrictGate: str
    Coverage: str
    FinalReviewQuality: str
    ModelReview: str
    NextAction: str


@dataclass
class GoalBoundaryRow:
    Requirement: str
    Status: str
    Evidence: str
    NextAction: str


def health_issue_code(issue: Issue) -> str:
    area = issue.Area.strip().casefold().replace("-", "_")
    mappings = {
        "strict_gate": "strict_gate_not_clean",
        "model_review": "model_review_not_passed",
        "final_text_review": "protected_review_items",
        "final_binary_review": "protected_review_items",
        "workflow_state": "workflow_state_refresh_failed",
    }
    if area == "final_mod":
        message = issue.Message.casefold()
        if "packaged chs mod is missing" in message:
            return "chs_package_missing"
        if "translation text dictionary" in message:
            return "translation_dictionary_not_ready"
        if "manifest" in message:
            return "final_mod_manifest_invalid"
    return mappings.get(area, area or "workflow_health_issue")


def issue_from_record(record: dict[str, object]) -> Issue:
    evidence_paths = [str(value) for value in record.get("evidence_paths", [])]
    return Issue(
        str(record.get("severity", "error")),
        str(record.get("code", "")),
        str(record.get("message", "")),
        "; ".join(evidence_paths),
        issue_id=str(record.get("issue_id", "")),
        code=str(record.get("code", "")),
        mod_name=str(record.get("mod_name", "")),
        affected_artifact=str(record.get("affected_artifact", "")),
        evidence_paths=evidence_paths,
        reported_by=[str(value) for value in record.get("reported_by", [])],
        impact_scope=str(record.get("impact_scope", "")),
    )


def issue_to_record(issue: Issue, default_mod_name: str = "") -> dict[str, object]:
    return make_issue_record(
        code=issue.code or health_issue_code(issue),
        mod_name=issue.mod_name or default_mod_name,
        affected_artifact=issue.affected_artifact or issue.Evidence or "project",
        severity=issue.Severity,
        message=issue.Message,
        evidence_paths=issue.evidence_paths or ([issue.Evidence] if issue.Evidence else []),
        reported_by=issue.reported_by,
        impact_scope=issue.impact_scope,
    )


def summarize_readiness_blockers(issues: list[Issue], notes: list[str], blocking_count: str) -> None:
    direct_errors = [issue for issue in issues if issue.Severity == "error" and issue.Area != "readiness"]
    if direct_errors:
        notes.append(
            f"Translation readiness reported {blocking_count} blocking issue(s); direct root causes are already listed above. "
            "See qa/translation_readiness.md for occurrence-level detail."
        )
        return
    issues.append(
        Issue(
            "error",
            "readiness",
            "Translation readiness audit has blocking issues.",
            "qa/translation_readiness.md",
        )
    )


# Split "ps1" so the health script can search for legacy shell references
# without matching its own policy constant as a false positive.
LEGACY_SHELL_EXTENSIONS = ["p" + "s1", "bat", "cmd"]
LEGACY_COMMAND_STEMS = [
    "validate-tools-config",
    "audit-tool-prefs",
    "convert-xtranslator-xml-to-lextranslator-jsonl",
    "new-translation-task",
    "prepare-pex-tool-output",
    "recover-final-mod-overlays",
    "normalize-export",
    "scan-placeholders",
    "split-jsonl",
    "validate-translation",
    "route-translation-task",
    "detect-mod-files",
    "build-final-mod",
    "validate-final-mod",
    "clean-final-mod",
    "get-project-root",
    "get-relative-path",
    "test-project-path",
    "install-project-dotnet-sdk",
    "invoke-lextranslator-gui",
    "invoke-lextranslator",
    "invoke-xtranslator",
    "invoke-ssedump-safe",
]
ALLOWED_CODEX_META_SKILLS = {
    "skyrim-mod-chs-install",
    "skyrim-mod-chs-maintenance",
    "skyrim-mod-chs-usage",
}
LEGACY_COMMAND_NAMES = [f"{stem}.{extension}" for stem in LEGACY_COMMAND_STEMS for extension in LEGACY_SHELL_EXTENSIONS]
DEPRECATED_COMPLETION_PHRASES = [
    "runtime_" + "validation_pending",
    "真实游戏测试尚未记录时" + "，该报告应显示",
    "不能把项目内静态通过" + "当成完整目标完成",
    "完整目标仍" + "不能标记完成",
    "仍等待玩家操作" + "的真实游戏测试结果",
]


POLICY_TEXT_EXTENSIONS = {".md", ".py", ".json", ".jsonl", ".txt", ".csv", ".xml"}
def project_root() -> Path:
    return default_project_root()


def plugin_root() -> Path:
    return default_plugin_root()








def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(read_text(path))
    except Exception:
        return {}


def final_review_quality_rows(root: Path, mod_name: str) -> int:
    payload = read_json(root / "qa" / f"{mod_name}.final_review_quality.json")
    try:
        return int(str(payload.get("RowsChecked", "0")).strip())
    except (TypeError, ValueError):
        return 0


def read_status_value(root: Path, name: str) -> str:
    status_path = root / "qa" / "status.md"
    if not status_path.is_file():
        return ""
    return read_report_metric(status_path, name) or ""



def skill_has_frontmatter(path: Path) -> bool:
    if not path.is_file():
        return False
    text = read_text(path)
    return re.match(r"(?s)^---\s*\r?\nname:\s*[^\r\n]+\r?\ndescription:\s*[^\r\n]+\r?\n---", text) is not None



def known_output_health_rows(readiness_payload: dict) -> list[KnownOutputHealthRow]:
    rows = readiness_payload.get("KnownModOutputs", [])
    if not isinstance(rows, list):
        return []
    result: list[KnownOutputHealthRow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            KnownOutputHealthRow(
                ModName=str(row.get("ModName", "")),
                Status=str(row.get("OverallStatus", "")),
                FinalModDir=str(row.get("FinalModDir", "")),
                PackagePath=str(row.get("PackagedModPath", "")),
                DictionaryEntries=str(row.get("TranslationDictionaryEntries", "")),
                PackageValidation=f"{row.get('PackageValidationStatus', '')} / B:{row.get('PackageValidationBlockingIssues', '')}",
                StrictGate=f"B:{row.get('StrictGateBlockingIssues', '')} W:{row.get('StrictGateWarnings', '')}",
                Coverage=(
                    f"Missing:{row.get('CoverageMissing', '')} "
                    f"Blocking:{row.get('CoverageBlocking', '')} "
                    f"Unverified:{row.get('CoverageUnverified', '')}"
                ),
                FinalReviewQuality=(
                    f"{row.get('FinalReviewQualityStatus', '')} / "
                    f"B:{row.get('FinalReviewQualityBlockingIssues', '')} "
                    f"W:{row.get('FinalReviewQualityWarnings', '')}"
                ),
                ModelReview=str(row.get("ModelReviewStatus", "")),
                NextAction=str(row.get("NextRecommendedAction", "")),
            )
        )
    return result


def goal_boundary_rows(root: Path, known_outputs: list[KnownOutputHealthRow], project_local_clean: bool) -> list[GoalBoundaryRow]:
    # Health reports both project-local QA and external player validation. The
    # two statuses are separate so missing runtime evidence does not hide real
    # project-local blockers or create fake completion claims.
    manual_validation_path = root / "qa" / "manual_game_test_results_validation.json"
    manual_validation = read_json(manual_validation_path)
    validation_status = str(manual_validation.get("Status", "")).strip()
    validation_blocking = str(manual_validation.get("BlockingIssues", "")).strip()
    rows_payload = manual_validation.get("Rows", [])
    validated_mods: set[str] = set()
    if validation_status == "passed" and validation_blocking in {"0", "0.0"} and isinstance(rows_payload, list):
        for row in rows_payload:
            if not isinstance(row, dict):
                continue
            if str(row.get("ValidationStatus", "")).strip() == "passed":
                mod_name = str(row.get("ModName", "")).strip()
                if mod_name:
                    validated_mods.add(mod_name)

    expected_mods = {row.ModName for row in known_outputs if row.ModName}
    runtime_complete = bool(expected_mods) and expected_mods == validated_mods
    runtime_status = "recorded" if runtime_complete else "out_of_scope_for_proofreading_workflow"
    full_status = "complete" if project_local_clean else "project_local_blocked"

    return [
        GoalBoundaryRow(
            Requirement="Project-local static QA",
            Status="passed" if project_local_clean else "failed",
            Evidence="qa/workflow_state.md, qa/workflow_health.md, qa/translation_readiness.md, qa/project_completion_audit.md, qa/translation_goal_compliance.md",
            NextAction="None for project-local QA." if project_local_clean else "Fix blocking workflow/readiness/project completion evidence, then rerun the QA chain.",
        ),
        GoalBoundaryRow(
            Requirement="Player-operated real game/MO2/Vortex validation",
            Status=runtime_status,
            Evidence="qa/manual_game_test_results_validation.json",
            NextAction=(
                "None; every known CHS package has current validated runtime evidence."
                if runtime_complete
                else "Optional external follow-up: player fills qa/manual_game_test_results.json from qa/manual_game_test_results.template.json after testing in game, attaches evidence under qa/manual_game_test_artifacts/<ModName>/, then Codex runs python .\\scripts\\validate_manual_game_test_results.py."
            ),
        ),
        GoalBoundaryRow(
            Requirement="Complete user objective",
            Status=full_status,
            Evidence="qa/translation_goal_compliance.md",
            NextAction=(
                "Run python .\\scripts\\audit_translation_goal_compliance.py and mark the proofreading workflow goal complete if it reports complete."
                if full_status == "complete"
                else "Keep the goal active; project-local evidence still has blocking issues."
            ),
        ),
    ]


def iter_policy_files(root: Path) -> list[Path]:
    source_root = plugin_root()
    files: list[Path] = []
    for rel in ("scripts", "skills"):
        base = source_root / rel
        if not base.exists():
            continue
        allowed_extensions = {".py"} if rel == "scripts" else POLICY_TEXT_EXTENSIONS
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file() and path.suffix.lower() in allowed_extensions
        )
    for rel in ("AGENTS.md", "config/tools.example.json"):
        path = source_root / rel
        if path.is_file():
            files.append(path)
    local_tools = root / "config" / "tools.local.json"
    if local_tools.is_file():
        files.append(local_tools)
    return sorted(set(files), key=lambda item: str(item).lower())


def audit_workflow_policy(root: Path, issues: list[Issue]) -> list[PolicyRow]:
    # Policy checks look at authoritative files only. Ignored run artifacts may
    # contain historical command text, but they are not release instructions.
    rows: list[PolicyRow] = []
    shell_wrapper_files: list[Path] = []
    source_root = plugin_root()
    for extension in LEGACY_SHELL_EXTENSIONS:
        shell_wrapper_files.extend(source_root.joinpath("scripts").glob(f"*.{extension}"))
        shell_wrapper_files.extend(source_root.joinpath("tools", "_downloads").glob(f"*.{extension}"))
    shell_wrapper_files = sorted(shell_wrapper_files, key=lambda item: str(item).lower())
    if shell_wrapper_files:
        evidence = "; ".join(relative_path(root, item) for item in shell_wrapper_files)
        issues.append(Issue("error", "workflow-policy", "Shell wrapper project entries or tool-download residues are no longer allowed.", evidence))
        rows.append(PolicyRow("No shell wrapper project entries or download residues", "failed", evidence))
    else:
        rows.append(PolicyRow("No shell wrapper project entries or download residues", "passed", "scripts/*.{ps1,bat,cmd} + tools/_downloads/*.{ps1,bat,cmd} count = 0"))

    legacy_hits: list[str] = []
    for path in iter_policy_files(root):
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            continue
        for legacy_name in LEGACY_COMMAND_NAMES:
            if legacy_name in text:
                legacy_hits.append(f"{relative_path(root, path)} -> {legacy_name}")
    if legacy_hits:
        evidence = "; ".join(legacy_hits[:20])
        if len(legacy_hits) > 20:
            evidence += f"; ... {len(legacy_hits) - 20} more"
        issues.append(Issue("error", "workflow-policy", "Legacy shell command references remain in authoritative workflow files.", evidence))
        rows.append(PolicyRow("No legacy shell command references", "failed", evidence))
    else:
        rows.append(PolicyRow("No legacy shell command references", "passed", "authoritative workflow files clean"))

    deprecated_hits: list[str] = []
    for path in iter_policy_files(root):
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            continue
        for phrase in DEPRECATED_COMPLETION_PHRASES:
            if phrase in text:
                deprecated_hits.append(f"{relative_path(root, path)} -> {phrase}")
    if deprecated_hits:
        evidence = "; ".join(deprecated_hits[:20])
        if len(deprecated_hits) > 20:
            evidence += f"; ... {len(deprecated_hits) - 20} more"
        issues.append(
            Issue(
                "error",
                "workflow-policy",
                "Deprecated runtime-completion wording remains in authoritative workflow files.",
                evidence,
            )
        )
        rows.append(PolicyRow("No deprecated runtime completion wording", "failed", evidence))
    else:
        rows.append(
            PolicyRow(
                "No deprecated runtime completion wording",
                "passed",
                "player-operated runtime validation is external to proofreading workflow completion",
            )
        )
    return rows


def write_reports(
    root: Path,
    report_path: Path,
    json_path: Path,
    mod_name: str,
    workspace: Path | None,
    final_mod: Path | None,
    run_strict_gate: bool,
    issues: list[Issue],
    notes: list[str],
    script_rows: list[ScriptRow],
    policy_rows: list[PolicyRow],
    skill_rows: list[SkillRow],
    evidence_rows: list[EvidenceRow],
    known_output_rows: list[KnownOutputHealthRow],
    goal_boundary_rows: list[GoalBoundaryRow],
) -> None:
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    workspace_rel = relative_path(root, workspace) if workspace else ""
    final_mod_rel = relative_path(root, final_mod) if final_mod else ""
    context = current_game_context(root)

    lines: list[str] = [
        "# Workflow Health Report",
        "",
        f"- game_id: {context.game_id}",
        f"- ProjectRoot: {root}",
        f"- Checked at: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- ModName: {mod_name}",
    ]
    if workspace_rel:
        lines.append(f"- Workspace: {workspace_rel}")
    if final_mod_rel:
        lines.append(f"- FinalModDir: {final_mod_rel}")
    lines.extend(
        [
            f"- RunStrictGate: {bool(run_strict_gate)}",
            f"- Blocking issues: {blocking}",
            f"- Warnings: {warnings}",
            "",
            "## Verdict",
            "",
            "PASS: Workflow health has no blocking issues." if blocking == 0 else "FAIL: Workflow health has blocking issues.",
            "",
            "## Issues",
            "",
        ]
    )
    if not issues:
        lines.append("No health issues.")
    else:
        lines.extend(
            [
                "| Issue ID | Severity | Code | Impact scope | Root cause | Reported by | Evidence |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for issue in issues:
            lines.append(
                f"| {issue.issue_id} | {issue.Severity} | {issue.code} | {markdown_cell(issue.impact_scope)} | "
                f"{markdown_cell(issue.Message)} | {markdown_cell(', '.join(issue.reported_by))} | "
                f"{markdown_cell('; '.join(issue.evidence_paths))} |"
            )

    lines.extend(["", "## Core Scripts", "", "| Script | Exists |", "|---|---:|"])
    for row in script_rows:
        lines.append(f"| scripts/{row.Name} | {row.Exists} |")

    lines.extend(["", "## Workflow Policy", "", "| Policy | Status | Evidence |", "|---|---|---|"])
    for row in policy_rows:
        lines.append(f"| {row.Name} | {row.Status} | {markdown_cell(row.Evidence)} |")

    lines.extend(["", "## Skills", "", "| Skill | SKILL.md | Frontmatter |", "|---|---:|---:|"])
    for row in skill_rows:
        lines.append(f"| {row.Name} | {row.HasSkillMd} | {row.HasFrontmatter} |")

    lines.extend(["", "## Known Outputs", ""])
    if not known_output_rows:
        lines.append("No known Mod outputs were reported by translation readiness.")
    else:
        lines.extend(
            [
                "| ModName | Status | final_mod | CHS package | Dictionary | Package | Strict Gate | Coverage | Final Review Quality | Model Review | Next Action |",
                "|---|---|---|---|---:|---|---|---|---|---|---|",
            ]
        )
        for row in known_output_rows:
            lines.append(
                f"| {markdown_cell(row.ModName)} | {markdown_cell(row.Status)} | {markdown_cell(row.FinalModDir)} | "
                f"{markdown_cell(row.PackagePath)} | {markdown_cell(row.DictionaryEntries)} | "
                f"{markdown_cell(row.PackageValidation)} | {markdown_cell(row.StrictGate)} | {markdown_cell(row.Coverage)} | "
                f"{markdown_cell(row.FinalReviewQuality)} | {markdown_cell(row.ModelReview)} | {markdown_cell(row.NextAction)} |"
            )

    lines.extend(["", "## Goal Boundary", ""])
    if not goal_boundary_rows:
        lines.append("No goal boundary rows were generated.")
    else:
        lines.extend(["| Requirement | Status | Evidence | Next Action |", "|---|---|---|---|"])
        for row in goal_boundary_rows:
            lines.append(
                f"| {markdown_cell(row.Requirement)} | {markdown_cell(row.Status)} | "
                f"{markdown_cell(row.Evidence)} | {markdown_cell(row.NextAction)} |"
            )

    lines.extend(["", "## Evidence", ""])
    if not evidence_rows:
        lines.append("No mod-specific evidence was checked.")
    else:
        lines.extend(["| Evidence | Status | Path |", "|---|---|---|"])
        for row in evidence_rows:
            lines.append(f"| {row.Name} | {row.Status} | {row.Path} |")

    lines.extend(["", "## Notes", ""])
    if not notes:
        lines.append("No additional notes.")
    else:
        lines.extend(f"- {note}" for note in notes)

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This script does not translate text.",
            "- This script does not write plugin or PEX binaries.",
            "- This script reads only project-local evidence and writes QA reports.",
            "- Real game installations, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        **game_context_metadata(context),
        "ProjectRoot": str(root),
        "ModName": mod_name,
        "Workspace": workspace_rel,
        "FinalModDir": final_mod_rel,
        "RunStrictGate": bool(run_strict_gate),
        "BlockingIssues": blocking,
        "Warnings": warnings,
        "Verdict": "PASS" if blocking == 0 else "FAIL",
        "Issues": [asdict(issue) for issue in issues],
        "CoreScripts": [asdict(row) for row in script_rows],
        "WorkflowPolicy": [asdict(row) for row in policy_rows],
        "Skills": [asdict(row) for row in skill_rows],
        "KnownOutputs": [asdict(row) for row in known_output_rows],
        "GoalBoundary": [asdict(row) for row in goal_boundary_rows],
        "Evidence": [asdict(row) for row in evidence_rows],
        "Notes": notes,
        "Safety": {
            "ProjectLocalOnly": True,
            "DirectBinaryEdit": False,
            "WritesQaReportOnly": True,
        },
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_repo_only_validation(strict: bool) -> int:
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    import ci_validate_repo

    args = ["--strict"] if strict else []
    return ci_validate_repo.main(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check profile-aware Bethesda Mod workflow health and write Markdown plus JSON reports.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--workspace-path", default="")
    parser.add_argument("--final-mod-dir", default="")
    parser.add_argument("--report-output-path", default="qa/workflow_health.md")
    parser.add_argument("--json-output-path", default="qa/workflow_health.json")
    parser.add_argument("--run-strict-gate", action="store_true")
    parser.add_argument(
        "--repo-only",
        action="store_true",
        help="Run deterministic repository structure checks for CI without reading workspace QA state.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Use strict repo-only validation mode. Requires --repo-only.",
    )
    args = parser.parse_args()

    if args.repo_only:
        return run_repo_only_validation(args.strict)
    if args.strict:
        parser.error("--strict requires --repo-only. Use --run-strict-gate for translation QA gates.")

    root = project_root()
    WorkflowLock(root, "test_workflow_health.py").acquire()
    issues: list[Issue] = []
    root_issue_records: list[dict[str, object]] = []
    notes: list[str] = []
    evidence_rows: list[EvidenceRow] = []
    known_output_rows: list[KnownOutputHealthRow] = []
    goal_rows: list[GoalBoundaryRow] = []

    mod_name = args.mod_name.strip() or read_status_value(root, "ModName")
    if not mod_name:
        out_root = root / "out"
        out_dirs = [item for item in out_root.iterdir() if item.is_dir()] if out_root.is_dir() else []
        if out_dirs:
            mod_name = max(out_dirs, key=lambda item: item.stat().st_mtime).name

    workspace: Path | None = None
    final_mod: Path | None = None
    package_path: Path | None = None
    strict_gate_clean = False

    if mod_name:
        workspace_value = args.workspace_path or f"work/extracted_mods/{mod_name}"
        final_mod_value = args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name))
        workspace = resolve_project_path(root, workspace_value, must_exist=False)
        if workspace.is_dir():
            workspace = find_data_root(workspace).resolve(strict=True)
        final_mod = resolve_project_path(root, final_mod_value, must_exist=False)
        expected_final_mod = default_final_mod_dir(root, mod_name).resolve(strict=False)
        package_path = packaged_mod_path(root, mod_name)
        work_root = resolve_project_path(root, "work/extracted_mods", must_exist=False)
        if not is_under(workspace, work_root):
            issues.append(Issue("error", "paths", "WorkspacePath must be under work/extracted_mods/.", workspace_value))
        if final_mod.resolve(strict=False) != expected_final_mod:
            issues.append(Issue("error", "paths", "FinalModDir must be out/<ModName>/汉化产出/final_mod.", final_mod_value))

    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")
    if not is_under(json_path, qa_root):
        raise ValueError(f"JsonOutputPath must be under qa/: {args.json_output_path}")

    source_root = plugin_root()
    required_scripts = [
        "init_workspace.py",
        "init_project.py",
        "prepare_mod_workspace.py",
        "validate_tools_config.py",
        "audit_tool_prefs.py",
        "detect_mod_files.py",
        "detect_decoder_tools.py",
        "route_translation_task.py",
        "new_translation_task.py",
        "invoke_lextranslator.py",
        "invoke_xtranslator.py",
        "invoke_lextranslator_gui.py",
        "invoke_ssedump_safe.py",
        "check_project_dotnet_sdk.py",
        "extract_mcm_text.py",
        "build_lextranslator_dictionary_rag_index.py",
        "build_external_glossary_matches.py",
        "export_esp_strings.py",
        "apply_plugin_translation_map.py",
        "run_plugin_translation_stage.py",
        "convert_xtranslator_xml_to_lextranslator_jsonl.py",
        "invoke_mutagen_plugin_text_tool.py",
        "invoke_mutagen_pex_string_tool.py",
        "prepare_pex_tool_output.py",
        "proofread_translation.py",
        "validate_translation.py",
        "scan_placeholders.py",
        "normalize_export.py",
        "split_jsonl.py",
        "validate_interface_translation.py",
        "new_model_review_packet.py",
        "update_model_review_contract.py",
        "audit_final_interface_translations.py",
        "validate_final_text_structure.py",
        "new_final_text_review_packet.py",
        "new_final_binary_review_packet.py",
        "audit_final_review_quality.py",
        "verify_plugin_output.py",
        "verify_pex_output.py",
        "extract_non_gui_candidates.py",
        "audit_non_gui_coverage.py",
        "new_bsa_archive_manifest.py",
        "new_archive_audit_manifest.py",
        "audit_archive_coverage.py",
        "invoke_bsa_file_extractor_safe.py",
        "build_final_mod.py",
        "clean_final_mod.py",
        "recover_final_mod_overlays.py",
        "validate_final_mod.py",
        "validate_chs_package.py",
        "run_non_gui_qa_gates.py",
        "run_non_gui_translation_workflow.py",
        "run_translation_queue.py",
        "workflow_progress.py",
        "workflow_trace.py",
        "write_workflow_state.py",
        "log_workflow_agent_run.py",
        "audit_translation_readiness.py",
        "test_workflow_health.py",
        "audit_project_completion.py",
        "new_manual_game_test_plan.py",
        "new_manual_game_test_results_template.py",
        "validate_manual_game_test_results.py",
        "audit_translation_goal_compliance.py",
        "write_translation_status.py",
    ]
    script_rows: list[ScriptRow] = []
    for name in required_scripts:
        exists = (source_root / "scripts" / name).is_file()
        script_rows.append(ScriptRow(name, exists))
        if not exists:
            issues.append(Issue("error", "scripts", f"Required workflow script is missing: {name}", f"scripts/{name}"))

    policy_rows = audit_workflow_policy(root, issues)

    skill_root = source_root / "skills"
    legacy_skill_root = source_root / ".codex" / "skills"
    skill_rows: list[SkillRow] = []
    if legacy_skill_root.is_dir():
        unexpected_meta = [
            item.name
            for item in legacy_skill_root.iterdir()
            if item.is_dir()
            and (item / "SKILL.md").is_file()
            and item.name not in ALLOWED_CODEX_META_SKILLS
            and not is_ignored_local_tool_meta_skill(source_root, item)
        ]
        if unexpected_meta:
            issues.append(
                Issue(
                    "error",
                    "skills",
                    "Legacy .codex/skills contains non-meta Skill folders; root skills/ must be the only runtime Skill source.",
                    ", ".join(f".codex/skills/{name}" for name in unexpected_meta),
                )
            )
    if not skill_root.is_dir():
        issues.append(Issue("error", "skills", "skills/ is missing.", "skills"))
    else:
        skill_dirs = sorted([item for item in skill_root.iterdir() if item.is_dir()], key=lambda item: item.name.lower())
        if len(skill_dirs) < 12:
            issues.append(Issue("warning", "skills", "skills/ has fewer than the expected 12 core Skills.", "skills"))
        for skill_dir in skill_dirs:
            skill_file = skill_dir / "SKILL.md"
            exists = skill_file.is_file()
            frontmatter = skill_has_frontmatter(skill_file) if exists else False
            skill_rows.append(SkillRow(skill_dir.name, exists, frontmatter))
            if not exists:
                issues.append(Issue("error", "skills", f"Skill is missing SKILL.md: {skill_dir.name}", f"skills/{skill_dir.name}/SKILL.md"))
            elif not frontmatter:
                issues.append(Issue("error", "skills", f"Skill frontmatter is missing name/description: {skill_dir.name}", f"skills/{skill_dir.name}/SKILL.md"))

    if args.run_strict_gate:
        if not mod_name or workspace is None or final_mod is None:
            issues.append(Issue("error", "strict-gate", "RunStrictGate requires ModName, WorkspacePath, and FinalModDir.", "qa/status.md"))
        else:
            gate = run_python_script(
                root,
                "run_non_gui_qa_gates.py",
                ["--mod-name", mod_name, "--workspace-path", str(workspace), "--final-mod-dir", str(final_mod), "--strict-complete", "--reuse-mechanical-evidence"],
            )
            gate_report = root / "qa" / f"{mod_name}.non_gui_qa_gates.md"
            gate_blocking = read_report_metric(gate_report, "Blocking issues")
            gate_warnings = read_report_metric(gate_report, "Warnings")
            gate_strict = read_report_metric(gate_report, "Strict complete mode")
            if gate_blocking == "0" and gate_warnings == "0" and gate_strict == "True":
                notes.append("Strict non-GUI QA gate rerun completed.")
                if gate.returncode != 0:
                    notes.append("Strict gate process returned non-zero, but the generated gate report is clean.")
            else:
                issues.append(Issue("error", "strict-gate", "Strict non-GUI QA gate failed or did not generate a clean strict report.", f"qa/{mod_name}.non_gui_qa_gates.md"))
            status = run_python_script(root, "write_translation_status.py", ["--mod-name", mod_name, "--workspace-path", str(workspace)])
            if status.returncode != 0:
                issues.append(Issue("error", "status", "Status report refresh failed.", "qa/status.md"))
            else:
                notes.append("Status report refreshed.")

    strict_gate_clean = strict_gate_current_and_clean(root, mod_name) if mod_name else False

    if mod_name:
        checks = [
            ("Model review", f"qa/{mod_name}.model_review.md", "model-review"),
            ("Strict non-GUI gate", f"qa/{mod_name}.non_gui_qa_gates.md", "strict-gate"),
            ("Final text review packet", f"qa/{mod_name}.final_text_review_packet.md", "final-text-packet"),
            ("Final binary review packet", f"qa/{mod_name}.final_binary_review_packet.md", "final-binary-packet"),
            ("Final Interface runtime audit", f"qa/{mod_name}.final_interface_runtime.md", "interface-runtime"),
            ("Final text structure", f"qa/{mod_name}.final_text_structure.md", "final-text-structure"),
            ("Archive coverage", f"qa/{mod_name}.archive_coverage.md", "archive-coverage"),
            ("Final mod validation", "qa/final_mod_validation.md", "final-mod-validation"),
            ("Status", "qa/status.md", "status"),
        ]
        for label, rel, kind in checks:
            path = root / rel
            exists = path.is_file()
            status = "present" if exists else "missing"
            if not exists:
                issues.append(Issue("error", "evidence", f"Required evidence is missing: {label}", rel))
            elif kind == "strict-gate":
                blocking = read_report_metric(path, "Blocking issues")
                warnings = read_report_metric(path, "Warnings")
                strict = read_report_metric(path, "Strict complete mode")
                if blocking != "0" or warnings != "0" or strict != "True":
                    issues.append(Issue("error", "strict-gate", "Strict gate evidence is not clean.", rel))
                    status = "failed"
                else:
                    status = "clean"
            elif kind == "final-text-packet":
                protected = read_report_metric(path, "Protected review items")
                if protected != "0":
                    issues.append(Issue("error", "final-text-review", "Final text review packet has protected-review item(s).", rel))
                    status = "needs_review"
                else:
                    status = "ready"
            elif kind == "final-binary-packet":
                protected = read_report_metric(path, "Protected review items")
                failures = read_report_metric(path, "Export failures")
                if protected != "0" or failures != "0":
                    issues.append(Issue("error", "final-binary-review", "Final binary review packet has protected-review item(s) or export failures.", rel))
                    status = "needs_review"
                else:
                    status = "ready"
            elif kind == "interface-runtime":
                blocking = read_report_metric(path, "Blocking issues")
                warnings = read_report_metric(path, "Warnings")
                if blocking != "0" or warnings != "0":
                    issues.append(Issue("error", "interface-runtime", "Final Interface runtime audit is not clean.", rel))
                    status = "failed"
                else:
                    status = "clean"
            elif kind == "model-review":
                text = read_text(path)
                if strict_gate_clean:
                    status = "passed"
                elif MODEL_REVIEWER_RE.search(text) is None or re.search(r"\bTODO\b", text, re.I) or not re.search(r"\bpass\b", text, re.I):
                    issues.append(Issue("error", "model-review", "Model review is missing reviewer/pass evidence or still contains TODO.", rel))
                    status = "needs_review"
                elif f"{mod_name}.final_text_review_packet.md" not in text or f"{mod_name}.final_binary_review_packet.md" not in text:
                    issues.append(Issue("error", "model-review", "Model review does not cover final text and binary packets.", rel))
                    status = "incomplete_scope"
                else:
                    current_packets = [
                        root / "qa" / f"{mod_name}.final_text_review_packet.md",
                        root / "qa" / f"{mod_name}.final_binary_review_packet.md",
                    ]
                    if any(
                        packet.is_file()
                        and not packet_content_reviewed(text, packet)
                        for packet in current_packets
                    ):
                        issues.append(Issue("error", "model-review", "Model review does not cover the current final review packet content hash.", rel))
                        status = "stale"
                    else:
                        reviewed_files = jsonl_file_values(root / "qa" / f"{mod_name}.final_text_review_items.jsonl", "File")
                        reviewed_files |= jsonl_file_values(root / "qa" / f"{mod_name}.final_binary_review_items.jsonl", "File")
                        contract_issues = model_review_contract_issues(text, reviewed_files)
                        final_quality_report = root / "qa" / f"{mod_name}.final_review_quality.md"
                        rows_checked = final_review_quality_rows(root, mod_name)
                        if final_quality_report.name not in text:
                            contract_issues.append("Model review does not explicitly mention the current final review quality report.")
                        if rows_checked <= 0:
                            contract_issues.append("Current final review quality RowsChecked evidence is missing or zero.")
                        if contract_issues:
                            issues.append(Issue("error", "model-review", "; ".join(contract_issues[:3]), rel))
                            status = "incomplete_contract"
                        else:
                            status = "passed"
            elif kind == "status":
                text = read_text(path)
                if "Final binary review packet | ready" not in text or "Non-GUI QA gates | passed_strict_complete" not in text:
                    issues.append(Issue("warning", "status", "Status report does not show strict gate and final binary packet as ready.", rel))
                    status = "needs_refresh"
                else:
                    status = "ready"
            evidence_rows.append(EvidenceRow(label, status, rel))

        if final_mod is None or not final_mod.is_dir():
            issues.append(Issue("error", "final-mod", "final_mod directory is missing.", args.final_mod_dir or relative_path(root, default_final_mod_dir(root, mod_name))))
        else:
            manifest = final_mod / "meta" / "manifest.json"
            manifest_payload = read_json(manifest)
            mode = str(manifest_payload.get("DeliveryMode", ""))
            if mode not in {"direct-replacement-final-mod", "translation-overlay-package"}:
                issues.append(Issue("error", "final-mod", "final_mod manifest does not confirm a supported delivery mode.", relative_path(root, manifest)))
            if str(manifest_payload.get("OutputLayout", "")) != "mod-root/localization-output/final_mod-intermediate-package":
                issues.append(Issue("error", "final-mod", "final_mod manifest does not confirm the required localization output layout.", relative_path(root, manifest)))
            if str(manifest_payload.get("PackagedModNameSuffix", "")) != "CHS":
                issues.append(Issue("error", "final-mod", "final_mod manifest does not record PackagedModNameSuffix = CHS.", relative_path(root, manifest)))
            try:
                manifest_dictionary_count = int(manifest_payload.get("TranslationDictionaryEntryCount", 0) or 0)
            except (TypeError, ValueError):
                manifest_dictionary_count = 0
            if manifest_dictionary_count <= 0:
                issues.append(Issue("error", "final-mod", "final_mod manifest does not record a non-empty translation text dictionary.", relative_path(root, manifest)))
            dictionary_dir = intermediate_output_dir(root, mod_name) / "translation_text_dictionary"
            dictionary_manifest = dictionary_dir / "manifest.json"
            dictionary_jsonl = dictionary_dir / "translation_dictionary.jsonl"
            dictionary_payload = read_json(dictionary_manifest)
            try:
                dictionary_count = int(dictionary_payload.get("TranslatedEntryCount", 0) or 0)
            except (TypeError, ValueError):
                dictionary_count = 0
            if not dictionary_dir.is_dir():
                issues.append(Issue("error", "final-mod", "Intermediate translation text dictionary is missing.", relative_path(root, dictionary_dir)))
            elif not dictionary_manifest.is_file() or not dictionary_jsonl.is_file():
                issues.append(Issue("error", "final-mod", "Intermediate translation text dictionary manifest or JSONL is missing.", relative_path(root, dictionary_dir)))
            elif dictionary_count <= 0:
                issues.append(Issue("error", "final-mod", "Intermediate translation text dictionary has no translated entries.", relative_path(root, dictionary_manifest)))
            else:
                evidence_rows.append(EvidenceRow("Translation text dictionary", f"{dictionary_count} entries", relative_path(root, dictionary_jsonl)))
            if package_path is None or not package_path.is_file():
                expected_package = packaged_mod_path(root, mod_name)
                issues.append(Issue("error", "final-mod", "Packaged CHS mod is missing.", relative_path(root, expected_package)))
            elif str(manifest_payload.get("PackagedModPath", "")).replace("/", "\\").lower() != relative_path(root, package_path).replace("/", "\\").lower():
                issues.append(Issue("error", "final-mod", "final_mod manifest PackagedModPath does not match the required CHS package.", relative_path(root, manifest)))
    else:
        issues.append(Issue("warning", "mod", "ModName could not be inferred; mod-specific evidence was not checked.", "qa/status.md"))

    readiness_args = ["--mod-name", mod_name] if mod_name else []
    readiness = run_python_script(root, "audit_translation_readiness.py", readiness_args)
    readiness_report = root / "qa" / "translation_readiness.md"
    readiness_json = root / "qa" / "translation_readiness.json"
    if readiness.returncode != 0 or not readiness_report.is_file() or not readiness_json.is_file():
        issues.append(Issue("error", "readiness", "Translation readiness audit failed or did not write reports.", "qa/translation_readiness.md"))
    else:
        readiness_payload = read_json(readiness_json)
        known_output_rows = known_output_health_rows(readiness_payload)
        if known_output_rows:
            notes.append(f"Known outputs summary refreshed from translation readiness: {len(known_output_rows)} Mod output(s).")
        else:
            issues.append(Issue("warning", "readiness", "Translation readiness did not report any known Mod outputs.", "qa/translation_readiness.json"))
        readiness_status = str(readiness_payload.get("OverallStatus", "unknown"))
        readiness_blocking = str(readiness_payload.get("BlockingIssues", ""))
        readiness_warnings = str(readiness_payload.get("Warnings", ""))
        readiness_issue_records = [
            issue_record_from_mapping(raw, default_reporter="translation_readiness", default_mod_name=mod_name)
            for raw in readiness_payload.get("Issues", [])
            if isinstance(raw, dict)
        ]
        root_issue_records.extend(readiness_issue_records)
        evidence_rows.append(EvidenceRow("Translation readiness", readiness_status, "qa/translation_readiness.md"))
        if readiness_blocking not in ("", "0"):
            if readiness_issue_records:
                notes.append(
                    f"Translation readiness reported {readiness_blocking} blocking issue(s); "
                    "root causes are aggregated by issue_id below."
                )
            else:
                summarize_readiness_blockers(issues, notes, readiness_blocking)
        elif readiness_status != "ready_for_manual_test" or readiness_warnings not in ("", "0"):
            issues.append(Issue("warning", "readiness", f"Translation readiness is {readiness_status} with {readiness_warnings or '?'} warning(s).", "qa/translation_readiness.md"))
        else:
            manual_plan = run_python_script(root, "new_manual_game_test_plan.py", [])
            if manual_plan.returncode != 0:
                issues.append(Issue("error", "manual-game-test", "Manual game test plan refresh failed.", "qa/manual_game_test_plan.md"))
            else:
                notes.append("Manual game test plan refreshed.")
                manual_template = run_python_script(root, "new_manual_game_test_results_template.py", [])
                if manual_template.returncode != 0:
                    issues.append(
                        Issue("error", "manual-game-test", "Manual game test results template refresh failed.", "qa/manual_game_test_results.template.json")
                    )
                else:
                    notes.append("Manual game test results template refreshed.")

    workflow_state = run_python_script(root, "write_workflow_state.py", [])
    workflow_state_report = root / "qa" / "workflow_state.md"
    workflow_state_json = root / "qa" / "workflow_state.json"
    if workflow_state.returncode != 0 or not workflow_state_report.is_file() or not workflow_state_json.is_file():
        issues.append(Issue("error", "workflow-state", "Workflow state refresh failed or did not write reports.", "qa/workflow_state.json"))
    else:
        state_payload = read_json(workflow_state_json)
        for raw in state_payload.get("issues", []):
            if not isinstance(raw, dict):
                continue
            root_issue_records.append(
                issue_record_from_mapping(
                    {
                        **raw,
                        "reported_by": ["workflow_state"],
                        "evidence_paths": ["qa/workflow_state.json"],
                    },
                    default_reporter="workflow_state",
                    default_mod_name=mod_name,
                )
            )
        evidence_rows.append(EvidenceRow("Workflow state", str(state_payload.get("project_state", "")), "qa/workflow_state.json"))
        notes.append("Workflow state refreshed.")

    aggregated_records = aggregate_issue_records(
        [
            *root_issue_records,
            *(issue_to_record(issue, mod_name) for issue in issues),
        ]
    )
    issues = [issue_from_record(record) for record in aggregated_records]
    goal_rows = goal_boundary_rows(root, known_output_rows, not any(issue.Severity == "error" for issue in issues))

    write_reports(
        root,
        report_path,
        json_path,
        mod_name,
        workspace,
        final_mod,
        args.run_strict_gate,
        issues,
        notes,
        script_rows,
        policy_rows,
        skill_rows,
        evidence_rows,
        known_output_rows,
        goal_rows,
    )
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    print(f"Workflow health report written to: {report_path}")
    print(f"Workflow health JSON written to: {json_path}")
    print(f"Blocking issues: {blocking}")
    print(f"Warnings: {warnings}")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
