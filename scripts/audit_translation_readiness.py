"""Summarize current project readiness without rerunning the full workflow.

The report is the handoff entry point for later Codex sessions: it classifies
mod/ inputs, known out/<ModName>/ outputs, QA freshness, and the next action.
"""

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from adapter_result_io import read_adapter_result
from capability_resolver import resolve_resource_capability
from game_context import (
    GAME_METADATA_KEYS,
    GameContext,
    game_context_metadata,
    game_display_label,
    game_metadata_mismatches,
)
from model_review_contract import (
    MODEL_REVIEWER_RE,
    REQUIRED_MODEL_CLAIMS,
    changed_files_from_packets,
    final_review_quality_evidence,
    has_model_claim,
    model_text_mentions_path,
    packet_content_reviewed,
    read_jsonl_objects as read_jsonl,
    read_report_metric,
    report_covers_artifacts,
)
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import find_data_root, is_under, packaged_mod_path, project_root, relative_path, resolve_project_path
from route_translation_task import current_game_context, route_for
from plugin_resource_evidence import (
    PluginReportTraits,
    plugin_resource_descriptor,
    read_plugin_report_traits,
    validate_plugin_post_verify_report,
    validate_plugin_report_identity,
    validate_plugin_report_status,
)
from translation_dictionary import inspect_translation_dictionary
from used_capabilities import UsedCapabilityError, write_used_capabilities
from validate_chs_package import sha256_file, validate as validate_chs_package_contents
from workflow_issues import make_issue_record
from file_utils import py7zr_available, read_json_object_or_invalid_any as read_json
from file_utils import read_text_utf8_sig_strict as read_text
from report_utils import markdown_cell
from report_utils import is_zero_metric as zero


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
    UsedCapabilitiesPath: str
    UsedCapabilitiesStatus: str
    UsedCapabilitiesBlockingIssues: str
    PluginStagePath: str
    PluginStageStatus: str
    PluginStageBlockingIssues: str
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
    CoverageBlocking: str
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
    issue_id: str = ""
    code: str = ""
    mod_name: str = ""
    affected_artifact: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    reported_by: list[str] = field(default_factory=lambda: ["translation_readiness"])
    impact_scope: str = ""


def enrich_report_issue(issue: ReportIssue) -> ReportIssue:
    record = make_issue_record(
        code=issue.code or issue.Area,
        mod_name=issue.mod_name,
        affected_artifact=issue.affected_artifact or issue.Evidence or "project",
        severity=issue.Severity,
        message=issue.Message,
        evidence_paths=issue.evidence_paths or ([issue.Evidence] if issue.Evidence else []),
        reported_by=issue.reported_by,
        impact_scope=issue.impact_scope,
    )
    for key in (
        "issue_id",
        "code",
        "mod_name",
        "affected_artifact",
        "evidence_paths",
        "reported_by",
        "impact_scope",
    ):
        setattr(issue, key, record[key])
    return issue


MOD_INPUT_EXTENSIONS = {".zip", ".rar", ".7z", ".esp", ".esm", ".esl", ".bsa", ".ba2", ".pex", ".txt", ".xml", ".json", ".jsonl", ".csv"}
NON_MOD_OUTPUT_NAMES = {"project_packages"}
PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}
PLUGIN_STAGE_SCHEMA = "skyrim-mod-chs.plugin-translation-stage"
PLUGIN_STAGE_SCHEMA_VERSION = 3
PLUGIN_STAGE_SUCCESS_STATUSES = {
    "no_candidates",
    "translated_tool_output_ready",
    "experimental_tool_output_ready",
}
PLUGIN_WRITE_SUCCESS_STATUSES = {
    "translated_tool_output_ready",
    "experimental_tool_output_ready",
}
PLUGIN_ATTEMPT_OPERATIONS = {
    "export": "read",
    "apply": "write",
    "output_export": "read",
    "adapter_verify": "read",
    "post_verify": "read",
}
PLUGIN_RESOLVER_OPERATIONS = {
    "resolve_inventory": "inventory",
    "resolve_read": "read",
    "resolve_write": "write",
    "resolve_apply_write": "write",
    "resolve_verify_read": "read",
}
PLUGIN_RESOLVER_REPORT_PHASE = {
    "resolve_inventory": "export",
    "resolve_read": "export",
    "resolve_write": "export",
    "resolve_apply_write": "apply",
    "resolve_verify_read": "adapter_verify",
}


def declared_game_metadata(path: Path) -> dict[str, object]:
    if path.suffix.lower() == ".json":
        payload = read_json(path)
        return {key: payload[key] for key in GAME_METADATA_KEYS if key in payload}
    if path.suffix.lower() == ".jsonl":
        for payload in read_jsonl(path):
            declared = {key: payload[key] for key in GAME_METADATA_KEYS if key in payload}
            if declared:
                return declared
        return {}
    if path.suffix.lower() != ".md":
        return {}
    payload: dict[str, object] = {}
    for key in GAME_METADATA_KEYS:
        raw = read_report_metric(path, key)
        if not raw:
            continue
        if key == "game_profile_version":
            try:
                payload[key] = int(raw)
            except ValueError:
                payload[key] = raw
        else:
            payload[key] = raw
    return payload


