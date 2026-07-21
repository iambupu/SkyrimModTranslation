"""Inspect normalized translation dictionary evidence consistently."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from file_utils import validate_regular_path_under
from project_paths import intermediate_output_dir


@dataclass(frozen=True)
class TranslationDictionaryInspection:
    directory: Path
    manifest_path: Path
    dictionary_path: Path
    directory_exists: bool
    manifest_exists: bool
    manifest_valid: bool
    manifest_entries: int
    manifest_entries_valid: bool
    source_files: int
    source_files_valid: bool
    dictionary_exists: bool
    line_count: int
    invalid_rows: int
    translated_rows: int


def _nonnegative_int(payload: dict[str, Any], key: str) -> tuple[int, bool]:
    try:
        value = int(payload.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0, False
    return value, value >= 0


def inspect_translation_dictionary(root: Path, mod_name: str) -> TranslationDictionaryInspection:
    directory = intermediate_output_dir(root, mod_name) / "translation_text_dictionary"
    manifest_path = directory / "manifest.json"
    dictionary_path = directory / "translation_dictionary.jsonl"

    directory_exists = directory.exists()
    if directory_exists:
        directory = validate_regular_path_under(
            directory,
            root,
            kind="directory",
            label="Translation dictionary directory",
        )
        manifest_path = directory / "manifest.json"
        dictionary_path = directory / "translation_dictionary.jsonl"

    manifest_exists = manifest_path.exists()
    if manifest_exists:
        manifest_path = validate_regular_path_under(
            manifest_path,
            directory,
            kind="file",
            label="Translation dictionary manifest",
        )

    dictionary_exists = dictionary_path.exists()
    if dictionary_exists:
        dictionary_path = validate_regular_path_under(
            dictionary_path,
            directory,
            kind="file",
            label="Translation dictionary JSONL",
        )

    manifest: dict[str, Any] = {}
    manifest_valid = False
    if manifest_exists:
        try:
            candidate = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if isinstance(candidate, dict):
                manifest = candidate
                manifest_valid = True
        except (OSError, json.JSONDecodeError):
            pass

    manifest_entries, manifest_entries_valid = _nonnegative_int(manifest, "TranslatedEntryCount")
    source_files, source_files_valid = _nonnegative_int(manifest, "SourceFileCount")

    line_count = 0
    invalid_rows = 0
    translated_rows = 0
    if dictionary_exists:
        for line in dictionary_path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            line_count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_rows += 1
                continue
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            source = str(row.get("source", "")).strip()
            target = str(row.get("target", "")).strip()
            if not source or not target:
                invalid_rows += 1
            elif source != target:
                translated_rows += 1

    return TranslationDictionaryInspection(
        directory=directory,
        manifest_path=manifest_path,
        dictionary_path=dictionary_path,
        directory_exists=directory_exists,
        manifest_exists=manifest_exists,
        manifest_valid=manifest_valid,
        manifest_entries=manifest_entries,
        manifest_entries_valid=manifest_entries_valid,
        source_files=source_files,
        source_files_valid=source_files_valid,
        dictionary_exists=dictionary_exists,
        line_count=line_count,
        invalid_rows=invalid_rows,
        translated_rows=translated_rows,
    )
