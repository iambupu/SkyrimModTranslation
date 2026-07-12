"""Independently verify a BA2 receipt, manifest, source archive, and files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from game_context import load_game_context
from new_ba2_archive_manifest import (
    ADAPTER_PROTOCOL,
    FILES_FILE_NAME,
    MANIFEST_SCHEMA,
    MANIFEST_VERSION,
    REQUIRED_SAFETY,
    archive_snapshot,
    collect_file_rows,
    expected_audit_dir,
    extractor_identity,
    load_and_validate_receipt,
    resolve_controlled_adapter,
    resolve_workspace_contract_path,
    sha256_file,
    validate_archive_input,
    validate_archive_relative_path,
    validate_layout,
    validate_output_dir,
)
from new_archive_audit_manifest import count_by
from project_paths import project_root, relative_path, resolve_project_path


def verify_manifest(root: Path, manifest_path: Path) -> tuple[bool, list[str], dict[str, Any] | None]:
    issues: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"manifest-read-failed:{exc}"], None
    if not isinstance(manifest, dict):
        return False, ["manifest-root-not-object"], None
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("version") != MANIFEST_VERSION:
        issues.append("manifest-schema-or-version-invalid")

    try:
        game_id = load_game_context(root).game_id
        if manifest.get("game_id") != game_id:
            issues.append("manifest-game-id-mismatch")
        archive_path = resolve_workspace_contract_path(root, str(manifest.get("ArchivePath", "")), must_exist=True)
        validate_archive_input(root, archive_path)
        extracted_dir = resolve_workspace_contract_path(root, str(manifest.get("ExtractedDir", "")), must_exist=True)
        mod_name, archive_name = validate_layout(root, str(manifest.get("ModName", "")), archive_path, extracted_dir)
        output_dir = manifest_path.parent.resolve(strict=False)
        validate_output_dir(root, mod_name, archive_name, output_dir)
        expected_manifest = expected_audit_dir(root, mod_name, archive_name) / "manifest.json"
        if manifest_path.resolve(strict=False) != expected_manifest.resolve(strict=False):
            issues.append("manifest-path-not-exact-audit-contract")
        identity = manifest.get("ExtractorIdentity")
        if not isinstance(identity, dict):
            raise ValueError("ExtractorIdentity is missing")
        extractor_path = resolve_controlled_adapter(root, str(identity.get("Path", "")), must_exist=True)
        actual_identity = extractor_identity(root, extractor_path)
        if identity != actual_identity:
            issues.append("extractor-identity-mismatch")
        if manifest.get("ExtractorPath") != identity.get("Path"):
            issues.append("extractor-path-mismatch")
        if manifest.get("AdapterProtocol") != ADAPTER_PROTOCOL:
            issues.append("adapter-protocol-mismatch")
        receipt_path = resolve_workspace_contract_path(root, str(manifest.get("ReceiptPath", "")), must_exist=True)
        expected_receipt = output_dir / "extraction_receipt.json"
        if receipt_path.resolve(strict=False) != expected_receipt.resolve(strict=False):
            issues.append("receipt-path-mismatch")
        if manifest.get("ReceiptSha256") != sha256_file(receipt_path):
            issues.append("receipt-sha256-mismatch")
        try:
            receipt = load_and_validate_receipt(
                root=root,
                receipt_path=receipt_path,
                game_id=game_id,
                mod_name=mod_name,
                archive_path=archive_path,
                extracted_dir=extracted_dir,
                extractor_path=extractor_path,
                output_dir=output_dir,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"receipt-invalid:{exc}")
            receipt = None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, [*issues, f"manifest-path-or-identity-invalid:{exc}"], manifest

    snapshot = archive_snapshot(archive_path)
    if snapshot["sha256"] != manifest.get("ArchiveSha256"):
        issues.append("archive-sha256-mismatch")
    if snapshot["size"] != manifest.get("ArchiveSize"):
        issues.append("archive-size-mismatch")
    if manifest.get("AuditMode") != "verified-safe-extraction":
        issues.append("audit-mode-invalid")
    if isinstance(receipt, dict):
        before = receipt["ArchiveBefore"]
        after = receipt["ArchiveAfter"]
        receipt_derived = {
            "ArchiveSha256": before["sha256"],
            "ArchiveSize": before["size"],
            "ArchiveMtimeNsBefore": before["mtime_ns"],
            "ArchiveMtimeNsAfter": after["mtime_ns"],
            "ExtractorIdentity": receipt["ExtractorIdentity"],
            "AdapterProtocol": receipt["AdapterProtocol"],
            "ReceiptBindingSha256": receipt["BindingSha256"],
            "PayloadRootSha256": receipt["PayloadSnapshot"]["RootSha256"],
        }
        for key, expected in receipt_derived.items():
            if manifest.get(key) != expected:
                issues.append(f"receipt-derived-field-mismatch:{key}")

    limits = manifest.get("Limits") if isinstance(manifest.get("Limits"), dict) else {}
    try:
        rows, total_bytes = collect_file_rows(
            root,
            extracted_dir,
            max_files=int(limits.get("MaxFiles", 0)),
            max_file_bytes=int(limits.get("MaxFileBytes", 0)),
            max_total_bytes=int(limits.get("MaxTotalBytes", 0)),
        )
    except (OSError, ValueError) as exc:
        return False, [*issues, f"extraction-scan-failed:{exc}"], manifest
    actual_rows = [row.__dict__ for row in rows]
    expected_rows = manifest.get("Files")
    if not isinstance(expected_rows, list):
        issues.append("manifest-files-not-list")
        expected_rows = []
    else:
        for index, row in enumerate(expected_rows):
            if not isinstance(row, dict):
                issues.append(f"manifest-file-row-{index}-not-object")
                continue
            try:
                relative = validate_archive_relative_path(str(row.get("RelativePath", "")))
                expected_project = relative_path(root, extracted_dir / Path(*relative.split("/"))).replace("\\", "/")
                if row.get("ProjectPath") != expected_project:
                    issues.append(f"manifest-file-row-{index}-project-path-mismatch")
            except ValueError as exc:
                issues.append(f"manifest-file-row-{index}-relative-path-invalid:{exc}")
    if manifest.get("FilesScanned") != len(actual_rows):
        issues.append("files-scanned-mismatch")
    if manifest.get("TotalBytes") != total_bytes:
        issues.append("total-bytes-mismatch")
    if expected_rows != actual_rows:
        issues.append("manifest-file-rows-mismatch")
    if manifest.get("ByKind") != count_by(rows, "Kind"):
        issues.append("by-kind-mismatch")
    if manifest.get("ByRisk") != count_by(rows, "Risk"):
        issues.append("by-risk-mismatch")
    if isinstance(receipt, dict):
        for key in ("MaxFiles", "MaxFileBytes", "MaxTotalBytes"):
            value = limits.get(key)
            if not isinstance(value, int) or value <= 0 or value > int(receipt["Limits"][key]):
                issues.append(f"manifest-limit-invalid:{key}")

    files_value = str(manifest.get("FilesJsonl", ""))
    expected_files_path = manifest_path.with_name(FILES_FILE_NAME)
    try:
        files_path = resolve_workspace_contract_path(root, files_value, must_exist=True)
        if files_path.resolve(strict=False) != expected_files_path.resolve(strict=False):
            issues.append("files-jsonl-path-mismatch")
        jsonl_rows = [json.loads(line) for line in files_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        issues.append(f"files-jsonl-read-failed:{exc}")
        jsonl_rows = []
    if jsonl_rows != actual_rows:
        issues.append("files-jsonl-rows-mismatch")

    safety = manifest.get("Safety") if isinstance(manifest.get("Safety"), dict) else {}
    for key, expected in REQUIRED_SAFETY.items():
        if safety.get(key) is not expected:
            issues.append(f"safety-{key}-not-{str(expected).lower()}")
    if manifest.get("allow_repack") is not False:
        issues.append("allow-repack-not-false")
    return not issues, issues, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify BA2 extraction receipt, manifest, source archive, and materialized files.")
    parser.add_argument("--manifest-path", required=True)
    args = parser.parse_args()
    root = project_root()
    manifest_path = resolve_project_path(root, args.manifest_path, must_exist=True)
    passed, issues, _ = verify_manifest(root, manifest_path)
    if passed:
        print(f"BA2 extraction verification passed: {manifest_path}")
        return 0
    for issue in issues:
        print(f"BA2 extraction verification issue: {issue}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
