"""Summarize current project readiness without rerunning the full workflow.

The report is the handoff entry point for later Codex sessions: it classifies
mod/ inputs, known out/<ModName>/ outputs, QA freshness, and the next action.
"""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import packaged_mod_path
from route_translation_task import is_under, project_root, relative_path, resolve_project_path, route_for
from validate_chs_package import sha256_file, validate as validate_chs_package_contents


@dataclass
class ModInputRow:
    Path: str
    Kind: str
    LikelyModName: str
    RouteSkill: str
    PrimaryTool: str
    Risk: str
    RecommendedCommand: str


@dataclass
class OutputRow:
    ModName: str
    Workspace: str
    WorkspaceExists: bool
    FinalModDir: str
    FinalModExists: bool
    ProvenancePath: str
    ProvenanceStatus: str
    PackagedModPath: str
    PackagedModExists: bool
    TranslationDictionaryPath: str
    TranslationDictionaryStatus: str
    TranslationDictionaryEntries: str
    PackageValidationReport: str
    PackageValidationStatus: str
    PackageValidationBlockingIssues: str
    PackageValidationSha256: str
    DeliveryMode: str
    WorkflowVerdict: str
    WorkflowBlockingIssues: str
    WorkflowWarnings: str
    StrictGateBlockingIssues: str
    StrictGateWarnings: str
    CoverageMissing: str
    CoverageUnverified: str
    FinalTextReviewItems: str
    FinalTextProtectedItems: str
    FinalBinaryReviewItems: str
    FinalBinaryProtectedItems: str
    FinalBinaryExportFailures: str
    FinalReviewQualityStatus: str
    FinalReviewQualityBlockingIssues: str
    FinalReviewQualityWarnings: str
    ModelReviewStatus: str
    OverallStatus: str
    NextRecommendedAction: str


@dataclass
class ReportIssue:
    Severity: str
    Area: str
    Message: str
    Evidence: str = ""


