"""Resolve enforced Mod scale limits and record the effective execution policy."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from audit_mod_scale import load_scale_config
from file_utils import sha256_file
from project_paths import is_under, plugin_root, relative_path
from report_utils import utc_now


LIMIT_KEYS = (
    "max_files",
    "max_file_bytes",
    "max_total_bytes",
    "timeout_seconds",
    "max_parallel_tasks",
    "max_parallel_binary_tasks",
    "max_parallel_archive_tasks",
)
EXTRACT_MODES = {"full", "filtered", "selective", "selective-sharded", "multi-project"}
PACKAGE_MODES = {"complete", "translation-overlay", "aggregate-only"}


@dataclass(frozen=True)
class ScaleExecutionPolicy:
    scale_level: str
    risk_level: str
    extract_mode: str
    package_mode: str
    limits: dict[str, int]
    overrides: dict[str, object]
    checkpoint_every_files: int
    translation_batch_rows: int
    estimated_materialized_bytes: int
    required_free_bytes: int
    available_free_bytes: int
    refresh_existing_output: bool
    config_path: Path
    assessment_path: Path

    @property
    def selective(self) -> bool:
        return self.extract_mode in {"filtered", "selective", "selective-sharded"}


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _existing_disk_anchor(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    if not candidate.exists():
        raise ValueError(f"Cannot determine disk usage for output path: {path}")
    return candidate


def _evidence_path(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    if is_under(resolved, root):
        return relative_path(root, resolved).replace("\\", "/")
    source_root = plugin_root().resolve(strict=True)
    if is_under(resolved, source_root):
        return "plugin:" + relative_path(source_root, resolved).replace("\\", "/")
    return f"external:{resolved.name}"


def read_scale_assessment(path: Path, mod_name: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Scale assessment must use schema_version=1")
    if payload.get("report_type") != "mod-scale-assessment":
        raise ValueError("Scale assessment report_type is invalid")
    if str(payload.get("mod_name") or "") != mod_name:
        raise ValueError("Scale assessment mod_name does not match the requested Mod")
    return payload


def resolve_scale_execution_policy(
    *,
    root: Path,
    mod_name: str,
    assessment_path: Path,
    config_path: Path,
    output_path: Path,
    overrides: Mapping[str, object] | None = None,
    expected_game_id: str = "",
) -> tuple[ScaleExecutionPolicy, dict[str, Any]]:
    assessment = read_scale_assessment(assessment_path, mod_name)
    if expected_game_id and assessment.get("game_id") != expected_game_id:
        raise ValueError("Scale assessment game_id does not match the current workspace")
    config = load_scale_config(config_path)
    scale_level = str(assessment.get("scale_level") or "")
    profile = config["profiles"].get(scale_level)
    if not isinstance(profile, dict):
        raise ValueError(f"Unknown scale level in assessment: {scale_level}")
    recommendations = profile.get("recommendations")
    if not isinstance(recommendations, dict):
        raise ValueError(f"Scale profile {scale_level} has no recommendations")

    requested = {
        key: value
        for key, value in dict(overrides or {}).items()
        if value is not None and value != ""
    }
    absolute_limits = config.get("absolute_limits")
    if not isinstance(absolute_limits, dict):
        raise ValueError("mod_scale_profiles.json is missing absolute_limits")

    limits: dict[str, int] = {}
    recorded_overrides: dict[str, object] = {}
    for key in LIMIT_KEYS:
        cap = _positive_int(absolute_limits.get(key), f"absolute_limits.{key}")
        raw = requested.get(key, recommendations.get(key, cap))
        value = _positive_int(raw, key)
        if value > cap:
            raise ValueError(f"{key}={value} exceeds absolute safety cap {cap}")
        limits[key] = value
        if key in requested:
            recorded_overrides[key] = value

    extract_mode = str(requested.get("extract_mode", recommendations.get("extract_mode", "")))
    package_mode = str(requested.get("package_mode", recommendations.get("package_mode", "")))
    if extract_mode not in EXTRACT_MODES:
        raise ValueError(f"Unsupported extract_mode: {extract_mode}")
    if package_mode not in PACKAGE_MODES:
        raise ValueError(f"Unsupported package_mode: {package_mode}")
    if "extract_mode" in requested:
        recorded_overrides["extract_mode"] = extract_mode
    if "package_mode" in requested:
        recorded_overrides["package_mode"] = package_mode
    if scale_level == "L5" or extract_mode == "multi-project" or package_mode == "aggregate-only":
        raise ValueError(
            "L5 input must be split into independent translation workspaces and completed through the aggregate project; "
            "single-workspace materialization is blocked."
        )

    checkpoint_every_files = _positive_int(
        requested.get("checkpoint_every_files", recommendations.get("checkpoint_every_files", 1000)),
        "checkpoint_every_files",
    )
    translation_batch_rows = _positive_int(
        requested.get("translation_batch_rows", recommendations.get("translation_batch_rows", 5000)),
        "translation_batch_rows",
    )
    checkpoint_cap = _positive_int(
        absolute_limits.get("max_checkpoint_every_files"),
        "absolute_limits.max_checkpoint_every_files",
    )
    translation_batch_cap = _positive_int(
        absolute_limits.get("max_translation_batch_rows"),
        "absolute_limits.max_translation_batch_rows",
    )
    if checkpoint_every_files > checkpoint_cap:
        raise ValueError(
            "checkpoint_every_files="
            f"{checkpoint_every_files} exceeds absolute safety cap {checkpoint_cap}"
        )
    if translation_batch_rows > translation_batch_cap:
        raise ValueError(
            "translation_batch_rows="
            f"{translation_batch_rows} exceeds absolute safety cap {translation_batch_cap}"
        )
    if "checkpoint_every_files" in requested:
        recorded_overrides["checkpoint_every_files"] = checkpoint_every_files
    if "translation_batch_rows" in requested:
        recorded_overrides["translation_batch_rows"] = translation_batch_rows

    estimated_unpacked = max(0, int(assessment.get("estimated_unpacked_bytes", 0) or 0))
    protected_bytes = max(0, int(assessment.get("protected_bytes", 0) or 0))
    estimated_materialized = (
        max(0, estimated_unpacked - protected_bytes)
        if extract_mode in {"filtered", "selective", "selective-sharded"}
        else estimated_unpacked
    )
    measured_file_count = max(0, int(assessment.get("file_count", 0) or 0))
    largest_file = max(0, int(assessment.get("largest_file_bytes", 0) or 0))
    measured_file_count_for_limit = measured_file_count
    if extract_mode in {"filtered", "selective", "selective-sharded"}:
        # The assessment does not retain one row per protected file. Actual
        # materialization performs the exact count check again.
        measured_file_count_for_limit = min(measured_file_count, limits["max_files"])
    violations: list[str] = []
    if measured_file_count_for_limit > limits["max_files"]:
        violations.append(f"file count {measured_file_count_for_limit} exceeds max_files {limits['max_files']}")
    if largest_file > limits["max_file_bytes"] and not (
        extract_mode in {"filtered", "selective", "selective-sharded"}
        and protected_bytes >= largest_file
    ):
        violations.append(f"largest file {largest_file} exceeds max_file_bytes {limits['max_file_bytes']}")
    if estimated_materialized > limits["max_total_bytes"]:
        violations.append(
            f"estimated materialized bytes {estimated_materialized} exceeds max_total_bytes {limits['max_total_bytes']}"
        )
    if violations:
        raise ValueError("Scale execution limits rejected the input: " + "; ".join(violations))

    output_exists = output_path.is_dir() and any(output_path.iterdir())
    disk_policy = config.get("disk_policy")
    if not isinstance(disk_policy, dict):
        raise ValueError("mod_scale_profiles.json is missing disk_policy")
    multiplier_key = "refresh_materialization_multiplier" if output_exists else "new_materialization_multiplier"
    multiplier = float(disk_policy.get(multiplier_key, 0))
    if multiplier <= 0:
        raise ValueError(f"disk_policy.{multiplier_key} must be positive")
    safety_margin = _positive_int(disk_policy.get("safety_margin_bytes"), "disk_policy.safety_margin_bytes")
    compressed_bytes = max(0, int(assessment.get("compressed_bytes", 0) or 0))
    required_free = int(estimated_materialized * multiplier) + compressed_bytes + safety_margin
    available_free = int(shutil.disk_usage(_existing_disk_anchor(output_path)).free)
    if available_free < required_free:
        raise ValueError(
            "Insufficient disk space for materialization: "
            f"required={required_free} bytes, available={available_free} bytes. "
            "Use selective extraction, exclude protected resources, choose another workspace drive, or split by archive."
        )

    policy = ScaleExecutionPolicy(
        scale_level=scale_level,
        risk_level=str(assessment.get("risk_level") or ""),
        extract_mode=extract_mode,
        package_mode=package_mode,
        limits=limits,
        overrides=recorded_overrides,
        checkpoint_every_files=checkpoint_every_files,
        translation_batch_rows=translation_batch_rows,
        estimated_materialized_bytes=estimated_materialized,
        required_free_bytes=required_free,
        available_free_bytes=available_free,
        refresh_existing_output=output_exists,
        config_path=config_path,
        assessment_path=assessment_path,
    )
    report = {
        "schema_version": 1,
        "report_type": "mod-scale-execution",
        "generated_at": utc_now(),
        "mod_name": mod_name,
        "game_id": assessment.get("game_id"),
        "scale_level": scale_level,
        "risk_level": policy.risk_level,
        "assessment_path": _evidence_path(root, assessment_path),
        "assessment_sha256": sha256_file(assessment_path),
        "config_path": _evidence_path(root, config_path),
        "config_sha256": sha256_file(config_path),
        "profile_defaults": dict(recommendations),
        "overrides": dict(recorded_overrides),
        "effective": {
            **limits,
            "extract_mode": extract_mode,
            "package_mode": package_mode,
            "checkpoint_every_files": checkpoint_every_files,
            "translation_batch_rows": translation_batch_rows,
        },
        "absolute_limits": dict(absolute_limits),
        "disk_preflight": {
            "refresh_existing_output": output_exists,
            "estimated_materialized_bytes": estimated_materialized,
            "required_free_bytes": required_free,
            "available_free_bytes": available_free,
            "passed": True,
        },
        "status": "ready",
    }
    return policy, report


def write_scale_execution_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
