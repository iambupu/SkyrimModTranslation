"""Safely invoke the project-local BSAFileExtractor tool."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from adapter_registry import require_adapter
from adapter_result_io import (
    build_result,
    prepare_adapter_result_path,
    write_adapter_result_if_requested,
)
from capability_resolver import resolve_capability
from archive_execution_policy import (
    disk_preflight,
    resolve_archive_execution_policy,
    validate_archive_inventory,
    validate_materialized_inventory,
    write_archive_execution_evidence,
)
from file_utils import is_reparse_point, sha256_file, validate_regular_path_under
from game_context import load_game_context
from new_archive_audit_manifest import collect_file_rows, write_manifest
from new_ba2_archive_manifest import DEFAULT_MAX_FILE_BYTES, DEFAULT_MAX_FILES, DEFAULT_MAX_TOTAL_BYTES
from project_paths import (
    is_under,
    project_root,
    relative_path,
    require_under_any,
    resolve_project_path,
    safe_file_name,
)
from resource_model import classify_resource
from managed_tool_resolver import leased_payload_path, load_workspace_tool_config
from smt_windows import validate_regular_single_link_file


ADAPTER_ID_FALLBACK = "bethesda-bsa"


@dataclass(frozen=True)
class OutputRootState:
    device: int
    inode: int
    kind: int
    attributes: int


def capture_output_root(path: Path) -> OutputRootState:
    entry_stat = path.lstat()
    if path.is_symlink() or is_reparse_point(entry_stat) or not stat.S_ISDIR(entry_stat.st_mode):
        raise ValueError("BSA extraction output root must be a regular directory")
    return OutputRootState(
        device=entry_stat.st_dev,
        inode=entry_stat.st_ino,
        kind=stat.S_IFMT(entry_stat.st_mode),
        attributes=getattr(entry_stat, "st_file_attributes", 0),
    )


def ensure_empty_output_root(path: Path) -> OutputRootState:
    before = capture_output_root(path)
    with os.scandir(path) as entries:
        if next(entries, None) is not None:
            raise ValueError("BSA extraction target exists and is not empty")
    after = capture_output_root(path)
    if after != before:
        raise ValueError("BSA extraction output root changed during preflight")
    return before


def ensure_output_root_identity(path: Path, expected: OutputRootState) -> None:
    if capture_output_root(path) != expected:
        raise RuntimeError("BSA extraction output root identity changed during adapter invocation")


def remove_link_entry(path: Path, entry_stat: os.stat_result) -> None:
    if stat.S_ISDIR(entry_stat.st_mode):
        os.rmdir(path)
        return
    try:
        path.unlink()
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)


def clear_owned_directory(path: Path) -> None:
    with os.scandir(path) as entries:
        children = list(entries)
    for entry in children:
        child = Path(entry.path)
        entry_stat = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or is_reparse_point(entry_stat):
            remove_link_entry(child, entry_stat)
        elif stat.S_ISDIR(entry_stat.st_mode):
            clear_owned_directory(child)
            os.rmdir(child)
        else:
            child.unlink()


def cleanup_output_root(
    path: Path,
    expected: OutputRootState,
    *,
    restore_empty: bool,
) -> None:
    if os.path.lexists(path):
        current_stat = path.lstat()
        if path.is_symlink() or is_reparse_point(current_stat):
            remove_link_entry(path, current_stat)
        elif stat.S_ISDIR(current_stat.st_mode) and capture_output_root(path) == expected:
            clear_owned_directory(path)
            if not restore_empty:
                os.rmdir(path)
        else:
            quarantine = path.with_name(f".{path.name}.rejected-{uuid.uuid4().hex}")
            os.replace(path, quarantine)
    if restore_empty and not os.path.lexists(path):
        path.mkdir(parents=True, exist_ok=False)
        capture_output_root(path)


def validated_materialized_files(output_dir: Path) -> tuple[Path, ...]:
    validate_regular_path_under(
        output_dir, output_dir, kind="directory", label="BSA output directory"
    )
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(
        output_dir, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        for name in directory_names:
            validate_regular_path_under(
                current_path / name,
                output_dir,
                kind="directory",
                label="BSA materialized directory",
            )
        for name in file_names:
            file_path = current_path / name
            validate_regular_path_under(
                file_path,
                output_dir,
                kind="file",
                label="BSA materialized file",
            )
            files.append(file_path)
    return tuple(sorted(files, key=lambda path: str(path).casefold()))


def write_report(
    path: Path,
    *,
    root: Path,
    archive_path: Path,
    output_dir: Path,
    capability_level: str,
    files: tuple[Path, ...],
) -> None:
    lines = [
        "# BSA Controlled Materialization Report",
        "",
        "- capability: archive.bsa",
        f"- capability_level: {capability_level}",
        f"- Archive: {relative_path(root, archive_path)}",
        f"- Archive SHA256: {sha256_file(archive_path)}",
        f"- Output directory: {relative_path(root, output_dir)}",
        f"- Materialized files: {len(files)}",
        "- Source archive unchanged: True",
        "- Archive repacked: False",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def extraction_mod_name(root: Path, output_dir: Path) -> str:
    extraction_root = (root / "work" / "archive_extracts").resolve(strict=False)
    relative = output_dir.resolve(strict=False).relative_to(extraction_root)
    if not relative.parts:
        raise ValueError("BSA extraction output must identify a Mod lane")
    mod_name = safe_file_name(relative.parts[0])
    if mod_name != relative.parts[0]:
        raise ValueError("BSA extraction output Mod lane is not a canonical safe file name")
    return mod_name


def read_adapter_list(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Archive list adapter returned a non-object row")
        rows.append(value)
    return rows


def selected_archive_rows(
    context,
    rows: list[dict[str, object]],
    *,
    selective: bool,
    filters: list[str],
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for row in rows:
        value = str(row.get("path") or "")
        relative = Path(*value.replace("\\", "/").split("/"))
        if filters and not any(item.casefold() in value.casefold() for item in filters):
            continue
        descriptor = classify_resource(context, relative)
        if selective and not filters and (
            descriptor.container == "protected" or descriptor.category == "protected_binary"
        ):
            continue
        selected.append(row)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Project-local safe wrapper for BSAFileExtractor."
    )
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--tool-path",
        default="",
        help="Validated one-off manual payload path; otherwise use the managed binding.",
    )
    parser.add_argument("--config-path", default="config/tools.local.json")
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        help="Optional file path substring to extract. Repeat for multiple filters.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--show-header", action="store_true")
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
    report_path: Path | None = None
    manifest_path: Path | None = None
    manifest_files_path: Path | None = None
    manifest_report_path: Path | None = None
    output_dir: Path | None = None
    created_output = False
    reused_empty_output = False
    adapter_invoked = False
    output_root_state: OutputRootState | None = None
    execution_policy = None
    disk_evidence: dict[str, int | bool] = {}
    execution_evidence_path: Path | None = None
    selected_files: int | None = None
    leases = ExitStack()
    try:
        context = load_game_context(root)
        decision = resolve_capability(context, "archive.bsa", "read")
        adapter_id = decision.adapter_id or ADAPTER_ID_FALLBACK
        if not decision.supported:
            if result_path is None:
                raise ValueError(
                    f"Game Profile {context.game_id} does not declare .bsa "
                    f"materialization support: {decision.reason}"
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

        archive_path = resolve_project_path(root, args.archive_path, must_exist=True)
        output_dir = resolve_project_path(root, args.output_dir, must_exist=False)
        archive_extracts_root = resolve_project_path(
            root, "work/archive_extracts", must_exist=False
        )
        if archive_path.suffix.lower() != ".bsa":
            raise ValueError(
                "BSAFileExtractor only supports .bsa input: "
                f"{relative_path(root, archive_path)}"
            )
        require_under_any(
            archive_path,
            [root / "mod", root / "work" / "extracted_mods"],
            "ArchivePath",
        )
        if not is_under(output_dir, archive_extracts_root):
            raise ValueError("BSA extraction output must be under work/archive_extracts/.")
        mod_name = extraction_mod_name(root, output_dir)
        execution_policy = resolve_archive_execution_policy(
            root=root,
            mod_name=mod_name,
            requested={
                "max_files": args.max_files,
                "max_file_bytes": args.max_file_bytes,
                "max_total_bytes": args.max_total_bytes,
                "timeout_seconds": args.timeout_seconds,
                "extract_mode": "selective" if args.filter else args.extract_mode,
            },
            default_max_files=DEFAULT_MAX_FILES,
            default_max_file_bytes=DEFAULT_MAX_FILE_BYTES,
            default_max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
            expected_game_id=context.game_id,
        )
        selective = execution_policy.extract_mode == "selective"
        built_in_adapter = (Path(__file__).resolve().parent / "bethesda_archive_adapter.py").resolve(strict=True)
        tool_path: Path | None = None
        if not selective:
            if args.tool_path.strip():
                manual = resolve_project_path(root, args.tool_path, must_exist=True)
                tool_path = validate_regular_single_link_file(
                    manual,
                    root,
                    label="manual BSAFileExtractor payload",
                )
            else:
                config_path = resolve_project_path(
                    root,
                    args.config_path,
                    must_exist=True,
                )
                resolution = leases.enter_context(
                    leased_payload_path(
                        root,
                        load_workspace_tool_config(root, config_path),
                        "BsaFileExtractorPath",
                        command="materialize BSA archive",
                    )
                )
                if resolution.path is None:
                    raise FileNotFoundError(
                        "managed BSAFileExtractor binding is unavailable"
                    )
                tool_path = resolution.path
        if not selective:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="smt-bsa-inventory-", dir=output_dir.parent) as temp_dir:
                list_path = Path(temp_dir) / "entries.jsonl"
                list_result = subprocess.run(
                    [
                        sys.executable,
                        str(built_in_adapter),
                        "--archive-path",
                        str(archive_path),
                        "--list-output",
                        str(list_path),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=execution_policy.timeout_seconds,
                )
                if list_result.returncode != 0:
                    raise RuntimeError(list_result.stderr.strip() or "BSA inventory adapter failed")
                selected_rows = selected_archive_rows(
                    context,
                    read_adapter_list(list_path),
                    selective=False,
                    filters=[],
                )
                selected_files, selected_bytes = validate_archive_inventory(
                    selected_rows,
                    execution_policy,
                )
            disk_evidence = disk_preflight(
                root=root,
                archive_path=archive_path,
                output_dir=output_dir,
                selected_bytes=selected_bytes,
            )
        archive_hash = sha256_file(archive_path)
        if not os.path.lexists(output_dir):
            output_dir.mkdir(parents=True, exist_ok=False)
            created_output = True
            output_root_state = capture_output_root(output_dir)
        else:
            reused_empty_output = True
            output_root_state = ensure_empty_output_root(output_dir)
        if selective:
            with tempfile.TemporaryDirectory(prefix="smt-bsa-plan-", dir=output_dir.parent) as temp_dir:
                temp_root = Path(temp_dir)
                list_path = temp_root / "entries.jsonl"
                list_result = subprocess.run(
                    [
                        sys.executable,
                        str(built_in_adapter),
                        "--archive-path",
                        str(archive_path),
                        "--list-output",
                        str(list_path),
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=execution_policy.timeout_seconds,
                )
                if list_result.returncode != 0:
                    raise RuntimeError(list_result.stderr.strip() or "BSA inventory adapter failed")
                selected_rows = selected_archive_rows(
                    context,
                    read_adapter_list(list_path),
                    selective=True,
                    filters=args.filter,
                )
                selected_files, selected_bytes = validate_archive_inventory(
                    selected_rows,
                    execution_policy,
                )
                include_path = temp_root / "include.txt"
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
                command = [
                    sys.executable,
                    str(built_in_adapter),
                    "--archive-path",
                    str(archive_path),
                    "--output-dir",
                    str(output_dir),
                    "--include-list",
                    str(include_path),
                    "--max-files",
                    str(execution_policy.max_files),
                    "--max-file-bytes",
                    str(execution_policy.max_file_bytes),
                    "--max-total-bytes",
                    str(execution_policy.max_total_bytes),
                ]
                adapter_invoked = True
                result = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=execution_policy.timeout_seconds,
                )
        else:
            if tool_path is None:
                raise RuntimeError("BSAFileExtractor path was not resolved")
            command = [
                sys.executable,
                str(tool_path),
                archive_path.name,
                "-i",
                str(archive_path.parent),
                "-o",
                str(output_dir),
            ]
            if args.show_header:
                command.append("-h")
            if args.verbose:
                command.append("-v")

            adapter_invoked = True
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=execution_policy.timeout_seconds,
            )

        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if sha256_file(archive_path) != archive_hash:
            raise RuntimeError("source BSA changed during adapter invocation")
        if result.returncode != 0:
            raise RuntimeError(
                f"BSAFileExtractor failed with exit code {result.returncode}"
            )

        if output_root_state is None:
            raise RuntimeError("BSA extraction output root state was not recorded")
        ensure_output_root_identity(output_dir, output_root_state)
        materialized_files = validated_materialized_files(output_dir)
        validate_materialized_inventory(
            selected_rows,
            (
                {
                    "path": path.relative_to(output_dir).as_posix(),
                    "size": path.stat().st_size,
                }
                for path in materialized_files
            ),
        )
        total_bytes = sum(path.stat().st_size for path in materialized_files)
        if len(materialized_files) > execution_policy.max_files:
            raise ValueError(f"BSA extracted file count exceeds limit: {execution_policy.max_files}")
        if any(path.stat().st_size > execution_policy.max_file_bytes for path in materialized_files):
            raise ValueError(f"BSA extracted file exceeds byte limit: {execution_policy.max_file_bytes}")
        if total_bytes > execution_policy.max_total_bytes:
            raise ValueError(f"BSA extracted total bytes exceed limit: {execution_policy.max_total_bytes}")
        mod_name = extraction_mod_name(root, output_dir)
        archive_name = safe_file_name(archive_path.stem)
        manifest_dir = resolve_project_path(
            root,
            f"out/{mod_name}/archive_audits/{archive_name}",
            must_exist=False,
        )
        manifest_path = manifest_dir / "manifest.json"
        manifest_files_path = manifest_dir / "files.jsonl"
        manifest_report_path = resolve_project_path(
            root,
            f"qa/{mod_name}.{archive_name}.archive_audit_manifest.md",
            must_exist=False,
        )
        write_manifest(
            root,
            mod_name,
            archive_path,
            output_dir,
            manifest_dir,
            manifest_report_path,
            collect_file_rows(root, output_dir),
        )
        execution_evidence_path = write_archive_execution_evidence(
            root=root,
            mod_name=mod_name,
            archive_path=archive_path,
            policy=execution_policy,
            disk=disk_evidence,
            selected_files=len(materialized_files),
            status="success",
        )
        evidence: tuple[Path, ...] = ()
        if result_path is not None:
            report_path = result_path.with_suffix(".md")
            report_path.unlink(missing_ok=True)
            write_report(
                report_path,
                root=root,
                archive_path=archive_path,
                output_dir=output_dir,
                capability_level=decision.level,
                files=materialized_files,
            )
            evidence = (report_path, manifest_path, execution_evidence_path)
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="success",
                error_code=None,
                operation="extract",
                adapter_id=adapter_id,
                artifact_paths=materialized_files,
                evidence_paths=evidence,
                mod_name=mod_name,
                input_paths=(archive_path,),
            ),
        )
        return 0
    except Exception as exc:
        error_message = str(exc)
        if execution_policy is not None and output_dir is not None and 'archive_path' in locals() and 'mod_name' in locals():
            execution_evidence_path = write_archive_execution_evidence(
                root=root,
                mod_name=mod_name,
                archive_path=archive_path,
                policy=execution_policy,
                disk=disk_evidence,
                selected_files=selected_files,
                status="failed",
                error=error_message,
            )
        if (
            output_dir is not None
            and output_root_state is not None
            and (created_output or reused_empty_output)
        ):
            cleanup_output_root(
                output_dir,
                output_root_state,
                restore_empty=reused_empty_output,
            )
        if report_path is not None:
            report_path.unlink(missing_ok=True)
        for generated_path in (manifest_path, manifest_files_path, manifest_report_path):
            if generated_path is not None:
                generated_path.unlink(missing_ok=True)
        write_adapter_result_if_requested(
            result_path,
            lambda: build_result(
                root=root,
                status="error",
                error_code=(
                    "adapter_failed" if adapter_invoked else "adapter_preflight_failed"
                ),
                operation="extract",
                adapter_id=adapter_id,
                blockers=(error_message,),
            ),
        )
        if result_path is None:
            raise
        print(f"BSA extraction failed: {exc}", file=sys.stderr)
        return 1
    finally:
        leases.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BSA extraction failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
