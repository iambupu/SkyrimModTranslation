"""Invoke a controlled BA2 adapter through an isolated, fail-closed wrapper."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from archive_execution_policy import (
    disk_preflight,
    resolve_archive_execution_policy,
    validate_archive_inventory,
    validate_materialized_inventory,
    write_archive_execution_evidence,
)
from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    prepare_adapter_result_path,
    write_adapter_result_if_requested,
)
from capability_resolver import resolve_capability
from game_context import load_game_context
from managed_tool_resolver import load_workspace_tool_config
from new_ba2_archive_manifest import (
    ADAPTER_PROTOCOL,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    archive_snapshot,
    collect_file_rows,
    create_receipt,
    finalize_receipt_publication,
    expected_audit_dir,
    resolve_controlled_adapter,
    resolve_workspace_contract_path,
    validate_archive_input,
    validate_layout,
    write_manifest_from_receipt,
)
from project_paths import project_root, resolve_project_path
from resource_model import classify_resource
from verify_ba2_extraction import verify_manifest


ADAPTER_ID_FALLBACK = "bethesda-ba2"


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def configured_adapter(root: Path, config_path: Path) -> Path:
    config = load_workspace_tool_config(root, config_path)
    decoder_tools = config.get("DecoderTools") if isinstance(config, dict) else None
    if not isinstance(decoder_tools, dict):
        raise ValueError("DecoderTools configuration is missing")
    if decoder_tools.get("Ba2ExtractorProtocol") != ADAPTER_PROTOCOL:
        raise ValueError(f"Ba2ExtractorProtocol must be {ADAPTER_PROTOCOL}")
    value = str(decoder_tools.get("Ba2ExtractorPath") or "").strip()
    if not value:
        raise ValueError("Ba2ExtractorPath is not configured")
    return resolve_controlled_adapter(root, value, must_exist=True)


def read_archive_list(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("BA2 inventory adapter returned a non-object row")
        rows.append(value)
    return rows


def selective_ba2_rows(context, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for row in rows:
        value = str(row.get("path") or "")
        relative = Path(*value.replace("\\", "/").split("/"))
        descriptor = classify_resource(context, relative)
        if descriptor.container == "protected" or descriptor.category == "protected_binary":
            continue
        selected.append(row)
    return selected


def validate_staging_root(staging_root: Path, payload_dir: Path) -> None:
    entries = list(staging_root.iterdir())
    if len(entries) != 1 or entries[0].resolve(strict=False) != payload_dir.resolve(strict=False):
        names = ", ".join(sorted(entry.name for entry in entries)) or "(empty)"
        raise ValueError(f"BA2 adapter wrote outside the staging payload directory: {names}")


def _backup_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")


def publish_directories(
    payload_staging: Path,
    payload_target: Path,
    evidence_staging: Path,
    evidence_target: Path,
) -> tuple[Path | None, Path | None]:
    payload_backup = _backup_path(payload_target) if payload_target.exists() else None
    evidence_backup = _backup_path(evidence_target) if evidence_target.exists() else None
    payload_published = False
    evidence_published = False
    try:
        if payload_backup is not None:
            os.replace(payload_target, payload_backup)
        if evidence_backup is not None:
            os.replace(evidence_target, evidence_backup)
        os.replace(payload_staging, payload_target)
        payload_published = True
        os.replace(evidence_staging, evidence_target)
        evidence_published = True
        return payload_backup, evidence_backup
    except Exception:
        if evidence_published:
            remove_path(evidence_target)
        if payload_published:
            remove_path(payload_target)
        if evidence_backup is not None and evidence_backup.exists():
            os.replace(evidence_backup, evidence_target)
        if payload_backup is not None and payload_backup.exists():
            os.replace(payload_backup, payload_target)
        raise


def rollback_directories(
    payload_target: Path,
    evidence_target: Path,
    payload_backup: Path | None,
    evidence_backup: Path | None,
) -> None:
    remove_path(evidence_target)
    remove_path(payload_target)
    if evidence_backup is not None and evidence_backup.exists():
        os.replace(evidence_backup, evidence_target)
    if payload_backup is not None and payload_backup.exists():
        os.replace(payload_backup, payload_target)


def discard_backups(payload_backup: Path | None, evidence_backup: Path | None) -> None:
    if payload_backup is not None:
        remove_path(payload_backup)
    if evidence_backup is not None:
        remove_path(evidence_backup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely invoke a configured BA2 adapter into an isolated workspace directory.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--max-files", type=int)
    parser.add_argument("--max-file-bytes", type=int)
    parser.add_argument("--max-total-bytes", type=int)
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--extract-mode", choices=("full", "selective"))
    parser.add_argument("--adapter-result-path", default="")
    args = parser.parse_args()

    root = project_root()
    result_path = prepare_adapter_result_path(root, args.adapter_result_path)
    adapter_id = ADAPTER_ID_FALLBACK
    output_dir: Path | None = None
    manifest_dir: Path | None = None
    staging_root: Path | None = None
    evidence_staging: Path | None = None
    payload_backup: Path | None = None
    evidence_backup: Path | None = None
    published = False
    adapter_invoked = False
    execution_policy = None
    disk_evidence: dict[str, int | bool] = {}
    execution_evidence_path: Path | None = None
    selected_files: int | None = None
    try:
        context = load_game_context(root)
        decision = resolve_capability(context, "archive.ba2", "read")
        adapter_id = decision.adapter_id or ADAPTER_ID_FALLBACK
        if not decision.supported:
            if result_path is None:
                raise ValueError(
                    "BA2 materialization is disabled by the current Game Profile: "
                    f"{context.game_id}; {decision.reason}"
                )
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="blocked",
                    error_code=decision.error_code or "capability_unsupported",
                    operation="extract",
                    adapter_id=adapter_id,
                    blockers=(decision.reason,),
                ),
            )
            return 2
        try:
            require_adapter(adapter_id, "extract")
        except ValueError as exc:
            error_message = str(exc)
            write_adapter_result_if_requested(
                result_path,
                lambda: build_result(
                    root=root,
                    status="blocked",
                    error_code="adapter_unavailable",
                    operation="extract",
                    adapter_id=adapter_id,
                    blockers=(error_message,),
                ),
            )
            return 2

        archive_path = resolve_workspace_contract_path(root, args.archive_path, must_exist=True)
        validate_archive_input(root, archive_path)
        output_dir = resolve_workspace_contract_path(root, args.output_dir, must_exist=False)
        safe_mod_name, archive_name = validate_layout(root, args.mod_name, archive_path, output_dir)
        manifest_dir = expected_audit_dir(root, safe_mod_name, archive_name)
        execution_policy = resolve_archive_execution_policy(
            root=root,
            mod_name=safe_mod_name,
            requested={
                "max_files": args.max_files,
                "max_file_bytes": args.max_file_bytes,
                "max_total_bytes": args.max_total_bytes,
                "timeout_seconds": args.timeout_seconds,
                "extract_mode": args.extract_mode,
            },
            default_max_files=DEFAULT_MAX_FILES,
            default_max_file_bytes=DEFAULT_MAX_FILE_BYTES,
            default_max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
            expected_game_id=context.game_id,
        )
        built_in_adapter = (Path(__file__).resolve().parent / "bethesda_archive_adapter.py").resolve(strict=True)
        config_candidate = resolve_project_path(root, args.config_path, must_exist=False)
        external_adapter = None
        if execution_policy.extract_mode != "selective" and config_candidate.is_file():
            external_adapter = configured_adapter(root, config_candidate)
        adapter_path = built_in_adapter if execution_policy.extract_mode == "selective" or external_adapter is None else external_adapter
        if output_dir.exists() and not output_dir.is_dir():
            raise ValueError("BA2 extraction target exists and is not a directory")
        if manifest_dir.exists() and not manifest_dir.is_dir():
            raise ValueError("BA2 evidence target exists and is not a directory")
        archive_before = archive_snapshot(archive_path)
        parent = output_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        manifest_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(prefix=f".{archive_name}.ba2-stage-", dir=parent))
        payload_dir = staging_root / "payload"
        payload_dir.mkdir()

        inventory_path = staging_root / "entries.jsonl"
        inventory_command = [
            sys.executable,
            str(built_in_adapter),
            "--archive-path",
            str(archive_path),
            "--list-output",
            str(inventory_path),
        ]
        inventory_result = subprocess.run(
            inventory_command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=execution_policy.timeout_seconds,
        )
        if inventory_result.returncode != 0:
            raise RuntimeError(inventory_result.stderr.strip() or "BA2 inventory adapter failed")
        inventory_rows = read_archive_list(inventory_path)
        selected_rows = (
            selective_ba2_rows(context, inventory_rows)
            if execution_policy.extract_mode == "selective"
            else inventory_rows
        )
        selected_files, selected_bytes = validate_archive_inventory(
            selected_rows,
            execution_policy,
        )
        inventory_path.unlink()

        include_path: Path | None = None
        if execution_policy.extract_mode == "selective":
            include_path = staging_root / "include.txt"
            include_path.write_text(
                "".join(f"{row['path']}\n" for row in selected_rows),
                encoding="utf-8",
            )
        disk_evidence = disk_preflight(
            root=root,
            archive_path=archive_path,
            output_dir=output_dir,
            selected_bytes=selected_bytes,
        )

        command = [str(adapter_path)]
        if adapter_path.suffix.lower() == ".py":
            command.insert(0, sys.executable)
        command.extend(["--archive-path", str(archive_path), "--output-dir", str(payload_dir)])
        if adapter_path == built_in_adapter:
            command.extend(
                [
                    "--max-files",
                    str(execution_policy.max_files),
                    "--max-file-bytes",
                    str(execution_policy.max_file_bytes),
                    "--max-total-bytes",
                    str(execution_policy.max_total_bytes),
                ]
            )
            if include_path is not None:
                command.extend(["--include-list", str(include_path)])
        adapter_invoked = True
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=execution_policy.timeout_seconds,
        )
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"BA2 adapter failed with exit code {completed.returncode}")
        if include_path is not None:
            include_path.unlink(missing_ok=True)

        validate_staging_root(staging_root, payload_dir)
        archive_after = archive_snapshot(archive_path)
        if archive_after != archive_before:
            raise RuntimeError("source BA2 changed during adapter invocation")
        payload_rows, payload_total_bytes = collect_file_rows(
            root,
            payload_dir,
            project_path_root=output_dir,
            max_files=execution_policy.max_files,
            max_file_bytes=execution_policy.max_file_bytes,
            max_total_bytes=execution_policy.max_total_bytes,
        )
        validate_materialized_inventory(
            selected_rows,
            ({"path": row.RelativePath, "size": row.Size} for row in payload_rows),
        )
        game_id = context.game_id
        evidence_staging = Path(
            tempfile.mkdtemp(prefix=f".{archive_name}.ba2-evidence-", dir=manifest_dir.parent)
        )
        receipt_path, _ = create_receipt(
            root=root,
            game_id=game_id,
            mod_name=safe_mod_name,
            archive_path=archive_path,
            archive_before=archive_before,
            archive_after=archive_after,
            extracted_dir=output_dir,
            extractor_path=adapter_path,
            output_dir=evidence_staging,
            limits={
                "MaxFiles": execution_policy.max_files,
                "MaxFileBytes": execution_policy.max_file_bytes,
                "MaxTotalBytes": execution_policy.max_total_bytes,
            },
            payload_rows=payload_rows,
            payload_total_bytes=payload_total_bytes,
        )
        finalize_receipt_publication(receipt_path)
        manifest_path = write_manifest_from_receipt(
            root=root,
            game_id=game_id,
            mod_name=safe_mod_name,
            archive_path=archive_path,
            extracted_dir=output_dir,
            extractor_path=adapter_path,
            output_dir=evidence_staging,
            receipt_path=receipt_path,
            max_files=execution_policy.max_files,
            max_file_bytes=execution_policy.max_file_bytes,
            max_total_bytes=execution_policy.max_total_bytes,
            payload_dir=payload_dir,
            contract_output_dir=manifest_dir,
        )
        passed, issues, _ = verify_manifest(
            root,
            manifest_path,
            physical_audit_dir=evidence_staging,
            physical_extracted_dir=payload_dir,
        )
        if not passed:
            raise RuntimeError("staged BA2 extraction failed independent verification: " + "; ".join(issues))
        payload_backup, evidence_backup = publish_directories(
            payload_dir,
            output_dir,
            evidence_staging,
            manifest_dir,
        )
        published = True
        staging_root.rmdir()
        staging_root = None
        evidence_staging = None
        manifest_path = manifest_dir / "manifest.json"
        receipt_path = manifest_dir / "extraction_receipt.json"
        passed, issues, _ = verify_manifest(root, manifest_path)
        if not passed:
            raise RuntimeError("published BA2 extraction failed independent verification: " + "; ".join(issues))
        published = False
        discard_backups(payload_backup, evidence_backup)
        payload_backup = None
        evidence_backup = None
        artifact_paths = tuple(
            sorted(
                (path for path in output_dir.rglob("*") if path.is_file()),
                key=lambda path: str(path).casefold(),
            )
        )
        evidence_paths = tuple(
            path
            for path in (
                manifest_path,
                receipt_path,
                manifest_path.with_name("files.jsonl"),
            )
            if path.is_file()
        )
        execution_evidence_path = write_archive_execution_evidence(
            root=root,
            mod_name=safe_mod_name,
            archive_path=archive_path,
            policy=execution_policy,
            disk=disk_evidence,
            selected_files=len(artifact_paths),
            status="success",
        )
        evidence_paths = (*evidence_paths, execution_evidence_path)
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="success",
                error_code=None,
                operation="extract",
                adapter_id=adapter_id,
                artifact_paths=artifact_paths,
                evidence_paths=evidence_paths,
            ),
        )
        print(f"BA2 extraction published: {output_dir}")
        print(f"BA2 extraction manifest: {manifest_path}")
        return 0
    except Exception as exc:
        error_message = str(exc)
        if execution_policy is not None and output_dir is not None and 'archive_path' in locals() and 'safe_mod_name' in locals():
            execution_evidence_path = write_archive_execution_evidence(
                root=root,
                mod_name=safe_mod_name,
                archive_path=archive_path,
                policy=execution_policy,
                disk=disk_evidence,
                selected_files=selected_files,
                status="failed",
                error=error_message,
            )
        if staging_root is not None:
            remove_path(staging_root)
        if evidence_staging is not None:
            remove_path(evidence_staging)
        if published and output_dir is not None and manifest_dir is not None:
            rollback_directories(
                output_dir,
                manifest_dir,
                payload_backup,
                evidence_backup,
            )
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code="adapter_failed" if adapter_invoked else "adapter_preflight_failed",
                operation="extract",
                adapter_id=adapter_id,
                blockers=(error_message,),
            ),
        )
        if result_path is None:
            raise
        print(f"BA2 extraction failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BA2 extraction failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
