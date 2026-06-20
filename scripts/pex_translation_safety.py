"""Safety helpers for PEX translation writeback rows."""

from __future__ import annotations

import json
from pathlib import Path


LOGIC_COMPARE_OPCODE_PREFIXES = ("CMP_",)
SOURCE_FIELDS = ("Source", "source", "original", "text")
TARGET_FIELDS = ("Result", "result", "Target", "target", "translation")
PROTECTED_RISKS = {
    "blocked",
    "logic",
    "manual",
    "protected",
    "protected-logic",
    "review",
    "unsafe",
}


def row_value(row: dict, *names: str) -> str:
    fallback = ""
    for name in names:
        if name in row and row[name] is not None:
            value = str(row[name])
            if not fallback:
                fallback = value
            if value.strip():
                return value
    return fallback


def pex_row_matches(row: dict, pex: Path) -> bool:
    pex_name = pex.name.lower()
    pex_stem = pex.stem.lower()
    direct_fields = ("ModName", "mod_name", "PexName", "pex_name", "OutputPex", "output_pex")
    path_fields = ("source_file", "SourceFile", "source_path", "SourcePath", "File", "file", "Path", "path")

    for field in direct_fields:
        value = row_value(row, field).strip()
        if not value:
            continue
        value_name = Path(value.replace("\\", "/")).name.lower()
        if value_name in {pex_name, pex_stem}:
            return True

    for field in path_fields:
        value = row_value(row, field).strip()
        if not value:
            continue
        value_path = Path(value.replace("\\", "/"))
        value_name = value_path.name.lower()
        value_stem = value_path.stem.lower()
        if value_name == pex_name or value_stem == pex_stem:
            return True

    return False


def pex_translation_skip_reason(row: dict) -> str:
    source = row_value(row, *SOURCE_FIELDS)
    target = row_value(row, *TARGET_FIELDS)
    risk = row_value(row, "risk", "Risk").strip().lower()
    opcode = row_value(row, "opcode", "Opcode", "op", "Op").strip().upper()

    if not source.strip():
        return "missing source"
    if not target.strip():
        return "missing target"
    if source == target:
        return "source equals target"
    if risk in PROTECTED_RISKS:
        return f"protected risk: {risk}"
    if any(opcode.startswith(prefix) for prefix in LOGIC_COMPARE_OPCODE_PREFIXES):
        return f"logic compare opcode: {opcode}"
    return ""


def pex_translation_row_protects_source(row: dict) -> bool:
    risk = row_value(row, "risk", "Risk").strip().lower()
    opcode = row_value(row, "opcode", "Opcode", "op", "Op").strip().upper()
    if risk in PROTECTED_RISKS:
        return True
    return any(opcode.startswith(prefix) for prefix in LOGIC_COMPARE_OPCODE_PREFIXES)


def normalized_pex_translation_line(row: dict, pex: Path, fallback_line: str) -> str:
    normalized = dict(row)
    normalized["ModName"] = pex.name
    normalized["Source"] = row_value(row, *SOURCE_FIELDS)
    normalized["Result"] = row_value(row, *TARGET_FIELDS)
    try:
        return json.dumps(normalized, ensure_ascii=False)
    except TypeError:
        return fallback_line


def pex_translation_row_is_writable(row: dict) -> bool:
    return pex_translation_skip_reason(row) == ""
