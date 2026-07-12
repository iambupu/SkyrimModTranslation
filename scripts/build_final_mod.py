"""Build the release-shaped CHS output from project-local sources only.

The important invariant is direct replacement: final_mod is a complete Skyrim
Data-root copy with translated files overlaid at their original relative paths.
Sidecar dictionaries and XML/JSONL import files stay under intermediate/.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

from game_context import GameContext, game_context_metadata, game_display_label
from project_paths import final_mod_dir as default_final_mod_dir
from project_paths import intermediate_output_dir, localization_output_root, packaged_mod_path
from project_paths import find_data_root
from project_paths import project_root
from project_paths import risky_marker
from project_paths import safe_file_name
from new_ba2_archive_manifest import validate_archive_relative_path
from translation_input_discovery import collect_translation_input_files
from verify_ba2_extraction import verify_manifest as verify_ba2_manifest
from route_translation_task import current_game_context


BINARY_EXTENSIONS = {".esp", ".esm", ".esl", ".bsa", ".ba2", ".pex", ".dll", ".exe"}
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
BACKUP_EXTENSIONS = {".bak", ".backup", ".old", ".tmp"}
TRANSLATION_DICTIONARY_DIR_NAME = "translation_text_dictionary"
TRANSLATION_DICTIONARY_JSONL_EXTENSIONS = {".jsonl"}
BA2_LOOSE_OVERRIDE_SIDECAR = "ba2_loose_overrides.jsonl"
SOURCE_TEXT_KEYS = ("source", "Source", "original", "Original", "OriginalText", "原文")
TARGET_TEXT_KEYS = ("target", "Target", "Result", "Dest", "TranslatedText", "translation", "Translation", "译文")
CONTEXT_KEYS = ("plugin", "ModName", "file", "record_type", "subrecord_type", "form_id", "editor_id", "Type")
Ba2ManifestCache = dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]]


def is_under(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve(strict=False)
    parent_resolved = parent.resolve(strict=False)
    try:
        common = os.path.commonpath([str(child_resolved).lower(), str(parent_resolved).lower()])
    except ValueError:
        return False
    return common == str(parent_resolved).lower()


def resolve_project_path(root: Path, value: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not is_under(resolved, root):
        raise ValueError(f"path is outside project root: {value}")
    return resolved


def relative_path(root: Path, value: Path) -> str:
    try:
        return str(value.resolve(strict=False).relative_to(root.resolve(strict=True)))
    except ValueError:
        return str(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_backup_artifact(path: Path) -> bool:
    name = path.name
    suffix = path.suffix.lower()
    if suffix in BACKUP_EXTENSIONS:
        return True
    lowered = name.lower()
    return any(f".{ext[1:]}." in lowered for ext in BINARY_EXTENSIONS)


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


def is_interface_translation_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    return (
        path.suffix.lower() == ".txt"
        and len(parts) >= 3
        and parts[-3] == "interface"
        and parts[-2] == "translations"
    )


def is_profile_protected_path(path: Path, source_root: Path, context: GameContext) -> bool:
    relative = path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    return bool(relative.parts and relative.parts[0].lower() in context.protected_directories)


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


def normalize_interface_translation_file(path: Path) -> None:
    text = read_interface_translation_text(path)
    lines = []
    for line in text.splitlines():
        if "\t" not in line:
            match = re.match(r"^(\$[^\s]+)\s+(.+)$", line)
            if match:
                line = f"{match.group(1)}\t{match.group(2)}"
        lines.append(line)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))


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
    archive_relative = relative_path(project_root_path, archive_path)
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
            ba2_claim = record.get("Ba2Provenance") if isinstance(record.get("Ba2Provenance"), dict) else None
            source_value = str(record.get("Source", ""))
            if ba2_claim:
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
            if ba2_claim:
                row.update(
                    {
                        "archive_path": ba2_claim["ArchivePath"],
                        "archive_sha256": ba2_claim["ArchiveSha256"],
                        "archive_entry_path": ba2_claim["EntryPath"],
                        "archive_entry_sha256": ba2_claim["SourceSha256"],
                        "archive_manifest": ba2_claim["ManifestPath"],
                        "qa_evidence": [ba2_claim["ManifestPath"], "qa/final_mod_validation.md"],
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


def write_text(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ba2_manifest_cache_key(manifest_path: Path) -> str:
    return os.path.normcase(str(manifest_path.resolve(strict=True)))


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

        expected_overlay = (root / "translated" / "final_mod" / safe_mod_name / Path(*entry_path.split("/"))).resolve(strict=False)
        overlay_path = resolve_project_path(root, str(row["OverlayPath"]), must_exist=True)
        if overlay_path.resolve(strict=False) != expected_overlay:
            raise ValueError(
                f"BA2 loose override sidecar line {line_number} has relative-path drift; "
                f"OverlayPath must equal translated/final_mod/{safe_mod_name}/{entry_path}"
            )
        if overlay_path.suffix.lower() in BINARY_EXTENSIONS or is_backup_artifact(overlay_path):
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
        matching_overlays: list[Path] = []
        for row in files:
            if not isinstance(row, dict):
                continue
            try:
                entry_path = validate_archive_relative_path(str(row.get("RelativePath") or ""))
            except ValueError:
                continue
            overlay = (root / "translated" / "final_mod" / safe_mod_name / Path(*entry_path.split("/"))).resolve(strict=False)
            if overlay.is_file():
                matching_overlays.append(overlay)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a project-local direct-replacement final_mod directory.")
    parser.add_argument("--mod-name", required=True)
    parser.add_argument("--source-mod-dir", default="mod")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--include-original-files", type=bool_value, default=True)
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

    if args.overlay_translated_files:
        require_translation_dictionary_entries(root, safe_mod_name)
    ba2_manifest_cache: Ba2ManifestCache = {}
    ba2_claims, ba2_claim_sidecar = load_ba2_loose_override_claims(root, safe_mod_name, ba2_manifest_cache)
    require_ba2_claims_for_matching_overlays(root, safe_mod_name, ba2_claims, ba2_manifest_cache)

    mod_out_root = resolve_project_path(root, f"out/{safe_mod_name}", must_exist=False)
    mod_out_root.mkdir(parents=True, exist_ok=True)
    localization_root = localization_output_root(root, safe_mod_name)
    output_value = args.output_dir or relative_path(root, default_final_mod_dir(root, safe_mod_name))
    output = resolve_project_path(root, output_value, must_exist=False)
    if not is_under(output, localization_root):
        raise ValueError(f"OutputDir must be under out/{safe_mod_name}/汉化产出/: {output_value}")
    if output.resolve(strict=False) == localization_root.resolve(strict=False):
        raise ValueError(f"OutputDir must be a child directory under out/{safe_mod_name}/汉化产出, not the localization output root itself.")

    if output.exists():
        existing = list(output.iterdir()) if output.is_dir() else [output]
        if existing and not args.force:
            raise ValueError(f"OutputDir already exists and is not empty. Re-run with --force to rebuild: {output}")
        if args.force:
            if not is_under(output, mod_out_root):
                raise ValueError(f"Refusing to remove path outside out/{safe_mod_name}/: {output}")
            remove_path_inside(output, mod_out_root)
    output.mkdir(parents=True, exist_ok=True)
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

    if args.include_original_files:
        # Start from a clean project-local source copy. Archives inside the Mod
        # are skipped because nested deliverables are not valid Skyrim Data
        # files and often hide unreviewed content.
        if source.is_dir():
            for file_path in sorted(item for item in source.rglob("*") if item.is_file() and item.name != ".gitkeep"):
                suffix = file_path.suffix.lower()
                if suffix in ARCHIVE_EXTENSIONS:
                    skipped_archive_files.append(relative_path(root, file_path))
                    continue
                record = copy_file(file_path, source, output, root)
                destination = resolve_project_path(root, str(record["Destination"]), must_exist=True)
                if is_interface_translation_path(destination.relative_to(output.resolve(strict=True))):
                    normalize_interface_translation_file(destination)
                copied_files.append(record)
                if record["Extension"] in BINARY_EXTENSIONS:
                    source_binary_files.append(str(record["Destination"]))
        else:
            source_archive_sha256 = sha256_file(source)
            with zipfile.ZipFile(source, "r") as archive:
                for entry in archive.infolist():
                    if entry.is_dir() or not Path(entry.filename).name:
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
                        normalize_interface_translation_file(destination)
                    copied_files.append(record)
                    if record["Extension"] in BINARY_EXTENSIONS:
                        source_binary_files.append(str(record["Destination"]))
        if skipped_archive_files:
            warnings.append(f"Archive files were skipped and not copied into final_mod: {len(skipped_archive_files)}")
    else:
        warnings.append("IncludeOriginalFiles=false; source mod files were not copied.")

    build_report_path = meta_dir / "build_report.md"
    write_text(
        build_report_path,
        [
            "# Final Mod Build Report",
            "",
            f"- ModName: {args.mod_name}",
            f"- Build started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- SourceModDir: {source}",
            f"- OutputDir: {output}",
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
        # Text overlays may add or replace files, but protected binary outputs
        # are only accepted from tool_outputs and only when replacing an
        # existing source-path counterpart.
        for overlay_relative in overlay_roots:
            overlay_root = resolve_project_path(root, overlay_relative, must_exist=False)
            if not overlay_root.is_dir():
                continue
            for file_path in sorted(item for item in overlay_root.rglob("*") if item.is_file() and item.name != ".gitkeep"):
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
                record = copy_file(file_path, overlay_root, output, root)
                ba2_claim = ba2_claims.get(str(file_path.resolve(strict=True)).lower())
                if ba2_claim:
                    record["Ba2Provenance"] = ba2_claim
                destination = resolve_project_path(root, str(record["Destination"]), must_exist=True)
                if is_interface_translation_path(destination.relative_to(output.resolve(strict=True))):
                    normalize_interface_translation_file(destination)
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
                suffix = file_path.suffix.lower()
                if is_backup_artifact(file_path):
                    warnings.append(f"Backup/tool history artifact skipped: {relative_path(root, file_path)}")
                    continue
                if suffix in ARCHIVE_EXTENSIONS:
                    skipped_archive_files.append(relative_path(root, file_path))
                    continue
                if suffix in {".dll", ".exe"} or is_profile_protected_path(file_path, overlay_root, context):
                    warnings.append(f"Protected tool output skipped: {relative_path(root, file_path)}")
                    continue
                destination = destination_for(file_path, overlay_root, output)
                if suffix in BINARY_EXTENSIONS and not destination.is_file():
                    warnings.append(
                        f"Binary tool output skipped because it does not replace an existing source file: {relative_path(root, file_path)}"
                    )
                    continue
                record = copy_file(file_path, overlay_root, output, root)
                overlay_files.append(record)
                if record["ReplacesExistingFile"]:
                    replacement_files.append(record)
                else:
                    added_overlay_files.append(record)
                translation_files.append(str(record["Destination"]))
                if record["Extension"] in BINARY_EXTENSIONS:
                    binary_tool_overlay_files.append(str(record["Destination"]))

        if not overlay_files:
            warnings.append(
                f"No structured translation overlay files were found. Place Data-root overlay files under translated/final_mod/{safe_mod_name} or out/{safe_mod_name}/final_mod_overlay."
            )
    else:
        warnings.append("OverlayTranslatedFiles=false; translation overlays were not applied.")

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
    intermediate_dir = intermediate_output_dir(root, safe_mod_name)
    if intermediate_dir.exists():
        remove_path_inside(intermediate_dir, localization_root)
    intermediate_entries, dictionary_manifest = copy_intermediate_outputs(root, safe_mod_name, intermediate_dir)
    if not dictionary_manifest.get("TranslatedEntryCount"):
        warnings.append(
            f"No translated source-to-target dictionary entries were found under {dictionary_manifest['DictionaryDir']}."
        )
    package_path = packaged_mod_path(root, safe_mod_name)
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
        "DeliveryMode": "direct-replacement-final-mod",
        "OutputLayout": "mod-root/localization-output/final_mod-intermediate-package",
        "LocalizationOutputDir": relative_path(root, localization_root),
        "IntermediateOutputDir": relative_path(root, intermediate_dir),
        "PackagedModPath": relative_path(root, package_path),
        "PackagedModNameSuffix": "CHS",
        "LanguagePatchOnly": False,
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
            "- No real Skyrim directory was accessed.",
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
    package_report_path = localization_root / "package_report.md"
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
