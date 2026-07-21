"""Single source of truth for machine-checkable agent model review claims."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from file_utils import discover_regular_files, read_json_object_or_invalid

MODEL_REVIEW_CLAIMS = (
    ("runtime safety", "No runtime-impacting issues remain"),
    ("translation completeness", "No required translation candidates remain untranslated"),
    ("semantic quality", "No semantic quality blockers remain"),
    ("changed file coverage", "All changed final_mod files listed in the review packets were reviewed"),
    ("model semantic review", "Mechanical checks do not replace agent model semantic review"),
    ("final review quality", "Final review quality audit has 0 blocking issues and 0 warnings"),
)
REQUIRED_MODEL_CLAIMS = tuple(claim for _, claim in MODEL_REVIEW_CLAIMS)
LEGACY_MODEL_CLAIMS = {
    "Mechanical checks do not replace agent model semantic review": (
        "Mechanical checks do not replace Codex model semantic review"
    ),
}
MODEL_REVIEWER_RE = re.compile(r"Reviewer:\s*(?:Agent|Codex) model", re.IGNORECASE)


def has_model_claim(text: str, claim: str, *, ignore_case: bool = False) -> bool:
    accepted = (claim, LEGACY_MODEL_CLAIMS.get(claim, ""))
    if ignore_case:
        normalized = text.casefold()
        return any(value and value.casefold() in normalized for value in accepted)
    return any(value and value in text for value in accepted)


def missing_model_claim_labels(text: str, *, ignore_case: bool = False) -> list[str]:
    return [
        label
        for label, claim in MODEL_REVIEW_CLAIMS
        if not has_model_claim(text, claim, ignore_case=ignore_case)
    ]


def model_claim_lines(*, code: bool = False) -> list[str]:
    if code:
        return [f"- `{claim}`" for claim in REQUIRED_MODEL_CLAIMS]
    return [f"- {claim}" for claim in REQUIRED_MODEL_CLAIMS]


def read_report_metric(path: Path, name: str, *, default: str = "") -> str:
    if not path.is_file():
        return default
    pattern = re.compile(rf"^- {re.escape(name)}:\s*(.+)$")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return default


def packet_content_reviewed(model_text: str, packet_path: Path) -> bool:
    if not packet_path.is_file() or packet_path.name not in model_text:
        return False
    packet_hash = read_report_metric(packet_path, "Items SHA256")
    return bool(packet_hash and packet_hash in model_text)


def model_text_mentions_path(model_text: str, path_value: str) -> bool:
    normalized_text = model_text.replace("/", "\\").casefold()
    normalized_path = path_value.replace("/", "\\").casefold()
    if normalized_path in normalized_text:
        return True
    basename = Path(path_value.replace("/", "\\")).name.casefold()
    return bool(basename and basename in normalized_text)


def model_review_contract_issues(
    model_text: str,
    reviewed_files: set[str],
    *,
    display_labels: bool = False,
    ignore_case: bool = False,
    missing_claim_suffix: str = "",
) -> list[str]:
    if display_labels:
        missing = missing_model_claim_labels(model_text, ignore_case=ignore_case)
    else:
        missing = [
            claim
            for claim in REQUIRED_MODEL_CLAIMS
            if not has_model_claim(model_text, claim, ignore_case=ignore_case)
        ]
    issues = [f"Missing required model-review claim: {value}{missing_claim_suffix}" for value in missing]
    missing_files = sorted(file for file in reviewed_files if not model_text_mentions_path(model_text, file))
    if missing_files:
        preview = "; ".join(missing_files[:10])
        suffix = "" if len(missing_files) <= 10 else f"; ... {len(missing_files) - 10} more"
        issues.append(f"Model review does not explicitly mention all changed final_mod files: {preview}{suffix}")
    return issues


def read_jsonl_objects(path: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    text = path.read_text(
        encoding="utf-8-sig",
        errors="strict" if strict else "replace",
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if strict:
                raise
            continue
        if isinstance(payload, dict):
            rows.append(payload)
        elif strict:
            raise ValueError(f"line {line_number} is not a JSON object")
    return rows


def jsonl_file_values(path: Path, property_name: str) -> set[str]:
    values: set[str] = set()
    for row in read_jsonl_objects(path, strict=True):
        value = row.get(property_name)
        if value is not None and str(value).strip():
            values.add(str(value).strip())
    return values


def changed_files_from_packets(root: Path, mod_name: str) -> set[str]:
    files: set[str] = set()
    for suffix in ("final_text_review_items.jsonl", "final_binary_review_items.jsonl"):
        for row in read_jsonl_objects(root / "qa" / f"{mod_name}.{suffix}", strict=True):
            value = row.get("File")
            if isinstance(value, str) and value.strip():
                files.add(value.strip())
    return files


def latest_file_mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_mtime
    latest = path.stat().st_mtime
    for item in discover_regular_files(path, label="Model review evidence directory"):
        latest = max(latest, item.stat().st_mtime)
    return latest


def report_covers_artifacts(report: Path, artifacts: list[Path]) -> bool:
    if not report.is_file() or any(not path.exists() for path in artifacts):
        return False
    report_mtime = report.stat().st_mtime
    return all(latest_file_mtime(path) <= report_mtime + 1e-6 for path in artifacts)


def final_review_quality_evidence(root: Path, mod_name: str) -> tuple[str, str, str, bool]:
    report_path = root / "qa" / f"{mod_name}.final_review_quality.md"
    json_path = root / "qa" / f"{mod_name}.final_review_quality.json"
    if not report_path.is_file() or not json_path.is_file():
        return "missing", "missing", "missing", False
    payload = read_json_object_or_invalid(json_path)
    if payload.get("_invalid_json"):
        return "invalid_json", "invalid_json", "invalid_json", False
    dependencies = [
        root / "qa" / f"{mod_name}.final_text_review_items.jsonl",
        root / "qa" / f"{mod_name}.final_binary_review_items.jsonl",
    ]
    return (
        str(payload.get("Status", "")).strip(),
        str(payload.get("BlockingIssues", "")).strip(),
        str(payload.get("Warnings", "")).strip(),
        report_covers_artifacts(report_path, dependencies),
    )


def strict_gate_current_and_clean(root: Path, mod_name: str) -> bool:
    gate_report = root / "qa" / f"{mod_name}.non_gui_qa_gates.md"
    dependencies = [
        root / "qa" / f"{mod_name}.model_review.md",
        root / "qa" / f"{mod_name}.final_text_review_packet.md",
        root / "qa" / f"{mod_name}.final_text_review_items.jsonl",
        root / "qa" / f"{mod_name}.final_binary_review_packet.md",
        root / "qa" / f"{mod_name}.final_binary_review_items.jsonl",
        root / "qa" / f"{mod_name}.final_review_quality.md",
        root / "qa" / f"{mod_name}.final_review_quality.json",
    ]
    return (
        read_report_metric(gate_report, "Blocking issues") == "0"
        and read_report_metric(gate_report, "Warnings") == "0"
        and read_report_metric(gate_report, "Strict complete mode") == "True"
        and report_covers_artifacts(gate_report, dependencies)
    )
