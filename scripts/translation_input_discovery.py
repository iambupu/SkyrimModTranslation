"""Shared discovery for project-local translation authoring files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


DEFAULT_TRANSLATION_SUFFIXES = {".jsonl"}
DICTIONARY_TRANSLATION_SUFFIXES = {".jsonl", ".xml"}
SOURCE_FIELDS = ("Source", "source", "original", "Original", "OriginalText", "text", "原文")
TARGET_FIELDS = ("Result", "result", "Target", "target", "Dest", "TranslatedText", "translation", "Translation", "译文")


def _path_key(path: Path) -> str:
    return str(path.resolve(strict=False)).lower()


def _relative_key(root: Path, path: Path) -> str:
    try:
        return str(path.resolve(strict=False).relative_to(root.resolve(strict=False))).lower()
    except ValueError:
        return str(path.resolve(strict=False)).lower()


def _iter_files(folder: Path, suffixes: set[str]) -> Iterable[Path]:
    if not folder.is_dir():
        return []
    return (
        item
        for item in sorted(folder.rglob("*"), key=lambda candidate: str(candidate).lower())
        if item.is_file()
        and item.name != ".gitkeep"
        and item.suffix.lower() in suffixes
        and ".template." not in item.name.lower()
    )


def _json_text_value(payload: dict[str, object], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        text = str(value)
        if text.strip():
            return text
    return ""


def jsonl_has_translated_rows(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        source = _json_text_value(payload, SOURCE_FIELDS)
        target = _json_text_value(payload, TARGET_FIELDS)
        if source.strip() and target.strip() and source != target:
            return True
    return False


def translation_input_roots(root: Path, mod_name: str, *, include_derived_pex_apply: bool = True) -> list[Path]:
    normalized_dir = root / "work" / "normalized" / mod_name
    roots = [
        root / "translated" / mod_name,
        root / "translated" / "plugin_exports" / mod_name,
        root / "translated" / "text_assets" / mod_name,
        root / "translated" / "final_mod" / mod_name,
        root / "translated" / "overlay" / mod_name,
        root / "translated" / "lextranslator_ready" / mod_name,
        root / "translated" / "xtranslator_ready" / mod_name,
        root / "out" / mod_name / "final_mod_overlay",
        normalized_dir / "pex_visible_strings",
    ]
    if include_derived_pex_apply:
        roots.append(normalized_dir / "pex_apply")
    return roots


def translation_input_evidence_roots(root: Path, mod_name: str, *, include_derived_pex_apply: bool = True) -> list[str]:
    roots = translation_input_roots(root, mod_name, include_derived_pex_apply=include_derived_pex_apply)
    roots.append(root / "work" / "normalized" / mod_name / "pex_visible_strings.jsonl")
    return [_relative_key(root, path).replace("/", "\\") for path in roots]


def collect_translation_input_files(
    root: Path,
    mod_name: str,
    *,
    suffixes: set[str] | None = None,
    include_derived_pex_apply: bool = True,
    require_translated_rows: bool = False,
) -> list[Path]:
    selected_suffixes = {suffix.lower() for suffix in (suffixes or DEFAULT_TRANSLATION_SUFFIXES)}
    candidates: list[Path] = []
    for folder in translation_input_roots(root, mod_name, include_derived_pex_apply=include_derived_pex_apply):
        candidates.extend(_iter_files(folder, selected_suffixes))

    normalized_single = root / "work" / "normalized" / mod_name / "pex_visible_strings.jsonl"
    if normalized_single.is_file() and normalized_single.suffix.lower() in selected_suffixes:
        candidates.append(normalized_single)

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _path_key(candidate)
        if key in seen:
            continue
        if require_translated_rows and candidate.suffix.lower() == ".jsonl" and not jsonl_has_translated_rows(candidate):
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped
