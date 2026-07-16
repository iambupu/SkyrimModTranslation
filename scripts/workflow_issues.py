"""Stable issue identity and aggregation for workflow JSON reports."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any


REPORTER_ORDER = {
    "translation_readiness": 0,
    "workflow_state": 1,
    "workflow_health": 2,
}


def _code(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")
    return normalized or "unknown_issue"


def _mod_name(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "_", str(value).strip().casefold()).strip("_")
    return normalized or "project"


def canonical_artifact(value: object) -> str:
    normalized = str(value).strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    return normalized.casefold() or "project"


def stable_issue_id(code: object, mod_name: object, affected_artifact: object) -> str:
    normalized_code = _code(code)
    normalized_mod = _mod_name(mod_name)
    artifact = canonical_artifact(affected_artifact)
    digest = hashlib.sha256(f"{normalized_code}\0{normalized_mod}\0{artifact}".encode("utf-8")).hexdigest()[:16]
    return f"{normalized_code}:{normalized_mod}:{digest}"


def _unique_strings(values: Iterable[object], *, path_values: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if path_values:
            text = text.replace("\\", "/")
            key = canonical_artifact(text)
        else:
            key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def make_issue_record(
    *,
    code: object,
    mod_name: object,
    affected_artifact: object,
    severity: object,
    message: object,
    evidence_paths: Iterable[object],
    reported_by: Iterable[object],
    impact_scope: object = "",
) -> dict[str, Any]:
    normalized_code = _code(code)
    normalized_mod = str(mod_name).strip()
    artifact = str(affected_artifact).strip().replace("\\", "/") or "project"
    return {
        "issue_id": stable_issue_id(normalized_code, normalized_mod, artifact),
        "code": normalized_code,
        "mod_name": normalized_mod,
        "affected_artifact": artifact,
        "severity": str(severity).strip().casefold() or "error",
        "message": str(message).strip(),
        "impact_scope": str(impact_scope).strip() or (f"mod:{normalized_mod}" if normalized_mod else "project"),
        "evidence_paths": _unique_strings(evidence_paths, path_values=True),
        "reported_by": sorted(
            _unique_strings(reported_by),
            key=lambda value: (REPORTER_ORDER.get(value, 99), value.casefold()),
        ),
    }


def issue_record_from_mapping(
    raw: dict[str, Any],
    *,
    default_reporter: str,
    default_mod_name: str = "",
) -> dict[str, Any]:
    evidence_value = raw.get("evidence_paths")
    if not isinstance(evidence_value, list):
        evidence_value = [raw.get("Evidence", raw.get("evidence", ""))]
    reporters = raw.get("reported_by")
    if not isinstance(reporters, list):
        reporters = [default_reporter]
    record = make_issue_record(
        code=raw.get("code", raw.get("Area", raw.get("area", "unknown_issue"))),
        mod_name=raw.get("mod_name", default_mod_name),
        affected_artifact=raw.get(
            "affected_artifact",
            raw.get("Evidence", raw.get("evidence", "project")),
        ),
        severity=raw.get("severity", raw.get("Severity", "error")),
        message=raw.get("message", raw.get("Message", "")),
        evidence_paths=evidence_value,
        reported_by=reporters,
        impact_scope=raw.get("impact_scope", ""),
    )
    supplied_id = str(raw.get("issue_id", "")).strip()
    if supplied_id:
        record["issue_id"] = supplied_id
    return record


def compact_issue_refs(records: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for record in records:
        issue_id = str(record.get("issue_id", "")).strip()
        code = _code(record.get("code", ""))
        if issue_id:
            refs.setdefault(issue_id, {"issue_id": issue_id, "code": code})
    return sorted(refs.values(), key=lambda item: (item["code"], item["issue_id"]))


def aggregate_issue_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for raw in records:
        issue_id = str(raw.get("issue_id", "")).strip()
        if not issue_id:
            raw = make_issue_record(
                code=raw.get("code", "unknown_issue"),
                mod_name=raw.get("mod_name", ""),
                affected_artifact=raw.get("affected_artifact", "project"),
                severity=raw.get("severity", "error"),
                message=raw.get("message", ""),
                evidence_paths=raw.get("evidence_paths", []),
                reported_by=raw.get("reported_by", []),
                impact_scope=raw.get("impact_scope", ""),
            )
            issue_id = str(raw["issue_id"])
        current = grouped.get(issue_id)
        if current is None:
            grouped[issue_id] = dict(raw)
            grouped[issue_id]["evidence_paths"] = _unique_strings(raw.get("evidence_paths", []), path_values=True)
            grouped[issue_id]["reported_by"] = _unique_strings(raw.get("reported_by", []))
            continue
        if str(raw.get("severity", "")).casefold() == "error":
            current["severity"] = "error"
        current["evidence_paths"] = _unique_strings(
            [*current.get("evidence_paths", []), *raw.get("evidence_paths", [])],
            path_values=True,
        )
        current["reported_by"] = _unique_strings(
            [*current.get("reported_by", []), *raw.get("reported_by", [])]
        )
    for record in grouped.values():
        record["reported_by"] = sorted(
            record.get("reported_by", []),
            key=lambda value: (REPORTER_ORDER.get(value, 99), value.casefold()),
        )
    return sorted(grouped.values(), key=lambda item: (str(item.get("code", "")), str(item.get("issue_id", ""))))
