"""Create hash-backed evidence for a wrapper-receipted BA2 extraction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from file_utils import is_reparse_point, lexical_path_chain_under
from game_context import load_game_context
from new_archive_audit_manifest import archive_content_route, count_by
from project_paths import is_under, plugin_root, project_root, relative_path, safe_file_name


MANIFEST_SCHEMA = "skyrim-mod-chs.ba2-extraction-manifest"
MANIFEST_VERSION = 2
RECEIPT_SCHEMA = "skyrim-mod-chs.ba2-extraction-receipt"
RECEIPT_VERSION = 2
ADAPTER_PROTOCOL = "skyrim-mod-chs.ba2-extractor.v1"
RECEIPT_FILE_NAME = "extraction_receipt.json"
FILES_FILE_NAME = "files.jsonl"
DEFAULT_MAX_FILES = 50_000
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
REQUIRED_SAFETY = {
    "ProjectLocalOnly": True,
    "ArchiveModified": False,
    "ExtractedContentModified": False,
    "RealGameDirectoriesAccessed": False,
    "SourceArchiveUnchanged": True,
    "NoPathTraversal": True,
    "NoLinks": True,
    "NoRepack": True,
    "PublishedAtomically": True,
    "StagingRootClean": True,
}


@dataclass(frozen=True)
class FileRow:
    RelativePath: str
    ProjectPath: str
    Extension: str
    Size: int
    Sha256: str
    Kind: str
    Risk: str
    RecommendedSkill: str
    Notes: str


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"file exceeds byte limit while hashing: {path}")
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive_relative_path(value: str) -> str:
    text = str(value)
    if not text or "\x00" in text:
        raise ValueError("archive entry path is empty or contains NUL")
    if text.startswith(("\\\\", "//", "\\", "/")):
        raise ValueError(f"absolute or UNC archive entry rejected: {value}")
    windows_path = PureWindowsPath(text.replace("/", "\\"))
    if windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"drive-qualified archive entry rejected: {value}")
    parts = windows_path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"relative archive entry rejected: {value}")
    for part in parts:
        trimmed = part.rstrip(" .")
        if not trimmed or trimmed != part or trimmed.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"reserved or ambiguous archive entry component rejected: {value}")
    return "/".join(parts)


def _reject_linked_components(anchor: Path, candidate: Path) -> None:
    _lexical_path, components = lexical_path_chain_under(
        candidate,
        anchor,
        label="BA2 workspace path",
    )
    for current in components:
        if not (current.exists() or current.is_symlink()):
            continue
        current_stat = current.lstat()
        if current.is_symlink() or is_reparse_point(current_stat):
            raise ValueError(f"workspace contract path contains a link or reparse point: {current}")


def resolve_workspace_contract_path(
    root: Path,
    value: str | Path,
    *,
    must_exist: bool,
) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.abspath(str(candidate)))
    lexical_root = Path(os.path.abspath(str(root)))
    if not is_under(lexical, lexical_root):
        raise ValueError(f"path is outside project root: {value}")
    _reject_linked_components(lexical_root, lexical)
    if must_exist and not lexical.exists():
        raise FileNotFoundError(lexical)
    resolved = lexical.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root after resolution: {value}")
    return resolved


def collect_file_rows(
    root: Path,
    extracted_dir: Path,
    *,
    project_path_root: Path | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> tuple[list[FileRow], int]:
    if max_files <= 0 or max_file_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("BA2 extraction limits must be positive")
    extracted_stat = extracted_dir.lstat()
    if extracted_dir.is_symlink() or is_reparse_point(extracted_stat) or not extracted_dir.is_dir():
        raise ValueError("ExtractedDir must be a regular directory, not a link or reparse point")

    rows: list[FileRow] = []
    total_bytes = 0
    projected_root = project_path_root or extracted_dir
    for current, dir_names, file_names in os.walk(extracted_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dir_names):
            directory = current_path / name
            directory_stat = directory.lstat()
            relative = validate_archive_relative_path(str(directory.relative_to(extracted_dir)))
            if directory.is_symlink() or is_reparse_point(directory_stat):
                raise ValueError(f"link or reparse-point directory rejected: {relative}")
        for name in file_names:
            file_path = current_path / name
            relative = validate_archive_relative_path(str(file_path.relative_to(extracted_dir)))
            file_stat = file_path.lstat()
            if file_path.is_symlink() or is_reparse_point(file_stat):
                raise ValueError(f"link or reparse-point file rejected: {relative}")
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"non-regular extracted entry rejected: {relative}")
            if file_stat.st_nlink != 1:
                raise ValueError(f"hardlink extracted entry rejected: {relative}")
            if not is_under(file_path, extracted_dir):
                raise ValueError(f"extracted entry escapes temporary root: {relative}")
            if len(rows) + 1 > max_files:
                raise ValueError(f"extracted file count exceeds limit: {max_files}")
            if file_stat.st_size > max_file_bytes:
                raise ValueError(f"extracted file exceeds per-file byte limit: {relative}")
            total_bytes += file_stat.st_size
            if total_bytes > max_total_bytes:
                raise ValueError(f"extracted total bytes exceed limit: {max_total_bytes}")
            projected_path = projected_root / Path(*relative.split("/"))
            kind, risk, skill, notes = archive_content_route(file_path, relative)
            rows.append(
                FileRow(
                    RelativePath=relative,
                    ProjectPath=relative_path(root, projected_path).replace("\\", "/"),
                    Extension=file_path.suffix.lower(),
                    Size=file_stat.st_size,
                    Sha256=sha256_file(file_path, max_bytes=max_file_bytes),
                    Kind=kind,
                    Risk=risk,
                    RecommendedSkill=skill,
                    Notes=notes,
                )
            )
    rows.sort(key=lambda row: row.RelativePath.lower())
    return rows, total_bytes


def archive_snapshot(path: Path) -> dict[str, int | str]:
    file_stat = path.stat()
    return {"sha256": sha256_file(path), "size": file_stat.st_size, "mtime_ns": file_stat.st_mtime_ns}


def payload_snapshot(rows: list[FileRow], total_bytes: int) -> dict[str, Any]:
    entries = [
        {"path": row.RelativePath, "size": row.Size, "sha256": row.Sha256}
        for row in sorted(rows, key=lambda item: item.RelativePath.lower())
    ]
    canonical = "".join(
        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for entry in entries
    ).encode("utf-8")
    return {
        "EntryCount": len(entries),
        "TotalBytes": total_bytes,
        "Entries": entries,
        "RootSha256": hashlib.sha256(canonical).hexdigest(),
    }


def receipt_binding_sha256(receipt: dict[str, Any]) -> str:
    binding = {
        key: receipt.get(key)
        for key in (
            "schema",
            "version",
            "generated_by",
            "game_id",
            "ModName",
            "ArchivePath",
            "ArchiveBefore",
            "ArchiveAfter",
            "ExtractedDir",
            "ExtractorIdentity",
            "AdapterProtocol",
            "Limits",
            "PayloadSnapshot",
            "SourceArchiveUnchanged",
            "PublishedAtomically",
            "StagingRootClean",
            "PayloadCapturedBeforePublication",
            "allow_repack",
        )
    }
    canonical = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compare_payload_snapshot(receipt: dict[str, Any], rows: list[FileRow], total_bytes: int) -> None:
    recorded = receipt.get("PayloadSnapshot")
    current = payload_snapshot(rows, total_bytes)
    if not isinstance(recorded, dict) or recorded != current:
        raise ValueError("current extracted payload does not match the pre-publication receipt snapshot")


def validate_archive_input(root: Path, archive_path: Path) -> None:
    if not archive_path.is_file() or archive_path.suffix.lower() != ".ba2":
        raise ValueError("ArchivePath must be an existing .ba2 file")
    allowed_roots = (root / "mod", root / "work" / "extracted_mods")
    if not any(is_under(archive_path, allowed_root) for allowed_root in allowed_roots):
        raise ValueError("ArchivePath must be under workspace mod/ or work/extracted_mods/")


def resolve_controlled_adapter(root: Path, value: str | Path, *, must_exist: bool = True) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        workspace_candidate = root / candidate
        plugin_candidate = plugin_root() / candidate
        candidate = workspace_candidate if workspace_candidate.exists() else plugin_candidate
    expanded = candidate.expanduser()
    lexical = Path(os.path.abspath(str(expanded)))
    workspace_root = Path(os.path.abspath(str(root)))
    source_root = Path(os.path.abspath(str(plugin_root())))
    if is_under(lexical, workspace_root):
        _reject_linked_components(workspace_root, lexical)
    elif is_under(lexical, source_root):
        _reject_linked_components(source_root, lexical)
    else:
        raise ValueError("BA2 adapter must stay inside the workspace or plugin root")
    resolved = lexical.resolve(strict=must_exist)
    if must_exist and not resolved.is_file():
        raise ValueError("BA2 adapter must be a file")
    return resolved


def extractor_identity(root: Path, extractor_path: Path) -> dict[str, Any]:
    controlled = resolve_controlled_adapter(root, extractor_path, must_exist=True)
    file_stat = controlled.stat()
    return {
        "Path": relative_path(root, controlled).replace("\\", "/") if is_under(controlled, root) else str(controlled),
        "Sha256": sha256_file(controlled),
        "Size": file_stat.st_size,
        "Protocol": ADAPTER_PROTOCOL,
    }


def validate_layout(root: Path, mod_name: str, archive_path: Path, extracted_dir: Path) -> tuple[str, str]:
    safe_mod_name = safe_file_name(mod_name)
    if safe_mod_name != mod_name:
        raise ValueError("ModName must already be a safe project file name")
    archive_name = safe_file_name(archive_path.stem)
    if archive_name != archive_path.stem:
        raise ValueError("ArchiveName must already be a safe project file name")
    expected = (root / "work" / "archive_extracts" / safe_mod_name / archive_name).resolve(strict=False)
    actual = extracted_dir.resolve(strict=False)
    if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        raise ValueError(f"ExtractedDir must exactly match work/archive_extracts/{safe_mod_name}/{archive_name}/")
    return safe_mod_name, archive_name


def expected_audit_dir(root: Path, mod_name: str, archive_name: str) -> Path:
    return resolve_workspace_contract_path(
        root,
        root / "out" / mod_name / "archive_audits" / archive_name,
        must_exist=False,
    )


def validate_output_dir(root: Path, mod_name: str, archive_name: str, output_dir: Path) -> None:
    expected = expected_audit_dir(root, mod_name, archive_name)
    actual = resolve_workspace_contract_path(root, output_dir, must_exist=output_dir.exists())
    if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
        raise ValueError(f"OutputDir must exactly match out/{mod_name}/archive_audits/{archive_name}/")


def create_receipt(
    *,
    root: Path,
    game_id: str,
    mod_name: str,
    archive_path: Path,
    archive_before: dict[str, int | str],
    archive_after: dict[str, int | str],
    extracted_dir: Path,
    extractor_path: Path,
    output_dir: Path,
    limits: dict[str, int],
    payload_rows: list[FileRow],
    payload_total_bytes: int,
    published_atomically: bool = False,
) -> tuple[Path, dict[str, Any]]:
    identity = extractor_identity(root, extractor_path)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": RECEIPT_VERSION,
        "generated_by": "invoke_ba2_extractor_safe.py",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "game_id": game_id,
        "ModName": mod_name,
        "ArchivePath": relative_path(root, archive_path).replace("\\", "/"),
        "ArchiveBefore": archive_before,
        "ArchiveAfter": archive_after,
        "ExtractedDir": relative_path(root, extracted_dir).replace("\\", "/"),
        "ExtractorIdentity": identity,
        "AdapterProtocol": ADAPTER_PROTOCOL,
        "Limits": limits,
        "PayloadSnapshot": payload_snapshot(payload_rows, payload_total_bytes),
        "SourceArchiveUnchanged": archive_before == archive_after,
        "PublishedAtomically": published_atomically,
        "StagingRootClean": True,
        "PayloadCapturedBeforePublication": True,
        "allow_repack": False,
    }
    receipt["BindingSha256"] = receipt_binding_sha256(receipt)
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / RECEIPT_FILE_NAME
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt_path, receipt


def finalize_receipt_publication(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    if not isinstance(receipt, dict) or receipt.get("PublishedAtomically") is not False:
        raise ValueError("BA2 pre-publication receipt is missing or already finalized")
    if receipt.get("BindingSha256") != receipt_binding_sha256(receipt):
        raise ValueError("BA2 pre-publication receipt binding is invalid")
    receipt["PublishedAtomically"] = True
    receipt["BindingSha256"] = receipt_binding_sha256(receipt)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def load_and_validate_receipt(
    *,
    root: Path,
    receipt_path: Path,
    game_id: str,
    mod_name: str,
    archive_path: Path,
    extracted_dir: Path,
    extractor_path: Path,
    output_dir: Path,
    payload_dir: Path | None = None,
) -> dict[str, Any]:
    expected_receipt = output_dir / RECEIPT_FILE_NAME
    if receipt_path.resolve(strict=False) != expected_receipt.resolve(strict=False):
        raise ValueError("ReceiptPath must be the extraction_receipt.json in the exact BA2 audit directory")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    if not isinstance(receipt, dict):
        raise ValueError("BA2 extraction receipt must contain an object")
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("version") != RECEIPT_VERSION:
        raise ValueError("BA2 extraction receipt schema/version is invalid")
    expected_values = {
        "generated_by": "invoke_ba2_extractor_safe.py",
        "game_id": game_id,
        "ModName": mod_name,
        "ArchivePath": relative_path(root, archive_path).replace("\\", "/"),
        "ExtractedDir": relative_path(root, extracted_dir).replace("\\", "/"),
        "AdapterProtocol": ADAPTER_PROTOCOL,
        "SourceArchiveUnchanged": True,
        "PublishedAtomically": True,
        "StagingRootClean": True,
        "PayloadCapturedBeforePublication": True,
        "allow_repack": False,
    }
    for key, expected in expected_values.items():
        if receipt.get(key) != expected:
            raise ValueError(f"BA2 extraction receipt field mismatch: {key}")
    if receipt.get("BindingSha256") != receipt_binding_sha256(receipt):
        raise ValueError("BA2 extraction receipt content binding is invalid")
    before = receipt.get("ArchiveBefore")
    after = receipt.get("ArchiveAfter")
    if not isinstance(before, dict) or not isinstance(after, dict) or before != after:
        raise ValueError("BA2 extraction receipt does not prove an unchanged source archive")
    current_archive = archive_snapshot(archive_path)
    if current_archive.get("sha256") != after.get("sha256") or current_archive.get("size") != after.get("size"):
        raise ValueError("BA2 source archive no longer matches the extraction receipt")
    if receipt.get("ExtractorIdentity") != extractor_identity(root, extractor_path):
        raise ValueError("BA2 adapter identity no longer matches the extraction receipt")
    receipt_limits = receipt.get("Limits")
    if not isinstance(receipt_limits, dict):
        raise ValueError("BA2 extraction receipt Limits are missing")
    for key in ("MaxFiles", "MaxFileBytes", "MaxTotalBytes"):
        value = receipt_limits.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"BA2 extraction receipt limit is invalid: {key}")
    physical_payload_dir = payload_dir or extracted_dir
    rows, total_bytes = collect_file_rows(
        root,
        physical_payload_dir,
        project_path_root=extracted_dir,
        max_files=int(receipt_limits["MaxFiles"]),
        max_file_bytes=int(receipt_limits["MaxFileBytes"]),
        max_total_bytes=int(receipt_limits["MaxTotalBytes"]),
    )
    compare_payload_snapshot(receipt, rows, total_bytes)
    return receipt


def write_manifest_from_receipt(
    *,
    root: Path,
    game_id: str,
    mod_name: str,
    archive_path: Path,
    extracted_dir: Path,
    extractor_path: Path,
    output_dir: Path,
    receipt_path: Path,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    payload_dir: Path | None = None,
    contract_output_dir: Path | None = None,
) -> Path:
    physical_payload_dir = payload_dir or extracted_dir
    logical_output_dir = contract_output_dir or output_dir
    receipt = load_and_validate_receipt(
        root=root,
        receipt_path=receipt_path,
        game_id=game_id,
        mod_name=mod_name,
        archive_path=archive_path,
        extracted_dir=extracted_dir,
        extractor_path=extractor_path,
        output_dir=output_dir,
        payload_dir=physical_payload_dir,
    )
    requested_limits = {
        "MaxFiles": max_files,
        "MaxFileBytes": max_file_bytes,
        "MaxTotalBytes": max_total_bytes,
    }
    receipt_limits = receipt["Limits"]
    for key, value in requested_limits.items():
        if value > int(receipt_limits[key]):
            raise ValueError(f"{key} cannot exceed receipt extraction limit")
    rows, total_bytes = collect_file_rows(
        root,
        physical_payload_dir,
        project_path_root=extracted_dir,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    files_path = output_dir / FILES_FILE_NAME
    contract_files_path = logical_output_dir / FILES_FILE_NAME
    contract_receipt_path = logical_output_dir / RECEIPT_FILE_NAME
    before = receipt["ArchiveBefore"]
    after = receipt["ArchiveAfter"]
    identity = receipt["ExtractorIdentity"]
    safety = dict(REQUIRED_SAFETY)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "version": MANIFEST_VERSION,
        "game_id": game_id,
        "ModName": mod_name,
        "ArchivePath": relative_path(root, archive_path).replace("\\", "/"),
        "ArchiveSha256": before["sha256"],
        "ArchiveSize": before["size"],
        "ArchiveMtimeNsBefore": before["mtime_ns"],
        "ArchiveMtimeNsAfter": after["mtime_ns"],
        "ExtractedDir": relative_path(root, extracted_dir).replace("\\", "/"),
        "ExtractorPath": identity["Path"],
        "ExtractorIdentity": identity,
        "AdapterProtocol": ADAPTER_PROTOCOL,
        "ReceiptPath": relative_path(root, contract_receipt_path).replace("\\", "/"),
        "ReceiptSha256": sha256_file(receipt_path),
        "ReceiptBindingSha256": receipt["BindingSha256"],
        "PayloadRootSha256": receipt["PayloadSnapshot"]["RootSha256"],
        "AuditMode": "verified-safe-extraction",
        "GeneratedAt": datetime.now().isoformat(timespec="seconds"),
        "FilesScanned": len(rows),
        "TotalBytes": total_bytes,
        "ByKind": count_by(rows, "Kind"),
        "ByRisk": count_by(rows, "Risk"),
        "Limits": requested_limits,
        "FilesJsonl": relative_path(root, contract_files_path).replace("\\", "/"),
        "Files": [asdict(row) for row in rows],
        "allow_repack": False,
        "Safety": safety,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with files_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False, sort_keys=True) + "\n")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh BA2 extraction evidence from a safe-wrapper extraction receipt.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--archive-path", required=True)
    parser.add_argument("--extracted-dir", required=True)
    parser.add_argument("--extractor-path", required=True)
    parser.add_argument("--receipt-path", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    args = parser.parse_args()

    root = project_root()
    archive_path = resolve_workspace_contract_path(root, args.archive_path, must_exist=True)
    validate_archive_input(root, archive_path)
    extracted_dir = resolve_workspace_contract_path(root, args.extracted_dir, must_exist=True)
    safe_mod_name, archive_name = validate_layout(root, args.mod_name, archive_path, extracted_dir)
    output_dir = resolve_workspace_contract_path(
        root,
        args.output_dir or f"out/{safe_mod_name}/archive_audits/{archive_name}",
        must_exist=False,
    )
    validate_output_dir(root, safe_mod_name, archive_name, output_dir)
    receipt_path = resolve_workspace_contract_path(root, args.receipt_path, must_exist=True)
    extractor_path = resolve_controlled_adapter(root, args.extractor_path, must_exist=True)
    game_id = load_game_context(root).game_id
    manifest_path = write_manifest_from_receipt(
        root=root,
        game_id=game_id,
        mod_name=safe_mod_name,
        archive_path=archive_path,
        extracted_dir=extracted_dir,
        extractor_path=extractor_path,
        output_dir=output_dir,
        receipt_path=receipt_path,
        max_files=args.max_files,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
    )
    print(f"BA2 extraction manifest written to: {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BA2 extraction manifest failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
