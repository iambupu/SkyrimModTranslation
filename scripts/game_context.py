from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


PLUGIN_ROOT_ENV = "SKYRIM_CHS_PLUGIN_ROOT"
WORKSPACE_MARKER = ".skyrim-chs-workspace.json"
PROFILE_DIR = Path("config") / "game_profiles"
SUPPORTED_INTERFACE_TRANSLATION_ENCODINGS = frozenset({"utf-16-le-bom"})
GLOSSARY_FORMAT_CONSUMERS = {
    "markdown": frozenset({"rag"}),
    "lextranslator-text": frozenset({"rag", "lextranslator"}),
    "sst": frozenset({"rag", "xtranslator"}),
    "eet": frozenset({"rag", "esp-esm-translator"}),
}
CAPABILITY_LEVELS = (
    "unsupported",
    "inventory_only",
    "read_only",
    "experimental_write",
    "stable",
)
CAPABILITY_LEVEL_RANKS = MappingProxyType(
    {level: rank for rank, level in enumerate(CAPABILITY_LEVELS)}
)
SUPPORTED_PROFILE_SCHEMA_VERSIONS = frozenset({2})
SUPPORTED_GAME_SUPPORT_LEVELS = frozenset({"stable", "experimental"})
REMOVED_PROFILE_FIELDS = frozenset(
    {
        "plugin_adapter",
        "plugin_adapter_version",
        "mutagen_release",
        "pex_category",
        "archive_extensions",
        "supports_localized_plugins",
        "string_tables_enabled",
        "pex_export_supported",
        "pex_writeback_status",
        "archive_default_delivery",
        "archive_materialization_extensions",
        "archive_repack_extensions",
        "archive_materialization_enabled",
        "archive_allow_repack",
    }
)
GAME_METADATA_KEYS = (
    "game_id",
    "game_profile_version",
    "game_display_name",
    "support_level",
    "interface_translation_encoding",
)


@dataclass(frozen=True)
class GlossarySource:
    relative_path: Path
    format: str
    consumers: frozenset[str]
    required: bool


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True)
class CapabilitySpec:
    level: str
    adapter_id: str
    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.level not in CAPABILITY_LEVEL_RANKS:
            raise ValueError(f"Invalid capability level: {self.level!r}")
        if not isinstance(self.adapter_id, str):
            raise ValueError("Capability adapter_id must be a string")
        if self.level != "unsupported" and not self.adapter_id.strip():
            raise ValueError("Supported capability adapter_id must be a non-empty string")
        if not isinstance(self.options, Mapping):
            raise ValueError("Capability options must be an object")
        object.__setattr__(self, "adapter_id", self.adapter_id.strip())
        object.__setattr__(self, "options", _freeze_value(self.options))


@dataclass(frozen=True)
class GameContext:
    schema_version: int
    game_id: str
    display_name: str
    support_level: str
    format_families: Mapping[str, str]
    capabilities: Mapping[str, CapabilitySpec]
    plugin_extensions: frozenset[str]
    string_table_extensions: frozenset[str]
    data_directories: frozenset[str]
    protected_directories: frozenset[str]
    risky_paths: tuple[str, ...]
    glossary_path: Path
    glossary_sources: tuple[GlossarySource, ...]
    plugin_root: Path
    interface_translation_encoding: str

    def __post_init__(self) -> None:
        if not isinstance(self.format_families, Mapping):
            raise ValueError("GameContext format_families must be a mapping")
        if not isinstance(self.capabilities, Mapping):
            raise ValueError("GameContext capabilities must be a mapping")
        frozen_capabilities: dict[str, CapabilitySpec] = {}
        for name, spec in self.capabilities.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("GameContext capability names must be non-empty strings")
            if not isinstance(spec, CapabilitySpec):
                raise ValueError(f"GameContext capability '{name}' must be a CapabilitySpec")
            frozen_capabilities[name] = CapabilitySpec(
                level=spec.level,
                adapter_id=spec.adapter_id,
                options=spec.options,
            )
        object.__setattr__(self, "format_families", _freeze_value(self.format_families))
        object.__setattr__(self, "capabilities", MappingProxyType(frozen_capabilities))

    def capability(self, name: str) -> CapabilitySpec | None:
        return self.capabilities.get(name)

    def require_capability(self, name: str) -> CapabilitySpec:
        spec = self.capability(name)
        if spec is None:
            raise ValueError(f"Game profile is missing capability '{name}'")
        return spec

    def capability_at_least(self, name: str, minimum_level: str) -> bool:
        if minimum_level not in CAPABILITY_LEVEL_RANKS:
            raise ValueError(f"Unknown capability level: {minimum_level}")
        spec = self.capability(name)
        if spec is None:
            return False
        return CAPABILITY_LEVEL_RANKS[spec.level] >= CAPABILITY_LEVEL_RANKS[minimum_level]

    def capability_option_text(self, name: str, key: str) -> str:
        return _capability_option_text(self.capability(name), key)

    def capability_option_positive_int(self, name: str, key: str) -> int:
        return _capability_option_positive_int(self.capability(name), key)

    def archive_extensions_at_least(self, minimum_level: str) -> frozenset[str]:
        return frozenset(
            name.removeprefix("archive")
            for name, spec in self.capabilities.items()
            if name.startswith("archive.")
            and CAPABILITY_LEVEL_RANKS[spec.level]
            >= CAPABILITY_LEVEL_RANKS[minimum_level]
        )

    def capability_write_status(self, name: str) -> str:
        spec = self.capability(name)
        if spec is None:
            return "blocked"
        if spec.level == "stable":
            return "stable"
        if spec.level == "experimental_write":
            return "experimental"
        return "blocked"

    def can_materialize_archive(self, extension: str) -> bool:
        return self.capability_at_least(f"archive{extension.lower()}", "read_only")

    def can_repack_archive(self, extension: str) -> bool:
        return self.capability_at_least(
            f"archive{extension.lower()}",
            "experimental_write",
        )


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
        "interface_translation_encoding": context.interface_translation_encoding,
    }


