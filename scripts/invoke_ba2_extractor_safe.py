"""Invoke a controlled BA2 adapter through an isolated, fail-closed wrapper."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    prepare_adapter_result_path,
    write_adapter_result_if_requested,
)
from capability_resolver import resolve_capability
from game_context import load_game_context
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
from verify_ba2_extraction import verify_manifest


ADAPTER_ID_FALLBACK = "bethesda-ba2"


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def configured_adapter(root: Path, config_path: Path) -> Path:
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    decoder_tools = config.get("DecoderTools") if isinstance(config, dict) else None
    if not isinstance(decoder_tools, dict):
        raise ValueError("DecoderTools configuration is missing")
    if decoder_tools.get("Ba2ExtractorProtocol") != ADAPTER_PROTOCOL:
        raise ValueError(f"Ba2ExtractorProtocol must be {ADAPTER_PROTOCOL}")
    value = str(decoder_tools.get("Ba2ExtractorPath") or "").strip()
    if not value:
        raise ValueError("Ba2ExtractorPath is not configured")
    return resolve_controlled_adapter(root, value, must_exist=True)


def validate_staging_root(staging_root: Path, payload_dir: Path) -> None:
    entries = list(staging_root.iterdir())
    if len(entries) != 1 or entries[0].resolve(strict=False) != payload_dir.resolve(strict=False):
        names = ", ".join(sorted(entry.name for entry in entries)) or "(empty)"
        raise ValueError(f"BA2 adapter wrote outside the staging payload directory: {names}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely invoke a configured BA2 adapter into an isolated workspace directory.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--adapter-result-path", default="")
    args = parser.parse_args()

    root = project_root()
    result_path = prepare_adapter_result_path(root, args.adapter_result_path)
    adapter_id = ADAPTER_ID_FALLBACK
    output_dir: Path | None = None
    manifest_dir: Path | None = None
    staging_root: Path | None = None
    published = False
    adapter_invoked = False
    evidence_replaced = False
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
        config_path = resolve_project_path(root, args.config_path, must_exist=True)
        adapter_path = configured_adapter(root, config_path)
        if args.max_files <= 0 or args.max_file_bytes <= 0 or args.max_total_bytes <= 0:
            raise ValueError("BA2 extraction limits must be positive")
        if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
            raise ValueError("BA2 extraction target exists and is not empty")
        archive_before = archive_snapshot(archive_path)
        parent = output_dir.parent
        parent.mkdir(parents=True, exist_ok=True)

        remove_path(manifest_dir)
        evidence_replaced = True
        staging_root = Path(tempfile.mkdtemp(prefix=f".{archive_name}.ba2-stage-", dir=parent))
        payload_dir = staging_root / "payload"
        payload_dir.mkdir()

        command = [str(adapter_path)]
        if adapter_path.suffix.lower() == ".py":
            command.insert(0, sys.executable)
        command.extend(["--archive-path", str(archive_path), "--output-dir", str(payload_dir)])
        adapter_invoked = True
        completed = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"BA2 adapter failed with exit code {completed.returncode}")

        validate_staging_root(staging_root, payload_dir)
        archive_after = archive_snapshot(archive_path)
        if archive_after != archive_before:
            raise RuntimeError("source BA2 changed during adapter invocation")
        payload_rows, payload_total_bytes = collect_file_rows(
            root,
            payload_dir,
            project_path_root=output_dir,
            max_files=args.max_files,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
        )
        game_id = context.game_id
        receipt_path, _ = create_receipt(
            root=root,
            game_id=game_id,
            mod_name=safe_mod_name,
            archive_path=archive_path,
            archive_before=archive_before,
            archive_after=archive_after,
            extracted_dir=output_dir,
            extractor_path=adapter_path,
            output_dir=manifest_dir,
            limits={
                "MaxFiles": args.max_files,
                "MaxFileBytes": args.max_file_bytes,
                "MaxTotalBytes": args.max_total_bytes,
            },
            payload_rows=payload_rows,
            payload_total_bytes=payload_total_bytes,
        )
        if output_dir.exists():
            output_dir.rmdir()
        os.replace(payload_dir, output_dir)
        staging_root.rmdir()
        staging_root = None
        published = True
        finalize_receipt_publication(receipt_path)
        manifest_path = write_manifest_from_receipt(
            root=root,
            game_id=game_id,
            mod_name=safe_mod_name,
            archive_path=archive_path,
            extracted_dir=output_dir,
            extractor_path=adapter_path,
            output_dir=manifest_dir,
            receipt_path=receipt_path,
            max_files=args.max_files,
            max_file_bytes=args.max_file_bytes,
            max_total_bytes=args.max_total_bytes,
        )
        passed, issues, _ = verify_manifest(root, manifest_path)
        if not passed:
            raise RuntimeError("published BA2 extraction failed independent verification: " + "; ".join(issues))
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
        if staging_root is not None:
            remove_path(staging_root)
        if published and output_dir is not None:
            remove_path(output_dir)
        if evidence_replaced and manifest_dir is not None:
            remove_path(manifest_dir)
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
