"""Bounded, selective, and resumable materialization for directory/ZIP/7Z inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from file_utils import is_reparse_point, py7zr_available, sha256_file
from game_context import GameContext
from mod_scale_policy import ScaleExecutionPolicy
from new_ba2_archive_manifest import validate_archive_relative_path
from project_paths import is_under, relative_path
from report_utils import utc_now
from resource_model import classify_resource


BINARY_EXTENSIONS = {".esp", ".esm", ".esl", ".bsa", ".ba2", ".pex", ".dll", ".exe"}


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError("Mod materialization exceeded timeout_seconds")


def _sha256_with_deadline(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            _check_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceEntry:
    relative_path: Path
    size: int
    source_identity: str
    category: str
    subtype: str
    container: str
    selected: bool


@dataclass(frozen=True)
class MaterializationResult:
    output_dir: Path
    extracted_files: list[str]
    binary_files: list[str]
    skipped_entries: list[str]
    warnings: list[str]
    reused_files: int
    materialized_files: int


def _canonical_member_path(value: str) -> Path:
    canonical = validate_archive_relative_path(value)
    return Path(*canonical.split("/"))


def _entry_selected(context: GameContext, relative: Path, policy: ScaleExecutionPolicy) -> tuple[str, str, str, bool]:
    descriptor = classify_resource(context, relative)
    selected = True
    if policy.selective:
        selected = descriptor.container != "protected" and descriptor.category != "protected_binary"
    return descriptor.category, descriptor.subtype, descriptor.container, selected


def _directory_entries(
    source: Path,
    context: GameContext,
    policy: ScaleExecutionPolicy,
    deadline: float,
) -> tuple[list[SourceEntry], list[str]]:
    entries: list[SourceEntry] = []
    skipped: list[str] = []
    source_root = source.resolve(strict=True)
    stack = [(source_root, Path())]
    while stack:
        current, relative_dir = stack.pop()
        with os.scandir(current) as scan:
            directory_entries = sorted(scan, key=lambda item: item.name.casefold())
        for directory_entry in directory_entries:
            _check_deadline(deadline)
            relative = relative_dir / directory_entry.name
            item = Path(directory_entry.path)
            entry_stat = item.lstat()
            if directory_entry.is_symlink() or is_reparse_point(entry_stat):
                skipped.append(f"Link or reparse point blocked: {relative.as_posix()}")
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                stack.append((item, relative))
                continue
            if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1:
                skipped.append(f"Non-regular or hardlinked file blocked: {relative.as_posix()}")
                continue
            category, subtype, container, selected = _entry_selected(context, relative, policy)
            identity = (
                _sha256_with_deadline(item, deadline)
                if selected
                else f"excluded:{entry_stat.st_size}:{entry_stat.st_mtime_ns}"
            )
            entries.append(
                SourceEntry(
                    relative_path=relative,
                    size=entry_stat.st_size,
                    source_identity=identity,
                    category=category,
                    subtype=subtype,
                    container=container,
                    selected=selected,
                )
            )
    return entries, skipped


def _validate_unique_archive_path(seen: set[str], relative: Path, source_name: str) -> None:
    key = relative.as_posix().casefold()
    if key in seen:
        raise ValueError(f"Archive contains a duplicate Windows-equivalent path: {source_name}")
    seen.add(key)


def _zip_entries(
    source: Path,
    context: GameContext,
    policy: ScaleExecutionPolicy,
    deadline: float,
) -> tuple[list[SourceEntry], list[str]]:
    entries: list[SourceEntry] = []
    skipped: list[str] = []
    seen: set[str] = set()
    archive_hash = _sha256_with_deadline(source, deadline)
    with zipfile.ZipFile(source, "r") as archive:
        for member in archive.infolist():
            _check_deadline(deadline)
            if member.is_dir() or member.filename.endswith(("/", "\\")):
                continue
            try:
                relative = _canonical_member_path(member.filename)
            except ValueError:
                skipped.append(f"Unsafe archive entry blocked: {member.filename}")
                continue
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if unix_mode and stat.S_ISLNK(unix_mode):
                skipped.append(f"ZIP link entry blocked: {member.filename}")
                continue
            if member.flag_bits & 0x1:
                skipped.append(f"Encrypted archive entry blocked: {member.filename}")
                continue
            _validate_unique_archive_path(seen, relative, member.filename)
            category, subtype, container, selected = _entry_selected(context, relative, policy)
            identity = f"{archive_hash}:{member.CRC:08x}:{member.file_size}"
            entries.append(
                SourceEntry(relative, member.file_size, identity, category, subtype, container, selected)
            )
    return entries, skipped


def _seven_zip_entries(
    source: Path,
    context: GameContext,
    policy: ScaleExecutionPolicy,
    deadline: float,
) -> tuple[list[SourceEntry], list[str]]:
    if not py7zr_available():
        raise RuntimeError("py7zr is required for selective/resumable 7Z materialization")
    import py7zr

    entries: list[SourceEntry] = []
    skipped: list[str] = []
    seen: set[str] = set()
    archive_hash = _sha256_with_deadline(source, deadline)
    with py7zr.SevenZipFile(source, mode="r") as archive:
        for member in archive.list():
            _check_deadline(deadline)
            if bool(getattr(member, "is_directory", False)):
                continue
            name = str(getattr(member, "filename", "") or "")
            try:
                relative = _canonical_member_path(name)
            except ValueError:
                skipped.append(f"Unsafe archive entry blocked: {name}")
                continue
            if bool(getattr(member, "is_symlink", False)) or not bool(getattr(member, "is_file", True)):
                skipped.append(f"Link or special archive entry blocked: {name}")
                continue
            _validate_unique_archive_path(seen, relative, name)
            size = int(getattr(member, "uncompressed", 0) or 0)
            crc = str(getattr(member, "crc32", "") or "")
            category, subtype, container, selected = _entry_selected(context, relative, policy)
            entries.append(
                SourceEntry(relative, size, f"{archive_hash}:{crc}:{size}", category, subtype, container, selected)
            )
    return entries, skipped


def inventory_entries(
    source: Path,
    context: GameContext,
    policy: ScaleExecutionPolicy,
    deadline: float,
) -> tuple[list[SourceEntry], list[str], str]:
    if source.is_dir():
        entries, skipped = _directory_entries(source, context, policy, deadline)
        return entries, skipped, ""
    extension = source.suffix.casefold()
    if extension == ".zip":
        entries, skipped = _zip_entries(source, context, policy, deadline)
        archive_hash = entries[0].source_identity.partition(":")[0] if entries else _sha256_with_deadline(source, deadline)
        return entries, skipped, archive_hash
    if extension == ".7z":
        entries, skipped = _seven_zip_entries(source, context, policy, deadline)
        archive_hash = entries[0].source_identity.partition(":")[0] if entries else _sha256_with_deadline(source, deadline)
        return entries, skipped, archive_hash
    raise ValueError(f"Unsupported materialization source: {source}")


def _validate_selected_limits(entries: Iterable[SourceEntry], policy: ScaleExecutionPolicy) -> tuple[int, int]:
    selected = [entry for entry in entries if entry.selected]
    count = len(selected)
    total = sum(entry.size for entry in selected)
    largest = max((entry.size for entry in selected), default=0)
    failures: list[str] = []
    if count > policy.limits["max_files"]:
        failures.append(f"selected file count {count} exceeds max_files {policy.limits['max_files']}")
    if largest > policy.limits["max_file_bytes"]:
        failures.append(f"selected file size {largest} exceeds max_file_bytes {policy.limits['max_file_bytes']}")
    if total > policy.limits["max_total_bytes"]:
        failures.append(f"selected bytes {total} exceeds max_total_bytes {policy.limits['max_total_bytes']}")
    if failures:
        raise ValueError("Materialization limits rejected the selected resources: " + "; ".join(failures))
    return count, total


def _shard_id(entry: SourceEntry) -> str:
    prefix = (entry.category or "unknown").replace("_", "-")
    digest = hashlib.sha256(entry.relative_path.as_posix().casefold().encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _read_previous_index(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Materialization index is invalid; use --force to rebuild it: {path}") from exc
    rows = payload.get("shards") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != "mod-materialization-shards"
        or not isinstance(rows, list)
    ):
        raise ValueError(f"Materialization index schema is invalid; use --force to rebuild it: {path}")
    indexed: dict[str, dict[str, object]] = {}
    for row in rows:
        relative = str(row.get("relative_path") or "") if isinstance(row, dict) else ""
        if not relative:
            raise ValueError(f"Materialization index contains an invalid shard row: {path}")
        key = relative.casefold()
        if key in indexed:
            raise ValueError(f"Materialization index contains a duplicate path: {relative}")
        indexed[key] = row
    return indexed


def _read_materialization_checkpoint(path: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise ValueError(f"Materialization checkpoint encoding is invalid; use --force to rebuild it: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Materialization checkpoint is invalid at line {line_number}; use --force to rebuild it: {path}"
            ) from exc
        if not isinstance(row, dict):
            raise ValueError(
                f"Materialization checkpoint row {line_number} is not an object; use --force to rebuild it: {path}"
            )
        relative = str(row.get("relative_path") or "")
        source_identity = str(row.get("source_identity") or "")
        output_hash = str(row.get("output_sha256") or "")
        if not relative or not source_identity or not re.fullmatch(r"[0-9a-fA-F]{64}", output_hash):
            raise ValueError(
                f"Materialization checkpoint row {line_number} is incomplete; use --force to rebuild it: {path}"
            )
        key = relative.casefold()
        if key in rows:
            raise ValueError(
                f"Materialization checkpoint contains a duplicate path at line {line_number}: {relative}"
            )
        rows[key] = row
    return rows


def _append_materialization_checkpoint(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _validate_existing_output_tree(output_dir: Path) -> None:
    root_stat = output_dir.lstat()
    if output_dir.is_symlink() or is_reparse_point(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("Materialization output root must be a regular directory")
    for current, directory_names, file_names in os.walk(output_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            entry_stat = path.lstat()
            if path.is_symlink() or is_reparse_point(entry_stat) or not stat.S_ISDIR(entry_stat.st_mode):
                raise ValueError(f"Unsafe directory in materialization output: {path.relative_to(output_dir)}")
        for name in file_names:
            path = current_path / name
            entry_stat = path.lstat()
            if (
                path.is_symlink()
                or is_reparse_point(entry_stat)
                or not stat.S_ISREG(entry_stat.st_mode)
                or entry_stat.st_nlink != 1
            ):
                raise ValueError(f"Unsafe file in materialization output: {path.relative_to(output_dir)}")


def _append_event(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"timestamp": utc_now(), **payload}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _can_reuse(output_dir: Path, entry: SourceEntry, previous: dict[str, dict[str, object]]) -> tuple[bool, str]:
    row = previous.get(entry.relative_path.as_posix().casefold())
    destination = (output_dir / entry.relative_path).resolve(strict=False)
    if row is None or not destination.is_file() or not is_under(destination, output_dir):
        return False, ""
    if str(row.get("source_identity") or "") != entry.source_identity:
        return False, ""
    output_hash = str(row.get("output_sha256") or "")
    if len(output_hash) != 64 or sha256_file(destination) != output_hash:
        return False, ""
    return True, output_hash


def _publish_stream(source_handle, destination: Path, deadline: float, expected_size: int) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    bytes_written = 0
    with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp") as target:
        temporary = Path(target.name)
        try:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                _check_deadline(deadline)
                bytes_written += len(chunk)
                if bytes_written > expected_size:
                    raise RuntimeError(
                        f"Materialized file exceeded its inventoried size: {destination.name}"
                    )
                digest.update(chunk)
                target.write(chunk)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    if bytes_written != expected_size:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Materialized file size changed after inventory: {destination.name}")
    os.replace(temporary, destination)
    return digest.hexdigest()


def _copy_directory_pending(
    source: Path,
    output_dir: Path,
    pending: list[SourceEntry],
    deadline: float,
    on_materialized: Callable[[SourceEntry, str], None],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    source_root = source.resolve(strict=True)
    for entry in pending:
        _check_deadline(deadline)
        source_file = (source_root / entry.relative_path).resolve(strict=True)
        destination = (output_dir / entry.relative_path).resolve(strict=False)
        if not is_under(source_file, source_root) or not is_under(destination, output_dir):
            raise ValueError(f"Unsafe materialization path: {entry.relative_path.as_posix()}")
        with source_file.open("rb") as handle:
            output_hash = _publish_stream(handle, destination, deadline, entry.size)
        if output_hash != entry.source_identity:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Directory source changed during materialization: {entry.relative_path.as_posix()}")
        hashes[entry.relative_path.as_posix().casefold()] = output_hash
        on_materialized(entry, output_hash)
    return hashes


def _copy_zip_pending(
    source: Path,
    output_dir: Path,
    pending: list[SourceEntry],
    deadline: float,
    on_materialized: Callable[[SourceEntry, str], None],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    pending_by_name = {entry.relative_path.as_posix().casefold(): entry for entry in pending}
    with zipfile.ZipFile(source, "r") as archive:
        for member in archive.infolist():
            _check_deadline(deadline)
            if member.is_dir():
                continue
            try:
                relative = _canonical_member_path(member.filename)
            except ValueError:
                continue
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if (unix_mode and stat.S_ISLNK(unix_mode)) or member.flag_bits & 0x1:
                continue
            key = relative.as_posix().casefold()
            entry = pending_by_name.get(key)
            if entry is None:
                continue
            destination = (output_dir / relative).resolve(strict=False)
            if not is_under(destination, output_dir):
                raise ValueError(f"Unsafe ZIP destination: {member.filename}")
            with archive.open(member, "r") as handle:
                hashes[key] = _publish_stream(handle, destination, deadline, entry.size)
            on_materialized(entry, hashes[key])
    if len(hashes) != len(pending):
        raise RuntimeError("ZIP changed between inventory and materialization")
    return hashes


def _copy_7z_pending(
    source: Path,
    output_dir: Path,
    pending: list[SourceEntry],
    deadline: float,
    on_materialized: Callable[[SourceEntry, str], None],
) -> dict[str, str]:
    if not pending:
        return {}
    import py7zr

    with tempfile.TemporaryDirectory(prefix="smt-7z-stage-", dir=output_dir.parent) as temp_dir:
        staging = Path(temp_dir)
        targets = [entry.relative_path.as_posix() for entry in pending]
        _check_deadline(deadline)
        with py7zr.SevenZipFile(source, mode="r") as archive:
            archive.extract(path=staging, targets=targets)
        _check_deadline(deadline)
        hashes: dict[str, str] = {}
        for entry in pending:
            staged = (staging / entry.relative_path).resolve(strict=True)
            staged_stat = staged.lstat()
            if (
                not is_under(staged, staging)
                or not staged.is_file()
                or staged.is_symlink()
                or is_reparse_point(staged_stat)
                or not stat.S_ISREG(staged_stat.st_mode)
                or staged_stat.st_nlink != 1
                or staged_stat.st_size != entry.size
            ):
                raise RuntimeError(f"7Z entry was not materialized: {entry.relative_path.as_posix()}")
            destination = (output_dir / entry.relative_path).resolve(strict=False)
            with staged.open("rb") as handle:
                output_hash = _publish_stream(handle, destination, deadline, entry.size)
            hashes[entry.relative_path.as_posix().casefold()] = output_hash
            on_materialized(entry, output_hash)
        return hashes


def _write_inventory_and_plan(
    *,
    root: Path,
    mod_name: str,
    source: Path,
    entries: list[SourceEntry],
    skipped: list[str],
    policy: ScaleExecutionPolicy,
    deadline: float,
) -> None:
    selected = [entry for entry in entries if entry.selected]
    categories: dict[str, dict[str, int]] = {}
    for entry in entries:
        _check_deadline(deadline)
        bucket = categories.setdefault(entry.category, {"files": 0, "bytes": 0, "selected_files": 0, "selected_bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += entry.size
        if entry.selected:
            bucket["selected_files"] += 1
            bucket["selected_bytes"] += entry.size
    inventory_path = root / "qa" / f"{mod_name}.resource_inventory.json"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "mod-resource-inventory",
                "generated_at": utc_now(),
                "mod_name": mod_name,
                "source_path": relative_path(root, source).replace("\\", "/"),
                "extract_mode": policy.extract_mode,
                "files": len(entries),
                "bytes": sum(entry.size for entry in entries),
                "selected_files": len(selected),
                "selected_bytes": sum(entry.size for entry in selected),
                "excluded_files": len(entries) - len(selected),
                "skipped_entries": skipped,
                "categories": categories,
                "file_index": f"work/shards/{mod_name}/files.jsonl",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plan_path = root / "qa" / f"{mod_name}.extraction_plan.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Extraction Plan",
                "",
                f"- Mod: {mod_name}",
                f"- Source: {relative_path(root, source)}",
                f"- Scale: {policy.scale_level}-{policy.risk_level}",
                f"- Extract mode: {policy.extract_mode}",
                f"- Selected: {len(selected)} files / {sum(entry.size for entry in selected)} bytes",
                f"- Excluded: {len(entries) - len(selected)} files",
                f"- Checkpoint interval: {policy.checkpoint_every_files} files",
                "",
                "Protected resources are excluded only in filtered/selective modes; archive and plugin files remain available for their controlled adapters.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def materialize_source(
    *,
    root: Path,
    mod_name: str,
    source: Path,
    output_dir: Path,
    context: GameContext,
    policy: ScaleExecutionPolicy,
    force: bool,
    resume: bool,
) -> MaterializationResult:
    deadline = time.monotonic() + policy.limits["timeout_seconds"]
    entries, skipped, source_snapshot_sha256 = inventory_entries(source, context, policy, deadline)
    selected_count, selected_bytes = _validate_selected_limits(entries, policy)
    _write_inventory_and_plan(
        root=root,
        mod_name=mod_name,
        source=source,
        entries=entries,
        skipped=skipped,
        policy=policy,
        deadline=deadline,
    )
    if source_snapshot_sha256 and _sha256_with_deadline(source, deadline) != source_snapshot_sha256:
        raise RuntimeError("Archive source changed after inventory")

    output_dir = output_dir.resolve(strict=False)
    extracted_root = (root / "work" / "extracted_mods").resolve(strict=False)
    if not is_under(output_dir, extracted_root) or output_dir == extracted_root:
        raise ValueError("Materialization output must be a child of work/extracted_mods")
    if output_dir.exists():
        _validate_existing_output_tree(output_dir)
        if any(output_dir.iterdir()) and not (force or resume):
            raise FileExistsError(f"OutputDir already exists and is not empty: {output_dir}")
        if force:
            shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    shard_root = root / "work" / "shards" / mod_name
    shard_root.mkdir(parents=True, exist_ok=True)
    index_path = shard_root / "index.json"
    events_path = shard_root / "events.jsonl"
    checkpoint_path = shard_root / "materialization_checkpoint.jsonl"
    if force or not resume:
        checkpoint_path.unlink(missing_ok=True)
    previous = _read_previous_index(index_path) if resume and not force else {}
    if resume and not force:
        previous.update(_read_materialization_checkpoint(checkpoint_path))
    selected = [entry for entry in entries if entry.selected]
    reused_hashes: dict[str, str] = {}
    pending: list[SourceEntry] = []
    for entry in selected:
        reused, output_hash = _can_reuse(output_dir, entry, previous)
        if reused:
            reused_hashes[entry.relative_path.as_posix().casefold()] = output_hash
        else:
            pending.append(entry)
    _append_event(
        events_path,
        {
            "event": "materialization_started",
            "selected_files": selected_count,
            "selected_bytes": selected_bytes,
            "reused_files": len(reused_hashes),
            "pending_files": len(pending),
        },
    )

    checkpoint_buffer: list[dict[str, str]] = []

    def record_materialized(entry: SourceEntry, output_hash: str) -> None:
        checkpoint_buffer.append(
            {
                "relative_path": entry.relative_path.as_posix(),
                "source_identity": entry.source_identity,
                "output_sha256": output_hash,
            }
        )
        if len(checkpoint_buffer) >= policy.checkpoint_every_files:
            _append_materialization_checkpoint(checkpoint_path, checkpoint_buffer)
            _append_event(events_path, {"event": "checkpoint", "files_recorded": len(checkpoint_buffer)})
            checkpoint_buffer.clear()

    if source.is_dir():
        materialized_hashes = _copy_directory_pending(source, output_dir, pending, deadline, record_materialized)
    elif source.suffix.casefold() == ".zip":
        materialized_hashes = _copy_zip_pending(source, output_dir, pending, deadline, record_materialized)
    else:
        materialized_hashes = _copy_7z_pending(source, output_dir, pending, deadline, record_materialized)
    _append_materialization_checkpoint(checkpoint_path, checkpoint_buffer)
    checkpoint_buffer.clear()

    selected_paths = {entry.relative_path.as_posix().casefold() for entry in selected}
    removed_stale_files = 0
    for existing in sorted((path for path in output_dir.rglob("*") if path.is_file()), reverse=True):
        _check_deadline(deadline)
        relative_existing = existing.relative_to(output_dir).as_posix().casefold()
        if relative_existing not in selected_paths:
            existing.unlink()
            removed_stale_files += 1
    for directory in sorted((path for path in output_dir.rglob("*") if path.is_dir()), reverse=True):
        _check_deadline(deadline)
        try:
            directory.rmdir()
        except OSError:
            pass
    if removed_stale_files:
        _append_event(events_path, {"event": "stale_outputs_removed", "files": removed_stale_files})

    if source_snapshot_sha256 and _sha256_with_deadline(source, deadline) != source_snapshot_sha256:
        raise RuntimeError("Archive source changed during materialization")

    rows: list[dict[str, object]] = []
    for index, entry in enumerate(entries, start=1):
        _check_deadline(deadline)
        key = entry.relative_path.as_posix().casefold()
        output_hash = reused_hashes.get(key) or materialized_hashes.get(key, "")
        rows.append(
            {
                "shard_id": _shard_id(entry),
                "relative_path": entry.relative_path.as_posix(),
                "category": entry.category,
                "subtype": entry.subtype,
                "container": entry.container,
                "size": entry.size,
                "selected": entry.selected,
                "source_identity": entry.source_identity,
                "source_sha256": entry.source_identity if source.is_dir() else "",
                "status": "materialized" if entry.selected else "excluded",
                "output_files": [entry.relative_path.as_posix()] if entry.selected else [],
                "output_sha256": output_hash,
                "qa_status": "pending" if entry.selected else "not-applicable",
                "reused": key in reused_hashes,
            }
        )
    files_path = shard_root / "files.jsonl"
    with files_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            _check_deadline(deadline)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    index_payload = {
        "schema_version": 1,
        "kind": "mod-materialization-shards",
        "updated_at": utc_now(),
        "mod_name": mod_name,
        "source_path": relative_path(root, source).replace("\\", "/"),
        "source_sha256": source_snapshot_sha256,
        "scale_level": policy.scale_level,
        "extract_mode": policy.extract_mode,
        "selected_files": selected_count,
        "selected_bytes": selected_bytes,
        "reused_files": len(reused_hashes),
        "materialized_files": len(materialized_hashes),
        "removed_stale_files": removed_stale_files,
        "shards": rows,
    }
    temporary_index = index_path.with_suffix(".json.tmp")
    temporary_index.write_text(json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_index, index_path)
    checkpoint_path.unlink(missing_ok=True)
    _append_event(events_path, {"event": "materialization_completed", "files": selected_count})

    extracted_files = [
        relative_path(root, output_dir / entry.relative_path)
        for entry in selected
    ]
    binary_files = [
        relative_path(root, output_dir / entry.relative_path)
        for entry in selected
        if entry.relative_path.suffix.casefold() in BINARY_EXTENSIONS
    ]
    warnings = []
    if skipped:
        warnings.append(f"Blocked {len(skipped)} unsafe or unsupported entries; see resource inventory.")
    if policy.selective:
        warnings.append(f"Selective materialization excluded {len(entries) - selected_count} protected files.")
    if removed_stale_files:
        warnings.append(f"Removed {removed_stale_files} stale materialized files no longer selected by the source and policy.")
    return MaterializationResult(
        output_dir=output_dir.resolve(strict=True),
        extracted_files=extracted_files,
        binary_files=binary_files,
        skipped_entries=skipped,
        warnings=warnings,
        reused_files=len(reused_hashes),
        materialized_files=len(materialized_hashes),
    )