def _values_match(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


def game_metadata_mismatches(
    payload: dict[str, Any],
    context: GameContext,
) -> list[str]:
    expected = game_context_metadata(context)
    mismatches: list[str] = []
    for key in GAME_METADATA_KEYS:
        if key not in payload:
            mismatches.append(f"missing {key}")
            continue
        if not _values_match(payload[key], expected[key]):
            mismatches.append(f"{key}: expected {expected[key]!r}, found {payload[key]!r}")
    return mismatches

def plugin_root() -> Path:
    configured = os.environ.get(PLUGIN_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return Path(__file__).resolve().parents[1]


def supported_game_ids() -> tuple[str, ...]:
    """Return profile ids from the active plugin root without assuming game names."""
    profile_dir = plugin_root() / PROFILE_DIR
    if not profile_dir.is_dir():
        return ()
    return tuple(sorted(path.stem for path in profile_dir.glob("*.json") if path.is_file()))


def other_game_glossary_paths(game_id: str) -> frozenset[Path]:
    supported = supported_game_ids()
    if game_id not in supported:
        raise ValueError(
            f"Unsupported game id '{game_id}'. Supported ids: {', '.join(supported) or '<none>'}"
        )
    return frozenset(
        load_game_profile(other_game_id).glossary_path
        for other_game_id in supported
        if other_game_id != game_id
    )


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


def _require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"Game profile field '{key}' must be a list of non-empty strings")
    return [item.strip() for item in value]


def _require_supported_text(
    data: dict[str, Any],
    key: str,
    supported_values: frozenset[str],
) -> str:
    value = _require_text(data, key)
    if value not in supported_values:
        supported = ", ".join(sorted(supported_values))
        raise ValueError(
            f"Game profile field '{key}' must be one of: {supported}; found {value!r}"
        )
    return value


def _load_format_families(data: dict[str, Any]) -> Mapping[str, str]:
    raw_families = data.get("format_families")
    if not isinstance(raw_families, dict) or not raw_families:
        raise ValueError("Game profile field 'format_families' must be a non-empty object")
    families: dict[str, str] = {}
    for name, value in raw_families.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Game profile format family names must be non-empty strings")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Game profile format family '{name}' must be a non-empty string")
        families[name.strip()] = value.strip()
    return MappingProxyType(families)


def _load_capabilities(data: dict[str, Any]) -> Mapping[str, CapabilitySpec]:
    raw_capabilities = data.get("capabilities")
    if not isinstance(raw_capabilities, dict):
        raise ValueError("Game profile field 'capabilities' must be an object")
    capabilities: dict[str, CapabilitySpec] = {}
    capability_names_by_casefold: dict[str, str] = {}
    for name, raw_spec in raw_capabilities.items():
        if not isinstance(name, str):
            raise ValueError("Game profile capability names must be non-empty strings")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Game profile capability names must be non-empty strings")
        if name != normalized_name:
            raise ValueError(
                f"Game profile capability name must not contain surrounding whitespace: {name!r}"
            )
        casefolded_name = normalized_name.casefold()
        previous_name = capability_names_by_casefold.get(casefolded_name)
        if previous_name is not None and casefolded_name.startswith("archive."):
            raise ValueError(
                f"Game profile has duplicate archive capability names after casefold: "
                f"{previous_name!r}, {normalized_name!r}"
            )
        if casefolded_name.startswith("archive."):
            if (
                len(casefolded_name) <= len("archive.")
                or normalized_name != casefolded_name
            ):
                raise ValueError(
                    "Game profile archive capability name must use canonical lowercase form: "
                    f"{normalized_name!r}"
                )
        capability_names_by_casefold[casefolded_name] = normalized_name
        label = f"capabilities.{normalized_name}"
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Game profile capability '{normalized_name}' must be an object")
        level = raw_spec.get("level")
        if level not in CAPABILITY_LEVEL_RANKS:
            supported = ", ".join(CAPABILITY_LEVELS)
            raise ValueError(
                f"Game profile capability '{normalized_name}' level must be one of: {supported}"
            )
        adapter_id = raw_spec.get("adapter", "")
        if not isinstance(adapter_id, str):
            raise ValueError(f"Game profile field '{label}.adapter' must be a string")
        if level != "unsupported" and not adapter_id.strip():
            raise ValueError(f"Game profile field '{label}.adapter' must be a non-empty string")
        options = raw_spec.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"Game profile field '{label}.options' must be an object")
        capabilities[normalized_name] = CapabilitySpec(level, adapter_id, options)
    return MappingProxyType(capabilities)


def _capability_option_text(spec: CapabilitySpec | None, key: str) -> str:
    if spec is None or spec.level == "unsupported":
        return ""
    value = spec.options.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Game profile capability option '{key}' must be a non-empty string")
    return value.strip()


def _capability_option_positive_int(spec: CapabilitySpec | None, key: str) -> int:
    if spec is None or spec.level == "unsupported":
        return 0
    raw_value = spec.options.get(key)
    if isinstance(raw_value, bool):
        value = 0
    elif isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str) and raw_value.isdecimal():
        value = int(raw_value)
    else:
        value = 0
    if value < 1:
        raise ValueError(
            f"Game profile capability option '{key}' must be a positive integer"
        )
    return value


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


def _validate_glossary_source_path(value: str, root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError("Game profile glossary source paths must stay under glossary/")
    resolved = (root / candidate).resolve(strict=False)
    glossary_root = (root / "glossary").resolve(strict=False)
    try:
        relative_to_glossary = resolved.relative_to(glossary_root)
    except ValueError as exc:
        raise ValueError("Game profile glossary source paths must stay under glossary/") from exc
    if not relative_to_glossary.parts:
        raise ValueError("Game profile glossary source path cannot be the whole glossary directory")
    return Path("glossary") / relative_to_glossary


def _load_glossary_sources(
    data: dict[str, Any],
    root: Path,
    primary_glossary_path: Path,
) -> tuple[GlossarySource, ...]:
    primary_relative = primary_glossary_path.relative_to(root)
    raw_sources = data.get("glossary_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("Game profile field 'glossary_sources' must be a non-empty list")

    sources: list[GlossarySource] = []
    seen_paths: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        label = f"glossary_sources[{index}]"
        if not isinstance(raw_source, dict):
            raise ValueError(f"Game profile field '{label}' must be an object")
        path_value = raw_source.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"Game profile field '{label}.path' must be a non-empty string")
        relative_path = _validate_glossary_source_path(path_value.strip(), root)
        path_key = relative_path.as_posix().casefold()
        if path_key in seen_paths:
            raise ValueError(f"Game profile glossary source path is duplicated: {relative_path.as_posix()}")
        seen_paths.add(path_key)

        format_value = raw_source.get("format")
        if not isinstance(format_value, str) or format_value not in GLOSSARY_FORMAT_CONSUMERS:
            supported = ", ".join(sorted(GLOSSARY_FORMAT_CONSUMERS))
            raise ValueError(
                f"Game profile field '{label}.format' must be one of: {supported}"
            )
        raw_consumers = raw_source.get("consumers")
        if (
            not isinstance(raw_consumers, list)
            or not raw_consumers
            or not all(isinstance(item, str) and item.strip() for item in raw_consumers)
        ):
            raise ValueError(f"Game profile field '{label}.consumers' must be a non-empty string list")
        consumers = frozenset(item.strip() for item in raw_consumers)
        unsupported_consumers = consumers - GLOSSARY_FORMAT_CONSUMERS[format_value]
        if unsupported_consumers:
            raise ValueError(
                f"Game profile field '{label}.consumers' is incompatible with format "
                f"'{format_value}': {', '.join(sorted(unsupported_consumers))}"
            )
        required = raw_source.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(f"Game profile field '{label}.required' must be a boolean")
        sources.append(GlossarySource(relative_path, format_value, consumers, required))

    primary_source = next(
        (source for source in sources if source.relative_path == primary_relative),
        None,
    )
    if primary_source is None or "rag" not in primary_source.consumers:
        raise ValueError(
            "Game profile glossary_path must also appear in glossary_sources with the rag consumer"
        )
    return tuple(sources)


def _profile_path(game_id: str) -> Path:
    supported = supported_game_ids()
    if game_id not in supported:
        raise ValueError(
            f"Unsupported game id '{game_id}'. Supported ids: {', '.join(supported) or '<none>'}"
        )
    return plugin_root() / PROFILE_DIR / f"{game_id}.json"


def load_game_profile(game_id: str) -> GameContext:
    profile_path = _profile_path(game_id)
    if not profile_path.is_file():
        raise ValueError(f"Missing game profile: {profile_path}")

    data = _load_json(profile_path)
    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in SUPPORTED_PROFILE_SCHEMA_VERSIONS
    ):
        raise ValueError("Game profile field 'schema_version' must be 2")
    removed_fields = sorted(REMOVED_PROFILE_FIELDS.intersection(data))
    if removed_fields:
        raise ValueError(
            "Game profile schema v2 does not accept removed top-level fields; move all "
            f"capability decisions under capabilities.*: {', '.join(removed_fields)}"
        )

    actual_game_id = _require_text(data, "game_id")
    if actual_game_id != game_id:
        raise ValueError(
            f"Game profile game_id mismatch: expected '{game_id}', found '{actual_game_id}'"
        )
    support_level = _require_supported_text(
        data,
        "support_level",
        SUPPORTED_GAME_SUPPORT_LEVELS,
    )

    format_families = _load_format_families(data)
    capabilities = _load_capabilities(data)
    plugin_capability = capabilities.get("plugin_text")
    pex_capability = capabilities.get("pex")
    _capability_option_positive_int(
        plugin_capability,
        "adapter_contract_version",
    )
    _capability_option_text(plugin_capability, "mutagen_release")
    localized_plugin_policy = _capability_option_text(
        plugin_capability,
        "localized_plugin_policy",
    )
    if localized_plugin_policy not in {"", "allow", "block"}:
        raise ValueError(
            "Game profile capability option 'localized_plugin_policy' must be "
            "'allow' or 'block'"
        )
    _capability_option_text(pex_capability, "pex_category")
    interface_translation_encoding = _require_text(data, "interface_translation_encoding")
    if interface_translation_encoding not in SUPPORTED_INTERFACE_TRANSLATION_ENCODINGS:
        supported = ", ".join(sorted(SUPPORTED_INTERFACE_TRANSLATION_ENCODINGS))
        raise ValueError(
            "Game profile field 'interface_translation_encoding' has unsupported policy "
            f"'{interface_translation_encoding}'. Supported policies: {supported}"
        )

    for name in capabilities:
        if not name.startswith("archive."):
            continue
        extension = name.removeprefix("archive")
        if len(extension) < 2 or not extension.startswith("."):
            raise ValueError(f"Game profile archive capability has invalid name: {name}")
    plugin_root_path = plugin_root()
    glossary_path = _validate_glossary_path(_require_text(data, "glossary_path"), plugin_root_path)
    return GameContext(
        schema_version=schema_version,
        game_id=actual_game_id,
        display_name=_require_text(data, "display_name"),
        support_level=support_level,
        format_families=format_families,
        capabilities=capabilities,
        plugin_extensions=frozenset(_require_string_list(data, "plugin_extensions")),
        string_table_extensions=frozenset(_require_string_list(data, "string_table_extensions"))
        if "string_table_extensions" in data
        else frozenset(),
        data_directories=frozenset(item.lower() for item in _require_string_list(data, "data_directories")),
        protected_directories=frozenset(item.lower() for item in _require_string_list(data, "protected_directories")),
        risky_paths=tuple(_require_string_list(data, "risky_paths")),
        glossary_path=glossary_path,
        glossary_sources=_load_glossary_sources(data, plugin_root_path, glossary_path),
        plugin_root=plugin_root_path,
        interface_translation_encoding=interface_translation_encoding,
    )




def load_game_context(workspace_root: Path) -> GameContext:
    marker_path = workspace_root / WORKSPACE_MARKER
    marker = _load_json(marker_path)
    if "game_id" not in marker:
        raise ValueError(f"Workspace marker is missing required game_id: {marker_path}")
    game_id = marker["game_id"]
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


def resolve_workspace_game_context(
    workspace_root: Path,
    explicit_game: str = "",
) -> GameContext:
    """Resolve the profile without allowing CLI input to override a workspace marker."""
    marker_exists = (workspace_root / WORKSPACE_MARKER).is_file()
    if marker_exists:
        marker_context = load_game_context(workspace_root)
        if explicit_game and explicit_game != marker_context.game_id:
            raise ValueError(
                f"explicit game '{explicit_game}' conflicts with workspace marker game "
                f"'{marker_context.game_id}'"
            )
        return marker_context
    if explicit_game:
        return load_game_profile(explicit_game)
    raise ValueError(
        f"Workspace marker is required when --game is not provided: "
        f"{workspace_root / WORKSPACE_MARKER}"
    )
