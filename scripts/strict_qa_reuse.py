"""Content-bound reuse for a clean strict QA mechanical pass."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from file_utils import discover_regular_files, sha256_file


SNAPSHOT_SCHEMA_VERSION = 1
ADAPTER_DEFINITION_SUFFIXES = {".cs", ".csproj", ".json", ".props", ".targets"}


def _relative_key(root: Path, path: Path, source_root: Path | None = None) -> str:
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        return f"workspace/{resolved_path.relative_to(resolved_root).as_posix()}"
    except ValueError:
        pass
    if source_root is not None:
        resolved_source_root = source_root.resolve(strict=True)
        try:
            return f"plugin/{resolved_path.relative_to(resolved_source_root).as_posix()}"
        except ValueError:
            pass
    raise ValueError(f"strict QA snapshot path is outside workspace and plugin source: {path}")


def _tree_files(path: Path, *, suffixes: set[str] | None = None) -> list[Path]:
    if not path.is_dir():
        return []
    return [
        item
        for item in discover_regular_files(path, label="Strict QA snapshot directory")
        if suffixes is None or item.suffix.casefold() in suffixes
    ]


def _definition_files(root: Path, source_root: Path) -> list[Path]:
    paths = [
        *_tree_files(source_root / "scripts", suffixes={".py"}),
        *_tree_files(source_root / "config", suffixes={".json"}),
        *_tree_files(source_root / "adapters", suffixes=ADAPTER_DEFINITION_SUFFIXES),
    ]
    marker = root / ".skyrim-chs-workspace.json"
    if marker.is_file():
        paths.append(marker)
    for name in ("pyproject.toml", "uv.lock"):
        path = source_root / name
        if path.is_file():
            paths.append(path)
    return paths


def _input_paths(
    root: Path,
    workspace: Path,
    final_mod: Path,
    translation_inputs: Iterable[Path],
    source_root: Path,
) -> list[Path]:
    paths = [
        *_tree_files(workspace),
        *_tree_files(final_mod),
        *translation_inputs,
        *_definition_files(root, source_root),
    ]
    unique = {path.resolve(strict=True): path for path in paths if path.is_file()}
    return sorted(unique.values(), key=lambda path: _relative_key(root, path, source_root).casefold())


def _file_manifest(root: Path, paths: Iterable[Path], source_root: Path | None = None) -> dict[str, str]:
    return {
        _relative_key(root, path, source_root): sha256_file(path)
        for path in sorted(paths, key=lambda item: _relative_key(root, item, source_root).casefold())
    }


def write_reusable_mechanical_snapshot(
    *,
    root: Path,
    snapshot_path: Path,
    mod_name: str,
    workspace: Path,
    final_mod: Path,
    translation_inputs: list[Path],
    evidence_paths: list[Path],
    game_metadata: dict[str, object],
    metrics: dict[str, object],
    notes: list[str],
    source_root: Path | None = None,
) -> None:
    source_root = source_root or root
    missing_evidence = [path for path in evidence_paths if not path.is_file()]
    if missing_evidence:
        raise ValueError(f"strict QA review evidence is missing: {missing_evidence[0]}")

    payload: dict[str, Any] = {
        "SchemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "StrictComplete": True,
        "ModName": mod_name,
        "Workspace": _relative_key(root, workspace, source_root),
        "FinalModDir": _relative_key(root, final_mod, source_root),
        "GameContext": game_metadata,
        "TranslationInputs": sorted(_relative_key(root, path, source_root) for path in translation_inputs),
        "TrackedInputs": _file_manifest(
            root,
            _input_paths(root, workspace, final_mod, translation_inputs, source_root),
            source_root,
        ),
        "ReviewEvidence": _file_manifest(root, evidence_paths, source_root),
        "Metrics": metrics,
        "Notes": notes,
    }
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = snapshot_path.with_name(f".{snapshot_path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(snapshot_path)


def load_reusable_mechanical_snapshot(
    *,
    root: Path,
    snapshot_path: Path,
    mod_name: str,
    workspace: Path,
    final_mod: Path,
    translation_inputs: list[Path],
    evidence_paths: list[Path],
    game_metadata: dict[str, object],
    source_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    source_root = source_root or root
    if not snapshot_path.is_file():
        return None, "snapshot is missing"
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "snapshot is unreadable"
    if not isinstance(payload, dict) or payload.get("SchemaVersion") != SNAPSHOT_SCHEMA_VERSION:
        return None, "snapshot schema is unsupported"
    if payload.get("StrictComplete") is not True:
        return None, "snapshot is not from strict-complete QA"
    if payload.get("ModName") != mod_name:
        return None, "snapshot ModName changed"
    if payload.get("Workspace") != _relative_key(root, workspace, source_root):
        return None, "workspace path changed"
    if payload.get("FinalModDir") != _relative_key(root, final_mod, source_root):
        return None, "final_mod path changed"
    if payload.get("GameContext") != game_metadata:
        return None, "game context changed"

    current_translation_inputs = sorted(_relative_key(root, path, source_root) for path in translation_inputs)
    if payload.get("TranslationInputs") != current_translation_inputs:
        return None, "translation input set changed"
    if any(not path.is_file() for path in evidence_paths):
        return None, "review evidence is missing"
    if payload.get("ReviewEvidence") != _file_manifest(root, evidence_paths, source_root):
        return None, "review evidence changed"
    if payload.get("TrackedInputs") != _file_manifest(
        root,
        _input_paths(root, workspace, final_mod, translation_inputs, source_root),
        source_root,
    ):
        return None, "tracked inputs changed"
    if not isinstance(payload.get("Metrics"), dict) or not isinstance(payload.get("Notes"), list):
        return None, "snapshot payload is invalid"
    if not all(isinstance(note, str) for note in payload["Notes"]):
        return None, "snapshot payload is invalid"
    return payload, ""
