"""Shared limits, timeout, disk checks, and evidence for archive materialization."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from audit_mod_scale import default_scale_config_path, load_scale_config
from project_paths import relative_path
from report_utils import utc_now


@dataclass(frozen=True)
class ArchiveExecutionPolicy:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    timeout_seconds: int
    extract_mode: str
    source: str
    overrides: dict[str, object]


def validate_archive_inventory(
    rows: Iterable[Mapping[str, object]],
    policy: ArchiveExecutionPolicy,
) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    seen: set[str] = set()
    for row in rows:
        path = str(row.get("path") or "").replace("\\", "/").strip()
        if not path:
            raise ValueError("Archive inventory contains an empty path")
        key = path.casefold()
        if key in seen:
            raise ValueError(f"Archive inventory contains a duplicate path: {path}")
        seen.add(key)
        size = row.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"Archive inventory contains an invalid size: {path}")
        if size > policy.max_file_bytes:
            raise ValueError(
                "Archive per-file size exceeds "
                f"max_file_bytes={policy.max_file_bytes}: {path}"
            )
        file_count += 1
        total_bytes += size
        if file_count > policy.max_files:
            raise ValueError(f"Archive file count exceeds max_files={policy.max_files}")
        if total_bytes > policy.max_total_bytes:
            raise ValueError(
                f"Archive total bytes exceed max_total_bytes={policy.max_total_bytes}"
            )
    return file_count, total_bytes


def validate_materialized_inventory(
    expected_rows: Iterable[Mapping[str, object]],
    actual_rows: Iterable[Mapping[str, object]],
) -> None:
    def indexed(rows: Iterable[Mapping[str, object]], label: str) -> dict[str, tuple[str, int]]:
        result: dict[str, tuple[str, int]] = {}
        for row in rows:
            path = str(row.get("path") or "").replace("\\", "/").strip()
            size = row.get("size")
            if not path or isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"{label} contains an invalid path or size")
            key = path.casefold()
            if key in result:
                raise ValueError(f"{label} contains a duplicate Windows path: {path}")
            result[key] = (path, size)
        return result

    expected = indexed(expected_rows, "Archive inventory")
    actual = indexed(actual_rows, "Materialized output")
    missing = sorted(expected[key][0] for key in expected.keys() - actual.keys())
    unexpected = sorted(actual[key][0] for key in actual.keys() - expected.keys())
    mismatched = sorted(
        expected[key][0]
        for key in expected.keys() & actual.keys()
        if expected[key][1] != actual[key][1]
    )
    issues: list[str] = []
    if missing:
        issues.append("missing entries: " + ", ".join(missing[:8]))
    if unexpected:
        issues.append("unexpected entries: " + ", ".join(unexpected[:8]))
    if mismatched:
        issues.append("size mismatches: " + ", ".join(mismatched[:8]))
    if issues:
        raise ValueError("Materialized archive output does not match inventory: " + "; ".join(issues))


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def resolve_archive_execution_policy(
    *,
    root: Path,
    mod_name: str,
    requested: Mapping[str, object | None],
    default_max_files: int,
    default_max_file_bytes: int,
    default_max_total_bytes: int,
    default_timeout_seconds: int = 1800,
    expected_game_id: str = "",
) -> ArchiveExecutionPolicy:
    config = load_scale_config(default_scale_config_path())
    absolute = config["absolute_limits"]
    effective: dict[str, object] = {
        "max_files": default_max_files,
        "max_file_bytes": default_max_file_bytes,
        "max_total_bytes": default_max_total_bytes,
        "timeout_seconds": default_timeout_seconds,
        "extract_mode": "full",
    }
    source = "archive-wrapper-defaults"
    scale_report = root / "qa" / f"{mod_name}.scale_execution.json"
    if scale_report.is_file():
        try:
            payload = json.loads(scale_report.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"Invalid scale execution report: {scale_report}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ready" or payload.get("mod_name") != mod_name:
            raise ValueError(f"Scale execution report is not ready for {mod_name}: {scale_report}")
        if expected_game_id and payload.get("game_id") != expected_game_id:
            raise ValueError(f"Scale execution report game_id does not match the current workspace: {scale_report}")
        recorded = payload.get("effective")
        if not isinstance(recorded, dict):
            raise ValueError("Scale execution report is missing effective parameters")
        effective.update({key: recorded[key] for key in effective if key in recorded})
        source = relative_path(root, scale_report).replace("\\", "/")

    overrides = {key: value for key, value in requested.items() if value is not None and value != ""}
    effective.update(overrides)
    limits: dict[str, int] = {}
    for key in ("max_files", "max_file_bytes", "max_total_bytes", "timeout_seconds"):
        value = _positive(effective[key], key)
        cap = _positive(absolute[key], f"absolute_limits.{key}")
        if value > cap:
            raise ValueError(f"{key}={value} exceeds absolute safety cap {cap}")
        limits[key] = value
    raw_mode = str(effective.get("extract_mode") or "full")
    extract_mode = "selective" if raw_mode in {"filtered", "selective", "selective-sharded"} else raw_mode
    if extract_mode not in {"full", "selective"}:
        raise ValueError(f"Archive materialization does not support extract_mode={raw_mode}")
    return ArchiveExecutionPolicy(
        max_files=limits["max_files"],
        max_file_bytes=limits["max_file_bytes"],
        max_total_bytes=limits["max_total_bytes"],
        timeout_seconds=limits["timeout_seconds"],
        extract_mode=extract_mode,
        source=source,
        overrides=dict(overrides),
    )


def disk_preflight(
    *,
    root: Path,
    archive_path: Path,
    output_dir: Path,
    selected_bytes: int,
) -> dict[str, int | bool]:
    config = load_scale_config(default_scale_config_path())
    disk = config["disk_policy"]
    refresh = output_dir.is_dir() and any(output_dir.iterdir())
    multiplier = float(
        disk["refresh_materialization_multiplier"]
        if refresh
        else disk["new_materialization_multiplier"]
    )
    required = int(max(0, selected_bytes) * multiplier) + archive_path.stat().st_size + int(disk["safety_margin_bytes"])
    anchor = output_dir.parent
    while not anchor.exists() and anchor.parent != anchor:
        anchor = anchor.parent
    available = int(shutil.disk_usage(anchor).free)
    if available < required:
        raise ValueError(
            "Insufficient disk space for archive materialization: "
            f"required={required} bytes, available={available} bytes. "
            "Use selective extraction, another workspace drive, or split by archive."
        )
    return {
        "refresh_existing_output": refresh,
        "selected_bytes": max(0, selected_bytes),
        "required_free_bytes": required,
        "available_free_bytes": available,
        "passed": True,
    }


def write_archive_execution_evidence(
    *,
    root: Path,
    mod_name: str,
    archive_path: Path,
    policy: ArchiveExecutionPolicy,
    disk: Mapping[str, int | bool],
    selected_files: int | None,
    status: str,
    error: str = "",
) -> Path:
    archive_name = archive_path.stem
    path = root / "qa" / f"{mod_name}.{archive_name}.archive_execution.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "archive-scale-execution",
                "generated_at": utc_now(),
                "mod_name": mod_name,
                "archive_path": relative_path(root, archive_path).replace("\\", "/"),
                "policy_source": policy.source,
                "overrides": policy.overrides,
                "effective": {
                    "max_files": policy.max_files,
                    "max_file_bytes": policy.max_file_bytes,
                    "max_total_bytes": policy.max_total_bytes,
                    "timeout_seconds": policy.timeout_seconds,
                    "extract_mode": policy.extract_mode,
                },
                "disk_preflight": dict(disk),
                "selected_files": selected_files,
                "status": status,
                "error": error,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