def collect_game_identity_issues(root: Path, context: GameContext) -> list[ReportIssue]:
    candidates = list((root / "qa").rglob("*.md")) + list((root / "qa").rglob("*.json"))
    for evidence_root in (
        root / "source" / "plugin_exports",
        root / "source" / "pex_exports",
        root / "translated" / "plugin_exports",
        root / "work" / "normalized",
    ):
        if evidence_root.is_dir():
            candidates.extend(evidence_root.rglob("*.jsonl"))
    candidates.extend((root / "out").glob("*/汉化产出/final_mod/meta/manifest.json"))
    issues: list[ReportIssue] = []
    required_evidence_names = {
        "agent_handoff.json",
        "codex_handoff.json",
        "translation_readiness.json",
        "workflow_health.json",
        "workflow_state.json",
        "workflow_tasks.json",
    }
    game_id_required_evidence_names = required_evidence_names | {
        "agent_handoff.md",
        "codex_handoff.md",
        "translation_readiness.md",
        "workflow_health.md",
        "workflow_state.md",
        "workflow_tasks.md",
    }
    for path in sorted(set(candidates), key=lambda item: str(item).lower()):
        if path.suffix.lower() == ".jsonl":
            declarations = []
            for line in read_text(path).splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    row = None
                declarations.append(
                    {key: row[key] for key in GAME_METADATA_KEYS if key in row}
                    if isinstance(row, dict)
                    else {}
                )
        else:
            declarations = [declared_game_metadata(path)]
        final_manifest = path.as_posix().lower().endswith("/final_mod/meta/manifest.json")
        complete_metadata_required = path.name.lower() in required_evidence_names or final_manifest
        metadata_required = (
            path.name.lower() in game_id_required_evidence_names
            or complete_metadata_required
            or (path.suffix.lower() == ".jsonl" and bool(declarations))
        )
        for declared in declarations:
            if not declared:
                if not metadata_required:
                    continue
            if complete_metadata_required:
                mismatches = game_metadata_mismatches(declared, context)
            else:
                expected = game_context_metadata(context)
                merged = dict(expected)
                merged.update(declared)
                mismatches = game_metadata_mismatches(merged, context)
                if "game_id" not in declared:
                    mismatches.insert(0, "missing game_id")
            if mismatches:
                issues.append(
                    ReportIssue(
                        "error",
                        "game_identity_mismatch",
                        f"Downstream evidence game metadata mismatch: {'; '.join(mismatches)}",
                        relative_path(root, path),
                    )
                )
                break
    return issues



def positive_int(value: object) -> bool:
    try:
        return int(str(value).strip()) > 0
    except Exception:
        return False


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
    inspection = inspect_translation_dictionary(root, mod_name)
    dictionary_rel = relative_path(root, inspection.dictionary_path)
    if not inspection.directory_exists:
        return dictionary_rel, "missing-directory", "0"
    if not inspection.dictionary_exists:
        return dictionary_rel, "missing-jsonl", "0"
    if not inspection.manifest_exists:
        return dictionary_rel, "missing-manifest", "0"
    if not inspection.manifest_valid:
        return dictionary_rel, "invalid-manifest", "0"
    if inspection.invalid_rows:
        return dictionary_rel, "invalid-jsonl-row", str(inspection.translated_rows)
    if not inspection.manifest_entries_valid or inspection.manifest_entries <= 0:
        return dictionary_rel, "empty-manifest", str(inspection.translated_rows)
    if inspection.translated_rows <= 0:
        return dictionary_rel, "empty-jsonl", "0"
    if inspection.manifest_entries != inspection.line_count:
        return dictionary_rel, "count-mismatch", str(inspection.translated_rows)
    return dictionary_rel, "present", str(inspection.translated_rows)



def safe_mod_name(value: str) -> str:
    name = re.sub(r"[\s/\\:;]+", "_", value.strip())
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._")
    return name or "UnnamedMod"


def command_for_input(path_rel: str, mod_name: str) -> str:
    return f'python .\\scripts\\run_non_gui_translation_workflow.py --mod-name "{mod_name}" --source-path ".\\{path_rel}" --force-prepare'


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


def collect_mod_inputs(root: Path, known_mod_names: list[str], context: GameContext | None = None) -> list[ModInputRow]:
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
        route = route_for(root, route_path, context)
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
    if MODEL_REVIEWER_RE.search(text) is None:
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
        if not has_model_claim(text, claim):
            return "missing_required_model_claim"
    for changed_file in changed_files_from_packets(root, mod_name):
        if not model_text_mentions_path(text, changed_file):
            return "missing_changed_file_scope"
    return "passed"


def final_review_quality_status(root: Path, mod_name: str) -> tuple[str, str, str]:
    status, blocking, warnings, current = final_review_quality_evidence(root, mod_name)
    if status in {"missing", "invalid_json"}:
        return status, status, status
    if not current:
        return "stale", blocking or "stale", warnings or "stale"
    return status or "unknown", blocking or "?", warnings or "?"


def workflow_run_status(root: Path, mod_name: str) -> tuple[str, str, str]:
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


def workflow_status(root: Path, mod_name: str) -> tuple[str, str, str]:
    health_path = root / "qa" / "workflow_health.json"
    workflow_path = root / "qa" / f"{mod_name}.non_gui_workflow_run.json"
    health = read_json(health_path)
    workflow_is_newer = workflow_path.is_file() and (
        not health_path.is_file() or workflow_path.stat().st_mtime > health_path.stat().st_mtime + 1e-6
    )
    if workflow_is_newer:
        return workflow_run_status(root, mod_name)

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

    return workflow_run_status(root, mod_name)