MOD_INPUT_EXTENSIONS = {".zip", ".rar", ".7z", ".esp", ".esm", ".esl", ".bsa", ".ba2", ".pex", ".txt", ".xml", ".json", ".jsonl", ".csv"}
REQUIRED_MODEL_CLAIMS = (
    "No runtime-impacting issues remain",
    "No required translation candidates remain untranslated",
    "No semantic quality blockers remain",
    "All changed final_mod files listed in the review packets were reviewed",
    "Mechanical checks do not replace Codex model semantic review",
    "Final review quality audit has 0 blocking issues and 0 warnings",
)
NON_MOD_OUTPUT_NAMES = {"project_packages"}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(read_text(path))
    except Exception:
        return {"_invalid_json": True}


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def read_report_metric(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in read_text(path).splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def zero(value: object) -> bool:
    return str(value).strip() in {"0", "0.0"}


def positive_int(value: object) -> bool:
    try:
        return int(str(value).strip()) > 0
    except Exception:
        return False


def latest_file_mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_mtime
    latest = path.stat().st_mtime
    for item in path.rglob("*"):
        if item.is_file():
            latest = max(latest, item.stat().st_mtime)
    return latest


def report_covers_artifacts(report: Path, artifacts: list[Path]) -> bool:
    if not report.is_file():
        return False
    if any(not path.exists() for path in artifacts):
        return False
    report_mtime = report.stat().st_mtime
    return all(latest_file_mtime(path) <= report_mtime + 1e-6 for path in artifacts)


def packet_content_reviewed(model_text: str, packet_path: Path) -> bool:
    if not packet_path.is_file():
        return False
    if packet_path.name not in model_text:
        return False
    packet_hash = read_report_metric(packet_path, "Items SHA256")
    return bool(packet_hash and packet_hash in model_text)


def model_text_mentions_path(model_text: str, path_value: str) -> bool:
    normalized_text = model_text.replace("/", "\\").lower()
    normalized_path = path_value.replace("/", "\\").lower()
    if normalized_path in normalized_text:
        return True
    basename = Path(path_value.replace("/", "\\")).name.lower()
    return bool(basename and basename in normalized_text)


def changed_files_from_packets(root: Path, mod_name: str) -> set[str]:
    files: set[str] = set()
    for suffix in ("final_text_review_items.jsonl", "final_binary_review_items.jsonl"):
        for row in read_jsonl(root / "qa" / f"{mod_name}.{suffix}"):
            value = row.get("File")
            if isinstance(value, str) and value.strip():
                files.add(value.strip())
    return files


def normalized_report_path(root: Path, path: Path) -> str:
    return relative_path(root, path).replace("/", "\\").lower()


def package_validation_status(root: Path, mod_name: str, final_mod: Path, package_path: Path) -> tuple[str, str, str, str]:
    # Re-validate current contents before trusting the saved report. This
    # catches a rebuilt package whose old qa/*.json still says "passed".
    report_path = root / "qa" / f"{mod_name}.chs_package_validation.md"
    json_path = root / "qa" / f"{mod_name}.chs_package_validation.json"
    report_rel = relative_path(root, report_path)
    current_sha = sha256_file(package_path) if package_path.is_file() else ""

    _, current_issues = validate_chs_package_contents(root, mod_name, final_mod, package_path)
    current_blocking = sum(1 for issue in current_issues if issue.Severity == "error")
    if current_blocking:
        return report_rel, "failed-current-content", str(current_blocking), current_sha

    if not report_path.is_file() or not json_path.is_file():
        return report_rel, "missing", "missing", current_sha

    payload = read_json(json_path)
    if payload.get("_invalid_json"):
        return report_rel, "invalid_json", "invalid_json", current_sha

    status = str(payload.get("Status", "")).strip()
    blocking = str(payload.get("BlockingIssues", "")).strip()
    if status != "passed" or not zero(blocking):
        return report_rel, status or "failed-report", blocking or "?", current_sha

    expected_final = normalized_report_path(root, final_mod)
    expected_package = normalized_report_path(root, package_path)
    actual_final = str(payload.get("FinalModDir", "")).replace("/", "\\").lower()
    actual_package = str(payload.get("PackagePath", "")).replace("/", "\\").lower()
    if actual_final != expected_final or actual_package != expected_package:
        return report_rel, "stale-path", "stale-path", current_sha

    recorded_sha = str(payload.get("PackageSha256", "")).strip().lower()
    if current_sha and recorded_sha != current_sha.lower():
        return report_rel, "stale-package-hash", "stale-package-hash", current_sha

    if not report_covers_artifacts(report_path, [final_mod, package_path]):
        return report_rel, "stale", "stale", current_sha

    return report_rel, "passed", "0", current_sha


def translation_dictionary_status(root: Path, mod_name: str) -> tuple[str, str, str]:
    dictionary_dir = root / "out" / mod_name / "汉化产出" / "intermediate" / "translation_text_dictionary"
    dictionary_jsonl = dictionary_dir / "translation_dictionary.jsonl"
    manifest_path = dictionary_dir / "manifest.json"
    dictionary_rel = relative_path(root, dictionary_jsonl)
    if not dictionary_dir.is_dir():
        return dictionary_rel, "missing-directory", "0"
    if not dictionary_jsonl.is_file():
        return dictionary_rel, "missing-jsonl", "0"
    if not manifest_path.is_file():
        return dictionary_rel, "missing-manifest", "0"

    manifest = read_json(manifest_path)
    if manifest.get("_invalid_json"):
        return dictionary_rel, "invalid-manifest", "0"

    row_count = 0
    invalid_rows = 0
    for line in read_text(dictionary_jsonl).splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            invalid_rows += 1
            continue
        if isinstance(payload, dict) and str(payload.get("source", "")).strip() and str(payload.get("target", "")).strip():
            row_count += 1
        else:
            invalid_rows += 1
    if invalid_rows:
        return dictionary_rel, "invalid-jsonl-row", str(row_count)

    manifest_count = manifest.get("TranslatedEntryCount", 0)
    if not positive_int(manifest_count):
        return dictionary_rel, "empty-manifest", str(row_count)
    if row_count <= 0:
        return dictionary_rel, "empty-jsonl", "0"
    if int(str(manifest_count).strip()) != row_count:
        return dictionary_rel, "count-mismatch", str(row_count)
    return dictionary_rel, "present", str(row_count)


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")


def safe_mod_name(value: str) -> str:
    name = re.sub(r"[\s/\\:;]+", "_", value.strip())
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._")
    return name or "UnnamedMod"


def command_for_input(path_rel: str, mod_name: str) -> str:
    return f'python .\\scripts\\run_non_gui_translation_workflow.py --mod-name "{mod_name}" --source-path ".\\{path_rel}" --force-prepare'


def py7zr_available() -> bool:
    try:
        import py7zr  # noqa: F401
    except Exception:
        return False
    return True


def archive7z_configured(root: Path) -> bool:
    config_path = root / "config" / "tools.local.json"
    if not config_path.is_file():
        return False
    try:
        parsed = json.loads(read_text(config_path))
    except Exception:
        return False
    decoder_tools = parsed.get("DecoderTools", {}) if isinstance(parsed, dict) else {}
    value = str(decoder_tools.get("Archive7zPath", "") or "").strip() if isinstance(decoder_tools, dict) else ""
    if not value:
        return False
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.is_file()


def input_action(root: Path, path_rel: str, mod_name: str) -> str:
    suffix = Path(path_rel).suffix.lower()
    if suffix == ".7z" and not (py7zr_available() or archive7z_configured(root)):
        return f"Install Python package py7zr or configure DecoderTools.Archive7zPath, then run `{command_for_input(path_rel, mod_name)}`."
    if suffix == ".rar":
        return f"Add a project-local extraction adapter for `{path_rel}` or extract it into `work/extracted_mods/{mod_name}/`, then run the non-GUI workflow."
    return command_for_input(path_rel, mod_name)


def infer_known_mod_names(root: Path) -> list[str]:
    names: set[str] = set()
    status_mod = read_report_metric(root / "qa" / "status.md", "ModName")
    if status_mod:
        names.add(status_mod)
    out_root = root / "out"
    if out_root.is_dir():
        names.update(item.name for item in out_root.iterdir() if item.is_dir() and not is_non_mod_output_name(item.name))
    work_root = root / "work" / "extracted_mods"
    if work_root.is_dir():
        names.update(
            item.name
            for item in work_root.iterdir()
            if item.is_dir() and not is_workspace_variant_name(item.name) and not is_non_mod_output_name(item.name)
        )
    return sorted(names, key=str.lower)


def is_non_mod_output_name(name: str) -> bool:
    return name.strip().lower() in NON_MOD_OUTPUT_NAMES


def is_workspace_variant_name(name: str) -> bool:
    # Installer-resolution experiments are internal workspaces, not separate
    # Mod outputs. Keeping them out of readiness prevents false "unfinished
    # input" rows when a canonical ModName already owns the final_mod output.
    lowered = name.lower()
    if ".stale-" in lowered or lowered.endswith(".stale"):
        return True
    return re.search(r"_(?:se|ae|vr)_(?:bsa|loose)$", lowered) is not None


def match_mod_name_for_input(input_path: Path, known_mod_names: list[str]) -> str:
    stem = input_path.stem if input_path.is_file() else input_path.name
    guessed = safe_mod_name(stem)
    comparable = re.sub(r"[^a-z0-9]+", "", guessed.lower())
    for known in known_mod_names:
        known_comparable = re.sub(r"[^a-z0-9]+", "", known.lower())
        if comparable and (comparable == known_comparable or comparable in known_comparable or known_comparable in comparable):
            return known
    return guessed


def collect_mod_inputs(root: Path, known_mod_names: list[str]) -> list[ModInputRow]:
    # Only scan the project-local mod/ sandbox. Directories are routed by their
    # first interesting file so readiness can suggest the same workflow command
    # without unpacking or editing the input.
    mod_root = root / "mod"
    if not mod_root.is_dir():
        return []
    rows: list[ModInputRow] = []
    for item in sorted(mod_root.iterdir(), key=lambda value: value.name.lower()):
        if item.name == ".gitkeep":
            continue
        if item.is_dir():
            files = [child for child in item.rglob("*") if child.is_file()]
            interesting = [child for child in files if child.suffix.lower() in MOD_INPUT_EXTENSIONS]
            route_path = interesting[0] if interesting else item / "dummy.txt"
            kind = f"directory ({len(files)} files)"
        elif item.is_file():
            if item.suffix.lower() not in MOD_INPUT_EXTENSIONS:
                continue
            route_path = item
            kind = item.suffix.lower() or "file"
        else:
            continue
        route = route_for(root, route_path)
        mod_name = match_mod_name_for_input(item, known_mod_names)
        rel = relative_path(root, item)
        rows.append(
            ModInputRow(
                Path=rel,
                Kind=kind,
                LikelyModName=mod_name,
                RouteSkill=route.skill,
                PrimaryTool=route.primary_tool,
                Risk=route.risk,
                RecommendedCommand=input_action(root, rel, mod_name),
            )
        )
    return rows


def model_review_status(root: Path, mod_name: str) -> str:
    # The review must bind to both final text and final binary packets. Draft
    # translation review is not enough to call a packaged output ready.
    review_path = root / "qa" / f"{mod_name}.model_review.md"
    final_text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
    final_binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
    if not review_path.is_file():
        return "missing"
    text = read_text(review_path)
    if not re.search(r"Reviewer:\s*Codex model", text, re.I):
        return "missing_reviewer"
    if re.search(r"\bTODO\b", text, re.I):
        return "todo_present"
    if not re.search(r"\bpass\b", text, re.I):
        return "no_pass_evidence"
    if f"{mod_name}.final_text_review_packet.md" not in text:
        return "missing_final_text_packet_scope"
    if f"{mod_name}.final_binary_review_packet.md" not in text:
        return "missing_final_binary_packet_scope"
    if not packet_content_reviewed(text, final_text_packet):
        return "missing_final_text_packet_hash"
    if not packet_content_reviewed(text, final_binary_packet):
        return "missing_final_binary_packet_hash"
    for claim in REQUIRED_MODEL_CLAIMS:
        if claim not in text:
            return "missing_required_model_claim"
    for changed_file in changed_files_from_packets(root, mod_name):
        if not model_text_mentions_path(text, changed_file):
            return "missing_changed_file_scope"
    return "passed"


def final_review_quality_status(root: Path, mod_name: str) -> tuple[str, str, str]:
    report_path = root / "qa" / f"{mod_name}.final_review_quality.md"
    json_path = root / "qa" / f"{mod_name}.final_review_quality.json"
    text_items = root / "qa" / f"{mod_name}.final_text_review_items.jsonl"
    binary_items = root / "qa" / f"{mod_name}.final_binary_review_items.jsonl"
    if not report_path.is_file() or not json_path.is_file():
        return "missing", "missing", "missing"
    payload = read_json(json_path)
    if payload.get("_invalid_json"):
        return "invalid_json", "invalid_json", "invalid_json"
    status = str(payload.get("Status", "")).strip()
    blocking = str(payload.get("BlockingIssues", "")).strip()
    warnings = str(payload.get("Warnings", "")).strip()
    if not report_covers_artifacts(report_path, [text_items, binary_items]):
        return "stale", blocking or "stale", warnings or "stale"
    return status or "unknown", blocking or "?", warnings or "?"


def workflow_status(root: Path, mod_name: str) -> tuple[str, str, str]:
    health = read_json(root / "qa" / "workflow_health.json")
    if str(health.get("ModName", "")) == mod_name:
        issues = health.get("Issues", [])
        if isinstance(issues, list):
            blocking = sum(
                1
                for issue in issues
                if isinstance(issue, dict)
                and str(issue.get("Severity", "")).lower() == "error"
                and str(issue.get("Area", "")).lower() != "readiness"
            )
            warnings = sum(
                1
                for issue in issues
                if isinstance(issue, dict)
                and str(issue.get("Severity", "")).lower() == "warning"
                and str(issue.get("Area", "")).lower() != "readiness"
            )
            verdict = "PASS" if blocking == 0 and warnings == 0 else str(health.get("Verdict", ""))
            return verdict, str(blocking), str(warnings)
        return str(health.get("Verdict", "")), str(health.get("BlockingIssues", "")), str(health.get("Warnings", ""))

    workflow = read_json(root / "qa" / f"{mod_name}.non_gui_workflow_run.json")
    issues = workflow.get("Issues", [])
    if isinstance(issues, list):
        non_health_errors = [
            issue
            for issue in issues
            if isinstance(issue, dict)
            and str(issue.get("Severity", "")).lower() == "error"
            and str(issue.get("Step", "")).lower() != "workflow-health"
        ]
        warning_count = sum(
            1
            for issue in issues
            if isinstance(issue, dict) and str(issue.get("Severity", "")).lower() == "warning"
        )
        if not non_health_errors:
            return "PASS", "0", str(warning_count)
    return str(workflow.get("Verdict", "")), str(workflow.get("BlockingIssues", "")), str(workflow.get("Warnings", ""))


def classify_output(row: OutputRow) -> tuple[str, str]:
    # Ordering matters: start with physical deliverables, then dictionary,
    # package freshness, strict gate, coverage, packet cleanliness, and finally
    # model review. This produces the most useful next action.
    if not row.FinalModExists:
        example_input = r"mod\<ModArchive>.zip"
        return ("needs_translation", f"Run `{command_for_input(example_input, row.ModName)}` or build final_mod after preparing the workspace.")
    if row.ProvenanceStatus != "present":
        return ("blocked_by_qa", f"Rebuild final_mod to generate required provenance ledger: `{row.ProvenancePath}`.")
    if not row.PackagedModExists:
        return ("blocked_by_qa", f"Rebuild final_mod to generate required CHS package: `{row.PackagedModPath}`.")
    if row.TranslationDictionaryStatus != "present" or not positive_int(row.TranslationDictionaryEntries):
        return ("blocked_by_qa", f"Rebuild final_mod to generate required translation dictionary: `{row.TranslationDictionaryPath}`.")
    if row.PackageValidationStatus != "passed" or not zero(row.PackageValidationBlockingIssues):
        return ("blocked_by_qa", f"Rerun `python .\\scripts\\validate_chs_package.py --mod-name \"{row.ModName}\"` and inspect `{row.PackageValidationReport}`.")
    if row.DeliveryMode and row.DeliveryMode != "direct-replacement-final-mod":
        return ("blocked_by_qa", f"Inspect `{row.FinalModDir}\\meta\\manifest.json`; delivery mode is not direct-replacement-final-mod.")
    if not zero(row.StrictGateBlockingIssues) or not zero(row.StrictGateWarnings):
        return ("blocked_by_qa", f"Inspect `qa/{row.ModName}.non_gui_qa_gates.md` and rerun strict gate after fixes.")
    if not zero(row.CoverageMissing) or not zero(row.CoverageUnverified):
        return ("blocked_by_qa", f"Inspect `out/{row.ModName}/qa/non_gui_translation_coverage.md`; coverage is incomplete.")
    if not zero(row.FinalTextProtectedItems) or not zero(row.FinalBinaryProtectedItems) or not zero(row.FinalBinaryExportFailures):
        return ("needs_model_review", f"Inspect final review packets under `qa/{row.ModName}.*review_packet.md` before delivery.")
    if row.FinalReviewQualityStatus != "passed" or not zero(row.FinalReviewQualityBlockingIssues) or not zero(row.FinalReviewQualityWarnings):
        return ("blocked_by_qa", f"Inspect `qa/{row.ModName}.final_review_quality.md`; final delivered text quality audit is not clean.")
    if row.ModelReviewStatus != "passed":
        return ("needs_model_review", f"Complete Codex model review in `qa/{row.ModName}.model_review.md`.")
    # The workflow run report is a historical orchestration log. A later Codex
    # model review plus a clean strict gate is the current release evidence, so
    # an older workflow failure must not keep a completed output blocked.
    package_note = f" Package: `{row.PackagedModPath}`." if row.PackagedModExists else ""
    return ("ready_for_manual_test", f"Player inspects `{row.FinalModDir}`, then tests it as a local MO2/Vortex mod.{package_note}")


def collect_outputs(root: Path, mod_names: list[str]) -> list[OutputRow]:
    # Known outputs are inferred from both work/ and out/ so a partially prepared
    # Mod still appears in the report instead of disappearing from handoff.
    rows: list[OutputRow] = []
    for mod_name in mod_names:
        workspace = root / "work" / "extracted_mods" / mod_name
        final_mod = default_final_mod_dir(root, mod_name)
        provenance_path = final_mod / "meta" / "provenance.jsonl"
        package_path = packaged_mod_path(root, mod_name)
        manifest = read_json(final_mod / "meta" / "manifest.json")
        workflow_verdict, workflow_blocking, workflow_warnings = workflow_status(root, mod_name)
        dictionary_path, dictionary_status_value, dictionary_entries = translation_dictionary_status(root, mod_name)
        package_validation_report, package_validation_status_value, package_validation_blocking, package_validation_sha = package_validation_status(root, mod_name, final_mod, package_path)
        gate_path = root / "qa" / f"{mod_name}.non_gui_qa_gates.md"
        strict_gate_blocking = read_report_metric(gate_path, "Blocking issues")
        strict_gate_warnings = read_report_metric(gate_path, "Warnings")
        gate_final_mod = read_report_metric(gate_path, "FinalModDir")
        expected_final_mod = relative_path(root, final_mod)
        if not gate_path.is_file():
            strict_gate_blocking = "missing"
        elif gate_final_mod.replace("/", "\\").lower() != expected_final_mod.replace("/", "\\").lower():
            strict_gate_blocking = "stale-final-mod-path"
            strict_gate_warnings = "stale-final-mod-path"
        coverage_path = root / "out" / mod_name / "qa" / "non_gui_translation_coverage.md"
        final_text_packet = root / "qa" / f"{mod_name}.final_text_review_packet.md"
        final_binary_packet = root / "qa" / f"{mod_name}.final_binary_review_packet.md"
        quality_status, quality_blocking, quality_warnings = final_review_quality_status(root, mod_name)
        row = OutputRow(
            ModName=mod_name,
            Workspace=relative_path(root, workspace),
            WorkspaceExists=workspace.is_dir(),
            FinalModDir=relative_path(root, final_mod),
            FinalModExists=final_mod.is_dir(),
            ProvenancePath=relative_path(root, provenance_path),
            ProvenanceStatus="present" if provenance_path.is_file() else "missing",
            PackagedModPath=relative_path(root, package_path),
            PackagedModExists=package_path.is_file(),
            TranslationDictionaryPath=dictionary_path,
            TranslationDictionaryStatus=dictionary_status_value,
            TranslationDictionaryEntries=dictionary_entries,
            PackageValidationReport=package_validation_report,
            PackageValidationStatus=package_validation_status_value,
            PackageValidationBlockingIssues=package_validation_blocking,
            PackageValidationSha256=package_validation_sha,
            DeliveryMode=str(manifest.get("DeliveryMode", "")),
            WorkflowVerdict=workflow_verdict,
            WorkflowBlockingIssues=workflow_blocking,
            WorkflowWarnings=workflow_warnings,
            StrictGateBlockingIssues=strict_gate_blocking,
            StrictGateWarnings=strict_gate_warnings,
            CoverageMissing=read_report_metric(coverage_path, "Missing"),
            CoverageUnverified=read_report_metric(coverage_path, "Unverified"),
            FinalTextReviewItems=read_report_metric(final_text_packet, "Review items"),
            FinalTextProtectedItems=read_report_metric(final_text_packet, "Protected review items"),
            FinalBinaryReviewItems=read_report_metric(final_binary_packet, "Review items"),
            FinalBinaryProtectedItems=read_report_metric(final_binary_packet, "Protected review items"),
            FinalBinaryExportFailures=read_report_metric(final_binary_packet, "Export failures"),
            FinalReviewQualityStatus=quality_status,
            FinalReviewQualityBlockingIssues=quality_blocking,
            FinalReviewQualityWarnings=quality_warnings,
            ModelReviewStatus=model_review_status(root, mod_name),
            OverallStatus="",
            NextRecommendedAction="",
        )
        row.OverallStatus, row.NextRecommendedAction = classify_output(row)
        rows.append(row)
    return rows


def collect_issues(root: Path, input_rows: list[ModInputRow], output_rows: list[OutputRow]) -> list[ReportIssue]:
    issues: list[ReportIssue] = []
    output_by_name = {row.ModName: row for row in output_rows}
    input_mod_names = {row.LikelyModName for row in input_rows}
    if not (root / "mod").is_dir():
        issues.append(ReportIssue("error", "input", "Missing project-local mod/ input directory.", "mod"))
    elif not input_rows:
        issues.append(ReportIssue("warning", "input", "No actionable Mod input was found under mod/.", "mod"))
    for row in input_rows:
        output = output_by_name.get(row.LikelyModName)
        if output is None:
            issues.append(ReportIssue("error", "unprocessed_input", f"Input has no known workflow output: {row.Path}", row.RecommendedCommand))
        elif output.OverallStatus != "ready_for_manual_test":
            issues.append(ReportIssue("error", "unfinished_input", f"Input output is not ready: {row.Path}", row.RecommendedCommand))
    for row in output_rows:
        if row.ModName in input_mod_names:
            continue
        if row.OverallStatus in {"blocked_by_qa", "needs_model_review"}:
            issues.append(ReportIssue("warning", row.OverallStatus, f"{row.ModName}: {row.NextRecommendedAction}", row.FinalModDir))
        elif row.OverallStatus == "needs_translation":
            issues.append(ReportIssue("warning", "needs_translation", f"{row.ModName}: final_mod is not ready.", row.FinalModDir))
    return issues


def ready_next_action(ready_rows: list[OutputRow]) -> str:
    if not ready_rows:
        return ""
    if len(ready_rows) == 1:
        return ready_rows[0].NextRecommendedAction
    return (
        f"Inspect and game-test each of the {len(ready_rows)} ready `final_mod` directories and CHS packages listed in Known Mod Outputs; "
        "the player tests one Mod at a time as a local MO2/Vortex mod."
    )


def write_reports(root: Path, report_path: Path, json_path: Path, input_rows: list[ModInputRow], output_rows: list[OutputRow], issues: list[ReportIssue]) -> None:
    ready_rows = [row for row in output_rows if row.OverallStatus == "ready_for_manual_test"]
    blocking = sum(1 for issue in issues if issue.Severity == "error")
    warnings = sum(1 for issue in issues if issue.Severity == "warning")
    unprocessed_inputs = [issue for issue in issues if issue.Area in {"unprocessed_input", "unfinished_input"}]
    if blocking:
        project_status = "blocked"
        next_action = "Fix blocking readiness issues before rerunning the workflow."
    elif unprocessed_inputs:
        project_status = "needs_translation"
        next_action = unprocessed_inputs[0].Evidence
    elif ready_rows:
        project_status = "ready_for_manual_test"
        next_action = ready_next_action(ready_rows)
    elif output_rows:
        project_status = output_rows[0].OverallStatus
        next_action = output_rows[0].NextRecommendedAction
    elif input_rows:
        project_status = "needs_translation"
        next_action = input_rows[0].RecommendedCommand
    else:
        project_status = "needs_input"
        next_action = "Place a sandboxed Mod archive or directory under mod/."

    lines = [
        "# Translation Readiness Report",
        "",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Overall status: {project_status}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        f"- Ready outputs: {len(ready_rows)}",
        f"- Next recommended action: {next_action}",
        "",
        "## Mod Inputs",
        "",
    ]
    if not input_rows:
        lines.append("No actionable input found under `mod/`.")
    else:
        lines.extend(["| Path | Kind | Likely ModName | Route Skill | Primary Tool | Risk | Recommended Command |", "|---|---|---|---|---|---|---|"])
        for row in input_rows:
            lines.append(
                f"| {markdown_cell(row.Path)} | {markdown_cell(row.Kind)} | {markdown_cell(row.LikelyModName)} | "
                f"{markdown_cell(row.RouteSkill)} | {markdown_cell(row.PrimaryTool)} | {markdown_cell(row.Risk)} | "
                f"{markdown_cell(row.RecommendedCommand)} |"
            )

    lines.extend(["", "## Known Mod Outputs", ""])
    if not output_rows:
        lines.append("No known mod outputs found under `out/` or `work/extracted_mods/`.")
    else:
        lines.extend(
            [
                "| ModName | Workspace | final_mod | CHS package | Dictionary | Package validation | Delivery | Workflow | Strict Gate | Coverage | Reviews | Status | Next Action |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in output_rows:
            workflow = f"{row.WorkflowVerdict or 'unknown'} / B:{row.WorkflowBlockingIssues or '?'} W:{row.WorkflowWarnings or '?'}"
            dictionary = f"{row.TranslationDictionaryStatus or 'unknown'} / entries:{row.TranslationDictionaryEntries or '0'}"
            package_validation = f"{row.PackageValidationStatus or 'unknown'} / B:{row.PackageValidationBlockingIssues or '?'} / {row.PackageValidationSha256 or '?'}"
            gate = f"B:{row.StrictGateBlockingIssues or '?'} W:{row.StrictGateWarnings or '?'}"
            coverage = f"Missing:{row.CoverageMissing or '?'} Unverified:{row.CoverageUnverified or '?'}"
            reviews = (
                f"Text:{row.FinalTextReviewItems or '?'} protected:{row.FinalTextProtectedItems or '?'}; "
                f"Binary:{row.FinalBinaryReviewItems or '?'} protected:{row.FinalBinaryProtectedItems or '?'} "
                f"export_fail:{row.FinalBinaryExportFailures or '?'}; "
                f"FinalQuality:{row.FinalReviewQualityStatus}/B:{row.FinalReviewQualityBlockingIssues}/W:{row.FinalReviewQualityWarnings}; "
                f"Model:{row.ModelReviewStatus}"
            )
            lines.append(
                f"| {markdown_cell(row.ModName)} | {markdown_cell(row.Workspace)} ({row.WorkspaceExists}) | "
                f"{markdown_cell(row.FinalModDir)} ({row.FinalModExists}) | "
                f"{markdown_cell(row.PackagedModPath)} ({row.PackagedModExists}) | {markdown_cell(dictionary)} | {markdown_cell(package_validation)} | "
                f"{markdown_cell(row.DeliveryMode)} | {markdown_cell(workflow)} | {markdown_cell(gate)} | {markdown_cell(coverage)} | "
                f"{markdown_cell(reviews)} | {markdown_cell(row.OverallStatus)} | {markdown_cell(row.NextRecommendedAction)} |"
            )

    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("No readiness issues.")
    else:
        lines.extend(["| Severity | Area | Message | Evidence |", "|---|---|---|---|"])
        for issue in issues:
            lines.append(f"| {issue.Severity} | {issue.Area} | {markdown_cell(issue.Message)} | {markdown_cell(issue.Evidence)} |")

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This audit is read-only for Mod inputs and final_mod content.",
            "- This audit writes only `qa/translation_readiness.md` and `qa/translation_readiness.json`.",
            "- This audit does not translate text and does not modify ESP/ESM/ESL/PEX/BSA/BA2 files.",
            "- Real Skyrim, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "ProjectRoot": str(root),
        "CheckedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "OverallStatus": project_status,
        "BlockingIssues": blocking,
        "Warnings": warnings,
        "NextRecommendedAction": next_action,
        "ModInputs": [asdict(row) for row in input_rows],
        "KnownModOutputs": [asdict(row) for row in output_rows],
        "Issues": [asdict(issue) for issue in issues],
        "Safety": {
            "ProjectLocalOnly": True,
            "ReadOnlyForModAndFinalMod": True,
            "DirectBinaryEdit": False,
        },
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a project-local Skyrim translation readiness and handoff report.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--report-output-path", default="qa/translation_readiness.md")
    parser.add_argument("--json-output-path", default="qa/translation_readiness.json")
    args = parser.parse_args()

    root = project_root()
    report_path = resolve_project_path(root, args.report_output_path, must_exist=False)
    json_path = resolve_project_path(root, args.json_output_path, must_exist=False)
    qa_root = resolve_project_path(root, "qa", must_exist=False)
    if not is_under(report_path, qa_root):
        raise ValueError(f"ReportOutputPath must be under qa/: {args.report_output_path}")
    if not is_under(json_path, qa_root):
        raise ValueError(f"JsonOutputPath must be under qa/: {args.json_output_path}")

    known_mod_names = infer_known_mod_names(root)
    if args.mod_name.strip():
        mod_name = args.mod_name.strip()
        if mod_name not in known_mod_names:
            known_mod_names.append(mod_name)
            known_mod_names = sorted(set(known_mod_names), key=str.lower)

    input_rows = collect_mod_inputs(root, known_mod_names)
    output_names = sorted(set(name for name in known_mod_names if name), key=str.lower)
    output_rows = collect_outputs(root, output_names)
    input_command_by_mod = {row.LikelyModName: row.RecommendedCommand for row in input_rows}
    for row in output_rows:
        command = input_command_by_mod.get(row.ModName, "")
        if command and row.OverallStatus != "ready_for_manual_test":
            row.NextRecommendedAction = command
    issues = collect_issues(root, input_rows, output_rows)
    write_reports(root, report_path, json_path, input_rows, output_rows, issues)
    print(f"Translation readiness report written to: {report_path}")
    print(f"Translation readiness JSON written to: {json_path}")
    print(f"Known mod outputs: {len(output_rows)}")
    print(f"Blocking issues: {sum(1 for issue in issues if issue.Severity == 'error')}")
    print(f"Warnings: {sum(1 for issue in issues if issue.Severity == 'warning')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
