"""Deterministic JSON receipts for controlled adapter operations."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

from adapter_contract import AdapterArtifact, AdapterResult, validate_adapter_result
from file_utils import sha256_file
from project_paths import is_under, require_under_any, resolve_project_path


def prepare_adapter_result_path(root: Path, raw_path: str) -> Path | None:
    """Resolve and reset an optional AdapterResult path under qa/ or out/."""
    if not raw_path:
        return None
    result_path = resolve_project_path(root, raw_path, must_exist=False)
    require_under_any(result_path, [root / "qa", root / "out"], "AdapterResultPath")
    if result_path.suffix.lower() != ".json":
        raise ValueError("AdapterResultPath must be a JSON (.json) file.")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    return result_path


def write_adapter_result_if_requested(
    result_path: Path | None,
    factory: Callable[[], AdapterResult],
) -> None:
    """Build and write an AdapterResult only when the caller requested one."""
    if result_path is not None:
        write_adapter_result(result_path, factory())


def _relative_path(root: Path, path: Path) -> str:
    root_resolved = root.resolve(strict=True)
    path_resolved = path.resolve(strict=True)
    try:
        return path_resolved.relative_to(root_resolved).as_posix()
    except ValueError as exc:
        raise ValueError(f"Adapter result path is outside the workspace: {path}") from exc


def mod_lane_for_workspace_input(root: Path, path: Path) -> str:
    workspace_root = (root / "work" / "extracted_mods").resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"Adapter input is outside work/extracted_mods: {path}") from exc
    if len(relative.parts) < 2:
        raise ValueError(f"Adapter input does not identify a Mod lane: {path}")
    return relative.parts[0]


def mod_lane_for_adapter_input(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    candidate_roots = (
        root / "work" / "extracted_mods",
        root / "out",
        root / "translated" / "tool_outputs",
    )
    for base in candidate_roots:
        base_resolved = base.resolve(strict=False)
        if not is_under(resolved, base_resolved):
            continue
        relative = resolved.relative_to(base_resolved)
        if len(relative.parts) >= 2:
            return relative.parts[0]
    raise ValueError(f"Adapter input does not identify a supported Mod lane: {path}")


def require_translation_input_lane(root: Path, path: Path, mod_name: str) -> None:
    resolved = path.resolve(strict=True)
    normalized_root = (root / "work" / "normalized" / mod_name).resolve(strict=False)
    if is_under(resolved, normalized_root):
        return
    translated_root = (root / "translated").resolve(strict=False)
    if is_under(resolved, translated_root):
        relative = resolved.relative_to(translated_root)
        if len(relative.parts) >= 3 and relative.parts[1] == mod_name:
            return
    raise ValueError(
        "Adapter translation input must stay in the same Mod lane under "
        f"work/normalized/{mod_name} or translated/<kind>/{mod_name}: {path}"
    )


def build_result(
    *,
    root: Path,
    status: str,
    error_code: str | None,
    operation: str,
    adapter_id: str,
    artifact_paths: Iterable[Path] = (),
    evidence_paths: Iterable[Path] = (),
    warnings: Iterable[str] = (),
    blockers: Iterable[str] = (),
    mod_name: str = "",
    input_paths: Iterable[Path] = (),
) -> AdapterResult:
    artifacts = tuple(
        AdapterArtifact(path=_relative_path(root, path), sha256=sha256_file(path))
        for path in artifact_paths
    )
    evidence_files = tuple(_relative_path(root, path) for path in evidence_paths)
    return AdapterResult(
        status=status,
        error_code=error_code,
        operation=operation,
        adapter_id=adapter_id,
        artifacts=artifacts,
        evidence_files=evidence_files,
        warnings=tuple(warnings),
        blockers=tuple(blockers),
        mod_name=mod_name,
        inputs=tuple(
            AdapterArtifact(path=_relative_path(root, path), sha256=sha256_file(path))
            for path in input_paths
        ),
    )


def adapter_result_from_payload(payload: object) -> AdapterResult:
    if not isinstance(payload, dict):
        raise ValueError("Adapter result JSON must contain an object")
    artifacts_raw = payload.get("artifacts", [])
    inputs_raw = payload.get("inputs", [])
    for label, values in (("artifacts", artifacts_raw), ("inputs", inputs_raw)):
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise ValueError(f"Adapter result {label} must contain objects")
    result = AdapterResult(
        status=str(payload.get("status", "")),
        error_code=payload.get("error_code"),
        operation=str(payload.get("operation", "")),
        adapter_id=str(payload.get("adapter_id", "")),
        artifacts=tuple(
            AdapterArtifact(path=str(item.get("path", "")), sha256=str(item.get("sha256", "")))
            for item in artifacts_raw
        ),
        evidence_files=tuple(payload.get("evidence_files", [])),
        warnings=tuple(payload.get("warnings", [])),
        blockers=tuple(payload.get("blockers", [])),
        mod_name=str(payload.get("mod_name", "")),
        inputs=tuple(
            AdapterArtifact(path=str(item.get("path", "")), sha256=str(item.get("sha256", "")))
            for item in inputs_raw
        ),
    )
    return validate_adapter_result(result)


def read_adapter_result(path: Path) -> AdapterResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read AdapterResult {path}: {exc}") from exc
    return adapter_result_from_payload(payload)


def _payload(result: AdapterResult) -> dict[str, object]:
    validate_adapter_result(result)
    return {
        "status": result.status,
        "error_code": result.error_code,
        "operation": result.operation,
        "adapter_id": result.adapter_id,
        "artifacts": [
            {"path": artifact.path, "sha256": artifact.sha256}
            for artifact in result.artifacts
        ],
        "evidence_files": list(result.evidence_files),
        "warnings": list(result.warnings),
        "blockers": list(result.blockers),
        "mod_name": result.mod_name,
        "inputs": [
            {"path": item.path, "sha256": item.sha256}
            for item in result.inputs
        ],
    }


def write_adapter_result(path: Path, result: AdapterResult) -> None:
    """Atomically write exactly ``path``; callers own its path policy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        _payload(result),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
