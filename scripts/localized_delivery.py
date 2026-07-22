"""Deterministic contracts for localized plugins and their string tables."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from adapter_result_io import read_adapter_result
from file_utils import (
    create_regular_directory_under,
    discover_regular_tree,
    is_reparse_point,
    sha256_file,
    validate_regular_path_under,
)
from project_paths import is_under, relative_path


ADAPTER_ID = "bethesda-localized-delivery"
RECEIPT_SCHEMA_VERSION = 3
REFERENCE_SCHEMA_VERSION = 1
STRING_TABLE_SCHEMA_VERSION = 2
TABLE_TYPES = ("strings", "dlstrings", "ilstrings")
TABLE_EXTENSIONS = frozenset(f".{value}" for value in TABLE_TYPES)


@dataclass(frozen=True)
class LocalizedReference:
    game_id: str
    plugin: str
    mod_key: str
    record_type: str
    form_id: str
    owner_mod_key: str
    local_id: int
    master_style: str
    master_style_evidence: str
    field_path: str
    subrecord_type: str
    occurrence_index: int
    table_type: str
    string_id: int

    @property
    def occurrence_key(self) -> tuple[object, ...]:
        return (
            self.owner_mod_key.casefold(),
            self.local_id,
            self.record_type,
            self.field_path,
            self.subrecord_type,
            self.occurrence_index,
        )


@dataclass(frozen=True)
class LocalizedTableComponent:
    table_type: str
    source_path: Path
    output_path: Path
    export_jsonl: Path
    translation_jsonl: Path
    apply_result: Path
    verify_result: Path


@dataclass(frozen=True)
class LocalizedCoverage:
    reference_count: int
    resolved_count: int
    referenced_ids: Mapping[str, tuple[int, ...]]
    translated_ids: Mapping[str, tuple[int, ...]]
    missing: tuple[dict[str, object], ...]

    @property
    def passed(self) -> bool:
        return not self.missing and self.reference_count == self.resolved_count

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "status": "passed" if self.passed else "blocked",
            "reference_count": self.reference_count,
            "resolved_count": self.resolved_count,
            "referenced_ids": {
                table_type: list(values)
                for table_type, values in sorted(self.referenced_ids.items())
            },
            "translated_ids": {
                table_type: list(values)
                for table_type, values in sorted(self.translated_ids.items())
            },
            "missing": list(self.missing),
        }


class LocalizedPublicationTransaction:
    """Publish a small set of localized files with rollback on failure."""

    def __init__(self, root: Path, mod_name: str) -> None:
        self._root = root.resolve(strict=True)
        self._transaction_root = (
            self._root
            / "work"
            / "localized_delivery_transactions"
            / mod_name
            / uuid.uuid4().hex
        )
        if not is_under(self._transaction_root, self._root):
            raise ValueError("Localized transaction path escapes the workspace")
        self._backup_root = self._transaction_root / "backups"
        self._protected: dict[Path, Path | None] = {}
        self._committed = False

    def protect(self, path: Path) -> Path:
        destination = Path(os.path.abspath(path))
        try:
            relative_destination = destination.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"Localized publication escapes the workspace: {path}")
        if destination in self._protected:
            return destination

        backup: Path | None = None
        if os.path.lexists(destination):
            validate_regular_path_under(
                destination,
                self._root,
                kind="file",
                label="Localized publication target",
            )
            backup = self._backup_root / relative_destination
            create_regular_directory_under(
                backup.parent,
                self._root,
                label="Localized publication backup directory",
            )
            if os.path.lexists(backup):
                raise ValueError(
                    f"Localized publication backup already exists: {backup}"
                )
            os.replace(destination, backup)
        else:
            create_regular_directory_under(
                destination.parent,
                self._root,
                label="Localized publication target directory",
            )
        self._protected[destination] = backup
        return destination

    def publish(self, staged: Path, destination: Path) -> Path:
        source = validate_regular_path_under(
            staged,
            self._root,
            kind="file",
            label="Staged localized file",
        )
        target = self.protect(destination)
        os.replace(source, target)
        return target

    def commit(self) -> None:
        self._committed = True
        self._cleanup()

    def rollback(self) -> None:
        for destination, backup in reversed(tuple(self._protected.items())):
            if os.path.lexists(destination):
                validate_regular_path_under(
                    destination,
                    self._root,
                    kind="file",
                    label="Localized rollback target",
                )
                destination.unlink()
            if backup is not None and os.path.lexists(backup):
                validate_regular_path_under(
                    backup,
                    self._root,
                    kind="file",
                    label="Localized rollback backup",
                )
                create_regular_directory_under(
                    destination.parent,
                    self._root,
                    label="Localized rollback target directory",
                )
                os.replace(backup, destination)
        self._cleanup()

    def _cleanup(self) -> None:
        if not os.path.lexists(self._transaction_root):
            return
        validate_regular_path_under(
            self._transaction_root,
            self._root,
            kind="directory",
            label="Localized publication transaction cleanup",
        )
        discover_regular_tree(
            self._transaction_root,
            label="Localized publication transaction cleanup",
        )
        shutil.rmtree(self._transaction_root)

    def __enter__(self) -> "LocalizedPublicationTransaction":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self._committed:
            self.rollback()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row at {path}:{line_number} must be an object")
        rows.append(value)
    return rows


def _text(row: Mapping[str, Any], name: str, *, location: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} field {name} must be non-empty text")
    return value.strip()


def _uint32(row: Mapping[str, Any], name: str, *, location: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{location} field {name} must be a uint32")
    return value


def load_localized_references(
    path: Path,
    *,
    game_id: str,
    plugin_name: str,
) -> tuple[LocalizedReference, ...]:
    references: list[LocalizedReference] = []
    occurrence_keys: set[tuple[object, ...]] = set()
    for index, row in enumerate(_read_jsonl(path), start=1):
        location = f"localized reference row {index}"
        if row.get("schema_version") != REFERENCE_SCHEMA_VERSION:
            raise ValueError(f"{location} has unsupported schema_version")
        if _text(row, "game_id", location=location) != game_id:
            raise ValueError(f"{location} game_id does not match the active workspace")
        if _text(row, "plugin", location=location).casefold() != plugin_name.casefold():
            raise ValueError(f"{location} plugin does not match the bound plugin")
        if row.get("localized_flag") is not True:
            raise ValueError(f"{location} does not prove the localized header flag")
        table_type = _text(row, "table_type", location=location).lower()
        if table_type not in TABLE_TYPES:
            raise ValueError(f"{location} has unsupported table_type {table_type!r}")
        master_style = _text(row, "master_style", location=location).lower()
        if master_style not in {"full", "light", "unknown"}:
            raise ValueError(f"{location} has unsupported master_style {master_style!r}")
        master_style_evidence = _text(
            row,
            "master_style_evidence",
            location=location,
        )
        if (
            master_style == "unknown"
            and master_style_evidence != "unresolved:unseparated-master-order"
        ):
            raise ValueError(
                f"{location} has invalid unresolved master-style evidence"
            )
        form_id = _text(row, "form_id", location=location).upper()
        if len(form_id) != 8 or any(value not in "0123456789ABCDEF" for value in form_id):
            raise ValueError(f"{location} form_id must contain eight hexadecimal digits")
        occurrence = row.get("occurrence_index")
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
            raise ValueError(f"{location} occurrence_index must be a non-negative integer")
        reference = LocalizedReference(
            game_id=game_id,
            plugin=plugin_name,
            mod_key=_text(row, "mod_key", location=location),
            record_type=_text(row, "record_type", location=location),
            form_id=form_id,
            owner_mod_key=_text(row, "owner_mod_key", location=location),
            local_id=_uint32(row, "local_id", location=location),
            master_style=master_style,
            master_style_evidence=master_style_evidence,
            field_path=_text(row, "field_path", location=location),
            subrecord_type=_text(row, "subrecord_type", location=location),
            occurrence_index=occurrence,
            table_type=table_type,
            string_id=_uint32(row, "string_id", location=location),
        )
        if reference.master_style == "light" and reference.local_id > 0xFFF:
            raise ValueError(f"{location} light local_id exceeds 12 bits")
        if reference.occurrence_key in occurrence_keys:
            raise ValueError(f"{location} duplicates a localized field occurrence")
        occurrence_keys.add(reference.occurrence_key)
        references.append(reference)
    return tuple(references)


def _casefold_children(directory: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not directory.is_dir():
        return result
    directory = validate_regular_path_under(
        directory,
        directory,
        kind="directory",
        label="Localized table directory",
    )
    with os.scandir(directory) as entries:
        children = sorted(entries, key=lambda item: item.name.casefold())
    for entry in children:
        entry_stat = entry.stat(follow_symlinks=False)
        child = Path(entry.path)
        if entry.is_symlink() or is_reparse_point(entry_stat):
            raise ValueError(f"Localized table directory contains a link-like entry: {child}")
        if entry.is_dir(follow_symlinks=False):
            child = validate_regular_path_under(
                child,
                directory,
                kind="directory",
                label="Localized table directory entry",
            )
        elif entry.is_file(follow_symlinks=False):
            child = validate_regular_path_under(
                child,
                directory,
                kind="file",
                label="Localized table file",
            )
        else:
            raise ValueError(f"Localized table directory contains a non-regular entry: {child}")
        key = child.name.casefold()
        if key in result:
            raise ValueError(
                f"Case-insensitive path collision under {directory}: "
                f"{result[key].name!r}, {child.name!r}"
            )
        result[key] = child
    return result


def _strings_directory(data_root: Path) -> Path | None:
    child = _casefold_children(data_root).get("strings")
    if child is None:
        return None
    if not child.is_dir():
        raise ValueError(f"Localized Strings path is not a directory: {child}")
    return child


def discover_localized_tables(
    *,
    data_root: Path,
    plugin_path: Path,
    source_language: str,
    target_language: str,
    mod_name: str,
    root: Path,
    required_types: Iterable[str],
) -> tuple[LocalizedTableComponent, ...]:
    resolved_root = data_root.resolve(strict=True)
    resolved_plugin = plugin_path.resolve(strict=True)
    if not is_under(resolved_plugin, resolved_root):
        raise ValueError("Localized plugin must stay inside the detected Mod Data root")
    if resolved_plugin.suffix.lower() not in {".esp", ".esm", ".esl"}:
        raise ValueError("Localized delivery requires an ESP, ESM, or ESL plugin")
    required = {value.lower() for value in required_types}
    if not required.issubset(TABLE_TYPES):
        raise ValueError("Localized references contain an unsupported table type")

    strings_dir = _strings_directory(resolved_root)
    children = _casefold_children(strings_dir) if strings_dir is not None else {}
    plugin_basename = resolved_plugin.stem
    components: list[LocalizedTableComponent] = []
    missing: list[str] = []
    for table_type in TABLE_TYPES:
        expected_name = f"{plugin_basename}_{source_language}.{table_type}"
        source = children.get(expected_name.casefold())
        if source is None:
            if table_type in required:
                related = sorted(
                    child.name
                    for child in children.values()
                    if child.is_file()
                    and child.suffix.lower() == f".{table_type}"
                    and child.stem.casefold().startswith(plugin_basename.casefold() + "_")
                )
                suffix = f"; found language candidates: {', '.join(related)}" if related else ""
                missing.append(expected_name + suffix)
            continue
        if not source.is_file():
            raise ValueError(f"Localized table component is not a file: {source}")
        output_name = f"{plugin_basename}_{target_language}.{table_type}"
        export_name = f"{expected_name}.jsonl"
        components.append(
            LocalizedTableComponent(
                table_type=table_type,
                source_path=source.resolve(strict=True),
                output_path=(root / "out" / mod_name / "tool_outputs" / "Strings" / output_name),
                export_jsonl=(
                    root
                    / "source"
                    / "localized_delivery"
                    / mod_name
                    / export_name
                ),
                translation_jsonl=(
                    root
                    / "translated"
                    / "string_tables"
                    / mod_name
                    / export_name
                ),
                apply_result=(
                    root
                    / "qa"
                    / "localized_delivery"
                    / mod_name
                    / "components"
                    / f"{expected_name}.apply.adapter-result.json"
                ),
                verify_result=(
                    root
                    / "qa"
                    / "localized_delivery"
                    / mod_name
                    / "components"
                    / f"{expected_name}.verify.adapter-result.json"
                ),
            )
        )
    if missing:
        raise ValueError("Missing localized source table(s): " + "; ".join(missing))
    return tuple(components)


def load_table_export_ids(
    path: Path,
    *,
    root: Path,
    game_id: str,
    plugin_basename: str,
    table_type: str,
    source_language: str,
    source_table: Path,
) -> frozenset[int]:
    expected_hash = sha256_file(source_table)
    expected_path = relative_path(root, source_table).replace("\\", "/")
    ids: set[int] = set()
    for index, row in enumerate(_read_jsonl(path), start=1):
        location = f"string-table export row {index}"
        if row.get("schema_version") != STRING_TABLE_SCHEMA_VERSION:
            raise ValueError(f"{location} has unsupported schema_version")
        expected = {
            "game_id": game_id,
            "plugin_basename": plugin_basename,
            "table_type": table_type,
            "source_language": source_language,
        }
        for name, value in expected.items():
            if _text(row, name, location=location).casefold() != value.casefold():
                raise ValueError(f"{location} {name} does not match its component")
        if (
            _text(row, "source_table_sha256", location=location).casefold()
            != expected_hash.casefold()
        ):
            raise ValueError(f"{location} source table hash is stale")
        if _text(row, "source_table_path", location=location).replace("\\", "/") != expected_path:
            raise ValueError(f"{location} source table path does not match its component")
        string_id = _uint32(row, "string_id", location=location)
        if string_id in ids:
            raise ValueError(f"{location} duplicates string_id {string_id}")
        ids.add(string_id)
    return frozenset(ids)


def load_table_translation_ids(
    path: Path,
    *,
    root: Path,
    game_id: str,
    plugin_basename: str,
    table_type: str,
    source_language: str,
    source_table: Path,
) -> frozenset[int]:
    """Return IDs whose translation is non-empty and differs from the source."""
    valid_ids = load_table_export_ids(
        path,
        root=root,
        game_id=game_id,
        plugin_basename=plugin_basename,
        table_type=table_type,
        source_language=source_language,
        source_table=source_table,
    )
    translated: set[int] = set()
    for index, row in enumerate(_read_jsonl(path), start=1):
        location = f"string-table translation row {index}"
        source = row.get("Source")
        result = row.get("Result")
        if not isinstance(source, str) or not isinstance(result, str):
            raise ValueError(f"{location} Source and Result must be strings")
        string_id = _uint32(row, "string_id", location=location)
        if string_id not in valid_ids:
            raise ValueError(f"{location} is not bound to the source table export")
        if result.strip() and result != source:
            translated.add(string_id)
    return frozenset(translated)


def build_localized_review_rows(
    *,
    root: Path,
    source_paths: Mapping[str, Path],
    translation_paths: Mapping[str, Path],
    coverage: LocalizedCoverage,
) -> list[dict[str, object]]:
    """Build canonical review rows from the bound translation snapshots."""
    expected_ids = {
        (table_type, string_id)
        for table_type in set(coverage.referenced_ids) | set(coverage.translated_ids)
        for string_id in (
            set(coverage.referenced_ids.get(table_type, ()))
            | set(coverage.translated_ids.get(table_type, ()))
        )
    }
    rows: list[dict[str, object]] = []
    actual_ids: set[tuple[str, int]] = set()
    for table_type in TABLE_TYPES:
        review_ids = {
            string_id
            for current_type, string_id in expected_ids
            if current_type == table_type
        }
        if not review_ids:
            continue
        source_path = source_paths.get(table_type)
        translation_path = translation_paths.get(table_type)
        if source_path is None or translation_path is None:
            raise ValueError(
                f"Localized review inputs are missing the {table_type} component"
            )
        for index, row in enumerate(_read_jsonl(translation_path), start=1):
            string_id = _uint32(
                row,
                "string_id",
                location=f"localized review source row {index}",
            )
            if string_id not in review_ids:
                continue
            source = row.get("Source")
            result = row.get("Result")
            if not isinstance(source, str) or not isinstance(result, str):
                raise ValueError(
                    f"Localized review source row {index} Source/Result must be strings"
                )
            identity = (table_type, string_id)
            if identity in actual_ids:
                raise ValueError("Localized review source contains duplicate identities")
            actual_ids.add(identity)
            rows.append(
                {
                    "schema_version": 1,
                    "file": relative_path(root, source_path).replace("\\", "/"),
                    "table_type": table_type,
                    "string_id": string_id,
                    "Source": source,
                    "Result": result,
                    "risk": "candidate",
                }
            )
    if actual_ids != expected_ids:
        raise ValueError(
            "Localized review input does not cover every referenced or changed string ID"
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row["file"]).casefold(),
            str(row["table_type"]),
            int(row["string_id"]),
        ),
    )


def verify_localized_reference_coverage(
    references: Iterable[LocalizedReference],
    table_ids: Mapping[str, Iterable[int]],
    translated_ids: Mapping[str, Iterable[int]] | None = None,
) -> LocalizedCoverage:
    normalized_ids = {
        table_type: frozenset(values)
        for table_type, values in table_ids.items()
    }
    translation_required = translated_ids is not None
    normalized_translated = (
        {
            table_type: frozenset(values)
            for table_type, values in translated_ids.items()
        }
        if translated_ids is not None
        else {}
    )
    referenced: dict[str, set[int]] = {table_type: set() for table_type in TABLE_TYPES}
    missing: list[dict[str, object]] = []
    resolved_count = 0
    reference_rows = tuple(references)
    for reference in reference_rows:
        referenced[reference.table_type].add(reference.string_id)
        source_present = reference.string_id in normalized_ids.get(
            reference.table_type, frozenset()
        )
        translated = reference.string_id in normalized_translated.get(
            reference.table_type, frozenset()
        )
        if source_present and (not translation_required or translated):
            resolved_count += 1
            continue
        missing.append(
            {
                "record_type": reference.record_type,
                "form_id": reference.form_id,
                "owner_mod_key": reference.owner_mod_key,
                "local_id": reference.local_id,
                "field_path": reference.field_path,
                "subrecord_type": reference.subrecord_type,
                "occurrence_index": reference.occurrence_index,
                "table_type": reference.table_type,
                "string_id": reference.string_id,
                "reason": (
                    "translation_missing_or_unchanged"
                    if source_present and translation_required
                    else "source_id_missing"
                ),
            }
        )
    return LocalizedCoverage(
        reference_count=len(reference_rows),
        resolved_count=resolved_count,
        referenced_ids={
            table_type: tuple(sorted(values))
            for table_type, values in referenced.items()
            if values
        },
        translated_ids={
            table_type: tuple(sorted(values))
            for table_type, values in normalized_translated.items()
            if values
        },
        missing=tuple(missing),
    )


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _bound_file(root: Path, path: Path) -> dict[str, str]:
    resolved = validate_regular_path_under(
        path,
        root,
        kind="file",
        label="Composite receipt file",
    )
    return {
        "path": relative_path(root, resolved).replace("\\", "/"),
        "sha256": sha256_file(resolved),
    }


def _validate_component_result_binding(
    root: Path,
    result_path: Path,
    *,
    expected_operation: str,
    expected_output: Mapping[str, object],
    expected_inputs: Iterable[Mapping[str, object]],
    mod_name: str,
) -> None:
    result = read_adapter_result(result_path)
    if (
        result.status != "success"
        or result.adapter_id != "bethesda-string-tables"
        or result.operation != expected_operation
        or result.mod_name != mod_name
    ):
        raise ValueError(
            "Localized component AdapterResult conflicts with its operation or Mod lane"
        )
    expected_path = str(expected_output["path"]).replace("\\", "/").casefold()
    expected_hash = str(expected_output["sha256"])
    if not any(
        artifact.path.replace("\\", "/").casefold() == expected_path
        and artifact.sha256 == expected_hash
        for artifact in result.artifacts
    ):
        raise ValueError(
            "Localized component AdapterResult does not bind its expected output"
        )
    expected_input_claims = {
        str(item["path"]).replace("\\", "/").casefold(): str(item["sha256"])
        for item in expected_inputs
    }
    actual_input_claims = {
        item.path.replace("\\", "/").casefold(): item.sha256
        for item in result.inputs
    }
    if (
        len(actual_input_claims) != len(result.inputs)
        or actual_input_claims != expected_input_claims
    ):
        raise ValueError(
            "Localized component AdapterResult input lineage does not match its source, "
            "translation, and apply receipt contract"
        )


def _component_input_bindings(
    source_table: Mapping[str, object],
    operation: str,
) -> tuple[Mapping[str, object], ...]:
    translation = source_table.get("translation")
    apply_result = source_table.get("apply_result")
    if not isinstance(translation, dict) or not isinstance(apply_result, dict):
        raise ValueError(
            "Localized source table must bind its translation JSONL and apply AdapterResult"
        )
    bindings: list[Mapping[str, object]] = [source_table, translation]
    if operation == "verify":
        bindings.append(apply_result)
    return tuple(bindings)


def _table_type_map(
    values: Iterable[Mapping[str, object]],
    *,
    label: str,
) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    for item in values:
        table_type = item.get("table_type")
        if not isinstance(table_type, str) or table_type not in TABLE_TYPES:
            raise ValueError(f"Localized composite receipt {label} has invalid table_type")
        if table_type in result:
            raise ValueError(
                f"Localized composite receipt {label} has conflicting {table_type} entries"
            )
        result[table_type] = item
    return result


def build_composite_receipt(
    *,
    root: Path,
    operation: str,
    game_id: str,
    mod_name: str,
    capability_level: str,
    plugin_path: Path,
    references_path: Path,
    references: tuple[LocalizedReference, ...],
    source_language: str,
    target_language: str,
    components: tuple[LocalizedTableComponent, ...],
    component_result_paths: Iterable[Path],
    coverage: LocalizedCoverage,
    coverage_report: Path,
    review_input: Path,
    evidence_input_hashes: Mapping[Path, str],
    capability_decisions: Mapping[str, Mapping[str, object]],
    verification_result_paths: Iterable[Path] = (),
    master_style_context: Path | None = None,
) -> dict[str, object]:
    if operation not in {"apply", "verify"}:
        raise ValueError("Composite receipts are only valid for apply or verify")
    if not coverage.passed:
        raise ValueError("Cannot create a successful composite receipt with missing references")
    if not references:
        raise ValueError("Localized delivery requires at least one in-scope reference")
    mod_keys = {reference.mod_key.casefold(): reference.mod_key for reference in references}
    if len(mod_keys) != 1:
        raise ValueError("Localized inventory contains multiple plugin ModKeys")
    if not components:
        raise ValueError("Localized delivery requires at least one string-table component")
    component_types = tuple(component.table_type for component in components)
    if len(set(component_types)) != len(component_types):
        raise ValueError("Localized delivery contains conflicting string-table components")
    result_paths = tuple(component_result_paths)
    if len(result_paths) != len(components):
        raise ValueError("Composite receipt requires one AdapterResult per string table")
    verification_paths = tuple(verification_result_paths)
    if operation == "apply" and len(verification_paths) != len(components):
        raise ValueError(
            "Apply composite receipt requires one verified result per string table"
        )
    if operation == "verify" and verification_paths:
        raise ValueError("Verify composite receipt contains conflicting verification results")

    expected_input_hashes = {
        path.resolve(strict=True): value.casefold()
        for path, value in evidence_input_hashes.items()
    }

    def bind_semantic_input(path: Path) -> dict[str, str]:
        resolved = path.resolve(strict=True)
        expected_hash = expected_input_hashes.get(resolved)
        if expected_hash is None:
            raise ValueError(
                f"Localized composite receipt is missing a captured input hash: {path}"
            )
        binding = _bound_file(root, path)
        if binding["sha256"] != expected_hash:
            raise ValueError(
                f"Localized evidence input changed after coverage: {path}"
            )
        return binding

    source_tables: list[dict[str, object]] = []
    output_tables: list[dict[str, object]] = []
    for component in components:
        source_tables.append(
            {
                "table_type": component.table_type,
                **bind_semantic_input(component.source_path),
                "export": bind_semantic_input(component.export_jsonl),
                "translation": bind_semantic_input(component.translation_jsonl),
                "apply_result": _bound_file(root, component.apply_result),
                "referenced_ids": list(coverage.referenced_ids.get(component.table_type, ())),
                "translated_ids": list(coverage.translated_ids.get(component.table_type, ())),
            }
        )
        if operation in {"apply", "verify"}:
            output_tables.append(
                {
                    "table_type": component.table_type,
                    **_bound_file(root, component.output_path),
                }
            )

    output_by_type = _table_type_map(output_tables, label="output_tables")
    source_by_type = _table_type_map(source_tables, label="source_tables")
    component_results: list[dict[str, object]] = []
    for component, path in zip(components, result_paths, strict=True):
        _validate_component_result_binding(
            root,
            path,
            expected_operation=operation,
            expected_output=output_by_type[component.table_type],
            expected_inputs=_component_input_bindings(
                source_by_type[component.table_type],
                operation,
            ),
            mod_name=mod_name,
        )
        component_results.append(
            {"table_type": component.table_type, **_bound_file(root, path)}
        )
    verification_results: list[dict[str, object]] = []
    for component, path in zip(components, verification_paths):
        _validate_component_result_binding(
            root,
            path,
            expected_operation="verify",
            expected_output=output_by_type[component.table_type],
            expected_inputs=_component_input_bindings(
                source_by_type[component.table_type],
                "verify",
            ),
            mod_name=mod_name,
        )
        verification_results.append(
            {"table_type": component.table_type, **_bound_file(root, path)}
        )

    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "adapter_id": ADAPTER_ID,
        "operation": operation,
        "status": "success",
        "game_id": game_id,
        "mod_name": mod_name,
        "capability_level": capability_level,
        "plugin": {
            **bind_semantic_input(plugin_path),
            "basename": plugin_path.stem,
            "file_name": plugin_path.name,
            "mod_key": next(iter(mod_keys.values())),
            "localized": True,
            "identity_role": "original-copy",
        },
        "languages": {
            "source": source_language,
            "target": target_language,
        },
        "references": {
            **bind_semantic_input(references_path),
            "count": len(references),
            "ids_by_table": {
                key: list(value)
                for key, value in sorted(coverage.referenced_ids.items())
            },
        },
        "source_tables": source_tables,
        "output_tables": output_tables,
        "component_adapter_results": component_results,
        "component_verification_results": verification_results,
        "coverage": {
            **coverage.payload(),
            "report": _bound_file(root, coverage_report),
        },
        "review_input": _bound_file(root, review_input),
        "capability_decisions": {
            name: dict(value) for name, value in sorted(capability_decisions.items())
        },
    }
    if master_style_context is not None:
        payload["master_style_context"] = _bound_file(root, master_style_context)
    return payload


def _validate_composite_receipt_semantics(
    root: Path,
    payload: Mapping[str, object],
    source_tables: Mapping[str, Mapping[str, object]],
) -> None:
    game_id = payload.get("game_id")
    plugin = payload.get("plugin")
    references_binding = payload.get("references")
    languages = payload.get("languages")
    coverage_payload = payload.get("coverage")
    review_binding = payload.get("review_input")
    if (
        not isinstance(game_id, str)
        or not isinstance(plugin, dict)
        or not isinstance(references_binding, dict)
        or not isinstance(languages, dict)
        or not isinstance(coverage_payload, dict)
        or not isinstance(review_binding, dict)
    ):
        raise ValueError("Localized composite receipt semantic bindings are invalid")
    plugin_name = plugin.get("file_name")
    plugin_basename = plugin.get("basename")
    source_language = languages.get("source")
    if not all(
        isinstance(value, str) and value
        for value in (plugin_name, plugin_basename, source_language)
    ):
        raise ValueError("Localized composite receipt identity is incomplete")

    references_path = root / str(references_binding["path"]).replace("/", os.sep)
    references = load_localized_references(
        references_path,
        game_id=game_id,
        plugin_name=plugin_name,
    )
    table_ids: dict[str, frozenset[int]] = {}
    translated_ids: dict[str, frozenset[int]] = {}
    source_paths: dict[str, Path] = {}
    translation_paths: dict[str, Path] = {}
    for table_type, item in source_tables.items():
        source_path = root / str(item["path"]).replace("/", os.sep)
        export = item.get("export")
        translation = item.get("translation")
        if not isinstance(export, dict) or not isinstance(translation, dict):
            raise ValueError("Localized source table semantic inputs are incomplete")
        export_path = root / str(export["path"]).replace("/", os.sep)
        translation_path = root / str(translation["path"]).replace("/", os.sep)
        source_paths[table_type] = source_path
        translation_paths[table_type] = translation_path
        table_ids[table_type] = load_table_export_ids(
            export_path,
            root=root,
            game_id=game_id,
            plugin_basename=plugin_basename,
            table_type=table_type,
            source_language=source_language,
            source_table=source_path,
        )
        translated_ids[table_type] = load_table_translation_ids(
            translation_path,
            root=root,
            game_id=game_id,
            plugin_basename=plugin_basename,
            table_type=table_type,
            source_language=source_language,
            source_table=source_path,
        )
    recomputed = verify_localized_reference_coverage(
        references,
        table_ids,
        translated_ids,
    )
    reported_coverage = {
        key: value
        for key, value in coverage_payload.items()
        if key != "report"
    }
    if recomputed.payload() != reported_coverage:
        raise ValueError(
            "Localized composite receipt coverage does not match its bound inputs"
        )

    expected_reference_summary = {
        "count": len(references),
        "ids_by_table": {
            key: list(value)
            for key, value in sorted(recomputed.referenced_ids.items())
        },
    }
    actual_reference_summary = {
        "count": references_binding.get("count"),
        "ids_by_table": references_binding.get("ids_by_table"),
    }
    if actual_reference_summary != expected_reference_summary:
        raise ValueError(
            "Localized composite receipt reference summary does not match its inputs"
        )

    for table_type, item in source_tables.items():
        expected_table_summary = {
            "referenced_ids": list(recomputed.referenced_ids.get(table_type, ())),
            "translated_ids": list(recomputed.translated_ids.get(table_type, ())),
        }
        actual_table_summary = {
            "referenced_ids": item.get("referenced_ids"),
            "translated_ids": item.get("translated_ids"),
        }
        if actual_table_summary != expected_table_summary:
            raise ValueError(
                "Localized composite receipt source table summary does not match "
                f"its inputs: {table_type}"
            )

    report_binding = coverage_payload.get("report")
    if not isinstance(report_binding, dict):
        raise ValueError("Localized composite receipt coverage report is invalid")
    report_path = root / str(report_binding.get("path", "")).replace("/", os.sep)
    try:
        report_payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError("Localized coverage report is invalid JSON") from exc
    if report_payload != recomputed.payload():
        raise ValueError(
            "Localized coverage report does not match the bound semantic inputs"
        )

    review_path = root / str(review_binding["path"]).replace("/", os.sep)
    expected_review_rows = build_localized_review_rows(
        root=root,
        source_paths=source_paths,
        translation_paths=translation_paths,
        coverage=recomputed,
    )
    if _read_jsonl(review_path) != expected_review_rows:
        raise ValueError(
            "Localized review input does not match its bound translation snapshots"
        )


def validate_composite_receipt(root: Path, receipt_path: Path) -> dict[str, object]:
    receipt_path = validate_regular_path_under(
        receipt_path,
        root,
        kind="file",
        label="Localized composite receipt",
    )
    payload = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Localized composite receipt must contain an object")
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("Localized composite receipt schema_version is unsupported")
    if payload.get("adapter_id") != ADAPTER_ID or payload.get("status") != "success":
        raise ValueError("Localized composite receipt is not successful")
    if payload.get("operation") not in {"apply", "verify"}:
        raise ValueError("Localized composite receipt operation is invalid")

    bound_files: list[Mapping[str, object]] = []
    for name in (
        "plugin",
        "references",
        "master_style_context",
        "review_input",
    ):
        value = payload.get(name)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"Localized composite receipt {name} must be an object")
            bound_files.append(value)
        elif name == "review_input":
            raise ValueError(
                "Localized composite receipt does not bind its review input"
            )
    for name in (
        "source_tables",
        "output_tables",
        "component_adapter_results",
        "component_verification_results",
    ):
        values = payload.get(name)
        if values is None and name == "component_verification_results":
            values = []
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise ValueError(f"Localized composite receipt {name} must contain objects")
        bound_files.extend(values)
        if name == "source_tables":
            for value in values:
                export = value.get("export")
                if not isinstance(export, dict):
                    raise ValueError("Localized source table must bind its export JSONL")
                bound_files.append(export)
                translation = value.get("translation")
                apply_result = value.get("apply_result")
                if not isinstance(translation, dict) or not isinstance(apply_result, dict):
                    raise ValueError(
                        "Localized source table must bind its translation JSONL and apply AdapterResult"
                    )
                bound_files.extend((translation, apply_result))
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("status") != "passed":
        raise ValueError("Localized composite receipt coverage is not passed")
    report = coverage.get("report")
    if not isinstance(report, dict):
        raise ValueError("Localized composite receipt does not bind a coverage report")
    bound_files.append(report)

    resolved_root = root.resolve(strict=True)
    for item in bound_files:
        raw_path = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("Localized composite receipt contains an empty path")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("Localized composite receipt contains an invalid SHA256")
        path = validate_regular_path_under(
            root / raw_path.replace("/", os.sep),
            resolved_root,
            kind="file",
            label="Localized composite receipt bound file",
        )
        if sha256_file(path) != expected_hash:
            raise ValueError(f"Localized composite receipt is stale: {raw_path}")

    source_tables = _table_type_map(payload["source_tables"], label="source_tables")
    output_tables = _table_type_map(payload["output_tables"], label="output_tables")
    component_results = _table_type_map(
        payload["component_adapter_results"],
        label="component_adapter_results",
    )
    verification_results = _table_type_map(
        payload.get("component_verification_results", []),
        label="component_verification_results",
    )
    _validate_composite_receipt_semantics(root, payload, source_tables)
    if set(source_tables) != set(output_tables):
        raise ValueError("Localized composite receipt has a partial table publication")
    if set(component_results) != set(output_tables):
        raise ValueError("Localized composite receipt has conflicting component receipts")
    expected_operation = payload["operation"]
    mod_name = payload.get("mod_name")
    if not isinstance(mod_name, str) or not mod_name:
        raise ValueError("Localized composite receipt mod_name is invalid")
    for table_type, source_table in source_tables.items():
        apply_result = source_table["apply_result"]
        result_path = root / str(apply_result["path"]).replace("/", os.sep)
        _validate_component_result_binding(
            root,
            result_path,
            expected_operation="apply",
            expected_output=output_tables[table_type],
            expected_inputs=_component_input_bindings(source_table, "apply"),
            mod_name=mod_name,
        )
    for table_type, item in component_results.items():
        result_path = root / str(item["path"]).replace("/", os.sep)
        _validate_component_result_binding(
            root,
            result_path,
            expected_operation=expected_operation,
            expected_output=output_tables[table_type],
            expected_inputs=_component_input_bindings(
                source_tables[table_type],
                expected_operation,
            ),
            mod_name=mod_name,
        )
    if payload["operation"] == "apply" and set(verification_results) != set(
        output_tables
    ):
        raise ValueError(
            "Localized apply receipt does not bind every component verification"
        )
    if payload["operation"] == "verify" and verification_results:
        raise ValueError("Localized verify receipt has conflicting verification results")
    for table_type, item in verification_results.items():
        result_path = root / str(item["path"]).replace("/", os.sep)
        _validate_component_result_binding(
            root,
            result_path,
            expected_operation="verify",
            expected_output=output_tables[table_type],
            expected_inputs=_component_input_bindings(
                source_tables[table_type],
                "verify",
            ),
            mod_name=mod_name,
        )
    return payload
