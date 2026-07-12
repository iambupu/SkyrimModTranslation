from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
SUPPORTED_GAME_IDS = frozenset({"skyrim-se", "fallout4"})
PROFILE_DIR = Path("config") / "game_profiles"
PLUGIN_ADAPTER_VERSION = 1
SUPPORTED_INTERFACE_TRANSLATION_ENCODINGS = frozenset({"utf-16-le-bom"})
GAME_METADATA_KEYS = (
    "game_id",
    "game_profile_version",
    "game_display_name",
    "support_level",
    "plugin_adapter",
    "plugin_adapter_version",
    "pex_category",
    "pex_writeback_status",
    "interface_translation_encoding",
    "archive_delivery",
    "archive_allow_repack",
)


@dataclass(frozen=True)
class GameContext:
    schema_version: int
    game_id: str
    display_name: str
    support_level: str
    mutagen_release: str
    pex_category: str
    plugin_extensions: frozenset[str]
    archive_extensions: frozenset[str]
    string_table_extensions: frozenset[str]
    data_directories: frozenset[str]
    protected_directories: frozenset[str]
    risky_paths: tuple[str, ...]
    glossary_path: Path
    plugin_root: Path
    supports_localized_plugins: bool
    string_tables_enabled: bool
    pex_export_supported: bool
    pex_writeback_status: str
    interface_translation_encoding: str
    archive_default_delivery: str
    archive_allow_repack: bool


def plugin_adapter_name(context: GameContext) -> str:
    return "fallout4-mutagen" if context.game_id == "fallout4" else "skyrim-mutagen"


def game_display_label(context: GameContext) -> str:
    return game_display_label_from_metadata(game_context_metadata(context))


def game_display_label_from_metadata(metadata: dict[str, Any]) -> str:
    if metadata.get("game_id") == "skyrim-se":
        return "Skyrim SE/AE"
    display_name = str(metadata.get("game_display_name", "")).strip()
    if metadata.get("support_level") == "experimental":
        return f"{display_name} (Experimental)"
    return display_name


def game_context_metadata(context: GameContext) -> dict[str, object]:
    return {
        "game_id": context.game_id,
        "game_profile_version": context.schema_version,
        "game_display_name": context.display_name,
        "support_level": context.support_level,
        "plugin_adapter": plugin_adapter_name(context),
        "plugin_adapter_version": PLUGIN_ADAPTER_VERSION,
        "pex_category": context.pex_category,
        "pex_writeback_status": context.pex_writeback_status,
        "interface_translation_encoding": context.interface_translation_encoding,
        "archive_delivery": context.archive_default_delivery,
        "archive_allow_repack": context.archive_allow_repack,
    }


def game_metadata_mismatches(
    payload: dict[str, Any],
    context: GameContext,
    *,
    require_all: bool = False,
) -> list[str]:
    expected = game_context_metadata(context)
    mismatches: list[str] = []
    for key in GAME_METADATA_KEYS:
        if key not in payload:
            if require_all:
                mismatches.append(f"missing {key}")
            continue
        if payload[key] != expected[key]:
            mismatches.append(f"{key}: expected {expected[key]!r}, found {payload[key]!r}")
    return mismatches


def plugin_root() -> Path:
    configured = os.environ.get(PLUGIN_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Game profile must contain an object: {path}")
    return data


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Game profile field '{key}' must be a non-empty string")
    return value.strip()


def _require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Game profile field '{key}' must be a boolean")
    return value


def _require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"Game profile field '{key}' must be a list of non-empty strings")
    return [item.strip() for item in value]


def _validate_glossary_path(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError("Game profile glossary_path must stay under the plugin root")
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Game profile glossary_path must stay under the plugin root") from exc
    return resolved


def _profile_path(game_id: str) -> Path:
    if game_id not in SUPPORTED_GAME_IDS:
        raise ValueError(
            f"Unsupported game id '{game_id}'. Supported ids: {', '.join(sorted(SUPPORTED_GAME_IDS))}"
        )
    return plugin_root() / PROFILE_DIR / f"{game_id}.json"


def load_game_profile(game_id: str) -> GameContext:
    profile_path = _profile_path(game_id)
    if not profile_path.is_file():
        raise ValueError(f"Missing game profile: {profile_path}")

    data = _load_json(profile_path)
    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise ValueError("Game profile field 'schema_version' must be a positive integer")

    actual_game_id = _require_text(data, "game_id")
    if actual_game_id != game_id:
        raise ValueError(
            f"Game profile game_id mismatch: expected '{game_id}', found '{actual_game_id}'"
        )

    interface_translation_encoding = _require_text(data, "interface_translation_encoding")
    if interface_translation_encoding not in SUPPORTED_INTERFACE_TRANSLATION_ENCODINGS:
        supported = ", ".join(sorted(SUPPORTED_INTERFACE_TRANSLATION_ENCODINGS))
        raise ValueError(
            "Game profile field 'interface_translation_encoding' has unsupported policy "
            f"'{interface_translation_encoding}'. Supported policies: {supported}"
        )

    plugin_root_path = plugin_root()
    return GameContext(
        schema_version=schema_version,
        game_id=actual_game_id,
        display_name=_require_text(data, "display_name"),
        support_level=_require_text(data, "support_level"),
        mutagen_release=_require_text(data, "mutagen_release"),
        pex_category=_require_text(data, "pex_category"),
        plugin_extensions=frozenset(_require_string_list(data, "plugin_extensions")),
        archive_extensions=frozenset(_require_string_list(data, "archive_extensions")),
        string_table_extensions=frozenset(_require_string_list(data, "string_table_extensions"))
        if "string_table_extensions" in data
        else frozenset(),
        data_directories=frozenset(item.lower() for item in _require_string_list(data, "data_directories")),
        protected_directories=frozenset(item.lower() for item in _require_string_list(data, "protected_directories")),
        risky_paths=tuple(_require_string_list(data, "risky_paths")),
        glossary_path=_validate_glossary_path(_require_text(data, "glossary_path"), plugin_root_path),
        plugin_root=plugin_root_path,
        supports_localized_plugins=_require_bool(data, "supports_localized_plugins"),
        string_tables_enabled=_require_bool(data, "string_tables_enabled"),
        pex_export_supported=_require_bool(data, "pex_export_supported"),
        pex_writeback_status=_require_text(data, "pex_writeback_status"),
        interface_translation_encoding=interface_translation_encoding,
        archive_default_delivery=_require_text(data, "archive_default_delivery"),
        archive_allow_repack=_require_bool(data, "archive_allow_repack"),
    )


def other_game_glossary_paths(game_id: str) -> frozenset[Path]:
    if game_id not in SUPPORTED_GAME_IDS:
        raise ValueError(
            f"Unsupported game id '{game_id}'. Supported ids: {', '.join(sorted(SUPPORTED_GAME_IDS))}"
        )
    return frozenset(
        load_game_profile(other_game_id).glossary_path
        for other_game_id in sorted(SUPPORTED_GAME_IDS)
        if other_game_id != game_id
    )


def load_game_context(workspace_root: Path) -> GameContext:
    marker_path = workspace_root / WORKSPACE_MARKER
    marker = _load_json(marker_path)
    game_id = "skyrim-se" if "game_id" not in marker else marker["game_id"]
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError(f"Workspace marker has invalid game_id: {marker_path}")
    normalized_game_id = game_id.strip()
    if "game_profile" in marker:
        game_profile = marker["game_profile"]
        if not isinstance(game_profile, str) or not game_profile.strip():
            raise ValueError(f"Workspace marker has invalid game_profile: {marker_path}")
        if game_profile.strip() != normalized_game_id:
            raise ValueError(
                f"Workspace marker game_profile conflicts with game_id: {marker_path}"
            )
    return load_game_profile(normalized_game_id)