def _plugin_stage_file(
    root: Path,
    raw_value: object,
    *,
    allowed_root: Path,
    error_prefix: str,
) -> Path:
    value = str(raw_value or "").replace("\\", "/")
    parsed = PurePosixPath(value)
    if (
        not value
        or parsed.is_absolute()
        or bool(Path(value).drive)
        or ".." in parsed.parts
        or parsed.as_posix() != value
    ):
        raise ValueError(f"{error_prefix}_path_invalid")
    candidate = root.joinpath(*parsed.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{error_prefix}_missing_or_outside_project") from exc
    if not resolved.is_file() or not is_under(
        resolved,
        allowed_root.resolve(strict=False),
    ):
        raise ValueError(f"{error_prefix}_missing_or_outside_allowed_root")
    canonical = resolved.relative_to(root.resolve(strict=True)).as_posix()
    if canonical.casefold() != value.casefold():
        raise ValueError(f"{error_prefix}_path_not_canonical")
    return resolved


def _require_plugin_stage_hash(
    row: dict[str, object],
    field: str,
    path: Path,
    error_prefix: str,
) -> None:
    declared = str(row.get(field, "")).strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", declared) is None:
        raise ValueError(f"{error_prefix}_hash_invalid")
    if sha256_file(path) != declared:
        raise ValueError(f"{error_prefix}_hash_mismatch")


def _plugin_stage_relative(root: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()


def _validate_plugin_capability_row(
    row: dict[str, object],
    *,
    relative_plugin: str,
    resource: object,
    decision: object,
    expected_operation: str,
) -> None:
    required_text = (
        "resource_category",
        "resource_subtype",
        "resource_container",
        "capability",
        "operation",
        "effective_level",
        "reason",
    )
    if any(not isinstance(row.get(field), str) for field in required_text):
        raise ValueError("plugin_capability_fields_invalid")
    if row.get("resource_path") != relative_plugin:
        raise ValueError("plugin_capability_resource_path_mismatch")
    expected = {
        "resource_category": resource.category,
        "resource_subtype": resource.subtype,
        "resource_container": resource.container,
        "resource_traits": sorted(resource.traits),
        "capability": decision.capability,
        "operation": expected_operation,
        "effective_level": decision.level,
        "strict_complete_allowed": decision.strict_complete_allowed,
        "supported": decision.supported,
    }
    for field, value in expected.items():
        if row.get(field) != value:
            raise ValueError(f"plugin_capability_{field}_mismatch")
    expected_error = None if decision.supported else decision.error_code
    if "error_code" not in row or row.get("error_code") != expected_error:
        raise ValueError("plugin_capability_error_code_mismatch")


def _validate_plugin_success_evidence(
    root: Path,
    row: dict[str, object],
    *,
    status: str,
    context: GameContext,
    input_path: Path,
    relative_plugin: str,
    tool_output: Path | None,
    plugin_adapter: str,
) -> dict[str, Path]:
    evidence = row.get("CapabilityEvidence")
    if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
        raise ValueError("plugin_capability_evidence_invalid")
    attempts = [item for item in evidence if item.get("evidence_kind") == "adapter_attempt"]
    verification_attempts = [
        item for item in evidence if item.get("evidence_kind") == "verification_attempt"
    ]
    resolvers = [item for item in evidence if item.get("evidence_kind") == "resolver_decision"]
    if len(attempts) + len(verification_attempts) + len(resolvers) != len(evidence):
        raise ValueError("plugin_capability_evidence_kind_invalid")

    expected_attempts = (
        {"export"}
        if status == "no_candidates"
        else {"export", "apply", "output_export", "adapter_verify"}
    )
    expected_verification_attempts = set() if status == "no_candidates" else {"post_verify"}
    expected_resolvers = (
        {"resolve_inventory", "resolve_read", "resolve_write"}
        if status == "no_candidates"
        else set(PLUGIN_RESOLVER_OPERATIONS)
    )
    attempt_phases = [str(item.get("phase", "")) for item in attempts]
    verification_phases = [str(item.get("phase", "")) for item in verification_attempts]
    resolver_phases = [str(item.get("phase", "")) for item in resolvers]
    if len(attempt_phases) != len(set(attempt_phases)) or set(attempt_phases) != expected_attempts:
        raise ValueError("plugin_attempt_phase_contract_mismatch")
    if (
        len(verification_phases) != len(set(verification_phases))
        or set(verification_phases) != expected_verification_attempts
    ):
        raise ValueError("plugin_verification_phase_contract_mismatch")
    if len(resolver_phases) != len(set(resolver_phases)) or set(resolver_phases) != expected_resolvers:
        raise ValueError("plugin_resolver_phase_contract_mismatch")

    attempt_reports: dict[str, Path] = {}
    attempt_resources: dict[str, tuple[object, object]] = {}
    base_resource = plugin_resource_descriptor(context, Path(relative_plugin))
    requires_trait_contract = bool(
        context.resource_model.trait_level_caps.get(base_resource.capability, {})
    )
    for attempt in attempts:
        phase = str(attempt["phase"])
        if attempt.get("result") != "success" or type(attempt.get("return_code")) is not int or attempt.get("return_code") != 0:
            raise ValueError(f"plugin_attempt_{phase}_not_successful")
        report = _plugin_stage_file(
            root,
            attempt.get("report_path"),
            allowed_root=root / "qa",
            error_prefix=f"plugin_attempt_{phase}_report",
        )
        _require_plugin_stage_hash(
            attempt,
            "report_sha256",
            report,
            f"plugin_attempt_{phase}_report",
        )

        expected_input = tool_output if phase == "output_export" else input_path
        if expected_input is None:
            raise ValueError(f"plugin_attempt_{phase}_input_missing")
        expected_report_operation = {
            "export": "export",
            "apply": "apply",
            "output_export": "export",
            "adapter_verify": "verify",
        }[phase]
        if phase == "export" and not requires_trait_contract:
            report_traits = PluginReportTraits()
        else:
            validate_plugin_report_identity(
                report,
                project_root=root,
                expected_input=expected_input,
                expected_game=context.game_id,
                expected_operation=expected_report_operation,
            )
            validate_plugin_report_status(report, return_code=0)
            report_traits = read_plugin_report_traits(report)
        resource = plugin_resource_descriptor(
            context,
            Path(relative_plugin),
            report_traits,
        )
        operation = PLUGIN_ATTEMPT_OPERATIONS[phase]
        decision = resolve_resource_capability(context, resource, operation)
        if decision.adapter_id and decision.adapter_id != plugin_adapter:
            raise ValueError("plugin_adapter_mismatch")
        _validate_plugin_capability_row(
            attempt,
            relative_plugin=relative_plugin,
            resource=resource,
            decision=decision,
            expected_operation=operation,
        )
        attempt_reports[phase] = report
        attempt_resources[phase] = (resource, decision)

    for verification_attempt in verification_attempts:
        phase = str(verification_attempt["phase"])
        if (
            verification_attempt.get("result") != "success"
            or type(verification_attempt.get("return_code")) is not int
            or verification_attempt.get("return_code") != 0
        ):
            raise ValueError(f"plugin_attempt_{phase}_not_successful")
        report = _plugin_stage_file(
            root,
            verification_attempt.get("report_path"),
            allowed_root=root / "qa",
            error_prefix=f"plugin_attempt_{phase}_report",
        )
        _require_plugin_stage_hash(
            verification_attempt,
            "report_sha256",
            report,
            f"plugin_attempt_{phase}_report",
        )
        report_traits = read_plugin_report_traits(attempt_reports["adapter_verify"])
        resource = plugin_resource_descriptor(
            context,
            Path(relative_plugin),
            report_traits,
        )
        operation = PLUGIN_ATTEMPT_OPERATIONS[phase]
        decision = resolve_resource_capability(context, resource, operation)
        _validate_plugin_capability_row(
            verification_attempt,
            relative_plugin=relative_plugin,
            resource=resource,
            decision=decision,
            expected_operation=operation,
        )
        attempt_reports[phase] = report

    for resolver in resolvers:
        phase = str(resolver["phase"])
        report_phase = PLUGIN_RESOLVER_REPORT_PHASE[phase]
        report = attempt_reports[report_phase]
        if resolver.get("report_path") != _plugin_stage_relative(root, report):
            raise ValueError(f"plugin_resolver_{phase}_report_mismatch")
        if resolver.get("result") != "allowed" or resolver.get("return_code") is not None:
            raise ValueError(f"plugin_resolver_{phase}_decision_invalid")
        if report_phase == "export" and not requires_trait_contract:
            report_traits = PluginReportTraits()
        else:
            report_traits = read_plugin_report_traits(report)
        resource = plugin_resource_descriptor(
            context,
            Path(relative_plugin),
            report_traits,
        )
        operation = PLUGIN_RESOLVER_OPERATIONS[phase]
        decision = resolve_resource_capability(context, resource, operation)
        _validate_plugin_capability_row(
            resolver,
            relative_plugin=relative_plugin,
            resource=resource,
            decision=decision,
            expected_operation=operation,
        )
    return attempt_reports


def _canonical_receipt_claims(items: object) -> dict[str, str]:
    claims: dict[str, str] = {}
    for item in items:
        value = str(item.path).replace("\\", "/")
        parsed = PurePosixPath(value)
        if (
            not value
            or parsed.is_absolute()
            or bool(Path(value).drive)
            or ".." in parsed.parts
            or parsed.as_posix() != value
        ):
            raise ValueError("plugin_apply_receipt_claim_path_invalid")
        folded = value.casefold()
        if folded in claims:
            raise ValueError("plugin_apply_receipt_claim_duplicate")
        claims[folded] = item.sha256
    return claims


def _validate_plugin_apply_receipt(
    root: Path,
    row: dict[str, object],
    *,
    mod_name: str,
    plugin_adapter: str,
    input_path: Path,
    translation_jsonl: Path,
    tool_output: Path,
    apply_report: Path,
) -> None:
    receipt_path = _plugin_stage_file(
        root,
        row.get("ApplyReceipt"),
        allowed_root=root / "qa",
        error_prefix="plugin_apply_receipt",
    )
    _require_plugin_stage_hash(
        row,
        "ApplyReceiptSha256",
        receipt_path,
        "plugin_apply_receipt",
    )
    receipt = read_adapter_result(receipt_path)
    if (
        receipt.status != "success"
        or receipt.operation != "apply"
        or receipt.adapter_id != plugin_adapter
        or receipt.mod_name != mod_name
    ):
        raise ValueError("plugin_apply_receipt_identity_mismatch")

    expected_inputs = {
        _plugin_stage_relative(root, input_path).casefold(): sha256_file(input_path),
        _plugin_stage_relative(root, translation_jsonl).casefold(): sha256_file(translation_jsonl),
    }
    expected_artifacts = {
        _plugin_stage_relative(root, tool_output).casefold(): sha256_file(tool_output),
        _plugin_stage_relative(root, apply_report).casefold(): sha256_file(apply_report),
    }
    if _canonical_receipt_claims(receipt.inputs) != expected_inputs:
        raise ValueError("plugin_apply_receipt_inputs_mismatch")
    if _canonical_receipt_claims(receipt.artifacts) != expected_artifacts:
        raise ValueError("plugin_apply_receipt_artifacts_mismatch")
    evidence_files = [str(value).replace("\\", "/").casefold() for value in receipt.evidence_files]
    if evidence_files != [_plugin_stage_relative(root, apply_report).casefold()]:
        raise ValueError("plugin_apply_receipt_evidence_mismatch")


def plugin_stage_status(root: Path, mod_name: str) -> tuple[str, str, str]:
    path = root / "qa" / f"{mod_name}.plugin_translation_stage.json"
    context = current_game_context(root)
    workspace = root / "work" / "extracted_mods" / mod_name
    if workspace.is_dir():
        workspace = find_data_root(workspace, context=context)
    current_plugins = sorted(
        (
            item
            for item in workspace.rglob("*")
            if item.is_file() and item.suffix.casefold() in PLUGIN_EXTENSIONS
        ),
        key=lambda item: item.relative_to(workspace).as_posix().casefold(),
    ) if workspace.is_dir() else []
    if not path.is_file():
        return (
            relative_path(root, path),
            "missing" if current_plugins else "not_applicable",
            "missing" if current_plugins else "0",
        )
    payload = read_json(path)
    if not payload or payload.get("_invalid_json"):
        return relative_path(root, path), "invalid", "invalid_json"
    try:
        if payload.get("schema") != PLUGIN_STAGE_SCHEMA:
            raise ValueError("schema_mismatch")
        if payload.get("schema_version") != PLUGIN_STAGE_SCHEMA_VERSION:
            raise ValueError("schema_version_mismatch")
        if str(payload.get("ModName", "")) != mod_name:
            raise ValueError("mod_name_mismatch")
        if str(payload.get("game_id", "")) != context.game_id:
            raise ValueError("game_id_mismatch")
        plugin_adapter = str(payload.get("plugin_adapter", "")).strip()
        if not plugin_adapter:
            raise ValueError("plugin_adapter_missing")
        expected_workspace = relative_path(root, workspace)
        if str(payload.get("Workspace", "")).replace("\\", "/").casefold() != expected_workspace.replace("\\", "/").casefold():
            raise ValueError("workspace_mismatch")
        project_root_value = str(payload.get("ProjectRoot", "")).strip()
        if not project_root_value or Path(project_root_value).resolve(strict=False) != root.resolve(strict=True):
            raise ValueError("project_root_mismatch")

        plugin_rows = payload.get("Plugins")
        issues = payload.get("Issues")
        if not isinstance(plugin_rows, list) or not all(isinstance(row, dict) for row in plugin_rows):
            raise ValueError("plugins_schema_invalid")
        if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
            raise ValueError("issues_schema_invalid")
        blocking_value = payload.get("BlockingIssues")
        if type(blocking_value) is not int or blocking_value < 0:
            raise ValueError("blocking_issues_invalid")
        actual_errors = sum(
            1 for issue in issues if str(issue.get("Severity", "")).casefold() == "error"
        )
        if blocking_value != actual_errors:
            raise ValueError("blocking_issues_mismatch")

        reported_paths: set[str] = set()
        plugin_keys: set[str] = set()
        blocking_statuses: list[str] = []
        for row in plugin_rows:
            relative_value = str(row.get("RelativePath", "")).replace("\\", "/")
            relative_plugin = PurePosixPath(relative_value)
            if (
                not relative_value
                or relative_plugin.is_absolute()
                or ".." in relative_plugin.parts
                or relative_plugin.as_posix() != relative_value
                or relative_plugin.suffix.casefold() not in PLUGIN_EXTENSIONS
            ):
                raise ValueError("plugin_relative_path_invalid")
            folded_path = relative_value.casefold()
            if folded_path in reported_paths:
                raise ValueError("plugin_relative_path_duplicate")
            reported_paths.add(folded_path)

            plugin_key = str(row.get("PluginKey", "")).strip()
            if not plugin_key or plugin_key.casefold() in plugin_keys:
                raise ValueError("plugin_key_invalid_or_duplicate")
            plugin_keys.add(plugin_key.casefold())
            input_sha256 = str(row.get("InputSha256", "")).strip().casefold()
            if re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None:
                raise ValueError("plugin_input_hash_invalid")
            input_path = workspace.joinpath(*relative_plugin.parts)
            try:
                resolved_input = input_path.resolve(strict=True)
                resolved_input.relative_to(workspace.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise ValueError("plugin_input_missing_or_outside_workspace") from exc
            if not resolved_input.is_file() or sha256_file(resolved_input) != input_sha256:
                raise ValueError("plugin_input_hash_mismatch")

            status = str(row.get("Status", "")).strip()
            if not status:
                raise ValueError("plugin_status_missing")
            if status in PLUGIN_WRITE_SUCCESS_STATUSES:
                translation_jsonl = _plugin_stage_file(
                    root,
                    row.get("TranslationJsonl"),
                    allowed_root=root / "translated" / "plugin_exports" / mod_name,
                    error_prefix="plugin_translation_jsonl",
                )
                tool_output = _plugin_stage_file(
                    root,
                    row.get("ToolOutput"),
                    allowed_root=root / "out" / mod_name / "tool_outputs",
                    error_prefix="plugin_tool_output",
                )
                verification = _plugin_stage_file(
                    root,
                    row.get("Evidence"),
                    allowed_root=root / "qa",
                    error_prefix="plugin_verification_evidence",
                )
                output_export_jsonl = _plugin_stage_file(
                    root,
                    row.get("OutputExportJsonl"),
                    allowed_root=root / "source" / "plugin_exports" / mod_name,
                    error_prefix="plugin_output_export_jsonl",
                )
                _require_plugin_stage_hash(
                    row,
                    "TranslationJsonlSha256",
                    translation_jsonl,
                    "plugin_translation_jsonl",
                )
                _require_plugin_stage_hash(
                    row,
                    "ToolOutputSha256",
                    tool_output,
                    "plugin_tool_output",
                )
                _require_plugin_stage_hash(
                    row,
                    "EvidenceSha256",
                    verification,
                    "plugin_verification_evidence",
                )
                _require_plugin_stage_hash(
                    row,
                    "OutputExportJsonlSha256",
                    output_export_jsonl,
                    "plugin_output_export_jsonl",
                )
                attempt_reports = _validate_plugin_success_evidence(
                    root,
                    row,
                    status=status,
                    context=context,
                    input_path=resolved_input,
                    relative_plugin=relative_value,
                    tool_output=tool_output,
                    plugin_adapter=plugin_adapter,
                )
                if attempt_reports["post_verify"] != verification:
                    raise ValueError("plugin_verification_evidence_path_mismatch")
                validate_plugin_post_verify_report(
                    verification,
                    project_root=root,
                    expected_game=context.game_id,
                    expected_adapter=plugin_adapter,
                    expected_original=resolved_input,
                    expected_output=tool_output,
                    expected_translation_jsonl=translation_jsonl,
                    expected_output_export_jsonl=output_export_jsonl,
                    expected_writeback_report=attempt_reports["apply"],
                    expected_invariant_report=attempt_reports["adapter_verify"],
                )
                _validate_plugin_apply_receipt(
                    root,
                    row,
                    mod_name=mod_name,
                    plugin_adapter=plugin_adapter,
                    input_path=resolved_input,
                    translation_jsonl=translation_jsonl,
                    tool_output=tool_output,
                    apply_report=attempt_reports["apply"],
                )
            elif status == "no_candidates":
                attempt_reports = _validate_plugin_success_evidence(
                    root,
                    row,
                    status=status,
                    context=context,
                    input_path=resolved_input,
                    relative_plugin=relative_value,
                    tool_output=None,
                    plugin_adapter=plugin_adapter,
                )
                evidence_path = _plugin_stage_file(
                    root,
                    row.get("Evidence"),
                    allowed_root=root / "qa",
                    error_prefix="plugin_no_candidates_evidence",
                )
                if evidence_path != attempt_reports["export"]:
                    raise ValueError("plugin_no_candidates_evidence_path_mismatch")
                if any(
                    str(row.get(field, "")).strip()
                    for field in (
                        "TranslationJsonl",
                        "ToolOutput",
                        "ApplyReceipt",
                        "TranslationJsonlSha256",
                        "ToolOutputSha256",
                        "ApplyReceiptSha256",
                        "OutputExportJsonl",
                        "OutputExportJsonlSha256",
                    )
                ):
                    raise ValueError("plugin_no_candidates_write_artifact_mismatch")
            elif status not in PLUGIN_STAGE_SUCCESS_STATUSES:
                blocking_statuses.append(status)

        current_paths = {
            item.relative_to(workspace).as_posix().casefold() for item in current_plugins
        }
        if reported_paths != current_paths:
            raise ValueError("plugin_input_set_stale")
        if bool(blocking_statuses) != bool(blocking_value):
            raise ValueError("plugin_status_blocking_mismatch")
    except (OSError, ValueError) as exc:
        return relative_path(root, path), "invalid", str(exc)

    status = "blocked" if blocking_value else "passed"
    return (
        relative_path(root, path),
        status,
        ",".join(sorted(set(blocking_statuses), key=str.casefold)) if blocking_statuses else "0",
    )


def classify_output(row: OutputRow) -> tuple[str, str]:
    problems = output_root_issues(row, "error")
    if problems:
        first = problems[0]
        if first.code in {"workspace_missing", "final_mod_missing"} or (
            first.code == "plugin_stage_not_ready" and not row.FinalModExists
        ):
            state = "needs_translation"
        elif first.code in {"protected_review_items", "final_binary_export_failures", "model_review_not_passed"}:
            state = "needs_model_review"
        else:
            state = "blocked_by_qa"
        if first.code == "final_mod_missing":
            example_input = r"mod\<ModArchive>.zip"
            next_action = f"Run `{command_for_input(example_input, row.ModName)}` or build final_mod after preparing the workspace."
        elif first.code == "package_validation_not_clean":
            next_action = f"Rerun `python .\\scripts\\validate_chs_package.py --mod-name \"{row.ModName}\"` and inspect `{first.Evidence}`."
        elif first.code == "model_review_not_passed":
            next_action = f"Complete agent model review in `{first.Evidence}`."
        else:
            next_action = f"Inspect `{first.Evidence}`: {first.Message}"
        return state, next_action
    # The workflow run report is a historical orchestration log. A later Codex
    # model review plus a clean strict gate is the current release evidence, so
    # an older workflow failure must not keep a completed output blocked.
    package_note = f" Package: `{row.PackagedModPath}`." if row.PackagedModExists else ""
    return (
        "ready_for_manual_test",
        f"Project-local static QA is complete. Player inspects `{row.FinalModDir}` and `{row.PackagedModPath}`, then tests the CHS package as a local MO2/Vortex mod; real game validation is not performed by Codex.{package_note}",
    )


def collect_outputs(root: Path, mod_names: list[str]) -> list[OutputRow]:
    # Known outputs are inferred from both work/ and out/ so a partially prepared
    # Mod still appears in the report instead of disappearing from handoff.
    rows: list[OutputRow] = []
    for mod_name in mod_names:
        workspace = root / "work" / "extracted_mods" / mod_name
        final_mod = default_final_mod_dir(root, mod_name)
        provenance_path = final_mod / "meta" / "provenance.jsonl"
        used_capabilities_path = root / "qa" / f"{mod_name}.used_capabilities.json"
        used_capabilities_status = "missing"
        used_capabilities_blocking = "missing"
        if final_mod.is_dir() and provenance_path.is_file():
            try:
                write_used_capabilities(root, mod_name, final_mod, used_capabilities_path)
                used_capabilities_status = "passed"
                used_capabilities_blocking = "0"
            except UsedCapabilityError as exc:
                used_capabilities_status = "failed"
                used_capabilities_blocking = exc.error_code
            except OSError:
                used_capabilities_status = "failed"
                used_capabilities_blocking = "verification_failed"
        package_path = packaged_mod_path(root, mod_name)
        plugin_stage_path, plugin_stage_status_value, plugin_stage_blocking = plugin_stage_status(
            root, mod_name
        )
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
            UsedCapabilitiesPath=relative_path(root, used_capabilities_path),
            UsedCapabilitiesStatus=used_capabilities_status,
            UsedCapabilitiesBlockingIssues=used_capabilities_blocking,
            PluginStagePath=plugin_stage_path,
            PluginStageStatus=plugin_stage_status_value,
            PluginStageBlockingIssues=plugin_stage_blocking,
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
            CoverageBlocking=read_report_metric(coverage_path, "Blocking"),
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


def output_root_issues(row: OutputRow, severity: str) -> list[ReportIssue]:
    def issue(code: str, message: str, artifact: str) -> ReportIssue:
        return ReportIssue(
            severity,
            code,
            message,
            artifact,
            code=code,
            mod_name=row.ModName,
            affected_artifact=artifact,
        )

    if not row.WorkspaceExists:
        return [issue("workspace_missing", "Prepared Mod workspace is missing.", row.Workspace)]
    if row.PluginStageStatus in {"invalid", "missing", "blocked"}:
        return [
            issue(
                "plugin_stage_not_ready",
                f"Plugin translation stage is {row.PluginStageStatus}: {row.PluginStageBlockingIssues}.",
                row.PluginStagePath,
            )
        ]
    if not row.FinalModExists:
        return [issue("final_mod_missing", "final_mod has not been assembled.", row.FinalModDir)]

    issues: list[ReportIssue] = []
    if row.ProvenanceStatus != "present":
        issues.append(issue("provenance_missing", "final_mod provenance is missing.", row.ProvenancePath))
    if row.UsedCapabilitiesStatus != "passed" or not zero(row.UsedCapabilitiesBlockingIssues):
        issues.append(
            issue(
                "used_capabilities_not_ready",
                f"Used capability evidence is {row.UsedCapabilitiesStatus}: {row.UsedCapabilitiesBlockingIssues}.",
                row.UsedCapabilitiesPath,
            )
        )
    if not row.PackagedModExists:
        issues.append(issue("chs_package_missing", "Packaged CHS Mod is missing.", row.PackagedModPath))
    if row.TranslationDictionaryStatus != "present" or not positive_int(row.TranslationDictionaryEntries):
        issues.append(
            issue(
                "translation_dictionary_not_ready",
                f"Translation dictionary is {row.TranslationDictionaryStatus} with {row.TranslationDictionaryEntries or '0'} entries.",
                row.TranslationDictionaryPath,
            )
        )
    if row.PackageValidationStatus != "passed" or not zero(row.PackageValidationBlockingIssues):
        issues.append(
            issue(
                "package_validation_not_clean",
                f"Package validation is {row.PackageValidationStatus}: {row.PackageValidationBlockingIssues} blocking issue(s).",
                row.PackageValidationReport,
            )
        )
    if row.DeliveryMode and row.DeliveryMode != "direct-replacement-final-mod":
        issues.append(issue("delivery_mode_invalid", f"Unexpected delivery mode: {row.DeliveryMode}.", f"{row.FinalModDir}/meta/manifest.json"))
    if not zero(row.StrictGateBlockingIssues) or not zero(row.StrictGateWarnings):
        issues.append(
            issue(
                "strict_gate_not_clean",
                f"Strict gate has {row.StrictGateBlockingIssues or '?'} blocking issue(s) and {row.StrictGateWarnings or '?'} warning(s).",
                f"qa/{row.ModName}.non_gui_qa_gates.md",
            )
        )
    if not zero(row.CoverageMissing) or not zero(row.CoverageBlocking):
        issues.append(
            issue(
                "coverage_not_clean",
                f"Translation coverage has {row.CoverageMissing or '?'} missing and {row.CoverageBlocking or '?'} blocking item(s).",
                f"out/{row.ModName}/qa/non_gui_translation_coverage.md",
            )
        )
    if not zero(row.FinalTextProtectedItems):
        issues.append(
            issue(
                "protected_review_items",
                "Final text review packet contains changed protected text.",
                f"qa/{row.ModName}.final_text_review_packet.md",
            )
        )
    if not zero(row.FinalBinaryProtectedItems):
        issues.append(
            issue(
                "protected_review_items",
                "Final binary review packet contains changed protected text.",
                f"qa/{row.ModName}.final_binary_review_packet.md",
            )
        )
    if not zero(row.FinalBinaryExportFailures):
        issues.append(
            issue(
                "final_binary_export_failures",
                f"Final binary review has {row.FinalBinaryExportFailures} export failure(s).",
                f"qa/{row.ModName}.final_binary_review_packet.md",
            )
        )
    if row.FinalReviewQualityStatus != "passed" or not zero(row.FinalReviewQualityBlockingIssues) or not zero(row.FinalReviewQualityWarnings):
        issues.append(
            issue(
                "final_review_quality_not_passed",
                f"Final review quality is {row.FinalReviewQualityStatus}: B={row.FinalReviewQualityBlockingIssues}, W={row.FinalReviewQualityWarnings}.",
                f"qa/{row.ModName}.final_review_quality.md",
            )
        )
    if row.ModelReviewStatus != "passed":
        issues.append(
            issue(
                "model_review_not_passed",
                f"Agent model review is {row.ModelReviewStatus or 'missing'}.",
                f"qa/{row.ModName}.model_review.md",
            )
        )
    return issues


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
            issues.append(
                ReportIssue(
                    "error",
                    "unprocessed_input",
                    f"Input has no known workflow output: {row.Path}",
                    row.Path,
                    mod_name=row.LikelyModName,
                    affected_artifact=row.Path,
                    evidence_paths=[row.Path, row.RecommendedCommand],
                )
            )
    for row in output_rows:
        severity = "error" if row.ModName in input_mod_names else "warning"
        if row.OverallStatus != "ready_for_manual_test":
            issues.extend(output_root_issues(row, severity))
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


def write_reports(root: Path, report_path: Path, json_path: Path, input_rows: list[ModInputRow], output_rows: list[OutputRow], issues: list[ReportIssue], context: GameContext) -> None:
    issues = [enrich_report_issue(issue) for issue in issues]
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
        f"- game_id: {context.game_id}",
        f"- Game: {game_display_label(context)}",
        f"- Support level: {context.support_level}",
        f"- ProjectRoot: {root}",
        f"- Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Overall status: {project_status}",
        f"- Blocking issues: {blocking}",
        f"- Warnings: {warnings}",
        f"- Ready outputs: {len(ready_rows)}",
        f"- Next recommended action: {next_action}",
        "- Runtime validation boundary: project-local static QA can be ready, but real game/MO2/Vortex validation remains player-operated follow-up.",
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
            coverage = (
                f"Missing:{row.CoverageMissing or '?'} Blocking:{row.CoverageBlocking or '?'} "
                f"Unverified:{row.CoverageUnverified or '?'}"
            )
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
        lines.extend(
            [
                "| Issue ID | Severity | Code | Mod | Message | Evidence |",
                "|---|---|---|---|---|---|",
            ]
        )
        for issue in issues:
            lines.append(
                f"| {issue.issue_id} | {issue.Severity} | {issue.code} | {markdown_cell(issue.mod_name or 'project')} | "
                f"{markdown_cell(issue.Message)} | {markdown_cell('; '.join(issue.evidence_paths))} |"
            )

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This audit is read-only for Mod inputs and final_mod content.",
            "- This audit writes only `qa/translation_readiness.md` and `qa/translation_readiness.json`.",
            "- This audit does not translate text and does not modify ESP/ESM/ESL/PEX/BSA/BA2 files.",
            "- Real game installations, Steam, MO2/Vortex, AppData, and Documents/My Games paths are not accessed.",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        **game_context_metadata(context),
        "ProjectRoot": str(root),
        "CheckedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "OverallStatus": project_status,
        "BlockingIssues": blocking,
        "Warnings": warnings,
        "NextRecommendedAction": next_action,
        "RuntimeValidationBoundary": "Project-local static QA ready does not mean real game/MO2/Vortex validation was completed by Codex.",
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
    parser = argparse.ArgumentParser(description="Write a profile-aware Bethesda Mod translation readiness report.")
    parser.add_argument("--mod-name", default="")
    parser.add_argument("--report-output-path", default="qa/translation_readiness.md")
    parser.add_argument("--json-output-path", default="qa/translation_readiness.json")
    args = parser.parse_args()

    root = project_root()
    context = current_game_context(root)
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

    input_rows = collect_mod_inputs(root, known_mod_names, context)
    output_names = sorted(set(name for name in known_mod_names if name), key=str.lower)
    output_rows = collect_outputs(root, output_names)
    input_command_by_mod = {row.LikelyModName: row.RecommendedCommand for row in input_rows}
    for row in output_rows:
        command = input_command_by_mod.get(row.ModName, "")
        if command and row.OverallStatus != "ready_for_manual_test":
            row.NextRecommendedAction = command
    issues = collect_issues(root, input_rows, output_rows)
    issues.extend(collect_game_identity_issues(root, context))
    write_reports(root, report_path, json_path, input_rows, output_rows, issues, context)
    print(f"Translation readiness report written to: {report_path}")
    print(f"Translation readiness JSON written to: {json_path}")
    print(f"Known mod outputs: {len(output_rows)}")
    print(f"Blocking issues: {sum(1 for issue in issues if issue.Severity == 'error')}")
    print(f"Warnings: {sum(1 for issue in issues if issue.Severity == 'warning')}")
    return 1 if any(issue.Severity == "error" and issue.Area == "game_identity_mismatch" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
