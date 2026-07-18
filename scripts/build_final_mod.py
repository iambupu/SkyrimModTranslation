"""Build a complete or translation-overlay CHS package from project-local sources.

Both delivery modes preserve the active Game Profile's Data-root paths. Normal
translations use same-path replacement; controlled string tables may add only
the Profile-mapped target-language counterpart. Sidecar dictionaries and
XML/JSONL imports stay under intermediate/ rather than becoming runtime output.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from datetime import datetime
from functools import partial
from pathlib import Path

from capability_resolver import resolve_capability, resolve_resource_capability
from game_context import GameContext, game_context_metadata, game_display_label
from plugin_resource_evidence import (
    plugin_artifact_key,
    plugin_resource_descriptor,
    read_plugin_report_traits,
    unknown_write_plugin_trait_fields,
    validate_plugin_master_style_context,
    validate_plugin_report_identity,
    validate_plugin_report_output,
    validate_plugin_report_status,
    validate_regular_evidence_path_under,
)
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import intermediate_output_dir, localization_output_root, packaged_mod_path
from project_paths import find_data_root
from project_paths import is_interface_translation_path, is_under, project_root, resolve_project_path
from project_paths import risky_marker
from project_paths import safe_file_name
from new_ba2_archive_manifest import validate_archive_relative_path
from translation_input_discovery import collect_translation_input_files
from verify_ba2_extraction import verify_manifest as verify_ba2_manifest
from route_translation_task import current_game_context
from project_paths import relative_path
from file_utils import is_backup_artifact as file_is_backup_artifact, sha256_file
from report_utils import write_text_lines as write_text
from resource_model import classify_resource
from localized_delivery import ADAPTER_ID as LOCALIZED_DELIVERY_ADAPTER_ID
from localized_delivery import validate_composite_receipt


BINARY_EXTENSIONS = {
    ".esp",
    ".esm",
    ".esl",
    ".bsa",
    ".ba2",
    ".pex",
    ".strings",
    ".dlstrings",
    ".ilstrings",
    ".dll",
    ".exe",
    ".swf",
    ".gfx",
}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
BACKUP_EXTENSIONS = {".bak", ".backup", ".old", ".tmp"}
TRANSLATION_DICTIONARY_DIR_NAME = "translation_text_dictionary"
TRANSLATION_DICTIONARY_JSONL_EXTENSIONS = {".jsonl"}


class FinalModBuildTransaction:
    def __init__(self, final_output: Path, managed_paths: tuple[Path, ...]) -> None:
        token = uuid.uuid4().hex
        self.final_output = final_output
        self.staging_output = final_output.with_name(f".{final_output.name}.{token}.tmp")
        self._managed = tuple(dict.fromkeys((final_output, *managed_paths)))
        self._backups = {
            path: path.with_name(f".{path.name}.{token}.backup")
            for path in self._managed
        }
        self._published = False
        self._finished = False

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def begin(self) -> Path:
        self._remove(self.staging_output)
        self.staging_output.mkdir(parents=True)
        return self.staging_output

    def publish(self) -> None:
        moved: list[Path] = []
        try:
            for path in self._managed:
                backup = self._backups[path]
                self._remove(backup)
                if path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, backup)
                    moved.append(path)
            self.final_output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.staging_output, self.final_output)
            self._published = True
        except Exception:
            self._remove(self.final_output)
            for path in reversed(moved):
                backup = self._backups[path]
                if backup.exists():
                    os.replace(backup, path)
            self._remove(self.staging_output)
            raise

    def commit(self) -> None:
        for backup in self._backups.values():
            try:
                self._remove(backup)
            except OSError:
                pass
        try:
            self._remove(self.staging_output)
        except OSError:
            pass
        self._finished = True

    def rollback(self) -> None:
        if self._finished:
            return
        if self._published:
            for path in self._managed:
                self._remove(path)
            for path in reversed(self._managed):
                backup = self._backups[path]
                if backup.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, path)
        else:
            self._remove(self.staging_output)
        for backup in self._backups.values():
            self._remove(backup)
        self._finished = True


_ACTIVE_BUILD_TRANSACTION: FinalModBuildTransaction | None = None


def _remap_staged_path(value: str, root: Path, staging: Path, final_output: Path) -> str:
    normalized = value.replace("\\", "/")
    staging_relative = relative_path(root, staging).replace("\\", "/").rstrip("/")
    final_relative = relative_path(root, final_output).replace("\\", "/").rstrip("/")
    if normalized.casefold() == staging_relative.casefold():
        return final_relative
    prefix = staging_relative + "/"
    if normalized.casefold().startswith(prefix.casefold()):
        return final_relative + normalized[len(staging_relative) :]
    return value


def _publish_staged_build(
    transaction: FinalModBuildTransaction,
    root: Path,
    records: tuple[list[dict[str, object]], ...],
    path_lists: tuple[list[str], ...],
) -> Path:
    staging = transaction.staging_output
    final_output = transaction.final_output
    for values in records:
        for record in values:
            destination = record.get("Destination")
            if isinstance(destination, str):
                record["Destination"] = _remap_staged_path(
                    destination,
                    root,
                    staging,
                    final_output,
                )
    for values in path_lists:
        values[:] = [
            _remap_staged_path(value, root, staging, final_output)
            for value in values
        ]
    transaction.publish()
    return final_output
BA2_LOOSE_OVERRIDE_SIDECAR = "ba2_loose_overrides.jsonl"
is_backup_artifact = partial(
    file_is_backup_artifact,
    binary_extensions=BINARY_EXTENSIONS,
    backup_extensions=BACKUP_EXTENSIONS,
)
GENERATED_META_PATHS = frozenset(
    {
        "meta/build_report.md",
        "meta/manifest.json",
        "meta/provenance.jsonl",
        "meta/qa_report.md",
        "meta/redistribution_notes.md",
        "meta/source_files.md",
    }
)
SOURCE_TEXT_KEYS = ("source", "Source", "original", "Original", "OriginalText", "原文")
TARGET_TEXT_KEYS = ("target", "Target", "Result", "Dest", "TranslatedText", "translation", "Translation", "译文")
CONTEXT_KEYS = ("plugin", "ModName", "file", "record_type", "subrecord_type", "form_id", "editor_id", "Type")
Ba2ManifestCache = dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]]
BsaManifestCache = dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]]
DELIVERY_MODE_COMPLETE = "direct-replacement-final-mod"
DELIVERY_MODE_OVERLAY = "translation-overlay-package"





def is_generated_meta_path(relative: Path) -> bool:
    normalized = str(relative).replace("\\", "/").strip("/").lower()
    return normalized in GENERATED_META_PATHS


def make_writable(path: str | Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def remove_readonly_handler(function, path, _exc_info) -> None:
    make_writable(path)
    function(path)


def remove_path_inside(path: Path, allowed_root: Path) -> None:
    # Destructive cleanup is intentionally scoped to the known output root.
    # Callers must pass the narrowest allowed root, not the repository root.
    if not is_under(path, allowed_root):
        raise ValueError(f"Refusing to remove path outside allowed root: {path}")
    if path.is_dir():
        shutil.rmtree(path, onerror=remove_readonly_handler)
    elif path.exists():
        make_writable(path)
        path.unlink()


def copy_file(file_path: Path, source_root: Path, destination_root: Path, project_root_path: Path) -> dict[str, object]:
    relative = file_path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    destination = (destination_root / relative).resolve(strict=False)
    if not is_under(destination, destination_root):
        raise ValueError(f"unsafe destination rejected: {destination}")
    replaces = destination.is_file()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, destination)
    return {
        "Source": relative_path(project_root_path, file_path),
        "Destination": relative_path(project_root_path, destination),
        "Extension": file_path.suffix.lower(),
        "ReplacesExistingFile": replaces,
    }


def source_contains_relative(
    source: Path,
    relative: Path,
    zip_members: frozenset[str] | None = None,
) -> bool:
    if source.is_dir():
        candidate = (source / relative).resolve(strict=False)
        return is_under(candidate, source) and candidate.is_file()
    if source.suffix.casefold() != ".zip":
        return False
    if zip_members is None:
        raise ValueError("ZIP source membership was not inventoried before final assembly")
    return relative.as_posix().casefold() in zip_members


def string_table_source_relative(
    output_relative: Path,
    adapter_options: Mapping[str, object],
) -> Path:
    source_language = str(adapter_options.get("source_language") or "").strip()
    target_language = str(adapter_options.get("target_language") or "").strip()
    extension = output_relative.suffix.casefold()
    if not source_language or not target_language:
        raise ValueError("String-table capability is missing language filename options")
    target_suffix = f"_{target_language}{extension}"
    name = output_relative.name
    if not name.casefold().endswith(target_suffix.casefold()):
        raise ValueError(
            f"String-table output filename must end with the target language token: {target_suffix}"
        )
    plugin_basename = name[: -len(target_suffix)]
    if not plugin_basename:
        raise ValueError("String-table output filename has no plugin basename")
    return output_relative.with_name(
        f"{plugin_basename}_{source_language}{extension}"
    )


def _plugin_localized_flag(path: Path) -> bool:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:4] != b"TES4":
        raise ValueError(f"Localized delivery plugin has an invalid TES4 header: {path}")
    return bool(int.from_bytes(header[8:12], "little") & 0x00000080)


def validate_localized_delivery_for_output(
    *,
    root: Path,
    source: Path,
    safe_mod_name: str,
    output_file: Path,
    source_relative: Path,
    context: GameContext,
) -> dict[str, str] | None:
    if not source.is_dir():
        return None
    source_language = str(
        context.require_capability("string_tables").options.get("source_language") or ""
    ).strip()
    source_suffix = f"_{source_language}{source_relative.suffix}"
    if not source_relative.name.casefold().endswith(source_suffix.casefold()):
        raise ValueError("Localized source table does not match the active source language")
    plugin_basename = source_relative.name[: -len(source_suffix)]
    candidates = sorted(
        path
        for path in source.iterdir()
        if path.is_file()
        and path.suffix.casefold() in {".esp", ".esm", ".esl"}
        and path.stem.casefold() == plugin_basename.casefold()
    )
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError(
            "Localized string table has ambiguous plugin anchors: "
            + ", ".join(path.name for path in candidates)
        )
    plugin = candidates[0]
    if not _plugin_localized_flag(plugin):
        return None

    decision = resolve_capability(context, "localized_delivery", "write")
    if not decision.supported or decision.adapter_id != LOCALIZED_DELIVERY_ADAPTER_ID:
        raise ValueError(decision.reason)
    receipt = (
        root
        / "qa"
        / "localized_delivery"
        / safe_mod_name
        / f"{safe_file_name(plugin.name)}.verify.composite.json"
    )
    if not receipt.is_file():
        raise ValueError(
            "Localized string table has no verified composite receipt: "
            f"{relative_path(root, receipt)}"
        )
    payload = validate_composite_receipt(root, receipt)
    if (
        payload.get("operation") != "verify"
        or payload.get("game_id") != context.game_id
        or payload.get("mod_name") != safe_mod_name
    ):
        raise ValueError("Localized composite receipt identity does not match final assembly")
    plugin_binding = payload.get("plugin")
    expected_plugin = relative_path(root, plugin).replace("\\", "/")
    if (
        not isinstance(plugin_binding, dict)
        or plugin_binding.get("path") != expected_plugin
        or plugin_binding.get("sha256") != sha256_file(plugin)
        or plugin_binding.get("localized") is not True
    ):
        raise ValueError("Localized composite receipt does not bind the source plugin anchor")

    expected_output = relative_path(root, output_file).replace("\\", "/")
    output_binding = next(
        (
            item
            for item in payload.get("output_tables", [])
            if isinstance(item, dict) and item.get("path") == expected_output
        ),
        None,
    )
    if not isinstance(output_binding, dict) or output_binding.get("sha256") != sha256_file(
        output_file
    ):
        raise ValueError("Localized composite receipt does not bind the target table")
    expected_source = relative_path(root, source / source_relative).replace("\\", "/")
    source_binding = next(
        (
            item
            for item in payload.get("source_tables", [])
            if isinstance(item, dict) and item.get("path") == expected_source
        ),
        None,
    )
    if not isinstance(source_binding, dict) or source_binding.get("sha256") != sha256_file(
        source / source_relative
    ):
        raise ValueError("Localized composite receipt does not bind the source table")
    return {
        "receipt": relative_path(root, receipt).replace("\\", "/"),
        "receipt_sha256": sha256_file(receipt),
        "plugin": expected_plugin,
        "plugin_sha256": sha256_file(plugin),
    }


def source_zip_members(source: Path) -> frozenset[str]:
    members: set[str] = set()
    with zipfile.ZipFile(source, "r") as archive:
        for entry in archive.infolist():
            if entry.is_dir() or not Path(entry.filename).name:
                continue
            unix_mode = (entry.external_attr >> 16) & 0xFFFF
            if unix_mode and stat.S_ISLNK(unix_mode):
                raise ValueError(f"ZIP link entry is not allowed in final assembly: {entry.filename}")
            if entry.flag_bits & 0x1:
                raise ValueError(f"Encrypted ZIP entry is not allowed in final assembly: {entry.filename}")
            normalized = safe_zip_entry_name(entry.filename).as_posix().casefold()
            if normalized in members:
                raise ValueError(f"ZIP contains a duplicate Windows path: {entry.filename}")
            members.add(normalized)
    return frozenset(members)


def resolve_delivery_mode(
    root: Path,
    mod_name: str,
    requested: str,
    include_original: bool | None,
    expected_game_id: str = "",
) -> tuple[str, Path | None, dict[str, object] | None]:
    scale_report = root / "qa" / f"{mod_name}.scale_execution.json"
    scale_payload: dict[str, object] | None = None
    if scale_report.is_file():
        parsed = json.loads(scale_report.read_text(encoding="utf-8-sig"))
        if not isinstance(parsed, dict) or parsed.get("status") != "ready" or parsed.get("mod_name") != mod_name:
            raise ValueError(f"Scale execution report is not ready for final assembly: {scale_report}")
        if expected_game_id and parsed.get("game_id") != expected_game_id:
            raise ValueError(f"Scale execution report game_id does not match the current workspace: {scale_report}")
        scale_payload = parsed

    package_mode = ""
    if scale_payload is not None:
        effective = scale_payload.get("effective")
        if not isinstance(effective, dict):
            raise ValueError(f"Scale execution report is missing effective parameters: {scale_report}")
        package_mode = str(effective.get("package_mode") or "")
        if package_mode not in {"complete", "translation-overlay", "aggregate-only"}:
            raise ValueError(f"Scale execution report has an invalid package_mode: {package_mode}")
        if package_mode == "aggregate-only":
            raise ValueError("Scale policy requires aggregate-only delivery; run aggregate_translation_projects.py")

    selected = requested
    if selected == "auto":
        if include_original is False:
            selected = "translation-overlay"
        elif include_original is True:
            selected = "complete"
        elif scale_payload is not None:
            selected = package_mode or "complete"
        else:
            selected = "complete"
    if selected not in {"complete", "translation-overlay"}:
        raise ValueError(f"Unsupported delivery mode: {selected}")
    if selected == "translation-overlay" and include_original is True:
        raise ValueError("translation-overlay delivery cannot include the complete original Mod")
    if selected == "complete" and include_original is False:
        raise ValueError("complete delivery requires original Mod files")
    if selected == "translation-overlay" and scale_payload is None:
        raise ValueError("translation-overlay delivery requires a ready scale execution report")
    if package_mode and selected != package_mode:
        raise ValueError(
            f"Requested delivery mode {selected} conflicts with scale execution package_mode={package_mode}"
        )
    return selected, scale_report if scale_payload is not None else None, scale_payload


def is_profile_protected_path(path: Path, source_root: Path, context: GameContext) -> bool:
    relative = path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    return classify_resource(context, relative).container == "protected"


def read_interface_translation_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    return data.decode("utf-16", errors="replace")


def normalize_interface_translation_file(path: Path, context: GameContext) -> None:
    if context.interface_translation_encoding != "utf-16-le-bom":
        raise ValueError(
            "Unsupported interface_translation_encoding for final_mod normalization: "
            f"{context.interface_translation_encoding}"
        )
    text = read_interface_translation_text(path)
    lines = []
    for line in text.splitlines():
        if "\t" not in line:
            match = re.match(r"^(\$[^\s]+)\s+(.+)$", line)
            if match:
                line = f"{match.group(1)}\t{match.group(2)}"
        lines.append(line)
    encoded = ("\r\n".join(lines) + "\r\n").encode("utf-16-le")
    path.write_bytes(b"\xff\xfe" + encoded)


def destination_for(file_path: Path, source_root: Path, destination_root: Path) -> Path:
    relative = file_path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    destination = (destination_root / relative).resolve(strict=False)
    if not is_under(destination, destination_root):
        raise ValueError(f"unsafe destination rejected: {destination}")
    return destination


def safe_zip_entry_name(name: str) -> Path:
    # Archive entries are hostile input. Reject absolute paths and traversal
    # before joining them to final_mod.
    entry = Path(name.replace("/", "\\"))
    if entry.is_absolute() or any(part == ".." for part in entry.parts):
        raise ValueError(f"unsafe archive entry rejected: {name}")
    return entry


def copy_zip_entry(
    archive: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    archive_path: Path,
    archive_sha256: str,
    destination_root: Path,
    project_root_path: Path,
) -> dict[str, object] | None:
    if entry.is_dir() or not Path(entry.filename).name:
        return None
    relative = safe_zip_entry_name(entry.filename)
    destination = (destination_root / relative).resolve(strict=False)
    if not is_under(destination, destination_root):
        raise ValueError(f"unsafe archive destination rejected: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with archive.open(entry, "r") as source, destination.open("wb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            target.write(chunk)
    archive_relative = relative_path(project_root_path, archive_path).replace("\\", "/")
    return {
        "Source": f"{archive_relative}::{entry.filename}",
        "SourceSha256": digest.hexdigest(),
        "SourceArchive": archive_relative,
        "SourceArchiveSha256": archive_sha256,
        "SourceArchiveEntry": entry.filename,
        "Destination": relative_path(project_root_path, destination),
        "Extension": Path(entry.filename).suffix.lower(),
        "ReplacesExistingFile": False,
    }


def source_hash(root: Path, source_value: str) -> str:
    source_path, separator, source_entry = source_value.partition("::")
    if source_path.startswith("generated:"):
        return ""
    candidate = resolve_project_path(root, source_path, must_exist=False)
    if not candidate.is_file():
        return ""
    if not separator:
        return sha256_file(candidate)
    digest = hashlib.sha256()
    with zipfile.ZipFile(candidate, "r") as archive:
        with archive.open(source_entry, "r") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def provenance_tool_and_transform(record: dict[str, object], safe_mod_name: str) -> tuple[str, str]:
    explicit_transform = str(record.get("ProvenanceTransform") or "").strip()
    explicit_tool = str(record.get("ProvenanceTool") or "").strip()
    if explicit_transform or explicit_tool:
        if not explicit_transform or not explicit_tool:
            raise ValueError("Explicit provenance requires both transform and tool")
        return explicit_transform, explicit_tool
    if isinstance(record.get("BsaProvenance"), dict):
        return "bsa-loose-override", "Agent Text Pipeline"
    if isinstance(record.get("Ba2Provenance"), dict):
        return "ba2-loose-override", "Agent Text Pipeline"
    source = str(record.get("Source", ""))
    normalized_source = source.replace("/", "\\").lower()
    extension = str(record.get("Extension", "")).lower()
    tool_output_roots = (
        f"translated\\tool_outputs\\{safe_mod_name}".lower(),
        f"out\\{safe_mod_name}\\tool_outputs".lower(),
    )
    if any(normalized_source.startswith(root) for root in tool_output_roots):
        if extension in {".esp", ".esm", ".esl"}:
            return "controlled-tool-output", "MutagenAdapter/LexTranslator/xTranslator"
        if extension == ".pex":
            return "controlled-tool-output", "MutagenPexAdapter/LexTranslator/xTranslator"
        if extension in {".strings", ".dlstrings", ".ilstrings"}:
            return "controlled-string-table-output", "BethesdaStringTableTool"
        return "controlled-tool-output", "Controlled Tool Output"
    if str(record.get("Phase", "")) == "original":
        return "original-copy", "build_final_mod.py"
    return "text-resource-translation", "Agent Text Pipeline"


def provenance_row(
    root: Path,
    final_mod: Path,
    destination: Path,
    *,
    source: str,
    source_sha256: str,
    transform: str,
    tool: str,
    status: str,
    game_metadata: dict[str, object],
    replaces_existing: bool | None = None,
) -> dict[str, object]:
    final_relative = relative_path(final_mod, destination).replace("\\", "/")
    row: dict[str, object] = {
        **game_metadata,
        "file": f"final_mod/{final_relative}",
        "file_sha256": sha256_file(destination) if destination.is_file() else "",
        "source": source,
        "source_sha256": source_sha256,
        "transform": transform,
        "tool": tool,
        "generated_by": "build_final_mod.py",
        "status": status,
        "qa_evidence": ["qa/final_mod_validation.md"],
    }
    if replaces_existing is not None:
        row["replaces_existing"] = replaces_existing
    return row


def write_provenance_jsonl(
    root: Path,
    final_mod: Path,
    provenance_path: Path,
    copied_files: list[dict[str, object]],
    overlay_files: list[dict[str, object]],
    safe_mod_name: str,
    context: GameContext,
) -> int:
    metadata = game_context_metadata(context)
    rows_by_file: dict[str, dict[str, object]] = {}
    for phase, records in (("original", copied_files), ("overlay", overlay_files)):
        for record in records:
            destination = resolve_project_path(root, str(record["Destination"]), must_exist=True)
            record["Phase"] = phase
            transform, tool = provenance_tool_and_transform(record, safe_mod_name)
            bsa_claim = record.get("BsaProvenance") if isinstance(record.get("BsaProvenance"), dict) else None
            ba2_claim = record.get("Ba2Provenance") if isinstance(record.get("Ba2Provenance"), dict) else None
            archive_claim = bsa_claim or ba2_claim
            source_value = str(record.get("Source", ""))
            if archive_claim:
                source_value = source_value.replace("\\", "/")
            source_sha256 = str(record.get("SourceSha256") or source_hash(root, source_value))
            row = provenance_row(
                root,
                final_mod,
                destination,
                source=source_value,
                source_sha256=source_sha256,
                transform=transform,
                tool=tool,
                status="assembled",
                game_metadata=metadata,
                replaces_existing=bool(record.get("ReplacesExistingFile", False)),
            )
            if archive_claim:
                row.update(
                    {
                        "archive_path": archive_claim["ArchivePath"],
                        "archive_sha256": archive_claim["ArchiveSha256"],
                        "archive_entry_path": archive_claim["EntryPath"],
                        "archive_entry_sha256": archive_claim["SourceSha256"],
                        "archive_manifest": archive_claim["ManifestPath"],
                        "qa_evidence": [archive_claim["ManifestPath"], "qa/final_mod_validation.md"],
                    }
                )
            if record.get("SourceArchive"):
                row.update(
                    {
                        "source_archive": record["SourceArchive"],
                        "source_archive_sha256": record["SourceArchiveSha256"],
                        "source_archive_entry": record["SourceArchiveEntry"],
                    }
                )
            if record.get("StringTableSource"):
                row.update(
                    {
                        "string_table_source": record["StringTableSource"],
                        "string_table_source_sha256": record[
                            "StringTableSourceSha256"
                        ],
                    }
                )
            if record.get("LocalizedDeliveryReceipt"):
                row.update(
                    {
                        "localized_delivery_receipt": record["LocalizedDeliveryReceipt"],
                        "localized_delivery_receipt_sha256": record[
                            "LocalizedDeliveryReceiptSha256"
                        ],
                        "localized_plugin_anchor": record["LocalizedPluginAnchor"],
                        "localized_plugin_anchor_sha256": record[
                            "LocalizedPluginAnchorSha256"
                        ],
                    }
                )
            inherited = record.get("AggregateChildProvenance")
            if isinstance(inherited, dict):
                row.update(
                    {
                        "aggregate_child_project": inherited["project"],
                        "aggregate_child_manifest": inherited["manifest"],
                        "aggregate_child_manifest_sha256": inherited["manifest_sha256"],
                        "aggregate_child_provenance": inherited["provenance"],
                        "aggregate_child_provenance_sha256": inherited["provenance_sha256"],
                    }
                )
            rows_by_file[str(row["file"]).lower()] = row

    for item in sorted(path for path in final_mod.rglob("*") if path.is_file() and path.resolve(strict=False) != provenance_path.resolve(strict=False)):
        final_relative = relative_path(final_mod, item).replace("\\", "/")
        key = f"final_mod/{final_relative}".lower()
        if key in rows_by_file:
            continue
        rows_by_file[key] = provenance_row(
            root,
            final_mod,
            item,
            source="generated:build_final_mod.py",
            source_sha256="",
            transform="final-mod-assembly-metadata",
            tool="build_final_mod.py",
            status="generated",
            game_metadata=metadata,
        )

    rows = [rows_by_file[key] for key in sorted(rows_by_file)]
    provenance_relative = relative_path(final_mod, provenance_path).replace("\\", "/")
    rows.append(
        {
            **metadata,
            "file": f"final_mod/{provenance_relative}",
            "file_sha256": "",
            "source": "generated:build_final_mod.py",
            "source_sha256": "",
            "transform": "provenance-manifest",
            "tool": "build_final_mod.py",
            "generated_by": "build_final_mod.py",
            "status": "self-referential",
            "qa_evidence": ["qa/final_mod_validation.md"],
        }
    )
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    with provenance_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)



def ba2_manifest_cache_key(manifest_path: Path) -> str:
    return os.path.normcase(str(manifest_path.resolve(strict=True)))


def verified_bsa_manifest(
    root: Path,
    safe_mod_name: str,
    manifest_path: Path,
    cache: BsaManifestCache,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    canonical = manifest_path.resolve(strict=True)
    cache_key = ba2_manifest_cache_key(canonical)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    expected_audit_root = (root / "out" / safe_mod_name / "archive_audits").resolve(strict=False)
    if not is_under(canonical, expected_audit_root):
        raise ValueError("BSA extraction manifest must be under the Mod archive_audits directory")
    try:
        manifest = json.loads(canonical.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid BSA extraction manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("BSA extraction manifest root must be an object")
    if manifest.get("schema") == "skyrim-mod-chs.ba2-extraction-manifest":
        raise ValueError("BSA extraction evidence cannot use the BA2 manifest schema")
    if str(manifest.get("ModName") or "") != safe_mod_name:
        raise ValueError("BSA extraction manifest ModName does not match the delivery")

    archive_value = str(manifest.get("ArchivePath") or "").replace("\\", "/")
    archive_path = resolve_project_path(root, archive_value, must_exist=True)
    if archive_path.suffix.lower() != ".bsa":
        raise ValueError("BSA extraction manifest ArchivePath is not a .bsa file")
    if not any(is_under(archive_path, base) for base in (root / "mod", root / "work" / "extracted_mods")):
        raise ValueError("BSA extraction manifest ArchivePath is outside approved workspace inputs")
    archive_sha256 = str(manifest.get("ArchiveSha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", archive_sha256) or sha256_file(archive_path) != archive_sha256:
        raise ValueError("BSA extraction manifest archive hash does not match the source archive")
    archive_size = manifest.get("ArchiveSize")
    if type(archive_size) is not int or archive_size != archive_path.stat().st_size:
        raise ValueError("BSA extraction manifest archive size does not match the source archive")

    extracted_value = str(manifest.get("ExtractedDir") or "").replace("\\", "/")
    extracted_dir = resolve_project_path(root, extracted_value, must_exist=True)
    if not extracted_dir.is_dir() or not is_under(extracted_dir, root / "work" / "archive_extracts"):
        raise ValueError("BSA extraction manifest ExtractedDir is outside work/archive_extracts")
    expected_safety = {
        "ProjectLocalOnly": True,
        "ArchiveModified": False,
        "ExtractedContentModified": False,
        "RealGameDirectoriesAccessed": False,
    }
    safety = manifest.get("Safety")
    if not isinstance(safety, dict) or any(safety.get(key) is not value for key, value in expected_safety.items()):
        raise ValueError("BSA extraction manifest safety claims are missing or invalid")

    files = manifest.get("Files")
    if not isinstance(files, list) or manifest.get("FilesScanned") != len(files):
        raise ValueError("BSA extraction manifest file count is invalid")
    file_index: dict[str, dict[str, object]] = {}
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"BSA extraction manifest file row {index} is not an object")
        entry_path = validate_archive_relative_path(str(item.get("RelativePath") or ""))
        if str(item.get("RelativePath") or "").replace("\\", "/") != entry_path:
            raise ValueError(f"BSA extraction manifest file row {index} RelativePath is not canonical")
        if entry_path in file_index:
            raise ValueError(f"BSA extraction manifest duplicates entry: {entry_path}")
        project_value = str(item.get("ProjectPath") or "").replace("\\", "/")
        project_path = resolve_project_path(root, project_value, must_exist=True)
        expected_path = (extracted_dir / Path(*entry_path.split("/"))).resolve(strict=True)
        if project_path != expected_path or not project_path.is_file():
            raise ValueError(f"BSA extraction manifest ProjectPath does not match entry: {entry_path}")
        if type(item.get("Size")) is not int or item.get("Size") != project_path.stat().st_size:
            raise ValueError(f"BSA extraction manifest size does not match entry: {entry_path}")
        manifest_hash = item.get("Sha256")
        if manifest_hash is not None and str(manifest_hash).lower() != sha256_file(project_path):
            raise ValueError(f"BSA extraction manifest hash does not match entry: {entry_path}")
        file_index[entry_path] = item
    result = (manifest, file_index)
    cache[cache_key] = result
    return result


def archive_overlay_path(root: Path, safe_mod_name: str, entry_path: str) -> Path:
    return (
        root / "translated" / "final_mod" / safe_mod_name / Path(*entry_path.split("/"))
    ).resolve(strict=False)


def existing_archive_overlays(
    root: Path,
    safe_mod_name: str,
    rows: list[object],
) -> list[tuple[str, Path]]:
    overlays: list[tuple[str, Path]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        try:
            entry_path = validate_archive_relative_path(str(raw_row.get("RelativePath") or ""))
        except ValueError:
            continue
        overlay_path = archive_overlay_path(root, safe_mod_name, entry_path)
        if overlay_path.is_file():
            overlays.append((entry_path, overlay_path))
    return overlays


def archive_overlay_is_protected(path: Path) -> bool:
    return path.suffix.lower() in BINARY_EXTENSIONS or is_backup_artifact(path)


def load_bsa_loose_override_claims(
    root: Path,
    safe_mod_name: str,
    manifest_cache: BsaManifestCache | None = None,
) -> tuple[dict[str, dict[str, str]], str]:
    audit_root = root / "out" / safe_mod_name / "archive_audits"
    if not audit_root.is_dir():
        return {}, ""
    cache = manifest_cache if manifest_cache is not None else {}
    claims: dict[str, dict[str, str]] = {}
    for manifest_path in sorted(audit_root.glob("*/manifest.json"), key=lambda path: str(path).lower()):
        try:
            unverified = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(unverified, dict) or Path(str(unverified.get("ArchivePath") or "")).suffix.lower() != ".bsa":
            continue
        raw_files = unverified.get("Files")
        if not isinstance(raw_files, list):
            continue
        candidate_entries = existing_archive_overlays(root, safe_mod_name, raw_files)
        if not candidate_entries:
            continue
        try:
            manifest, file_index = verified_bsa_manifest(root, safe_mod_name, manifest_path, cache)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"BSA loose override references unverified extraction evidence: {exc}"
            ) from exc
        manifest_value = relative_path(root, manifest_path).replace("\\", "/")
        archive_path = str(manifest.get("ArchivePath") or "").replace("\\", "/")
        for entry_path, overlay_path in candidate_entries:
            manifest_row = file_index.get(entry_path)
            if not manifest_row:
                raise ValueError(f"BSA loose override EntryPath is absent from verified manifest: {entry_path}")
            if archive_overlay_is_protected(overlay_path):
                raise ValueError(f"BSA loose override cannot claim a protected or backup file: {entry_path}")
            extracted_path = resolve_project_path(root, str(manifest_row.get("ProjectPath") or ""), must_exist=True)
            source_sha256 = sha256_file(extracted_path)
            overlay_key = str(overlay_path).lower()
            if overlay_key in claims:
                raise ValueError(f"BSA loose override is ambiguously claimed by multiple manifests: {entry_path}")
            claims[overlay_key] = {
                "ManifestPath": manifest_value,
                "ArchivePath": archive_path,
                "ArchiveSha256": str(manifest.get("ArchiveSha256") or "").lower(),
                "EntryPath": entry_path,
                "OverlayPath": relative_path(root, overlay_path).replace("\\", "/"),
                "SourceSha256": source_sha256,
            }
    return claims, ""


def require_bsa_claims_for_matching_overlays(
    root: Path,
    safe_mod_name: str,
    claims: dict[str, dict[str, str]],
    manifest_cache: BsaManifestCache | None = None,
) -> None:
    cache = manifest_cache if manifest_cache is not None else {}
    audit_root = root / "out" / safe_mod_name / "archive_audits"
    if not audit_root.is_dir():
        return
    for manifest_path in sorted(audit_root.glob("*/manifest.json"), key=lambda path: str(path).lower()):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or Path(str(payload.get("ArchivePath") or "")).suffix.lower() != ".bsa":
            continue
        files = payload.get("Files")
        if not isinstance(files, list):
            continue
        matching_overlays = [
            overlay
            for _entry_path, overlay in existing_archive_overlays(root, safe_mod_name, files)
        ]
        if not matching_overlays:
            continue
        try:
            verified_bsa_manifest(root, safe_mod_name, manifest_path, cache)
        except (OSError, ValueError) as exc:
            raise ValueError(f"BSA loose override matches an unverified extraction manifest: {exc}") from exc
        for overlay in matching_overlays:
            if str(overlay).lower() not in claims:
                raise ValueError(
                    "BSA loose override is missing verified archive manifest evidence: "
                    + relative_path(root, overlay).replace("\\", "/")
                )


def verified_ba2_manifest(
    root: Path,
    manifest_path: Path,
    cache: Ba2ManifestCache,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    canonical = manifest_path.resolve(strict=True)
    cache_key = ba2_manifest_cache_key(canonical)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    verified, issues, manifest = verify_ba2_manifest(root, canonical)
    if not verified or not isinstance(manifest, dict):
        raise ValueError("unverified BA2 extraction evidence: " + "; ".join(issues))
    files = manifest.get("Files")
    file_index = {
        str(item.get("RelativePath")): item
        for item in files
        if isinstance(item, dict) and isinstance(item.get("RelativePath"), str)
    } if isinstance(files, list) else {}
    result = (manifest, file_index)
    cache[cache_key] = result
    return result


def load_ba2_loose_override_claims(
    root: Path,
    safe_mod_name: str,
    manifest_cache: Ba2ManifestCache | None = None,
) -> tuple[dict[str, dict[str, str]], str]:
    sidecar = root / "out" / safe_mod_name / "archive_audits" / BA2_LOOSE_OVERRIDE_SIDECAR
    if not sidecar.is_file():
        return {}, ""
    cache = manifest_cache if manifest_cache is not None else {}
    claims: dict[str, dict[str, str]] = {}
    for line_number, line in enumerate(sidecar.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"BA2 loose override sidecar line {line_number} is invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"BA2 loose override sidecar line {line_number} must be an object")
        required = ("ManifestPath", "ArchivePath", "EntryPath", "OverlayPath", "SourceSha256")
        missing = [key for key in required if not isinstance(row.get(key), str) or not str(row.get(key)).strip()]
        if missing:
            raise ValueError(f"BA2 loose override sidecar line {line_number} is missing: {', '.join(missing)}")

        manifest_path = resolve_project_path(root, str(row["ManifestPath"]), must_exist=True)
        try:
            manifest, file_index = verified_ba2_manifest(root, manifest_path, cache)
        except ValueError as exc:
            raise ValueError(
                f"BA2 loose override sidecar line {line_number} references unverified extraction evidence: "
                + str(exc)
            ) from exc
        manifest_value = relative_path(root, manifest_path).replace("\\", "/")
        if str(row["ManifestPath"]).replace("\\", "/") != manifest_value:
            raise ValueError(f"BA2 loose override sidecar line {line_number} ManifestPath is not canonical")
        archive_path = str(manifest.get("ArchivePath") or "").replace("\\", "/")
        if str(row["ArchivePath"]).replace("\\", "/") != archive_path:
            raise ValueError(f"BA2 loose override sidecar line {line_number} ArchivePath does not match manifest")

        entry_path = validate_archive_relative_path(str(row["EntryPath"]))
        if str(row["EntryPath"]).replace("\\", "/") != entry_path:
            raise ValueError(f"BA2 loose override sidecar line {line_number} EntryPath is not canonical")
        manifest_row = file_index.get(entry_path)
        if not manifest_row:
            raise ValueError(f"BA2 loose override sidecar line {line_number} EntryPath is absent from manifest")
        source_sha256 = str(manifest_row.get("Sha256") or "")
        if str(row["SourceSha256"]) != source_sha256:
            raise ValueError(f"BA2 loose override sidecar line {line_number} SourceSha256 does not match manifest")

        expected_overlay = archive_overlay_path(root, safe_mod_name, entry_path)
        overlay_path = resolve_project_path(root, str(row["OverlayPath"]), must_exist=True)
        if overlay_path.resolve(strict=False) != expected_overlay:
            raise ValueError(
                f"BA2 loose override sidecar line {line_number} has relative-path drift; "
                f"OverlayPath must equal translated/final_mod/{safe_mod_name}/{entry_path}"
            )
        if archive_overlay_is_protected(overlay_path):
            raise ValueError(f"BA2 loose override sidecar line {line_number} cannot claim a protected or backup file")
        overlay_key = str(overlay_path).lower()
        if overlay_key in claims:
            raise ValueError(f"BA2 loose override sidecar line {line_number} duplicates OverlayPath")
        claims[overlay_key] = {
            "ManifestPath": manifest_value,
            "ArchivePath": archive_path,
            "ArchiveSha256": str(manifest.get("ArchiveSha256") or ""),
            "EntryPath": entry_path,
            "OverlayPath": relative_path(root, overlay_path).replace("\\", "/"),
            "SourceSha256": source_sha256,
        }
    return claims, relative_path(root, sidecar).replace("\\", "/")


def require_ba2_claims_for_matching_overlays(
    root: Path,
    safe_mod_name: str,
    claims: dict[str, dict[str, str]],
    manifest_cache: Ba2ManifestCache | None = None,
) -> None:
    cache = manifest_cache if manifest_cache is not None else {}
    audit_root = root / "out" / safe_mod_name / "archive_audits"
    if not audit_root.is_dir():
        return
    for manifest_path in sorted(audit_root.glob("*/manifest.json"), key=lambda path: str(path).lower()):
        cached = cache.get(ba2_manifest_cache_key(manifest_path))
        if cached is not None:
            payload, file_index = cached
            files = list(file_index.values())
        else:
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("schema") != "skyrim-mod-chs.ba2-extraction-manifest":
                continue
            raw_files = payload.get("Files")
            if not isinstance(raw_files, list):
                continue
            files = raw_files
        if payload.get("schema") != "skyrim-mod-chs.ba2-extraction-manifest":
            continue
        matching_overlays = [
            overlay
            for _entry_path, overlay in existing_archive_overlays(root, safe_mod_name, files)
        ]
        if not matching_overlays:
            continue
        try:
            verified_ba2_manifest(root, manifest_path, cache)
        except ValueError as exc:
            raise ValueError(
                "BA2 loose override matches an unverified extraction manifest: "
                + str(exc)
            ) from exc
        for overlay in matching_overlays:
            if str(overlay).lower() not in claims:
                raise ValueError(
                    "BA2 loose override is missing provenance sidecar evidence: "
                    + relative_path(root, overlay).replace("\\", "/")
                )


def text_value(payload: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def dictionary_source_files(root: Path, safe_mod_name: str) -> list[Path]:
    # The handoff dictionary is built from translation intermediates, not from
    # final_mod. This keeps review provenance visible even after overlays are
    # copied into the release directory.
    sources = collect_translation_input_files(
        root,
        safe_mod_name,
        suffixes=TRANSLATION_DICTIONARY_JSONL_EXTENSIONS,
        include_derived_pex_apply=False,
    )

    xtranslator_ready = root / "translated" / "xtranslator_ready" / safe_mod_name
    if xtranslator_ready.is_dir():
        sources.extend(
            file_path
            for file_path in xtranslator_ready.rglob("*")
            if file_path.is_file() and file_path.name != ".gitkeep" and file_path.suffix.lower() == ".xml"
        )

    legacy_dictionary_root = root / "out" / safe_mod_name / "lex_dictionary"
    if legacy_dictionary_root.is_dir():
        for file_path in legacy_dictionary_root.rglob("*"):
            if file_path.is_file() and file_path.name != ".gitkeep":
                sources.append(file_path)

    return sorted(set(sources), key=lambda path: str(path).lower())


def jsonl_dictionary_entries(root: Path, source_file: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        lines = source_file.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return entries
    for line_number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        source = text_value(payload, SOURCE_TEXT_KEYS)
        target = text_value(payload, TARGET_TEXT_KEYS)
        if not source or not target or source == target:
            continue
        context = {key: payload[key] for key in CONTEXT_KEYS if key in payload and payload[key] not in ("", None)}
        entries.append(
            {
                "source": source,
                "target": target,
                "source_file": relative_path(root, source_file),
                "line": line_number,
                "format": "jsonl",
                "context": context,
            }
        )
    return entries


def xml_dictionary_entries(root: Path, source_file: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        document = ET.parse(source_file)
    except (ET.ParseError, OSError, UnicodeDecodeError):
        return entries
    addon = document.findtext(".//Params/Addon") or ""
    for index, element in enumerate(document.findall(".//String"), start=1):
        source = element.findtext("Source") or ""
        target = element.findtext("Dest") or element.findtext("Target") or ""
        if not source.strip() or not target.strip() or source == target:
            continue
        context: dict[str, object] = {}
        if addon:
            context["plugin"] = addon
        list_name = element.attrib.get("List")
        if list_name:
            context["List"] = list_name
        entries.append(
            {
                "source": source,
                "target": target,
                "source_file": relative_path(root, source_file),
                "line": index,
                "format": "xml",
                "context": context,
            }
        )
    return entries


def extract_dictionary_entries(root: Path, source_file: Path) -> list[dict[str, object]]:
    suffix = source_file.suffix.lower()
    if suffix == ".jsonl":
        return jsonl_dictionary_entries(root, source_file)
    if suffix == ".xml":
        return xml_dictionary_entries(root, source_file)
    return []


def translated_dictionary_entry_count(root: Path, safe_mod_name: str) -> int:
    count = 0
    for source_file in dictionary_source_files(root, safe_mod_name):
        count += len(extract_dictionary_entries(root, source_file))
    return count


def require_translation_dictionary_entries(root: Path, safe_mod_name: str) -> int:
    entry_count = translated_dictionary_entry_count(root, safe_mod_name)
    if entry_count <= 0:
        raise ValueError(
            "No translated source-to-target dictionary entries were found. "
            f"Fill project-local translation JSONL/XML inputs for {safe_mod_name} before building final_mod."
        )
    return entry_count


def markdown_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", "\\r").replace("\n", "\\n")


def create_translation_text_dictionary(root: Path, safe_mod_name: str, destination_root: Path) -> dict[str, object]:
    # The dictionary is mandatory evidence for release handoff. It is not loaded
    # by Skyrim and must not be packaged inside the CHS zip.
    dictionary_root = destination_root / TRANSLATION_DICTIONARY_DIR_NAME
    raw_root = dictionary_root / "raw_sources"
    dictionary_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)

    source_files = dictionary_source_files(root, safe_mod_name)
    copied_sources: list[str] = []
    entries: list[dict[str, object]] = []

    for source_file in source_files:
        raw_destination = raw_root / source_file.resolve(strict=True).relative_to(root.resolve(strict=True))
        raw_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, raw_destination)
        copied_sources.append(relative_path(root, raw_destination))

        for entry in extract_dictionary_entries(root, source_file):
            entries.append(entry)

    deduped_entries: list[dict[str, object]] = []
    seen_entries: set[str] = set()
    for entry in entries:
        key = json.dumps(
            {
                "source": entry.get("source", ""),
                "target": entry.get("target", ""),
                "context": entry.get("context", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if key in seen_entries:
            continue
        seen_entries.add(key)
        deduped_entries.append(entry)
    entries = deduped_entries

    entries.sort(key=lambda item: (str(item["source_file"]).lower(), int(item.get("line", 0)), str(item["source"])))

    dictionary_jsonl = dictionary_root / "translation_dictionary.jsonl"
    with dictionary_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    preview_limit = 200
    preview_entries = entries[:preview_limit]
    dictionary_md = dictionary_root / "translation_dictionary.md"
    markdown_lines = [
        "# Translation Text Dictionary",
        "",
        f"- ModName: {safe_mod_name}",
        f"- Translated entries: {len(entries)}",
        f"- Source dictionary files: {len(source_files)}",
        "- Complete JSONL: translation_dictionary.jsonl",
        "- Raw source mirrors: raw_sources/",
        "",
        "## Preview",
        "",
    ]
    if preview_entries:
        markdown_lines.extend(["| Source | Target | Context | Origin |", "|---|---|---|---|"])
        for entry in preview_entries:
            context = json.dumps(entry.get("context", {}), ensure_ascii=False, sort_keys=True)
            origin = f"{entry['source_file']}:{entry.get('line', '')}"
            markdown_lines.append(
                f"| {markdown_cell(entry['source'])} | {markdown_cell(entry['target'])} | {markdown_cell(context)} | {markdown_cell(origin)} |"
            )
        if len(entries) > preview_limit:
            markdown_lines.extend(["", f"Preview limited to {preview_limit} rows. Use translation_dictionary.jsonl for the complete dictionary."])
    else:
        markdown_lines.append("No translated source-target entries were found.")
    write_text(dictionary_md, markdown_lines)

    readme_path = dictionary_root / "README.md"
    write_text(
        readme_path,
        [
            "# Translation Text Dictionary",
            "",
            "This folder is a required intermediate output for handoff and manual inspection.",
            "",
            "- `translation_dictionary.jsonl` is the normalized complete source-to-target dictionary with one row per translated entry and context.",
            "- `translation_dictionary.md` is a readable preview.",
            "- `raw_sources/` mirrors the project-local dictionary inputs used to build the normalized dictionary.",
        ],
    )

    manifest_path = dictionary_root / "manifest.json"
    manifest = {
        "ModName": safe_mod_name,
        "GeneratedAt": datetime.now().isoformat(timespec="seconds"),
        "DictionaryDir": relative_path(root, dictionary_root),
        "DictionaryJsonl": relative_path(root, dictionary_jsonl),
        "DictionaryPreview": relative_path(root, dictionary_md),
        "RawSourceDir": relative_path(root, raw_root),
        "SourceFileCount": len(source_files),
        "TranslatedEntryCount": len(entries),
        "SourceFiles": copied_sources,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return manifest


def copy_intermediate_outputs(root: Path, safe_mod_name: str, destination_root: Path) -> tuple[list[str], dict[str, object]]:
    # Intermediate mirrors are for audit and future Codex handoff. They are
    # rebuilt every run so stale tool outputs cannot masquerade as current
    # release evidence.
    copied: list[str] = []
    destination_root.mkdir(parents=True, exist_ok=True)
    write_text(
        destination_root / "README.md",
        [
            "# Intermediate Outputs",
            "",
            "This directory mirrors project-local intermediate outputs for handoff and inspection.",
            "It must include `translation_text_dictionary/`, a source-to-target text dictionary with one row per translated entry and context.",
            "",
            "The complete translated mod is built in the sibling `final_mod/` directory.",
            "The installable archive is the sibling `<ModName>_CHS.zip` package.",
        ],
    )
    copied.append(relative_path(root, destination_root / "README.md"))
    dictionary_manifest = create_translation_text_dictionary(root, safe_mod_name, destination_root)
    copied.append(str(dictionary_manifest["DictionaryDir"]))
    for name in ("tool_outputs", "final_mod_overlay", "xtranslator_import", "dsd_patch", "lex_dictionary", "archive_audits", "qa"):
        source = root / "out" / safe_mod_name / name
        if not source.is_dir():
            continue
        target = destination_root / name
        if target.exists():
            remove_path_inside(target, destination_root)
        shutil.copytree(source, target)
        copied.append(relative_path(root, target))
    return copied, dictionary_manifest


def create_package(final_mod: Path, package_path: Path, root: Path) -> dict[str, object]:
    # The archive contains exactly final_mod contents. intermediate/ remains a
    # sibling directory so users can inspect evidence without installing it.
    if package_path.exists():
        remove_path_inside(package_path, package_path.parent)
    package_path.parent.mkdir(parents=True, exist_ok=True)
    entries = 0
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(path for path in final_mod.rglob("*") if path.is_file()):
            archive_name = item.relative_to(final_mod).as_posix()
            archive.write(item, archive_name)
            entries += 1
    return {
        "Path": relative_path(root, package_path),
        "Entries": entries,
        "SizeBytes": package_path.stat().st_size,
    }


def bool_value(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"expected boolean value, got: {value}")


def _main_impl() -> int:
    global _ACTIVE_BUILD_TRANSACTION
    parser = argparse.ArgumentParser(description="Build a project-local complete or translation-overlay CHS package.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--source-mod-dir", default="mod")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--delivery-mode", choices=("auto", "complete", "translation-overlay"), default="auto")
    parser.add_argument("--include-original-files", type=bool_value, default=None)
    parser.add_argument("--overlay-translated-files", type=bool_value, default=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    root = project_root()
    context = current_game_context(root)
    unsafe_marker = risky_marker(root, context=context)
    if unsafe_marker.lower() == "appdata" and is_under(root, Path(tempfile.gettempdir())):
        unsafe_marker = ""
    if unsafe_marker:
        raise ValueError(f"Workspace path matches protected {context.display_name} runtime marker: {unsafe_marker}")
    safe_mod_name = safe_file_name(args.mod_name)
    if not safe_mod_name:
        raise ValueError("ModName cannot be empty after sanitization.")

    source = resolve_project_path(root, args.source_mod_dir, must_exist=True)
    if source.is_dir():
        detected_source = find_data_root(source, context=context).resolve(strict=True)
        if detected_source != source:
            source = detected_source
    else:
        suffix = source.suffix.lower()
        if suffix == ".zip":
            print("SourceModDir is a project-local zip archive; it will be extracted read-only into final_mod.")
        elif suffix in {".rar", ".7z"}:
            raise ValueError(f"SourceModDir points to {suffix}. Extract it into mod/ first or add an explicit project-local extraction flow.")
        else:
            raise ValueError(f"SourceModDir must be a directory or a project-local .zip archive: {args.source_mod_dir}")
    zip_members = source_zip_members(source) if source.suffix.casefold() == ".zip" else None

    selected_delivery_mode, scale_execution_path, scale_execution_payload = resolve_delivery_mode(
        root,
        safe_mod_name,
        args.delivery_mode,
        args.include_original_files,
        context.game_id,
    )
    include_original_files = selected_delivery_mode == "complete"

    if args.overlay_translated_files:
        require_translation_dictionary_entries(root, safe_mod_name)
    bsa_manifest_cache: BsaManifestCache = {}
    bsa_claims, _ = load_bsa_loose_override_claims(root, safe_mod_name, bsa_manifest_cache)
    require_bsa_claims_for_matching_overlays(root, safe_mod_name, bsa_claims, bsa_manifest_cache)
    ba2_manifest_cache: Ba2ManifestCache = {}
    ba2_claims, ba2_claim_sidecar = load_ba2_loose_override_claims(root, safe_mod_name, ba2_manifest_cache)
    require_ba2_claims_for_matching_overlays(root, safe_mod_name, ba2_claims, ba2_manifest_cache)
    ambiguous_archive_claims = set(bsa_claims).intersection(ba2_claims)
    if ambiguous_archive_claims:
        raise ValueError("Loose override cannot claim both BSA and BA2 provenance")

    mod_out_root = resolve_project_path(root, f"out/{safe_mod_name}", must_exist=False)
    mod_out_root.mkdir(parents=True, exist_ok=True)
    localization_root = localization_output_root(root, safe_mod_name)
    output_value = args.output_dir or relative_path(root, default_final_mod_dir(root, safe_mod_name))
    final_output = resolve_project_path(root, output_value, must_exist=False)
    if not is_under(final_output, localization_root):
        raise ValueError(f"OutputDir must be under out/{safe_mod_name}/汉化产出/: {output_value}")
    if final_output.resolve(strict=False) == localization_root.resolve(strict=False):
        raise ValueError(f"OutputDir must be a child directory under out/{safe_mod_name}/汉化产出, not the localization output root itself.")

    if final_output.exists():
        existing = list(final_output.iterdir()) if final_output.is_dir() else [final_output]
        if existing and not args.force:
            raise ValueError(f"OutputDir already exists and is not empty. Re-run with --force to rebuild: {final_output}")
        if args.force:
            if not is_under(final_output, mod_out_root):
                raise ValueError(f"Refusing to refresh path outside out/{safe_mod_name}/: {final_output}")
    intermediate_dir = intermediate_output_dir(root, safe_mod_name)
    package_path = packaged_mod_path(root, safe_mod_name)
    package_report_path = localization_root / "package_report.md"
    transaction = FinalModBuildTransaction(
        final_output,
        (intermediate_dir, package_path, package_report_path),
    )
    _ACTIVE_BUILD_TRANSACTION = transaction
    output = transaction.begin()
    meta_dir = output / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    copied_files: list[dict[str, object]] = []
    overlay_files: list[dict[str, object]] = []
    replacement_files: list[dict[str, object]] = []
    added_overlay_files: list[dict[str, object]] = []
    source_binary_files: list[str] = []
    binary_tool_overlay_files: list[str] = []
    translation_files: list[str] = []
    skipped_archive_files: list[str] = []
    warnings: list[str] = []

    if include_original_files:
        # Start from a clean project-local source copy. Archives inside the Mod
        # are skipped because nested deliverables are not valid Skyrim Data
        # files and often hide unreviewed content.
        if source.is_dir():
            for file_path in sorted(item for item in source.rglob("*") if item.is_file() and item.name != ".gitkeep"):
                source_relative = file_path.resolve(strict=True).relative_to(source.resolve(strict=True))
                if is_generated_meta_path(source_relative):
                    warnings.append(f"Generated meta input skipped: {relative_path(root, file_path)}")
                    continue
                suffix = file_path.suffix.lower()
                if suffix in ARCHIVE_EXTENSIONS:
                    skipped_archive_files.append(relative_path(root, file_path))
                    continue
                record = copy_file(file_path, source, output, root)
                destination = resolve_project_path(root, str(record["Destination"]), must_exist=True)
                if is_interface_translation_path(destination.relative_to(output.resolve(strict=True))):
                    normalize_interface_translation_file(destination, context)
                copied_files.append(record)
                if record["Extension"] in BINARY_EXTENSIONS:
                    source_binary_files.append(str(record["Destination"]))
        else:
            source_archive_sha256 = sha256_file(source)
            with zipfile.ZipFile(source, "r") as archive:
                for entry in archive.infolist():
                    if entry.is_dir() or not Path(entry.filename).name:
                        continue
                    entry_relative = safe_zip_entry_name(entry.filename)
                    if is_generated_meta_path(entry_relative):
                        warnings.append(
                            f"Generated meta ZIP entry skipped: {relative_path(root, source)}::{entry.filename}"
                        )
                        continue
                    suffix = Path(entry.filename).suffix.lower()
                    if suffix in ARCHIVE_EXTENSIONS:
                        skipped_archive_files.append(f"{relative_path(root, source)}::{entry.filename}")
                        continue
                    record = copy_zip_entry(
                        archive,
                        entry,
                        source,
                        source_archive_sha256,
                        output,
                        root,
                    )
                    if record is None:
                        continue
                    destination = resolve_project_path(root, str(record["Destination"]), must_exist=True)
                    if is_interface_translation_path(destination.relative_to(output.resolve(strict=True))):
                        normalize_interface_translation_file(destination, context)
                    copied_files.append(record)
                    if record["Extension"] in BINARY_EXTENSIONS:
                        source_binary_files.append(str(record["Destination"]))
        if skipped_archive_files:
            warnings.append(f"Archive files were skipped and not copied into final_mod: {len(skipped_archive_files)}")
    else:
        warnings.append("Translation-overlay delivery selected; original Mod files were not copied.")

    build_report_path = meta_dir / "build_report.md"
    write_text(
        build_report_path,
        [
            "# Final Mod Build Report",
            "",
            f"- ModName: {args.mod_name}",
            f"- Build started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- SourceModDir: {source}",
            f"- OutputDir: {final_output}",
            f"- Original files copied: {len(copied_files)}",
            "",
            "Overlay phase is about to run. Final overlay details are appended after completion.",
        ],
    )

    overlay_roots = [
        f"translated/final_mod/{safe_mod_name}",
        f"translated/overlay/{safe_mod_name}",
        f"out/{safe_mod_name}/final_mod_overlay",
        f"out/{safe_mod_name}/xtranslator_import",
        f"out/{safe_mod_name}/dsd_patch",
    ]
    binary_overlay_roots = [
        f"translated/tool_outputs/{safe_mod_name}",
        f"out/{safe_mod_name}/tool_outputs",
    ]

    if args.overlay_translated_files:
        # Text overlays may add or replace files. Protected binary outputs are
        # accepted only from tool_outputs; plugins and PEX must replace an
        # existing path, while string tables may add the exact Profile-mapped
        # target-language counterpart of an existing source-language table.
        for overlay_relative in overlay_roots:
            overlay_root = resolve_project_path(root, overlay_relative, must_exist=False)
            if not overlay_root.is_dir():
                continue
            for file_path in sorted(item for item in overlay_root.rglob("*") if item.is_file() and item.name != ".gitkeep"):
                overlay_relative = file_path.resolve(strict=True).relative_to(overlay_root.resolve(strict=True))
                if is_generated_meta_path(overlay_relative):
                    warnings.append(f"Generated meta overlay skipped: {relative_path(root, file_path)}")
                    continue
                suffix = file_path.suffix.lower()
                if is_backup_artifact(file_path):
                    warnings.append(f"Backup/tool history artifact skipped: {relative_path(root, file_path)}")
                    continue
                if suffix in ARCHIVE_EXTENSIONS:
                    skipped_archive_files.append(relative_path(root, file_path))
                    continue
                if is_profile_protected_path(file_path, overlay_root, context):
                    warnings.append(f"Profile-protected overlay skipped: {relative_path(root, file_path)}")
                    continue
                if suffix in BINARY_EXTENSIONS:
                    warnings.append(f"Protected binary overlay skipped outside tool_outputs: {relative_path(root, file_path)}")
                    continue
                bsa_claim = bsa_claims.get(str(file_path.resolve(strict=True)).lower())
                ba2_claim = ba2_claims.get(str(file_path.resolve(strict=True)).lower())
                replaces_source = source_contains_relative(source, overlay_relative, zip_members) or bool(
                    bsa_claim or ba2_claim
                )
                record = copy_file(file_path, overlay_root, output, root)
                record["ReplacesExistingFile"] = replaces_source
                if bsa_claim:
                    record["BsaProvenance"] = bsa_claim
                if ba2_claim:
                    record["Ba2Provenance"] = ba2_claim
                destination = resolve_project_path(root, str(record["Destination"]), must_exist=True)
                if is_interface_translation_path(destination.relative_to(output.resolve(strict=True))):
                    normalize_interface_translation_file(destination, context)
                overlay_files.append(record)
                if record["ReplacesExistingFile"]:
                    replacement_files.append(record)
                else:
                    added_overlay_files.append(record)
                translation_files.append(str(record["Destination"]))

        for overlay_relative in binary_overlay_roots:
            overlay_root = resolve_project_path(root, overlay_relative, must_exist=False)
            if not overlay_root.is_dir():
                continue
            for file_path in sorted(item for item in overlay_root.rglob("*") if item.is_file() and item.name != ".gitkeep"):
                overlay_relative = file_path.resolve(strict=True).relative_to(overlay_root.resolve(strict=True))
                if is_generated_meta_path(overlay_relative):
                    warnings.append(f"Generated meta tool output skipped: {relative_path(root, file_path)}")
                    continue
                if is_backup_artifact(file_path):
                    warnings.append(f"Backup/tool history artifact skipped: {relative_path(root, file_path)}")
                    continue
                descriptor = classify_resource(context, overlay_relative)
                if (
                    descriptor.category != "plugin"
                    and descriptor.subtype != "papyrus.binary"
                    and descriptor.category != "string_table"
                ):
                    warnings.append(
                        f"Tool output skipped: {relative_path(root, file_path)}; "
                        f"ResourceDescriptor category '{descriptor.category}' subtype "
                        f"'{descriptor.subtype}' is not an allowed plugin, Papyrus binary, "
                        "or string table."
                    )
                    continue
                trait_caps = context.resource_model.trait_level_caps.get(descriptor.capability, {})
                if descriptor.category == "plugin" and trait_caps:
                    report_path = root / "qa" / f"{plugin_artifact_key(safe_mod_name, overlay_relative)}.apply.md"
                    try:
                        if not source.is_dir():
                            raise ValueError("current source is not a safely bindable Data root")
                        original_plugin = validate_regular_evidence_path_under(
                            source / overlay_relative,
                            source,
                            kind="file",
                            label="Original plugin",
                        )
                        tool_output = validate_regular_evidence_path_under(
                            file_path,
                            overlay_root,
                            kind="file",
                            label="Plugin tool output",
                        )
                        report_path = validate_regular_evidence_path_under(
                            report_path,
                            root / "qa",
                            kind="file",
                            label="Plugin apply report",
                        )
                        status = validate_plugin_report_status(report_path, return_code=0)
                        if status != "ready":
                            raise ValueError(f"Plugin apply report Status is not ready: {status}")
                        validate_plugin_report_identity(
                            report_path,
                            project_root=root,
                            expected_input=original_plugin,
                            expected_game=context.game_id,
                            expected_operation="apply",
                        )
                        validate_plugin_report_output(
                            report_path,
                            project_root=root,
                            expected_output=tool_output,
                        )
                        report_traits = read_plugin_report_traits(report_path)
                        master_style_context = validate_plugin_master_style_context(
                            report_path,
                            project_root=root,
                            expected_input=original_plugin,
                            expected_game=context.game_id,
                        )
                        if report_traits.light_context is not master_style_context.light_context:
                            raise ValueError(
                                "Plugin apply report light trait does not match its "
                                "master-style context evidence"
                            )
                        unknown_traits = unknown_write_plugin_trait_fields(context, report_traits)
                        if unknown_traits:
                            raise ValueError(
                                "Plugin apply report has unknown write traits: "
                                + ", ".join(unknown_traits)
                            )
                        descriptor = plugin_resource_descriptor(
                            context,
                            overlay_relative,
                            report_traits,
                        )
                    except (OSError, ValueError) as exc:
                        warnings.append(
                            f"Plugin tool output skipped because required apply evidence is "
                            f"missing, invalid, unknown, or blocked: {relative_path(root, file_path)}; {exc}"
                        )
                        continue
                decision = resolve_resource_capability(context, descriptor, "write")
                if not decision.supported:
                    warnings.append(
                        f"Tool output skipped: {relative_path(root, file_path)}; "
                        f"ResourceDescriptor subtype '{descriptor.subtype}' capability "
                        f"'{descriptor.capability}' write rejected at effective level "
                        f"'{decision.level}': {decision.reason.rstrip('.')}."
                    )
                    continue
                if is_profile_protected_path(file_path, overlay_root, context):
                    warnings.append(f"Protected tool output skipped: {relative_path(root, file_path)}")
                    continue
                bsa_claim = bsa_claims.get(str(file_path.resolve(strict=True)).lower())
                ba2_claim = ba2_claims.get(str(file_path.resolve(strict=True)).lower())
                replaces_source = source_contains_relative(
                    source,
                    overlay_relative,
                    zip_members,
                )
                string_table_source = ""
                string_table_source_sha256 = ""
                localized_delivery_claim: dict[str, str] | None = None
                if descriptor.category == "string_table":
                    try:
                        source_relative = string_table_source_relative(
                            overlay_relative,
                            decision.adapter_options,
                        )
                    except ValueError as exc:
                        warnings.append(
                            f"String-table tool output skipped: {relative_path(root, file_path)}; {exc}"
                        )
                        continue
                    if not source_contains_relative(source, source_relative, zip_members):
                        warnings.append(
                            "String-table tool output skipped because its Profile-mapped "
                            "source-language table is absent: "
                            f"{relative_path(root, file_path)} -> {source_relative.as_posix()}"
                        )
                        continue
                    if source.is_dir():
                        original_table = source / source_relative
                        string_table_source = relative_path(root, original_table)
                        string_table_source_sha256 = sha256_file(original_table)
                        try:
                            localized_delivery_claim = validate_localized_delivery_for_output(
                                root=root,
                                source=source,
                                safe_mod_name=safe_mod_name,
                                output_file=file_path,
                                source_relative=source_relative,
                                context=context,
                            )
                        except (OSError, ValueError) as exc:
                            warnings.append(
                                "Localized string-table output skipped because composite "
                                f"evidence is missing, stale, or blocked: {relative_path(root, file_path)}; {exc}"
                            )
                            continue
                    else:
                        string_table_source = (
                            f"{relative_path(root, source)}::{source_relative.as_posix()}"
                        )
                        string_table_source_sha256 = source_hash(
                            root,
                            string_table_source,
                        )
                elif not replaces_source and not (bsa_claim or ba2_claim):
                    warnings.append(
                        f"Tool output skipped because it does not replace an existing source file: {relative_path(root, file_path)}"
                    )
                    continue
                record = copy_file(file_path, overlay_root, output, root)
                record["ReplacesExistingFile"] = replaces_source
                if string_table_source:
                    record["StringTableSource"] = string_table_source
                    record["StringTableSourceSha256"] = string_table_source_sha256
                if localized_delivery_claim:
                    record.update(
                        {
                            "LocalizedDeliveryReceipt": localized_delivery_claim["receipt"],
                            "LocalizedDeliveryReceiptSha256": localized_delivery_claim[
                                "receipt_sha256"
                            ],
                            "LocalizedPluginAnchor": localized_delivery_claim["plugin"],
                            "LocalizedPluginAnchorSha256": localized_delivery_claim[
                                "plugin_sha256"
                            ],
                            "ProvenanceTransform": "controlled-localized-delivery",
                            "ProvenanceTool": "BethesdaLocalizedDeliveryAdapter",
                        }
                    )
                if bsa_claim:
                    record["BsaProvenance"] = bsa_claim
                if ba2_claim:
                    record["Ba2Provenance"] = ba2_claim
                overlay_files.append(record)
                if record["ReplacesExistingFile"]:
                    replacement_files.append(record)
                else:
                    added_overlay_files.append(record)
                translation_files.append(str(record["Destination"]))
                binary_tool_overlay_files.append(str(record["Destination"]))

        if not overlay_files:
            warnings.append(
                f"No structured translation overlay files were found. Place Data-root overlay files under translated/final_mod/{safe_mod_name} or out/{safe_mod_name}/final_mod_overlay."
            )
    else:
        warnings.append("OverlayTranslatedFiles=false; translation overlays were not applied.")

    output = _publish_staged_build(
        transaction,
        root,
        (copied_files, overlay_files, replacement_files, added_overlay_files),
        (source_binary_files, binary_tool_overlay_files, translation_files),
    )
    meta_dir = output / "meta"
    build_report_path = meta_dir / "build_report.md"

    write_text(
        meta_dir / "source_files.md",
        ["# Source Files", "", "## Original Files", ""]
        + [f"- {item['Source']} -> {item['Destination']}" for item in copied_files]
        + ["", "## Overlay Files", ""]
        + [f"- {item['Source']} -> {item['Destination']}" for item in overlay_files],
    )

    write_text(
        meta_dir / "qa_report.md",
        [
            "# Final Mod QA Report",
            "",
            "This file is generated during final_mod assembly.",
            "",
            "Post-build validation reports are written to the project QA directory, not back into final_mod, so validation remains read-only against this output directory.",
            "",
            "Required checks:",
            "",
            "- `qa/final_mod_validation.md` from `scripts/validate_final_mod.py`",
            "- `qa/<ModName>.pex_delivery_pre_build.md` and `qa/<ModName>.pex_delivery_post_build.md` from `scripts/audit_pex_delivery.py` when PEX files are replaced",
            "- PEX output verification reports from `scripts/verify_pex_output.py` when PEX files are replaced",
            "- Plugin output verification reports from `scripts/verify_plugin_output.py` when ESP/ESM/ESL files are replaced",
            "- Translation proofread reports from `scripts/proofread_translation.py` before binary writeback",
            "- Agent model review reports for semantic translation quality and over-translation risk",
            "",
            "Recommended command:",
            "",
            "```console",
            f"python .\\scripts\\validate_final_mod.py --final-mod-dir {output_value}",
            "```",
        ],
    )

    redistribution_notes_path = meta_dir / "redistribution_notes.md"
    write_text(
        redistribution_notes_path,
        [
            "# Redistribution Notes",
            "",
            "This final_mod output is generated for local review, MO2/Vortex local install testing, and manual packaging inside the current project workflow.",
            "",
            "Do not treat this directory as cleared for public redistribution by default.",
            "",
            "Before publishing a complete translated mod package, verify:",
            "",
            "- Original mod permissions allow redistribution of bundled assets and plugin files.",
            "- Required credits and license terms are documented.",
            "- Third-party assets included by the original mod can be redistributed.",
            "- Binary files copied from mod/ were copied unmodified unless a project-local tool output explicitly replaced them.",
            "- The packaged CHS archive does not include private game, MO2/Vortex, Steam, AppData, or Documents/My Games files.",
            "",
            "For private local testing, keep this directory inside out/<ModName>/汉化产出/final_mod/ and do not auto-install it into a real mod manager directory.",
        ],
    )
    if intermediate_dir.exists():
        remove_path_inside(intermediate_dir, localization_root)
    intermediate_entries, dictionary_manifest = copy_intermediate_outputs(root, safe_mod_name, intermediate_dir)
    if not dictionary_manifest.get("TranslatedEntryCount"):
        warnings.append(
            f"No translated source-to-target dictionary entries were found under {dictionary_manifest['DictionaryDir']}."
        )
    provenance_path = meta_dir / "provenance.jsonl"
    provenance_count = len(
        [item for item in output.rglob("*") if item.is_file() and item.resolve(strict=False) != provenance_path.resolve(strict=False)]
    )
    if not (meta_dir / "manifest.json").is_file():
        provenance_count += 1
    provenance_count += 1

    effective_source_binary_files = [item for item in source_binary_files if item not in set(binary_tool_overlay_files)]
    # The manifest is the validator contract for delivery mode, output layout,
    # and direct replacement evidence. Keep fields additive when possible.
    manifest = {
        **game_context_metadata(context),
        "ModName": args.mod_name,
        "BuildTime": datetime.now().isoformat(timespec="seconds"),
        "DeliveryMode": DELIVERY_MODE_COMPLETE if selected_delivery_mode == "complete" else DELIVERY_MODE_OVERLAY,
        "OutputLayout": "mod-root/localization-output/final_mod-intermediate-package",
        "LocalizationOutputDir": relative_path(root, localization_root),
        "IntermediateOutputDir": relative_path(root, intermediate_dir),
        "PackagedModPath": relative_path(root, package_path),
        "PackagedModNameSuffix": "CHS",
        "LanguagePatchOnly": False,
        "RequiresOriginalMod": selected_delivery_mode == "translation-overlay",
        "IncludesOriginalFiles": include_original_files,
        "ScaleExecutionReport": relative_path(root, scale_execution_path) if scale_execution_path is not None else "",
        "ScaleExecutionReportSha256": sha256_file(scale_execution_path) if scale_execution_path is not None else "",
        "ScaleLevel": str(scale_execution_payload.get("scale_level") or "") if scale_execution_payload else "",
        "SourceModDir": relative_path(root, source),
        "OutputDir": relative_path(root, output),
        "LocalTestingOutput": True,
        "PublicRedistributionCleared": False,
        "RedistributionNotes": relative_path(root, redistribution_notes_path),
        "ProvenancePath": relative_path(root, provenance_path),
        "ProvenanceEntryCount": provenance_count,
        "CopiedFiles": [item["Destination"] for item in copied_files],
        "OverlayFiles": [item["Destination"] for item in overlay_files],
        "ReplacementFilesApplied": [item["Destination"] for item in replacement_files],
        "AddedOverlayFiles": [item["Destination"] for item in added_overlay_files],
        "BinaryFilesCopiedUnmodified": effective_source_binary_files,
        "BinaryToolOutputsApplied": binary_tool_overlay_files,
        "TranslationFilesApplied": translation_files,
        "IntermediateOutputsMirrored": intermediate_entries,
        "TranslationTextDictionary": dictionary_manifest,
        "TranslationDictionaryEntryCount": dictionary_manifest.get("TranslatedEntryCount", 0),
        "BsaLooseOverrideClaims": len(bsa_claims),
        "Ba2LooseOverrideSidecar": ba2_claim_sidecar,
        "Ba2LooseOverrideClaims": len(ba2_claims),
        "SkippedArchiveFiles": skipped_archive_files,
        "Warnings": warnings,
    }
    (meta_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    final_lines = [
        "# Final Mod Build Report",
        "",
        f"- Game: {game_display_label(context)}",
        f"- Support level: {context.support_level}",
        f"- ModName: {args.mod_name}",
        f"- BuildTime: {manifest['BuildTime']}",
        f"- DeliveryMode: {manifest['DeliveryMode']}",
        f"- OutputLayout: {manifest['OutputLayout']}",
        f"- LocalizationOutputDir: {manifest['LocalizationOutputDir']}",
        f"- IntermediateOutputDir: {manifest['IntermediateOutputDir']}",
        f"- PackagedModPath: {manifest['PackagedModPath']}",
        f"- PackagedModNameSuffix: {manifest['PackagedModNameSuffix']}",
        f"- LanguagePatchOnly: {manifest['LanguagePatchOnly']}",
        f"- RequiresOriginalMod: {manifest['RequiresOriginalMod']}",
        f"- IncludesOriginalFiles: {manifest['IncludesOriginalFiles']}",
        f"- ScaleExecutionReport: {manifest['ScaleExecutionReport']}",
        f"- SourceModDir: {manifest['SourceModDir']}",
        f"- OutputDir: {manifest['OutputDir']}",
        f"- CopiedFiles: {len(copied_files)}",
        f"- OverlayFiles: {len(overlay_files)}",
        f"- ReplacementFilesApplied: {len(replacement_files)}",
        f"- AddedOverlayFiles: {len(added_overlay_files)}",
        f"- BinaryFilesCopiedUnmodified: {len(effective_source_binary_files)}",
        f"- BinaryToolOutputsApplied: {len(binary_tool_overlay_files)}",
        f"- TranslationFilesApplied: {len(translation_files)}",
        f"- TranslationDictionaryEntryCount: {manifest['TranslationDictionaryEntryCount']}",
        f"- BsaLooseOverrideClaims: {manifest['BsaLooseOverrideClaims']}",
        f"- Ba2LooseOverrideClaims: {manifest['Ba2LooseOverrideClaims']}",
        f"- SkippedArchiveFiles: {len(skipped_archive_files)}",
        f"- LocalTestingOutput: {manifest['LocalTestingOutput']}",
        f"- PublicRedistributionCleared: {manifest['PublicRedistributionCleared']}",
        f"- ProvenancePath: {manifest['ProvenancePath']}",
        f"- ProvenanceEntryCount: {manifest['ProvenanceEntryCount']}",
        "",
        "## Overlay Files",
        "",
    ]
    final_lines.extend([f"- {item['Source']} -> {item['Destination']}" for item in overlay_files] or ["No overlay files were applied."])
    final_lines.extend(["", "## Direct Replacement Files", ""])
    final_lines.extend([f"- {item['Source']} -> {item['Destination']}" for item in replacement_files] or ["No overlay files replaced existing source files."])
    final_lines.extend(["", "## Added Overlay Files", ""])
    final_lines.extend([f"- {item['Source']} -> {item['Destination']}" for item in added_overlay_files] or ["No overlay files were added as new paths."])
    final_lines.extend(["", "## Binary Files Copied Unmodified", ""])
    final_lines.extend([f"- {item}" for item in effective_source_binary_files] or ["No protected binary files were copied."])
    final_lines.extend(["", "## Binary Tool Outputs Applied", ""])
    final_lines.extend([f"- {item}" for item in binary_tool_overlay_files] or ["No binary tool outputs were applied."])
    final_lines.extend(["", "## Intermediate Outputs", ""])
    final_lines.extend([f"- {item}" for item in intermediate_entries] or ["No intermediate output directories were mirrored."])
    final_lines.extend(["", "## Provenance", ""])
    final_lines.extend(
        [
            f"- Path: {manifest['ProvenancePath']}",
            f"- Entries: {manifest['ProvenanceEntryCount']}",
            "- Each final_mod file is traced to its immediate project-local source, transform, tool, and SHA256 evidence.",
        ]
    )
    final_lines.extend(["", "## Packaged CHS Mod", ""])
    final_lines.extend(
        [
            f"- Path: {manifest['PackagedModPath']}",
            "- Generated after final_mod metadata is written.",
        ]
    )
    final_lines.extend(["", "## Warnings", ""])
    final_lines.extend([f"- {item}" for item in warnings] or ["No warnings."])
    final_lines.extend(["", "## Skipped Archive Files", ""])
    final_lines.extend([f"- {item}" for item in skipped_archive_files] or ["No archive files were skipped."])
    final_lines.extend(
        [
            "",
            "## Safety",
            "",
            "- No real game installation directory was accessed.",
            "- No real MO2/Vortex directory was accessed.",
            "- Protected binary files, if copied, were copied unmodified from project-local source.",
            "- Translation delivery defaults to replacing files at their original relative paths in final_mod, not relying on language patch sidecar files.",
            "- The build was compressed only into the project-local CHS package path and was not installed.",
            "- Public redistribution is not cleared by default; see meta/redistribution_notes.md.",
        ]
    )
    write_text(build_report_path, final_lines)
    actual_provenance_count = write_provenance_jsonl(
        root, output, provenance_path, copied_files, overlay_files, safe_mod_name, context
    )
    if actual_provenance_count != provenance_count:
        warnings.append(f"Provenance entry count changed during write: expected={provenance_count} actual={actual_provenance_count}")
    package_info = create_package(output, package_path, root)
    write_text(
        package_report_path,
        [
            "# Packaged CHS Mod Report",
            "",
            f"- ModName: {args.mod_name}",
            f"- PackagePath: {package_info['Path']}",
            "- PackageNameSuffix: CHS",
            f"- Entries: {package_info['Entries']}",
            f"- SizeBytes: {package_info['SizeBytes']}",
            f"- SourceFinalMod: {relative_path(root, output)}",
        ],
    )

    print(f"Final mod built: {output}")
    print(f"Copied files: {len(copied_files)}")
    print(f"Overlay files: {len(overlay_files)}")
    print(f"Manifest: {meta_dir / 'manifest.json'}")
    print(f"Build report: {build_report_path}")
    print(f"Provenance: {provenance_path}")
    print(f"Intermediate outputs: {intermediate_dir}")
    print(f"Packaged CHS mod: {package_info['Path']}")
    print(f"Package report: {package_report_path}")
    print(f"Redistribution notes: {redistribution_notes_path}")
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")
    transaction.commit()
    _ACTIVE_BUILD_TRANSACTION = None
    return 0


def main() -> int:
    global _ACTIVE_BUILD_TRANSACTION
    try:
        return _main_impl()
    except BaseException:
        if _ACTIVE_BUILD_TRANSACTION is not None:
            _ACTIVE_BUILD_TRANSACTION.rollback()
            _ACTIVE_BUILD_TRANSACTION = None
        raise


if __name__ == "__main__":
    raise SystemExit(main())
