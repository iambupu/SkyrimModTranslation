"""Safety helpers for PEX translation writeback rows."""

from __future__ import annotations

import json
import re
from pathlib import Path


LOGIC_COMPARE_OPCODE_PREFIXES = ("CMP_",)
SOURCE_FIELDS = ("Source", "source", "original", "text")
TARGET_FIELDS = ("Result", "result", "Target", "target", "translation")
CONTEXT_FIELDS = (
    "Context",
    "context",
    "object_name",
    "ObjectName",
    "state_name",
    "StateName",
    "function_name",
    "FunctionName",
    "event_name",
    "EventName",
    "property_name",
    "PropertyName",
    "instruction",
    "Instruction",
    "notes",
    "Notes",
    "reason",
    "Reason",
)
PROTECTED_RISKS = {
    "blocked",
    "context-review",
    "logic",
    "manual",
    "manual-review",
    "needs-context-review",
    "needs_context_review",
    "protected",
    "protected-logic",
    "protected-review",
    "review",
    "unsafe",
}
VISIBLE_CONFIRMATION_MARKERS = (
    "confirmed visible",
    "psc-confirmed",
    "visible mcm",
    "mcm visible",
    "mcm option",
    "option help",
    "option text",
    "option label",
    "menu label",
    "visible text",
)
VISIBLE_CONTEXT_PATTERN = re.compile(
    r"(?:\bmessagebox\b|\bnotification\b|\bdebug\.notification\b|\bshowmessage\b|\bshowmenu\b|\bmcm\b|\bmenu\b|\boption\b|\bpage\s+display\b)",
    re.IGNORECASE,
)
VISIBLE_CONFIRMATION_PATTERN = re.compile(
    "|".join(re.escape(marker).replace(r"\ ", r"\s+") for marker in VISIBLE_CONFIRMATION_MARKERS),
    re.IGNORECASE,
)
VISIBLE_CALL_PATTERN = re.compile(
    r"(?:\bmessagebox\b|\bnotification\b|\bdebug\.notification\b|\bshowmessage\b|\bshowmenu\b)",
    re.IGNORECASE,
)
TRACE_DEBUG_PREFIX = re.compile(r"^\s*(?:trace|debug|warn|warning|error|log|controller)\s*[:=]", re.IGNORECASE)
FILE_OR_PATH = re.compile(
    r"(?:[A-Za-z]:\\|[\\/]|[\w.-]+\.(?:esp|esm|esl|pex|psc|bsa|ba2|dll|exe|json|jsonl|xml|ini|txt|seq|swf|gfx)\b)",
    re.IGNORECASE,
)
KEY_LIKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
SCRIPT_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KEY_CONTEXT = re.compile(r"\b(?:key|page|state|event|property|script|plugin|file|path|config|storageutil|jsonutil)\b", re.IGNORECASE)


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


def row_context(row: dict) -> str:
    parts: list[str] = []
    for field in CONTEXT_FIELDS:
        value = row_value(row, field).strip()
        if value:
            parts.append(f"{field}={value}")
    opcode = row_value(row, "opcode", "Opcode", "op", "Op").strip()
    if opcode:
        parts.append(f"opcode={opcode}")
    return "; ".join(parts)


def pex_row_has_visible_context(row: dict) -> bool:
    context = row_context(row)
    if VISIBLE_CONTEXT_PATTERN.search(context):
        return True
    return bool(VISIBLE_CONFIRMATION_PATTERN.search(context))


def pex_row_has_confirmed_visible_context(row: dict) -> bool:
    context = row_context(row)
    if VISIBLE_CONFIRMATION_PATTERN.search(context):
        return True
    return bool(VISIBLE_CALL_PATTERN.search(context))


def pex_logic_protection_reason(row: dict) -> str:
    source = row_value(row, *SOURCE_FIELDS).strip()
    risk = row_value(row, "risk", "Risk").strip().lower()
    opcode = row_value(row, "opcode", "Opcode", "op", "Op").strip().upper()
    context = row_context(row)
    normalized_context = context.lower()

    if any(opcode.startswith(prefix) for prefix in LOGIC_COMPARE_OPCODE_PREFIXES):
        return f"logic compare opcode: {opcode}"
    if not source:
        return ""
    visibly_confirmed = pex_row_has_confirmed_visible_context(row)
    if risk in PROTECTED_RISKS and not (visibly_confirmed and risk in {"manual-review", "needs-context-review", "needs_context_review", "review"}):
        return f"protected risk: {risk}"
    if FILE_OR_PATH.search(source):
        return "file/path token"
    if TRACE_DEBUG_PREFIX.search(source):
        return "trace/debug prefix"
    if KEY_CONTEXT.search(context) and KEY_LIKE.fullmatch(source) and not visibly_confirmed:
        return "key-like PEX context"
    if SCRIPT_SYMBOL.fullmatch(source) and (
        "_" in source
        or re.search(r"[A-Z]", source[1:])
        or re.search(r"(?:Script|Quest|Alias|Event|State|Function|Property)$", source)
    ):
        if not visibly_confirmed and (
            "kind=pex" in normalized_context or ".pex" in normalized_context or "opcode=" in normalized_context or KEY_CONTEXT.search(context)
        ):
            return "script symbol"
    return ""


def pex_row_needs_context_review(row: dict) -> bool:
    """Return True for PEX rows that are not structurally protected or visibly safe.

    This deliberately avoids Mod-specific words. It only says that a row needs
    PSC/export context before it can be moved into either protected-logic or the
    writable translation queue.
    """
    source = row_value(row, *SOURCE_FIELDS).strip()
    if not source or pex_logic_protection_reason(row):
        return False
    context = row_context(row).lower()
    if not ("opcode=" in context or ".pex" in context or "kind=pex" in context):
        return False
    if pex_row_has_visible_context(row):
        return False
    if re.search(r"[A-Za-z]{3,}", source) and not re.search(r"[\u3400-\u9fff]", source):
        return True
    return False


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

    if not source.strip():
        return "missing source"
    protection_reason = pex_logic_protection_reason(row)
    if protection_reason:
        return protection_reason
    if not target.strip():
        return "missing target"
    if source == target:
        return "source equals target"
    if pex_row_needs_context_review(row):
        return "needs context review"
    return ""


def pex_translation_row_protects_source(row: dict) -> bool:
    return bool(pex_logic_protection_reason(row))


def normalized_pex_translation_line(row: dict, pex: Path, fallback_line: str) -> str:
    normalized = dict(row)
    normalized["ModName"] = pex.name
    normalized["Source"] = row_value(row, *SOURCE_FIELDS)
    normalized["Result"] = row_value(row, *TARGET_FIELDS)
    if pex_translation_skip_reason(normalized) == "":
        normalized["risk"] = "candidate"
    try:
        return json.dumps(normalized, ensure_ascii=False)
    except TypeError:
        return fallback_line
