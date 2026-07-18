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
from file_utils import sha256_file
from project_paths import is_under, relative_path


ADAPTER_ID = "bethesda-localized-delivery"
RECEIPT_SCHEMA_VERSION = 1
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
    missing: tuple[dict[str, object], ...]

    @property
    def passed(self) -> bool:
        return not self.missing and self.reference_count == self.resolved_count

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "passed" if self.passed else "blocked",
            "reference_count": self.reference_count,
            "resolved_count": self.resolved_count,
            "referenced_ids": {
                table_type: list(values)
                for table_type, values in sorted(self.referenced_ids.items())
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
        destination = path.resolve(strict=False)
        if not is_under(destination, self._root):
            raise ValueError(f"Localized publication escapes the workspace: {path}")
        if destination in self._protected:
            return destination

        backup: Path | None = None
        if destination.exists():
            if not destination.is_file():
                raise ValueError(
                    f"Localized publication target is not a file: {destination}"
                )
            backup = self._backup_root / destination.relative_to(self._root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, backup)
        self._protected[destination] = backup
        return destination

    def publish(self, staged: Path, destination: Path) -> Path:
        source = staged.resolve(strict=True)
        if not source.is_file() or not is_under(source, self._root):
            raise ValueError(f"Invalid staged localized file: {staged}")
        target = self.protect(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return target

    def commit(self) -> None:
        self._committed = True
        self._cleanup()

    def rollback(self) -> None:
        for destination, backup in reversed(tuple(self._protected.items())):
            if destination.exists():
                if not destination.is_file():
                    raise ValueError(
                        f"Localized rollback target is not a file: {destination}"
                    )
                destination.unlink()
            if backup is not None and backup.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._transaction_root.exists():
            shutil.rmtree(self._transaction_root, ignore_errors=True)

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
        if master_style not in {"full", "light"}:
            raise ValueError(f"{location} has unsupported master_style {master_style!r}")
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
            master_style_evidence=_text(row, "master_style_evidence", location=location),
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
    for child in directory.iterdir():
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


def verify_localized_reference_coverage(
    references: Iterable[LocalizedReference],
    table_ids: Mapping[str, Iterable[int]],
) -> LocalizedCoverage:
    normalized_ids = {
        table_type: frozenset(values)
        for table_type, values in table_ids.items()
    }
    referenced: dict[str, set[int]] = {table_type: set() for table_type in TABLE_TYPES}
    missing: list[dict[str, object]] = []
    resolved_count = 0
    reference_rows = tuple(references)
    for reference in reference_rows:
        referenced[reference.table_type].add(reference.string_id)
        if reference.string_id in normalized_ids.get(reference.table_type, frozenset()):
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
    resolved = path.resolve(strict=True)
    if not is_under(resolved, root.resolve(strict=True)):
        raise ValueError(f"Composite receipt file is outside the workspace: {resolved}")
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

    source_tables: list[dict[str, object]] = []
    output_tables: list[dict[str, object]] = []
    for component in components:
        source_tables.append(
            {
                "table_type": component.table_type,
                **_bound_file(root, component.source_path),
                "export": _bound_file(root, component.export_jsonl),
                "referenced_ids": list(coverage.referenced_ids.get(component.table_type, ())),
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
    component_results: list[dict[str, object]] = []
    for component, path in zip(components, result_paths, strict=True):
        _validate_component_result_binding(
            root,
            path,
            expected_operation=operation,
            expected_output=output_by_type[component.table_type],
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
            **_bound_file(root, plugin_path),
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
            **_bound_file(root, references_path),
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
        "capability_decisions": {
            name: dict(value) for name, value in sorted(capability_decisions.items())
        },
    }
    if master_style_context is not None:
        payload["master_style_context"] = _bound_file(root, master_style_context)
    return payload


def validate_composite_receipt(root: Path, receipt_path: Path) -> dict[str, object]:
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
    for name in ("plugin", "references", "master_style_context"):
        value = payload.get(name)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"Localized composite receipt {name} must be an object")
            bound_files.append(value)
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
        path = (root / raw_path.replace("/", os.sep)).resolve(strict=True)
        if not is_under(path, resolved_root):
            raise ValueError("Localized composite receipt path escapes the workspace")
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
    if set(source_tables) != set(output_tables):
        raise ValueError("Localized composite receipt has a partial table publication")
    if set(component_results) != set(output_tables):
        raise ValueError("Localized composite receipt has conflicting component receipts")
    expected_operation = payload["operation"]
    mod_name = payload.get("mod_name")
    if not isinstance(mod_name, str) or not mod_name:
        raise ValueError("Localized composite receipt mod_name is invalid")
    for table_type, item in component_results.items():
        result_path = root / str(item["path"]).replace("/", os.sep)
        _validate_component_result_binding(
            root,
            result_path,
            expected_operation=expected_operation,
            expected_output=output_tables[table_type],
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
            mod_name=mod_name,
        )
    return payload
